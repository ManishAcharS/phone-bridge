package dev.phonebridge.net

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.hardware.display.DisplayManager
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.Display
import android.view.Surface
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import java.io.ByteArrayOutputStream
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * A camera refusal the PC must be able to show verbatim. It *is* an [RpcFailure]
 * so `BridgeServer`'s existing handler turns it into the documented error code
 * (`camera_denied`, `no_camera`, `camera_busy`) with no extra plumbing.
 */
class CameraFailure(code: String, message: String) : RpcFailure(code, message)

/**
 * Phase 4 camera on the phone: CameraX `ImageAnalysis` -> JPEG -> `MSG_VIDEO`.
 *
 * The rules are the same three that shaped [AudioEngine], plus one that only
 * applies to a camera:
 *
 * 1. **Nothing starts by itself.** The camera opens only when an authenticated PC
 *    asks with `camera.start`, and only if the user already granted `CAMERA` in
 *    this app. A missing permission is reported as `camera_denied`, never as an
 *    empty preview that looks like a broken phone.
 * 2. **Bounded everywhere.** Analysis uses `STRATEGY_KEEP_ONLY_LATEST`, so a slow
 *    encoder can never build a frame backlog; the outbound queue holds
 *    [QUEUE_FRAMES] frames and drops the *oldest*, so latency stays flat on a
 *    slow link instead of growing into a slideshow of the past.
 * 3. **Everything is released.** [stop] unbinds every use case and shuts the
 *    analysis executor down; [release] does the same plus drops the provider. It
 *    is called on stop, on session close, on session error and in
 *    `BridgeService.onDestroy`, so no camera survives a closed window.
 * 4. **The user can see it.** [state] is a flow the Compose screen renders as a
 *    red "streaming" banner with the lens and frame count, plus a self-view
 *    thumbnail decoded from the frames actually being sent. A live camera the
 *    owner cannot see is exactly the failure mode this phase must avoid.
 *
 * Frames are JPEG because the PC decodes them with whatever it has and because a
 * lost message then costs one picture rather than a corrupted stream.
 */
class CameraEngine(
    private val context: Context,
    private val sendFrame: (streamId: Int, body: ByteArray) -> Unit,
    private val log: (level: String, message: String) -> Unit,
) {

    companion object {
        /** Two outbound frames of slack; the third arrival evicts the oldest. */
        const val QUEUE_FRAMES = 2
        const val MAX_FRAME_BYTES = VideoFrames.MAX_BODY

        /**
         * logcat tag. `adb logcat -s CameraEngine` is the camera's own channel:
         * the RPC layer's `RPC camera.start failed` line says *that* something
         * failed, this says *where*.
         */
        private const val TAG = "CameraEngine"

        /** How long a camera call may wait for the main thread before giving up. */
        private const val MAIN_THREAD_TIMEOUT_MS = 10_000L

        /** Opening the provider on a cold start is allowed to take a moment. */
        private const val PROVIDER_TIMEOUT_MS = 5_000L

        /**
         * The phone's own self-view is redrawn this often, not once per frame.
         *
         * `_state` carries the last JPEG, and the Compose card decodes it for the
         * thumbnail. At 30 fps that decode ran 30 times a second *in addition* to
         * encoding the same picture for the PC — measurable heat for a preview
         * nobody watches at 30 fps.
         */
        private const val PREVIEW_INTERVAL_MS = 250L
    }

    /** What the phone screen shows. */
    data class State(
        val active: Boolean = false,
        val facing: String = "back",
        val width: Int = 0,
        val height: Int = 0,
        val fps: Int = 0,
        val framesSent: Long = 0,
        val dropped: Long = 0,
        val permitted: Boolean = false,
        val lastError: String = "",
        val lastJpeg: ByteArray? = null,
    )

    private val _state = MutableStateFlow(State())
    val state: StateFlow<State> = _state.asStateFlow()

    private val lock = Any()
    private val running = AtomicBoolean(false)
    private val seq = AtomicInteger(0)
    private val framesSent = AtomicInteger(0)
    private val framesDropped = AtomicInteger(0)

    private val startedAt = SystemClock.elapsedRealtime()
    private var config = VideoFrames.Config()
    private var provider: ProcessCameraProvider? = null
    private var camera: Camera? = null
    private var analysis: ImageAnalysis? = null
    private var lifecycle: CameraLifecycle? = null
    private var executor = Executors.newSingleThreadExecutor { r ->
        Thread(r, "phonebridge-camera-analysis").apply { isDaemon = true }
    }
    private var sender: Thread? = null
    private var sendQueue: ArrayBlockingQueue<ByteArray>? = null
    private var lastSentAtMs = 0L
    private var lastPreviewAtMs = 0L

    // Measurement. All of it is reported once a second with Log.d and in
    // `camera.status`, so a slow frame rate can be attributed to a stage
    // instead of guessed at.
    private val encoded = AtomicInteger(0)
    private val encodeNanos = AtomicLong(0)
    private val sendNanos = AtomicLong(0)
    private var lastStatsAtMs = 0L
    private var lastStatsFrames = 0
    private var lastStatsEncodeNanos = 0L
    private var lastStatsSendNanos = 0L

    @Volatile private var measuredFps = 0.0f
    @Volatile private var lastEncodeMs = 0.0f
    @Volatile private var lastSendMs = 0.0f
    @Volatile private var lastFrameBytes = 0

    /**
     * Every CameraX lifecycle call goes through this handler.
     *
     * `LifecycleRegistry.setCurrentState` — which `bindToLifecycle`, `unbindAll`
     * and our own `CameraLifecycle` all reach — throws
     * `Method setCurrentState must be called on the main thread` when it is not.
     * `camera.start` arrives on a Java-WebSocket thread, which is exactly what
     * happened on the M2010J19SI test run: the phone answered `internal`, the PC
     * showed "camera error" and had no reason to show.
     */
    private val mainHandler = Handler(Looper.getMainLooper())

    fun isPermitted(): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED

    /** Camera hardware presence, separate from permission and from implementation. */
    fun hasHardware(): Boolean =
        context.packageManager.hasSystemFeature(PackageManager.FEATURE_CAMERA_ANY)

    /**
     * Open the camera and start sending.
     *
     * @throws CameraFailure with `camera_denied`, `no_camera`, `camera_busy` or
     *   `internal` — the PC turns these into the exact sentence the user sees.
     */
    fun start(requested: VideoFrames.Config): Map<String, Any?> {
        // Refusals that need no camera plumbing and no thread hop, first: the
        // user must learn "allow Camera" without waiting for a lifecycle.
        if (!hasHardware()) {
            _state.value = _state.value.copy(active = false, permitted = isPermitted(), lastError = "no camera on this device")
            fail("no_camera", "no camera on this device")
        }
        if (!isPermitted()) {
            _state.value = _state.value.copy(active = false, permitted = false, lastError = "camera permission not granted")
            fail(
                "camera_denied",
                "camera permission is not granted: open PhoneBridge on the phone and allow Camera",
            )
        }

        val selector = when (requested.facing) {
            "front" -> CameraSelector.DEFAULT_FRONT_CAMERA
            else -> CameraSelector.DEFAULT_BACK_CAMERA
        }
        val camProvider = obtainProvider()
        if (!camProvider.hasCamera(selector)) {
            _state.value = _state.value.copy(active = false, lastError = "no ${requested.facing} camera")
            fail("no_camera", "this phone has no ${requested.facing} camera")
        }

        // Building the use case does not touch a lifecycle, so it stays off the
        // main thread where this call already is.
        val analysisUseCase = ImageAnalysis.Builder()
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            // YUV, not RGBA: the JPEG encoder wants YCbCr, and every RGBA frame
            // cost a native conversion plus a Bitmap allocation (see toNv21).
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888)
            .setTargetResolution(android.util.Size(requested.width, requested.height))
            .build()
        // Make the rotation metadata mean something. `rotationDegrees` is
        // relative to the use case's target rotation, and a use case that is
        // not attached to a window defaults that to 0 - so every frame was
        // described as "upright in the phone's natural orientation", which is
        // sideways the moment the phone is held in landscape, and the PC drew
        // it that way. Tying it to the display rotation makes the number mean
        // "turn the picture this far clockwise to be upright as the phone is
        // held now", which is exactly what the receiver does with it.
        analysisUseCase.targetRotation = displayRotation()

        val queue = ArrayBlockingQueue<ByteArray>(QUEUE_FRAMES)
        analysisUseCase.setAnalyzer(executor) { proxy -> analyse(proxy, requested, queue) }

        // The one part that must be on the main thread, and it waits for the
        // answer: a bind that fails is a `camera_busy` the PC can print, not a
        // silently empty preview.
        return onMainThread("camera.start") {
            bindOnMainThread(camProvider, selector, analysisUseCase, requested, queue)
        }
    }

    /**
     * The main-thread half of [start]: bind, or report why not.
     *
     * Runs on the main thread with [lock] held, so the state fields it writes are
     * never observed half-updated by [stopLocked].
     */
    private fun bindOnMainThread(
        camProvider: ProcessCameraProvider,
        selector: CameraSelector,
        analysisUseCase: ImageAnalysis,
        requested: VideoFrames.Config,
        queue: ArrayBlockingQueue<ByteArray>,
    ): Map<String, Any?> = synchronized(lock) {
        if (running.get()) {
            // Switching lenses or resolution is a restart, not a second camera.
            stopLocked("restarting camera")
        }
        var created: CameraLifecycle? = null
        try {
            camProvider.unbindAll()
            // Created here, on the main thread: its constructor sets the registry
            // to RESUMED, which is itself a main-thread call.
            val owner = CameraLifecycle()
            created = owner
            val bound = camProvider.bindToLifecycle(owner, selector, analysisUseCase)
            provider = camProvider
            camera = bound
            analysis = analysisUseCase
            lifecycle = owner
            sendQueue = queue
            config = requested
            seq.set(0)
            framesSent.set(0)
            framesDropped.set(0)
            lastSentAtMs = 0
            running.set(true)
            startSender(queue)
            _state.value = State(
                active = true,
                facing = requested.facing,
                width = requested.width,
                height = requested.height,
                fps = requested.fps,
                permitted = true,
            )
            log("INFO", "camera streaming: ${requested.facing} ${requested.width}x${requested.height} @ ${requested.fps} fps")
            mapOf("ok" to true, "config" to config.asMap())
        } catch (e: Exception) {
            analysisUseCase.clearAnalyzer()
            runCatching { camProvider.unbindAll() }
            // Leave no half-built registry behind: an owner that never got bound
            // still owns a lifecycle that was told it was RESUMED.
            runCatching { created?.destroy() }
            _state.value = _state.value.copy(
                active = false,
                permitted = true,
                lastError = "camera busy: ${e.message}",
            )
            logFailure("camera.start bind", "${e.javaClass.simpleName}: ${e.message}", e)
            throw CameraFailure(
                "camera_busy",
                "the camera could not be opened (another app may be using it): ${e.message}",
            )
        }
    }

    /**
     * Close the camera. Safe to call when nothing is running, from any thread.
     *
     * `unbindAll` and the lifecycle's DESTROYED transition are main-thread calls,
     * so this waits for the main thread rather than doing them here. It never
     * throws: it is called from `onDestroy`, from session teardown and from the
     * phone's own UI, and a camera call must not be able to kill those.
     */
    fun stop() {
        closeCamera("stopped")
    }

    private fun closeCamera(reason: String) {
        try {
            onMainThread("camera.stop") { synchronized(lock) { stopLocked(reason) } }
        } catch (e: CameraFailure) {
            // A blocked main thread cannot release the lens. Say so loudly in
            // logcat instead of pretending the camera is closed.
            running.set(false)
            logFailure("camera.stop", e.message ?: "did not complete")
        }
    }

    private fun stopLocked(reason: String) {
        val wasRunning = running.getAndSet(false)
        analysis?.clearAnalyzer()
        analysis = null
        camera = null
        provider?.unbindAll()
        lifecycle?.destroy()
        lifecycle = null
        sendQueue?.clear()
        sendQueue = null
        sender?.interrupt()
        sender = null
        if (wasRunning) {
            log("INFO", "camera stopped ($reason): ${framesSent.get()} frames sent, ${framesDropped.get()} dropped")
        }
        _state.value = _state.value.copy(
            active = false,
            framesSent = framesSent.get().toLong(),
            dropped = framesDropped.get().toLong(),
            lastJpeg = null,
        )
    }

    /** Stop and give the analysis thread back. Called when the last session ends. */
    fun release() {
        stop()
        synchronized(lock) {
            if (!executor.isShutdown) {
                executor.shutdownNow()
            }
            executor = Executors.newSingleThreadExecutor { r ->
                Thread(r, "phonebridge-camera-analysis").apply { isDaemon = true }
            }
        }
    }

    fun status(): Map<String, Any?> = mapOf(
        "streaming" to running.get(),
        "facing" to config.facing,
        "width" to config.width,
        "height" to config.height,
        "fps" to config.fps,
        "frames_sent" to framesSent.get(),
        "dropped" to framesDropped.get(),
        "permitted" to isPermitted(),
        "hardware" to hasHardware(),
        "last_error" to _state.value.lastError,
        // Read by `phonebridge camera --stats` (docs/26). Same numbers as the
        // per-second Log.d line, so the two can be compared directly.
        "stats" to mapOf(
            "fps" to measuredFps.toDouble(),
            "encode_ms" to lastEncodeMs.toDouble(),
            "send_ms" to lastSendMs.toDouble(),
            "bytes_per_frame" to lastFrameBytes,
            "quality" to config.quality,
            "width" to config.width,
            "height" to config.height,
            "encoded" to encoded.get(),
            "sent" to framesSent.get(),
            "dropped" to framesDropped.get(),
        ),
    )

    /** The PC stopped or the session died: nothing should keep the lens open. */
    fun onSessionClosed(why: String) {
        if (!running.get()) return
        closeCamera("session closed: $why")
    }

    // --- internals --------------------------------------------------------- //

    /**
     * Run [block] on the main thread and return its result.
     *
     * Called from the WebSocket thread, so it posts and waits — and the wait is
     * bounded: a main thread that is stuck has to become a sentence the PC can
     * print (`camera_busy`/`internal`), never an RPC that hangs until the PC's
     * own timeout. Called *from* the main thread it runs inline, because posting
     * and waiting there would deadlock against itself.
     */
    private fun <T> onMainThread(
        what: String,
        timeoutMs: Long = MAIN_THREAD_TIMEOUT_MS,
        block: () -> T,
    ): T {
        if (Looper.myLooper() === Looper.getMainLooper()) return block()

        val latch = CountDownLatch(1)
        var outcome: Result<T>? = null
        val posted = mainHandler.post {
            outcome = runCatching(block)
            latch.countDown()
        }
        if (!posted) {
            logFailure(what, "the main thread refused the request")
            throw CameraFailure(
                "internal",
                "the camera could not be used: the phone's main thread is not accepting work",
            )
        }
        if (!latch.await(timeoutMs, TimeUnit.MILLISECONDS)) {
            logFailure(what, "still waiting for the main thread after $timeoutMs ms")
            throw CameraFailure(
                "internal",
                "the camera did not respond within ${timeoutMs / 1000} s: the phone's main thread is busy",
            )
        }
        // The latch is the handover: the main thread's write happens before it.
        return checkNotNull(outcome).getOrThrow()
    }

    /**
     * The camera provider, fetched without ever blocking the main thread.
     *
     * `ProcessCameraProvider.getInstance` completes *on* the main thread, so a
     * blocking `get()` from the main thread would wait for the very thread it is
     * holding. Off the main thread the wait is harmless and bounded.
     */
    private fun obtainProvider(): ProcessCameraProvider {
        val future = ProcessCameraProvider.getInstance(context)
        val onMain = Looper.myLooper() === Looper.getMainLooper()
        if (onMain && !future.isDone) {
            logFailure("camera.start", "the camera provider is still initialising on the main thread")
            throw CameraFailure(
                "internal",
                "the camera is still starting up on this phone; try again in a moment",
            )
        }
        return try {
            future.get(if (onMain) 0L else PROVIDER_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        } catch (e: Exception) {
            _state.value = _state.value.copy(active = false, lastError = "camera unavailable: ${e.message}")
            logFailure("camera provider", "${e.javaClass.simpleName}: ${e.message}", e)
            throw CameraFailure("internal", "camera provider unavailable: ${e.message}")
        }
    }

    /** A refusal that is already logged: every exit from the camera says why. */
    private fun fail(code: String, message: String): Nothing {
        logFailure(code, message)
        throw CameraFailure(code, message)
    }

    /** logcat under our own tag, and the in-app log the phone screen shows. */
    private fun logFailure(what: String, detail: String, error: Throwable? = null) {
        val line = "camera $what: $detail"
        if (error != null) {
            Log.e(TAG, line, error)
        } else {
            Log.e(TAG, line)
        }
        log("ERROR", line)
    }

    private fun analyse(proxy: ImageProxy, cfg: VideoFrames.Config, queue: ArrayBlockingQueue<ByteArray>) {
        try {
            if (!running.get()) return
            val now = SystemClock.elapsedRealtime()
            val interval = 1000L / cfg.fps.coerceAtLeast(1)
            if (lastSentAtMs != 0L && now - lastSentAtMs < interval) return

            val encodeStarted = System.nanoTime()
            val jpeg = proxy.toJpeg(cfg.quality) ?: return
            val encodeFinished = System.nanoTime()
            if (jpeg.size > MAX_FRAME_BYTES) {
                log("WARN", "dropping oversized camera frame (${jpeg.size} bytes)")
                return
            }
            encoded.incrementAndGet()
            encodeNanos.addAndGet(encodeFinished - encodeStarted)
            lastEncodeMs = (encodeFinished - encodeStarted) / 1_000_000f
            lastFrameBytes = jpeg.size
            lastSentAtMs = now
            val index = seq.getAndIncrement() and 0xFFFF
            val flags = VideoFrames.FLAG_KEY or (if (index == 0) VideoFrames.FLAG_START else 0)
            val body = VideoFrames.encode(
                VideoFrames.Frame(
                    seq = index,
                    timestampMs = SystemClock.elapsedRealtime() - startedAt,
                    payload = jpeg,
                    width = proxy.width,
                    height = proxy.height,
                    rotation = proxy.imageInfo.rotationDegrees,
                    flags = flags,
                )
            )
            if (now - lastPreviewAtMs >= PREVIEW_INTERVAL_MS) {
                lastPreviewAtMs = now
                _state.value = _state.value.copy(
                    width = proxy.width,
                    height = proxy.height,
                    lastJpeg = jpeg,
                    framesSent = framesSent.get().toLong(),
                    dropped = framesDropped.get().toLong(),
                )
            }
            reportOnceASecond(now, cfg)
            // Drop-oldest: a late frame is worth less than the newest one.
            if (!queue.offer(body)) {
                queue.poll()
                queue.offer(body)
                framesDropped.incrementAndGet()
            }
        } catch (e: Exception) {
            log("WARN", "camera analysis error: ${e.message}")
            _state.value = _state.value.copy(lastError = e.message ?: "analysis error")
        } finally {
            proxy.close()
        }
    }

    private fun startSender(queue: ArrayBlockingQueue<ByteArray>) {
        val thread = Thread {
            while (running.get()) {
                val body = try {
                    queue.take()
                } catch (e: InterruptedException) {
                    Thread.currentThread().interrupt()
                    return@Thread
                }
                try {
                    val sendStarted = System.nanoTime()
                    sendFrame(VideoFrames.VIDEO_STREAM, body)
                    val took = System.nanoTime() - sendStarted
                    sendNanos.addAndGet(took)
                    lastSendMs = took / 1_000_000f
                    framesSent.incrementAndGet()
                } catch (e: Exception) {
                    log("WARN", "could not send camera frame: ${e.message}")
                }
            }
        }
        thread.name = "phonebridge-camera-sender"
        thread.isDaemon = true
        thread.start()
        sender = thread
    }

    /**
     * The display rotation right now, as a `Surface.ROTATION_*` value.
     *
     * `DisplayManager` is used rather than `Context.getDisplay()` because this
     * runs in a service: a non-visual context has no display of its own, and the
     * window-manager route is deprecated. `DEFAULT_DISPLAY` is still the
     * documented way to ask for the built-in panel.
     */
    private fun displayRotation(): Int {
        val manager = context.getSystemService(Context.DISPLAY_SERVICE) as? DisplayManager
            ?: return Surface.ROTATION_0
        @Suppress("DEPRECATION")
        val rotation = manager.getDisplay(Display.DEFAULT_DISPLAY)?.rotation
        return rotation ?: Surface.ROTATION_0
    }

    /**
     * YUV_420_888 `ImageProxy` -> JPEG, without a Bitmap in between.
     *
     * The version this replaces asked CameraX for RGBA_8888, which means a native
     * YUV->RGB conversion per frame, a `Bitmap` allocation, a second `Bitmap` for
     * the stride crop and then `Bitmap.compress(JPEG)` — four full-frame copies
     * and an RGB->YCbCr conversion inside the encoder, for a picture that was
     * YCbCr to begin with. `YuvImage.compressToJpeg` feeds the planes straight to
     * the platform JPEG encoder, which is why the same frame costs a fraction of
     * the time (measured on the phone with the per-second Log.d line below).
     *
     * The one real cost left is packing the planes into NV21, because the
     * encoder wants one buffer. Both layouts that exist in the wild are handled
     * with bulk copies, not per-pixel loops: semi-planar (pixel stride 2 — most
     * devices, where the V plane already holds the interleaved VU pairs) and
     * fully planar (stride 1).
     */
    private fun ImageProxy.toJpeg(quality: Int): ByteArray? {
        val nv21 = toNv21() ?: return null
        return ByteArrayOutputStream(MAX_FRAME_BYTES / 4).use { out ->
            val image = YuvImage(nv21, ImageFormat.NV21, width, height, null)
            if (!image.compressToJpeg(Rect(0, 0, width, height), quality, out)) return null
            out.toByteArray()
        }
    }

    /** Pack a `YUV_420_888` frame into NV21, honouring every row stride. */
    private fun ImageProxy.toNv21(): ByteArray? {
        if (planes.size < 3) {
            log("WARN", "camera frame has ${planes.size} planes, expected 3")
            return null
        }
        val nv21 = ByteArray(width * height * 3 / 2)
        val y = planes[0]
        val yBuffer = y.buffer.duplicate()
        // The Y plane is row-padded on most phones: copy `width` bytes per row.
        for (row in 0 until height) {
            yBuffer.position(row * y.rowStride)
            yBuffer.get(nv21, row * width, width)
        }
        val u = planes[1]
        val v = planes[2]
        val chroma = width * height
        when {
            // Semi-planar (NV21/NV12): V and U are interleaved with stride 2, and
            // the V buffer's row *is* the VU pair sequence NV21 wants.
            v.pixelStride == 2 && u.pixelStride == 2 -> {
                val vBuffer = v.buffer.duplicate()
                for (row in 0 until height / 2) {
                    vBuffer.position(row * v.rowStride)
                    vBuffer.get(nv21, chroma + row * width, width)
                }
            }
            // Fully planar: interleave V then U, one row at a time.
            v.pixelStride == 1 && u.pixelStride == 1 -> {
                val uBuffer = u.buffer.duplicate()
                val vBuffer = v.buffer.duplicate()
                val uRow = ByteArray(u.rowStride)
                val vRow = ByteArray(v.rowStride)
                for (row in 0 until height / 2) {
                    uBuffer.position(row * u.rowStride)
                    vBuffer.position(row * v.rowStride)
                    uBuffer.get(uRow, 0, minOf(uRow.size, uBuffer.remaining()))
                    vBuffer.get(vRow, 0, minOf(vRow.size, vBuffer.remaining()))
                    var write = chroma + row * width
                    for (column in 0 until width / 2) {
                        nv21[write++] = vRow[column]
                        nv21[write++] = uRow[column]
                    }
                }
            }
            else -> {
                log(
                    "WARN",
                    "unsupported camera plane layout (pixel strides " +
                        "${u.pixelStride}/${v.pixelStride}); frame dropped",
                )
                return null
            }
        }
        return nv21
    }

    /**
     * One line a second, for the phone's own log and for the PC's `--stats`.
     *
     * `adb logcat -s CameraEngine` then shows whether a low frame rate comes from
     * capture, from encoding, or from the send path — the three numbers here.
     */
    private fun reportOnceASecond(now: Long, cfg: VideoFrames.Config) {
        if (lastStatsAtMs == 0L) {
            lastStatsAtMs = now
            lastStatsFrames = encoded.get()
            lastStatsEncodeNanos = encodeNanos.get()
            return
        }
        val elapsed = now - lastStatsAtMs
        if (elapsed < 1000L) return
        val frames = encoded.get()
        val produced = frames - lastStatsFrames
        val encodeDelta = encodeNanos.get() - lastStatsEncodeNanos
        val sendDelta = sendNanos.get() - lastStatsSendNanos
        val seconds = elapsed / 1000.0
        measuredFps = (produced / seconds).toFloat()
        val encodeMean = if (produced > 0) encodeDelta / 1_000_000.0 / produced else 0.0
        val sendMean = if (framesSent.get() > 0) sendDelta / 1_000_000.0 / maxOf(1, produced) else 0.0
        Log.d(
            TAG,
            "capture ${"%.1f".format(measuredFps)} fps  " +
                "yuv->jpeg ${"%.1f".format(encodeMean)} ms  " +
                "q${cfg.quality}  ${lastFrameBytes / 1024} KB  " +
                "send ${"%.1f".format(sendMean)} ms  " +
                "target ${cfg.width}x${cfg.height}@${cfg.fps}",
        )
        lastStatsAtMs = now
        lastStatsFrames = frames
        lastStatsEncodeNanos = encodeNanos.get()
        lastStatsSendNanos = sendNanos.get()
    }



    /**
     * CameraX binds to a `LifecycleOwner`; a foreground service is not one. This
     * tiny owner exists only to give the camera a lifecycle that matches the
     * stream: RESUMED while running, DESTROYED on stop.
     *
     * **Both transitions are main-thread calls** — `LifecycleRegistry` enforces
     * it — so this class is only ever constructed and destroyed inside
     * [onMainThread]. Creating it on the WebSocket thread was the bug behind
     * `Method setCurrentState must be called on the main thread`.
     */
    private class CameraLifecycle : LifecycleOwner {
        private val registry = LifecycleRegistry(this).apply {
            currentState = Lifecycle.State.RESUMED
        }

        override val lifecycle: Lifecycle get() = registry

        fun destroy() {
            registry.currentState = Lifecycle.State.DESTROYED
        }
    }
}
