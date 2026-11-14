package dev.phonebridge.net

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.ImageFormat
import android.graphics.PixelFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.hardware.display.DisplayManager
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.util.Size
import android.view.Display
import android.view.Surface
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
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

        /**
         * How long a freshly bound stream may go without producing a single JPEG
         * before the encoder gives up on the fast path and switches to RGBA.
         *
         * Two seconds is three iterations of the per-second stats line: long
         * enough that a slow first frame (CameraX opens the device, the HAL warms
         * up) is not mistaken for a broken path, short enough that a user who
         * just pressed "stream" is not left looking at "waiting for the first
         * frame" — which is exactly what the YUV path did on a HyperOS device.
         */
        private const val FALLBACK_AFTER_MS = 2_000L
    }

    /**
     * Which encoder produced a JPEG.
     *
     * Both paths exist because `ImageAnalysis` output is not entirely ours to
     * choose: the format we ask for is a request to CameraX and the device, and
     * a device that ignores it (or a CameraX version with a bug on that HAL) must
     * not turn into a camera that never sends a frame. [YUV] is the fast one
     * (`YuvImage` straight from the planes); [RGBA] is the old, known-good one
     * (Bitmap, scaled to the requested size, then `compress`).
     */
    private enum class Path(val code: String) {
        YUV("yuv"),
        RGBA("rgba"),
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
    @Volatile private var config = VideoFrames.Config()
    private var provider: ProcessCameraProvider? = null
    private var camera: Camera? = null
    private var analysis: ImageAnalysis? = null
    private var lifecycle: CameraLifecycle? = null
    private var cameraSelector: CameraSelector? = null
    private var executor = Executors.newSingleThreadExecutor { r ->
        Thread(r, "phonebridge-camera-analysis").apply { isDaemon = true }
    }
    private var sender: Thread? = null
    @Volatile private var sendQueue: ArrayBlockingQueue<ByteArray>? = null
    private var lastSentAtMs = 0L
    private var lastPreviewAtMs = 0L

    // Which encoder is live, and why. Reported in `camera.status` and in every
    // per-frame log line, because "the picture is 5 fps" and "the picture is
    // 400 KB" are both answered by knowing the path.
    @Volatile private var activePath = Path.YUV
    @Volatile private var pathReason = "yuv is the default: no Bitmap, no RGB round trip"

    /**
     * Set once the YUV encoder has failed on this device or produced nothing.
     *
     * Sticky for the life of the process: a phone that cannot encode from the
     * planes will not start being able to after a reconnect, and re-discovering
     * that costs the user two seconds of black picture on every restart.
     */
    @Volatile private var yuvBroken = false
    private val firstAnalyzerFrame = AtomicBoolean(false)
    private val firstJpeg = AtomicBoolean(false)
    private val firstSend = AtomicBoolean(false)
    @Volatile private var captureWidth = 0
    @Volatile private var captureHeight = 0
    @Volatile private var lastCaptureLayout = ""

    // Encoder fallback. The runnable is a field so `stop` can cancel a pending
    // switch instead of rebinding a camera the user already closed.
    private val fallbackWatchdog = Runnable { checkForSilence() }

    // Measurement. All of it is reported once a second with Log.d and in
    // `camera.status`, so a slow frame rate can be attributed to a stage
    // instead of guessed at.
    private val encoded = AtomicInteger(0)
    /** `encoded` when the current stream was bound; the watchdog compares to it. */
    @Volatile private var encodedAtStart = 0
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
        // First line of the logcat trail. `adb logcat -s CameraEngine` shows the
        // request, the bind, the first analyzer frame, the first JPEG and the
        // first send: a stream that never reaches the PC stops at exactly one of
        // those, in this order.
        Log.i(
            TAG,
            "camera.start received: ${requested.facing} ${requested.width}x${requested.height}" +
                " @ ${requested.fps} fps q${requested.quality}, encoder ${activePath.code}" +
                " (running=${running.get()})",
        )
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
        val analysisUseCase = buildAnalysis(requested)
        val queue = ArrayBlockingQueue<ByteArray>(QUEUE_FRAMES)
        analysisUseCase.setAnalyzer(executor) { proxy -> analyse(proxy) }

        // The one part that must be on the main thread, and it waits for the
        // answer: a bind that fails is a `camera_busy` the PC can print, not a
        // silently empty preview.
        return onMainThread("camera.start") {
            bindOnMainThread(camProvider, selector, analysisUseCase, requested, queue)
        }
    }

    /**
     * The `ImageAnalysis` use case for [requested], on the active encoder path.
     *
     * Two things here are load-bearing for the *resolution*, not just the
     * frame rate:
     *
     * * `ResolutionSelector` replaces `setTargetResolution`. The old call is
     *   deprecated, and it expresses the request as an *aspect ratio* hint with
     *   the size as a tie-break, which lets a device return something much
     *   bigger than asked — a 640x480 request that came back as a 1280x960 or
     *   1920x1440 frame is how a stream ends up at 400-590 KB per picture. A
     *   `ResolutionStrategy` with `CLOSEST_LOWER_THEN_HIGHER` asks for at most
     *   the requested size whenever the device has it.
     * * `OUTPUT_IMAGE_FORMAT_*` is a request, not a guarantee. If the device
     *   ignores it, [analyse] keys off the format the frame actually has, and
     *   [fallBack] rebinds on the other path when the chosen one produces
     *   nothing.
     */
    private fun buildAnalysis(requested: VideoFrames.Config): ImageAnalysis {
        val body = ImageAnalysis.Builder()
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .setResolutionSelector(
                ResolutionSelector.Builder()
                    .setResolutionStrategy(
                        ResolutionStrategy(
                            Size(requested.width, requested.height),
                            ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER,
                        )
                    )
                    .build()
            )
            .setOutputImageFormat(
                when (activePath) {
                    // YCbCr straight from the planes: no Bitmap, no RGB round trip.
                    Path.YUV -> ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888
                    // The known-good path: CameraX does the YUV->RGB conversion,
                    // we crop the row padding and scale to the requested size.
                    Path.RGBA -> ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888
                }
            )
            .build()
        // Make the rotation metadata mean something. `rotationDegrees` is
        // relative to the use case's target rotation, and a use case that is
        // not attached to a window defaults that to 0 - so every frame was
        // described as "upright in the phone's natural orientation", which is
        // sideways the moment the phone is held in landscape, and the PC drew
        // it that way. Tying it to the display rotation makes the number mean
        // "turn the picture this far clockwise to be upright as the phone is
        // held now", which is exactly what the receiver does with it.
        body.targetRotation = displayRotation()
        return body
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
            // Everything the analyzer reads is put in place *before* the bind:
            // binding starts the camera, so the first frame can reach the
            // analysis thread while this method is still running.
            provider = camProvider
            cameraSelector = selector
            lifecycle = owner
            sendQueue = queue
            config = requested
            seq.set(0)
            framesSent.set(0)
            framesDropped.set(0)
            encodedAtStart = encoded.get()
            lastSentAtMs = 0
            lastPreviewAtMs = 0
            lastEncodeMs = 0f
            lastSendMs = 0f
            lastFrameBytes = 0
            captureWidth = 0
            captureHeight = 0
            lastCaptureLayout = ""
            firstAnalyzerFrame.set(false)
            firstJpeg.set(false)
            firstSend.set(false)
            running.set(true)
            val bound = camProvider.bindToLifecycle(owner, selector, analysisUseCase)
            camera = bound
            analysis = analysisUseCase
            startSender(queue)
            // The bind succeeding says the device accepted the use case; it does
            // not say a single frame will come out of it. That is what this line
            // and the watchdog are for.
            Log.i(
                TAG,
                "camera bound: ${requested.facing} ${requested.width}x${requested.height}" +
                    " @ ${requested.fps} fps q${requested.quality}, encoder ${activePath.code}" +
                    " (${pathReason})",
            )
            armWatchdog()
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
            running.set(false)
            mainHandler.removeCallbacks(fallbackWatchdog)
            analysisUseCase.clearAnalyzer()
            runCatching { camProvider.unbindAll() }
            // Leave no half-built registry behind: an owner that never got bound
            // still owns a lifecycle that was told it was RESUMED.
            runCatching { created?.destroy() }
            analysis = null
            lifecycle = null
            sendQueue = null
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
        // A pending switch must not rebind a camera that is being closed.
        mainHandler.removeCallbacks(fallbackWatchdog)
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
            // Which encoder is live, and the size CameraX actually delivered.
            // `width`/`height` above are the request; these are the answer, and
            // the two differing is the explanation for oversized frames.
            "path" to activePath.code,
            "path_reason" to pathReason,
            "capture_width" to captureWidth,
            "capture_height" to captureHeight,
            "capture_layout" to lastCaptureLayout,
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

    /**
     * Arm the silence watchdog for [FALLBACK_AFTER_MS] from now.
     *
     * Called by the bind and re-armed by the fallback, so each path gets the same
     * grace period and a camera that produces nothing on either path is reported
     * once instead of stalling quietly.
     */
    private fun armWatchdog() {
        mainHandler.removeCallbacks(fallbackWatchdog)
        mainHandler.postDelayed(fallbackWatchdog, FALLBACK_AFTER_MS)
    }

    /**
     * Nothing encoded since the stream was bound: switch encoder, or say so.
     *
     * A separate function rather than a lambda body because it has to re-arm
     * itself, and a property initializer that mentions its own name is a
     * recursive-initialization error in Kotlin.
     */
    private fun checkForSilence() {
        if (!running.get()) return
        if (encoded.get() > encodedAtStart) return
        if (activePath == Path.RGBA) {
            logFailure(
                "camera.start",
                "no frame was encoded in ${FALLBACK_AFTER_MS} ms on the RGBA path either " +
                    "(${lastCaptureLayout.ifEmpty { "no frame reached the analyzer" }})",
            )
            return
        }
        fallBack("no frame was encoded within ${FALLBACK_AFTER_MS} ms")
        armWatchdog()
    }

    /**
     * One analysis frame: log it if it is the first, throttle, encode, queue.
     *
     * Every exit path closes the [ImageProxy] in `finally`. That is not
     * defensive style: with `STRATEGY_KEEP_ONLY_LATEST` a proxy that is never
     * closed stops the pipeline, so one early `return` without it is a camera
     * that shows "waiting for the first frame" forever.
     */
    private fun analyse(proxy: ImageProxy) {
        try {
            if (!running.get()) return
            val cfg = config
            val queue = sendQueue ?: return
            if (firstAnalyzerFrame.compareAndSet(false, true)) logFirstFrame(proxy, cfg)

            val now = SystemClock.elapsedRealtime()
            val interval = 1000L / cfg.fps.coerceAtLeast(1)
            if (lastSentAtMs != 0L && now - lastSentAtMs < interval) return

            val encodeStarted = System.nanoTime()
            val frame = encode(proxy, cfg) ?: return
            val encodeFinished = System.nanoTime()
            val encodeMs = (encodeFinished - encodeStarted) / 1_000_000f
            encodeNanos.addAndGet(encodeFinished - encodeStarted)
            if (frame.jpeg.size > MAX_FRAME_BYTES) {
                log("WARN", "dropping oversized camera frame (${frame.jpeg.size} bytes)")
                return
            }
            encoded.incrementAndGet()
            lastEncodeMs = encodeMs
            lastFrameBytes = frame.jpeg.size
            lastSentAtMs = now
            if (firstJpeg.compareAndSet(false, true)) {
                Log.i(
                    TAG,
                    "first JPEG encoded: ${frame.jpeg.size} bytes in " +
                        "${"%.1f".format(encodeMs)} ms on the ${frame.path.code} path " +
                        "(${frame.width}x${frame.height}, q${cfg.quality})",
                )
            }
            // One line per frame on purpose: when a stream is 10 fps or 500 KB a
            // frame, the per-second average hides which frames were the slow ones.
            Log.d(
                TAG,
                "frame: ${frame.jpeg.size} B in ${"%.1f".format(encodeMs)} ms " +
                    "(${frame.width}x${frame.height}, ${frame.path.code})",
            )
            val index = seq.getAndIncrement() and 0xFFFF
            val flags = VideoFrames.FLAG_KEY or (if (index == 0) VideoFrames.FLAG_START else 0)
            val body = VideoFrames.encode(
                VideoFrames.Frame(
                    seq = index,
                    timestampMs = SystemClock.elapsedRealtime() - startedAt,
                    payload = frame.jpeg,
                    width = frame.width,
                    height = frame.height,
                    rotation = proxy.imageInfo.rotationDegrees,
                    flags = flags,
                )
            )
            if (now - lastPreviewAtMs >= PREVIEW_INTERVAL_MS) {
                lastPreviewAtMs = now
                _state.value = _state.value.copy(
                    width = frame.width,
                    height = frame.height,
                    lastJpeg = frame.jpeg,
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
            // The exception text is the answer to "why no frames": put the whole
            // stack in logcat under our tag and keep the app log to one line.
            Log.e(TAG, "camera encode failed on the ${activePath.code} path: ${e.message}", e)
            log("WARN", "camera analysis error (${activePath.code} path): ${e.message}")
            _state.value = _state.value.copy(lastError = e.message ?: "analysis error")
            // The fast path is not allowed to be the reason there is no picture:
            // one failure is enough to switch to the path that predates it.
            fallBack("the ${activePath.code} encoder failed: ${e.javaClass.simpleName}: ${e.message}")
        } finally {
            proxy.close()
        }
    }

    /**
     * The single "where did it stop" line: the size and layout the frames really
     * have, next to the size that was asked for.
     */
    private fun logFirstFrame(proxy: ImageProxy, cfg: VideoFrames.Config) {
        captureWidth = proxy.width
        captureHeight = proxy.height
        lastCaptureLayout = buildString {
            append("${proxy.width}x${proxy.height}")
            append(" format=${formatName(proxy.format)}")
            append(" rotation=${proxy.imageInfo.rotationDegrees}")
            append(" crop=${proxy.cropRect.left},${proxy.cropRect.top},${proxy.cropRect.right},${proxy.cropRect.bottom}")
            proxy.planes.forEachIndexed { index, plane ->
                append(" plane$index(row=${plane.rowStride},pixel=${plane.pixelStride})")
            }
        }
        Log.i(
            TAG,
            "first analyzer frame: $lastCaptureLayout, requested ${cfg.width}x${cfg.height}," +
                " encoder ${activePath.code}",
        )
        // A frame four times the size of the request is the difference between a
        // 40 KB JPEG and a 500 KB one. If CameraX ignores the resolution we ask
        // for, take the path that scales, rather than shipping the big frame.
        if (activePath == Path.YUV &&
            (proxy.width > cfg.width * 5 / 4 || proxy.height > cfg.height * 5 / 4)
        ) {
            fallBack(
                "CameraX delivered ${proxy.width}x${proxy.height} for a " +
                    "${cfg.width}x${cfg.height} request",
            )
        }
    }

    private fun formatName(format: Int): String = when (format) {
        ImageFormat.YUV_420_888 -> "YUV_420_888"
        PixelFormat.RGBA_8888 -> "RGBA_8888"
        else -> "format $format"
    }

    /**
     * One frame -> one JPEG, on the path the *frame* allows.
     *
     * The output format is a request to CameraX, so this never assumes the
     * request was honoured: a frame that arrives as `RGBA_8888` is encoded as
     * RGBA whatever the builder asked for, and a YUV frame is encoded from its
     * planes unless that path has already been shown to be broken on this device.
     */
    private fun encode(proxy: ImageProxy, cfg: VideoFrames.Config): Encoded? {
        if (proxy.format == PixelFormat.RGBA_8888) {
            notePath(Path.RGBA, "the device delivered RGBA_8888")
            return rgbaToJpeg(proxy, cfg)
        }
        if (yuvBroken) {
            // Waiting for the rebind to RGBA; encoding the old format again would
            // fail the same way and log the same error 30 times a second.
            return null
        }
        return yuvToJpeg(proxy, cfg)
    }

    /** `YUV_420_888` planes -> NV21 -> JPEG, with no Bitmap in between. */
    private fun yuvToJpeg(proxy: ImageProxy, cfg: VideoFrames.Config): Encoded {
        val planes = proxy.planes
        if (planes.size < 3) {
            throw IllegalStateException(
                "a YUV_420_888 frame needs 3 planes, this one has ${planes.size}",
            )
        }
        val crop = proxy.cropRect
        val hasCrop = crop.width() > 0 && crop.height() > 0
        // 4:2:0 chroma has no half sample: round the crop origin and size down to
        // even pixels (inside the frame) so no row can be shifted by one sample.
        val left = if (hasCrop) crop.left and 1.inv() else 0
        val top = if (hasCrop) crop.top and 1.inv() else 0
        val right = if (hasCrop) minOf(proxy.width, crop.right) else proxy.width
        val bottom = if (hasCrop) minOf(proxy.height, crop.bottom) else proxy.height
        val width = (right - left) and 1.inv()
        val height = (bottom - top) and 1.inv()
        if (width <= 0 || height <= 0) {
            throw IllegalStateException(
                "no usable region in crop ${crop.left},${crop.top},${crop.right},${crop.bottom}" +
                    " of ${proxy.width}x${proxy.height}",
            )
        }
        val nv21 = Nv21.from(
            proxy.width,
            proxy.height,
            Nv21.Plane(planes[0].buffer, planes[0].rowStride, planes[0].pixelStride),
            Nv21.Plane(planes[1].buffer, planes[1].rowStride, planes[1].pixelStride),
            Nv21.Plane(planes[2].buffer, planes[2].rowStride, planes[2].pixelStride),
            cropLeft = left,
            cropTop = top,
            cropWidth = width,
            cropHeight = height,
        )
        val jpeg = ByteArrayOutputStream(MAX_FRAME_BYTES / 8).use { out ->
            val image = YuvImage(nv21, ImageFormat.NV21, width, height, null)
            if (!image.compressToJpeg(Rect(0, 0, width, height), cfg.quality, out)) {
                // Silent before: `compressToJpeg` returning false dropped the
                // frame with no counter and no message, which is indistinguishable
                // from a camera that never opened.
                throw IllegalStateException(
                    "the platform JPEG encoder refused ${width}x$height NV21 at q${cfg.quality}",
                )
            }
            out.toByteArray()
        }
        return Encoded(jpeg, width, height, Path.YUV)
    }

    /**
     * The pre-`41be215` path, kept as the fallback: `Bitmap` -> JPEG.
     *
     * Two fixes over the version it replaces. The row padding is cropped with the
     * bitmap's real stride instead of assumed to be zero, and the bitmap is
     * scaled down to the requested size when CameraX delivered a larger frame —
     * an unscaled 1280x960 frame is a 500 KB JPEG per frame, which is what the
     * PC was receiving at 640x480.
     */
    private fun rgbaToJpeg(proxy: ImageProxy, cfg: VideoFrames.Config): Encoded {
        val plane = proxy.planes.firstOrNull()
            ?: throw IllegalStateException("an RGBA_8888 frame has no planes")
        val rowPadding = plane.rowStride - plane.pixelStride * proxy.width
        if (rowPadding < 0) {
            throw IllegalStateException(
                "row stride ${plane.rowStride} is short for ${proxy.width} RGBA pixels",
            )
        }
        val created = mutableListOf<Bitmap>()
        fun keep(bitmap: Bitmap): Bitmap {
            if (created.none { it === bitmap }) created.add(bitmap)
            return bitmap
        }
        try {
            val paddedWidth = proxy.width + rowPadding / plane.pixelStride
            val padded = keep(Bitmap.createBitmap(paddedWidth, proxy.height, Bitmap.Config.ARGB_8888))
            plane.buffer.rewind()
            padded.copyPixelsFromBuffer(plane.buffer)
            val full = if (paddedWidth == proxy.width) {
                padded
            } else {
                keep(Bitmap.createBitmap(padded, 0, 0, proxy.width, proxy.height))
            }
            val scale = minOf(
                cfg.width.toFloat() / full.width,
                cfg.height.toFloat() / full.height,
            )
            val target = if (scale >= 1f) {
                full
            } else {
                keep(
                    Bitmap.createScaledBitmap(
                        full,
                        maxOf(2, (full.width * scale).toInt() and 1.inv()),
                        maxOf(2, (full.height * scale).toInt() and 1.inv()),
                        true,
                    )
                )
            }
            val jpeg = ByteArrayOutputStream(MAX_FRAME_BYTES / 8).use { out ->
                if (!target.compress(Bitmap.CompressFormat.JPEG, cfg.quality, out)) {
                    throw IllegalStateException(
                        "the platform JPEG encoder refused ${target.width}x${target.height} RGBA",
                    )
                }
                out.toByteArray()
            }
            return Encoded(jpeg, target.width, target.height, Path.RGBA)
        } finally {
            // Every bitmap in this method is scaffolding for one JPEG.
            created.forEach { runCatching { it.recycle() } }
        }
    }

    private fun notePath(path: Path, reason: String) {
        if (activePath == path) return
        activePath = path
        pathReason = reason
        Log.i(TAG, "camera encoder is now ${path.code}: $reason")
    }

    /**
     * Give up on the YUV encoder and rebind the same camera on RGBA.
     *
     * Called from the analysis thread (an encode failed) or from the watchdog
     * (nothing was encoded at all). The rebind is a main-thread operation, so it
     * is posted; [activePath] is flipped first so frames still arriving from the
     * old use case are dropped instead of failing over and over.
     */
    private fun fallBack(reason: String) {
        if (activePath == Path.RGBA) return
        yuvBroken = true
        activePath = Path.RGBA
        pathReason = reason
        Log.i(TAG, "camera falling back to the RGBA encoder: $reason")
        log("WARN", "camera encoder fell back to RGBA: $reason")
        val task = Runnable {
            synchronized(lock) {
                if (!running.get()) return@synchronized
                val camProvider = provider ?: return@synchronized
                val selector = cameraSelector ?: return@synchronized
                try {
                    // Same provider, same queue, same sender thread: only the
                    // frame source changes.
                    analysis?.clearAnalyzer()
                    runCatching { camProvider.unbindAll() }
                    runCatching { lifecycle?.destroy() }
                    analysis = null
                    lifecycle = null
                    // The new use case gets its own first-frame log: the whole
                    // point of the fallback is to show what the other path does.
                    firstAnalyzerFrame.set(false)
                    firstJpeg.set(false)
                    val owner = CameraLifecycle()
                    val useCase = buildAnalysis(config)
                    useCase.setAnalyzer(executor) { proxy -> analyse(proxy) }
                    camera = camProvider.bindToLifecycle(owner, selector, useCase)
                    analysis = useCase
                    lifecycle = owner
                    // The watchdog now measures the new path only.
                    encodedAtStart = encoded.get()
                    armWatchdog()
                    Log.i(TAG, "camera rebound on the RGBA path")
                } catch (e: Exception) {
                    logFailure("camera fallback", "${e.javaClass.simpleName}: ${e.message}", e)
                }
            }
        }
        if (Looper.myLooper() === Looper.getMainLooper()) task.run() else mainHandler.post(task)
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
                    if (firstSend.compareAndSet(false, true)) {
                        // Last line of the trail: an encoded frame that reaches
                        // this point has been handed to the socket. If the PC
                        // still shows nothing, the loss is on the link, not in
                        // the camera.
                        Log.i(
                            TAG,
                            "first frame sent: ${body.size} bytes in " +
                                "${"%.1f".format(took / 1_000_000f)} ms",
                        )
                    }
                } catch (e: Exception) {
                    log("WARN", "could not send camera frame: ${e.message}")
                    Log.e(TAG, "could not send camera frame: ${e.message}", e)
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
                "${activePath.code}->jpeg ${"%.1f".format(encodeMean)} ms  " +
                "q${cfg.quality}  ${lastFrameBytes / 1024} KB  " +
                "send ${"%.1f".format(sendMean)} ms  " +
                "target ${cfg.width}x${cfg.height}@${cfg.fps}  " +
                // `capture` is what CameraX actually delivered. Only when it
                // equals the target is a 500 KB frame our own fault.
                "capture ${captureWidth}x$captureHeight",
        )
        lastStatsAtMs = now
        lastStatsFrames = frames
        lastStatsEncodeNanos = encodeNanos.get()
        lastStatsSendNanos = sendNanos.get()
    }



    /**
     * One encoded picture plus the size it really has.
     *
     * The frame header carries the JPEG's own dimensions, not the requested
     * ones: with the RGBA path scaling a large capture down, or a device
     * delivering a different size than asked, the header and the JPEG would
     * otherwise disagree and the PC would letterbox the picture against numbers
     * that mean nothing.
     */
    private class Encoded(
        val jpeg: ByteArray,
        val width: Int,
        val height: Int,
        val path: Path,
    )

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
