package dev.phonebridge.net

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.hardware.display.DisplayManager
import android.os.SystemClock
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
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
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
        synchronized(lock) {
            if (running.get()) {
                // Switching lenses or resolution is a restart, not a second camera.
                stopLocked("restarting camera")
            }
            if (!hasHardware()) {
                _state.value = _state.value.copy(active = false, permitted = isPermitted(), lastError = "no camera on this device")
                throw CameraFailure("no_camera", "no camera on this device")
            }
            if (!isPermitted()) {
                _state.value = _state.value.copy(active = false, permitted = false, lastError = "camera permission not granted")
                throw CameraFailure(
                    "camera_denied",
                    "camera permission is not granted: open PhoneBridge on the phone and allow Camera",
                )
            }

            val selector = when (requested.facing) {
                "front" -> CameraSelector.DEFAULT_FRONT_CAMERA
                else -> CameraSelector.DEFAULT_BACK_CAMERA
            }
            val future = ProcessCameraProvider.getInstance(context)
            val camProvider: ProcessCameraProvider
            try {
                camProvider = future.get(5, java.util.concurrent.TimeUnit.SECONDS)
            } catch (e: Exception) {
                _state.value = _state.value.copy(active = false, lastError = "camera unavailable: ${e.message}")
                throw CameraFailure("internal", "camera provider unavailable: ${e.message}")
            }
            if (!camProvider.hasCamera(selector)) {
                _state.value = _state.value.copy(active = false, lastError = "no ${requested.facing} camera")
                throw CameraFailure("no_camera", "this phone has no ${requested.facing} camera")
            }

            // A lens already held by another app is `camera_busy`, not a hang.
            val owner = CameraLifecycle()
            val analysisUseCase = ImageAnalysis.Builder()
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)
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

            try {
                camProvider.unbindAll()
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
                return mapOf("ok" to true, "config" to config.asMap())
            } catch (e: Exception) {
                analysisUseCase.clearAnalyzer()
                camProvider.unbindAll()
                owner.destroy()
                _state.value = _state.value.copy(
                    active = false,
                    permitted = true,
                    lastError = "camera busy: ${e.message}",
                )
                throw CameraFailure(
                    "camera_busy",
                    "the camera could not be opened (another app may be using it): ${e.message}",
                )
            }
        }
    }

    /** Close the camera. Safe to call when nothing is running. */
    fun stop() {
        synchronized(lock) { stopLocked("stopped") }
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
    )

    /** The PC stopped or the session died: nothing should keep the lens open. */
    fun onSessionClosed(why: String) {
        if (running.get()) stopLocked("session closed: $why")
    }

    // --- internals --------------------------------------------------------- //

    private fun analyse(proxy: ImageProxy, cfg: VideoFrames.Config, queue: ArrayBlockingQueue<ByteArray>) {
        try {
            if (!running.get()) return
            val now = SystemClock.elapsedRealtime()
            val interval = 1000L / cfg.fps.coerceAtLeast(1)
            if (lastSentAtMs != 0L && now - lastSentAtMs < interval) return

            val jpeg = proxy.toJpeg(cfg.quality) ?: return
            if (jpeg.size > MAX_FRAME_BYTES) {
                log("WARN", "dropping oversized camera frame (${jpeg.size} bytes)")
                return
            }
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
            _state.value = _state.value.copy(
                width = proxy.width,
                height = proxy.height,
                lastJpeg = jpeg,
                framesSent = framesSent.get().toLong(),
                dropped = framesDropped.get().toLong(),
            )
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
                    sendFrame(VideoFrames.VIDEO_STREAM, body)
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

    /** RGBA_8888 `ImageProxy` -> JPEG. Row stride is honoured, not assumed. */
    private fun ImageProxy.toJpeg(quality: Int): ByteArray? {
        val plane = planes.firstOrNull() ?: return null
        val buffer = plane.buffer
        val pixelStride = plane.pixelStride
        val rowStride = plane.rowStride
        val rowPadding = rowStride - pixelStride * width
        val bitmapWidth = width + rowPadding / pixelStride
        val bitmap = Bitmap.createBitmap(bitmapWidth, height, Bitmap.Config.ARGB_8888)
        buffer.rewind()
        bitmap.copyPixelsFromBuffer(buffer)
        val cropped = if (bitmapWidth == width) {
            bitmap
        } else {
            Bitmap.createBitmap(bitmap, 0, 0, width, height).also { bitmap.recycle() }
        }
        return ByteArrayOutputStream().use { out ->
            cropped.compress(Bitmap.CompressFormat.JPEG, quality, out)
            cropped.recycle()
            out.toByteArray()
        }
    }

    /**
     * CameraX binds to a `LifecycleOwner`; a foreground service is not one. This
     * tiny owner exists only to give the camera a lifecycle that matches the
     * stream: STARTED while running, DESTROYED on stop.
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
