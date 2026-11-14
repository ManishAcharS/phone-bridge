package dev.phonebridge.net

import android.Manifest
import android.bluetooth.BluetoothManager
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFormat as AndroidAudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.os.SystemClock
import androidx.core.content.ContextCompat
import dev.phonebridge.BuildInfo
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * Phase 2 audio on the phone: `AudioTrack` for what the PC plays, `AudioRecord`
 * for what the PC listens to.
 *
 * Three rules shaped this class:
 *
 * 1. **Nothing starts by itself.** Playback starts when the PC asks and a session
 *    is authenticated; the microphone starts only when the PC asks *and* the user
 *    has already granted `RECORD_AUDIO` in this app. A refused permission is an
 *    error the PC shows verbatim — never a silent no-op.
 * 2. **Bounded everywhere, and shallow.** Playback holds at most [QUEUE_FRAMES]
 *    frames and drops the *oldest* when the PC is late. It aims for a third of
 *    that and grows only when the device reports underruns, because every frame
 *    of queue is 20 ms of delay the user hears. The microphone sends one frame
 *    per read with no queue at all.
 * 3. **Everything is released.** [release] stops both directions and frees the
 *    hardware; it is called when the last session ends, so a closed window cannot
 *    leave a hot microphone behind.
 */
class AudioEngine(
    private val context: Context,
    private val sendFrame: (streamId: Int, body: ByteArray) -> Unit,
    private val log: (level: String, message: String) -> Unit,
) {

    companion object {
        /**
         * Deepest the playback queue may grow: 8 frames of 20 ms = 160 ms.
         *
         * That is the *ceiling*, not the default. The queue used to be a fixed
         * eight frames, which is 160 ms of delay before a single Bluetooth
         * packet is involved; the adaptive controller below only climbs here
         * when the device proves it cannot keep up.
         */
        const val QUEUE_FRAMES = 8

        /** Shallowest the queue may get. One frame of slack, never zero. */
        const val MIN_QUEUE_FRAMES = 1

        /** What the phone aims for when the PC does not say: 3 x 20 ms. */
        const val DEFAULT_TARGET_MS = 60

        /** How often the controller looks at the device's underrun counter. */
        private const val ADAPT_INTERVAL_MS = 500L

        /** Sleep when the writer finds nothing: short enough to stay under a frame. */
        private const val IDLE_SLEEP_MS = 2L
    }

    private val lock = Any()
    private val startedAt = SystemClock.elapsedRealtime()

    private var playback: AudioTrack? = null
    private var capture: AudioRecord? = null
    private var writer: Thread? = null
    private var reader: Thread? = null

    private val writeQueue = ArrayDeque<ByteArray>()
    private val running = AtomicBoolean(false)
    private val capturing = AtomicBoolean(false)
    private val seqOut = AtomicInteger(0)

    // --- latency --------------------------------------------------------- //
    // Every number here exists because "about half a second" is not a
    // measurement. The queue target is adaptively sized, the player asks Android
    // for the low-latency path, and the probe times one frame from arrival to the
    // point the AudioTrack's play head passed it.

    /** Frames of queue the phone is aiming for right now (1..[QUEUE_FRAMES]). */
    private var targetFrames = 3
    private var minFrames = MIN_QUEUE_FRAMES
    private var maxFrames = QUEUE_FRAMES

    /** Whether the target may move on its own (the PC can pin it). */
    private var adaptive = true
    /** Which performance mode the track was built with. */
    private var lowLatency = true
    /** Track buffer actually granted, in milliseconds (0 before a track exists). */
    private var trackBufferMs = 0

    private var lastUnderruns = -1
    private var lastAdaptAtMs = 0L
    private var emptyPollsInWindow = 0
    private var writesInWindow = 0
    private var grewCount = 0
    private var shrankCount = 0

    // Latency probe: one marked frame, timed from arrival to the play head.
    private var probeArmed = false
    private var probeArrivedAtMs = 0L
    private var probeHeadTarget = 0L
    private var probeLatencyMs = -1L
    private var probeState = "idle"
    /** Frames handed to the track since it started; the probe's head target. */
    private var framesWrittenTotal = 0L

    private var format = AudioFrames.Format()
    private var volumePercent = 40
    private var muted = true
    private var playbackState = "idle"
    private var micState = "idle"
    private var micMuted = false
    private var droppedFrames = 0
    private var framesIn = 0
    private var framesOut = 0
    private var lastError: String? = null

    /** Chunks actually handed to `AudioTrack.write`, i.e. what the phone played. */
    private val framesWritten = AtomicInteger(0)

    // ------------------------------------------------------------------ //
    // PC audio -> this phone's speaker
    // ------------------------------------------------------------------ //
    fun startPlayback(params: Map<*, *>?): Map<String, Any> {
        val fmt = try {
            AudioFrames.Format.fromParams(params)
        } catch (e: IllegalArgumentException) {
            throw RpcFailure("bad_params", e.message ?: "unusable audio format")
        }
        // The PC states its own latency budget, and may arm the probe. Both are
        // optional: an older PC sends neither and gets the same behaviour as
        // before, with a queue that is now adaptive instead of fixed at 160 ms.
        val requestedTargetMs = intParam(params, "target_ms", DEFAULT_TARGET_MS)
        val wantAdaptive = boolParam(params, "adaptive", true)
        val wantLowLatency = boolParam(params, "low_latency", true)
        val wantProbe = boolParam(params, "probe", false)
        synchronized(lock) {
            if (playbackState == "active") return playbackResult("already active", fmt)
            playback?.let { runCatching { it.release() } }
            val channelMask = if (fmt.channels == 1) {
                AndroidAudioFormat.CHANNEL_OUT_MONO
            } else {
                AndroidAudioFormat.CHANNEL_OUT_STEREO
            }
            val minBuffer = AudioTrack.getMinBufferSize(
                fmt.sampleRate,
                channelMask,
                AndroidAudioFormat.ENCODING_PCM_16BIT,
            )
            val bytesPerFrame = fmt.bytesPerFrame
            // The target the PC asked for, in frames, clamped to what the queue
            // is allowed to be. The *track* then gets two frames of headroom: the
            // queue is where latency is decided, and a track buffer smaller than
            // the mixer period only produces underruns.
            minFrames = MIN_QUEUE_FRAMES
            maxFrames = QUEUE_FRAMES
            targetFrames = (requestedTargetMs / fmt.frameMs).coerceIn(minFrames, maxFrames)
            adaptive = wantAdaptive
            lowLatency = wantLowLatency
            val buffer = maxOf(minBuffer, bytesPerFrame * maxOf(2, minOf(targetFrames + 2, maxFrames)))
            val builder = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                .setAudioFormat(
                    AndroidAudioFormat.Builder()
                        .setEncoding(AndroidAudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(fmt.sampleRate)
                        .setChannelMask(channelMask)
                        .build()
                )
                .setTransferMode(AudioTrack.MODE_STREAM)
                .setBufferSizeInBytes(buffer)
            if (lowLatency) {
                // PERFOMANCE_MODE_LOW_LATENCY asks the platform for the fast
                // mixer path: the same request a game makes. It does nothing for
                // A2DP (the Bluetooth stack re-buffers anyway, see docs/19) and
                // it removes one full mixer period on the built-in speaker and on
                // wired output. Refused on some vendor ROMs, so it is guarded.
                runCatching { builder.setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY) }
            }
            val track = try {
                builder.build()
            } catch (e: Exception) {
                throw RpcFailure("no_output", "the phone could not open an audio output: ${e.message}")
            }
            track.setVolume(volumePercent / 100f)
            try {
                track.play()
            } catch (e: Exception) {
                runCatching { track.release() }
                throw RpcFailure("no_output", "the phone could not start playback: ${e.message}")
            }
            format = fmt
            playback = track
            playbackState = "active"
            writeQueue.clear()
            droppedFrames = 0
            framesWritten.set(0)
            framesWrittenTotal = 0
            trackBufferMs = if (fmt.sampleRate > 0) track.bufferSizeInFrames * 1000 / fmt.sampleRate else 0
            // A fresh track resets the controller: lastUnderruns of -1 means
            // "no snapshot yet", not "zero underruns".
            lastUnderruns = -1
            emptyPollsInWindow = 0
            lastAdaptAtMs = SystemClock.elapsedRealtime()
            grewCount = 0
            shrankCount = 0
            probeArmed = wantProbe
            probeState = if (wantProbe) "armed" else "idle"
            probeArrivedAtMs = 0
            probeHeadTarget = 0
            probeLatencyMs = -1
            running.set(true)
            writer = Thread({ writeLoop(track) }, "phonebridge-audio-write").also { it.isDaemon = true; it.start() }
            log(
                "INFO",
                "audio playback started at ${fmt.sampleRate} Hz, ${fmt.channels} ch, " +
                    "${fmt.frameMs} ms frames, queue target ${targetFrames} frames " +
                    "(${targetFrames * fmt.frameMs} ms), track buffer ${trackBufferMs} ms" +
                    (if (lowLatency) ", low latency" else "") +
                    (if (wantProbe) ", probe armed" else ""),
            )
            return playbackResult("playing", fmt)
        }
    }

    fun stopPlayback(): Map<String, Any> {
        synchronized(lock) {
            if (playbackState == "idle") return mapOf("ok" to true, "playback" to "idle")
            running.set(false)
            writeQueue.clear()
            writer?.let { runCatching { it.join(500) } }
            writer = null
            playback?.let {
                runCatching { it.stop() }
                runCatching { it.release() }
            }
            playback = null
            playbackState = "idle"
            probeArmed = false
            probeState = "idle"
            writeQueue.clear()
            log("INFO", "audio playback stopped")
            return mapOf("ok" to true, "playback" to "idle")
        }
    }

    fun setMuted(value: Boolean): Map<String, Any> {
        muted = value
        return mapOf("ok" to true, "muted" to muted)
    }

    fun setVolume(percent: Int): Map<String, Any> {
        if (percent !in 0..100) throw RpcFailure("bad_params", "percent out of range: $percent")
        volumePercent = percent
        playback?.let { track ->
            // The PC's slider scales the stream, not the phone's system volume: a
            // relay must not move a volume the user set for their own music.
            runCatching { track.setVolume(percent / 100f) }
        }
        return mapOf("ok" to true, "percent" to percent)
    }

    /** One frame from the PC. Decoding failures are counted, never fatal. */
    fun onFrame(streamId: Int, body: ByteArray) {
        val frame = try {
            AudioFrames.decode(body)
        } catch (e: IllegalArgumentException) {
            lastError = e.message
            log("WARN", "bad audio frame: ${e.message}")
            return
        }
        if (frame.isStop) {
            stopPlayback()
            return
        }
        framesIn++
        if (frame.isMuted || frame.payload.isEmpty()) return
        synchronized(lock) {
            if (playbackState != "active") return
            // The probe times the very first frame that arrives after the PC
            // armed it: that is the click, and it is the only frame whose delay
            // from arrival to the speaker can be attributed to this phone.
            if (probeArmed && probeArrivedAtMs == 0L) {
                probeArrivedAtMs = SystemClock.elapsedRealtime()
                probeState = "arrived"
            }
            // Drop-oldest: when the producer outruns the writer, the *stale*
            // frame is the one to lose. Dropping the newest instead would keep
            // the audio a full queue behind forever, which is exactly the delay
            // this queue is being shrunk to remove.
            while (writeQueue.size >= targetFrames) {
                writeQueue.removeFirst()
                droppedFrames++
            }
            writeQueue.addLast(frame.payload)
        }
    }

    private fun writeLoop(track: AudioTrack) {
        while (running.get()) {
            val chunk = synchronized(lock) { writeQueue.removeFirstOrNull() }
            if (chunk == null) {
                // Nothing to play: note it for the controller, then wait a
                // fraction of a frame instead of the old 5 ms so a frame that
                // lands mid-sleep is written promptly.
                emptyPollsInWindow++
                synchronized(lock) { updateProbeLocked() }
                adapt(track)
                Thread.sleep(IDLE_SLEEP_MS)
                continue
            }
            try {
                val written = track.write(chunk, 0, chunk.size)
                if (written > 0) {
                    framesWritten.incrementAndGet()
                    writesInWindow++
                    synchronized(lock) {
                        framesWrittenTotal += (written / format.bytesPerFrame).coerceAtLeast(1)
                        if (probeArmed && probeArrivedAtMs > 0 && probeHeadTarget == 0L) {
                            probeHeadTarget = framesWrittenTotal
                            probeState = "written"
                        }
                        updateProbeLocked()
                    }
                }
                adapt(track)
            } catch (e: Exception) {
                lastError = "playback write failed: ${e.message}"
                log("ERROR", "playback write failed: ${e.message}")
                running.set(false)
            }
        }
    }

    /**
     * Shrink the queue while it is healthy, grow it the moment it is not.
     *
     * The only trustworthy signal that the queue is too shallow is the device's
     * own underrun counter going up; the only trustworthy signal that it may be
     * shrunk is that counter standing still *and* the writer never finding the
     * queue empty during the window. Both are measured per window, so a device
     * that underruns once a minute still gets a deeper queue eventually, and a
     * stable one settles at one frame.
     */
    private fun adapt(track: AudioTrack) {
        val now = SystemClock.elapsedRealtime()
        val underruns = runCatching { track.underrunCount }.getOrDefault(0)
        if (lastUnderruns < 0) {
            lastUnderruns = underruns
            lastAdaptAtMs = now
            emptyPollsInWindow = 0
            writesInWindow = 0
            return
        }
        if (now - lastAdaptAtMs < ADAPT_INTERVAL_MS) return
        val fresh = underruns - lastUnderruns
        lastUnderruns = underruns
        val wasEmpty = emptyPollsInWindow > 0
        val writes = writesInWindow
        emptyPollsInWindow = 0
        writesInWindow = 0
        lastAdaptAtMs = now
        if (!adaptive) return
        // Silence is not pressure. With nothing written in the window the queue
        // cannot be "too shallow" - there was nothing to put in it - so neither
        // the underrun counter nor an empty queue says anything about its depth.
        // The PC's loopback produces no packets when the endpoint is idle, so a
        // silent PC makes the track underrun continuously; growing the queue for
        // that is how it sat at eight frames (160 ms) for audio nobody sent.
        if (writes == 0) return
        if (fresh > 0 || wasEmpty) {
            if (targetFrames < maxFrames) {
                targetFrames++
                grewCount++
                log(
                    "INFO",
                    "queue target grew to $targetFrames frames (${targetFrames * frameMs()} ms): " +
                        "$fresh new underruns, queue was empty $wasEmpty",
                )
            }
            return
        }
        if (targetFrames > minFrames) {
            targetFrames--
            shrankCount++
            log("INFO", "queue target shrank to $targetFrames frames (${targetFrames * frameMs()} ms): no underruns for ${ADAPT_INTERVAL_MS} ms")
        }
    }

    /** Frame length of the active format, for the log lines above. */
    private fun frameMs(): Int = format.frameMs

    /**
     * Turn "arrival -> the play head passed that frame" into a number.
     *
     * `playbackHeadPosition` is the AudioTrack sink clock: it advances when the
     * mixer consumes the samples, which is *before* the Bluetooth encoder and its
     * buffer. That is why the probe result is reported as "to the mixer", never
     * as "to the ear" — see docs/19 for the part Android does not tell us.
     */
    private fun updateProbeLocked() {
        if (!probeArmed || probeHeadTarget <= 0 || probeLatencyMs >= 0) return
        val played = runCatching { playback?.playbackHeadPosition?.toLong() }.getOrNull() ?: 0L
        if (played >= probeHeadTarget) {
            probeLatencyMs = (SystemClock.elapsedRealtime() - probeArrivedAtMs).coerceAtLeast(0L)
            probeState = "done"
            log("INFO", "latency probe: arrival -> mixer ${probeLatencyMs} ms (queue target $targetFrames frames, track buffer $trackBufferMs ms)")
        }
    }

    // ------------------------------------------------------------------ //
    // this phone's microphone -> the PC
    // ------------------------------------------------------------------ //
    fun startMic(params: Map<*, *>?): Map<String, Any> {
        val granted = ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.RECORD_AUDIO,
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            throw RpcFailure(
                "mic_denied",
                "microphone permission is not granted on the phone: open PhoneBridge " +
                    "on the phone, grant Microphone in the permissions card, then try again",
            )
        }
        val fmt = try {
            AudioFrames.Format.fromParams(params)
        } catch (e: IllegalArgumentException) {
            throw RpcFailure("bad_params", e.message ?: "unusable audio format")
        }
        synchronized(lock) {
            if (micState == "active") return micResult(fmt)
            val minBuffer = AudioRecord.getMinBufferSize(
                fmt.sampleRate,
                AndroidAudioFormat.CHANNEL_IN_MONO,
                AndroidAudioFormat.ENCODING_PCM_16BIT,
            )
            val record = try {
                AudioRecord(
                    MediaRecorder.AudioSource.MIC,
                    fmt.sampleRate,
                    AndroidAudioFormat.CHANNEL_IN_MONO,
                    AndroidAudioFormat.ENCODING_PCM_16BIT,
                    maxOf(minBuffer, fmt.bytesPerFrame * 4),
                )
            } catch (e: Exception) {
                throw RpcFailure("mic_unavailable", "the microphone could not be opened: ${e.message}")
            }
            if (record.state != AudioRecord.STATE_INITIALIZED) {
                runCatching { record.release() }
                throw RpcFailure("mic_unavailable", "the microphone did not initialise")
            }
            try {
                record.startRecording()
            } catch (e: Exception) {
                runCatching { record.release() }
                throw RpcFailure("mic_unavailable", "the microphone could not start: ${e.message}")
            }
            capture = record
            format = fmt
            micState = "active"
            capturing.set(true)
            reader = Thread({ readLoop(record, fmt) }, "phonebridge-audio-capture").also {
                it.isDaemon = true
                it.start()
            }
            log("INFO", "microphone capture started at ${fmt.sampleRate} Hz")
            return micResult(fmt)
        }
    }

    fun stopMic(): Map<String, Any> {
        synchronized(lock) {
            if (micState == "idle") return mapOf("ok" to true, "mic" to "idle")
            capturing.set(false)
            reader?.let { runCatching { it.join(500) } }
            reader = null
            capture?.let {
                runCatching { it.stop() }
                runCatching { it.release() }
            }
            capture = null
            micState = "idle"
            log("INFO", "microphone capture stopped")
            return mapOf("ok" to true, "mic" to "idle")
        }
    }

    fun setMicMuted(value: Boolean): Map<String, Any> {
        micMuted = value
        return mapOf("ok" to true, "mic_muted" to micMuted)
    }

    private fun readLoop(record: AudioRecord, fmt: AudioFrames.Format) {
        val buffer = ByteArray(fmt.bytesPerFrame)
        var first = true
        while (capturing.get()) {
            val read = try {
                record.read(buffer, 0, buffer.size)
            } catch (e: Exception) {
                lastError = "microphone read failed: ${e.message}"
                log("ERROR", "microphone read failed: ${e.message}")
                break
            }
            if (read <= 0) continue
            val payload = if (micMuted) ByteArray(read) else buffer.copyOfRange(0, read)
            var flags = 0
            if (first) {
                flags = flags or AudioFrames.FLAG_START
                first = false
            }
            if (micMuted) flags = flags or AudioFrames.FLAG_MUTED
            val body = AudioFrames.encode(
                AudioFrames.Frame(
                    seq = seqOut.getAndUpdate { (it + 1) and 0xFFFF },
                    timestampMs = SystemClock.elapsedRealtime() - startedAt,
                    payload = payload,
                    flags = flags,
                )
            )
            framesOut++
            sendFrame(AudioFrames.MIC_STREAM, body)
        }
    }

    // ------------------------------------------------------------------ //
    // status and teardown
    // ------------------------------------------------------------------ //
    fun status(): Map<String, Any> = synchronized(lock) {
        mapOf(
            // Which APK produced this answer. The PC prints it at connect and next
            // to any "the phone never reported X" error, so a stale build is
            // visible instead of inferred.
            "build_id" to BuildInfo.id,
            "playback" to playbackState,
            "mic" to micState,
            "volume" to volumePercent,
            "muted" to muted,
            "mic_muted" to micMuted,
            "permitted" to (ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.RECORD_AUDIO,
            ) == PackageManager.PERMISSION_GRANTED),
            "format" to format.asMap(),
            "frames_in" to framesIn,
            "frames_out" to framesOut,
            "dropped" to droppedFrames,
            "last_error" to (lastError ?: ""),
        ) + playbackSnapshotLocked()
    }

    /**
     * What the phone's own output is doing, for the PC's status line.
     *
     * `playback = active` above only means the RPC succeeded and an `AudioTrack`
     * exists. These are the numbers that answer "is the user actually hearing
     * it": which device Android routed the track to (a Bluetooth route and the
     * built-in speaker are different answers to the same question), whether the
     * track is still playing, how much audio is waiting in front of it, how many
     * times the device ran dry, and how many milliseconds it has played. A
     * `played_ms` that stops climbing while `frames_in` still climbs is a track
     * that has stalled with a full queue — invisible without these.
     */
    private fun playbackSnapshotLocked(): Map<String, Any> {
        val track = playback
        val rate = maxOf(1, format.sampleRate)
        val bufferedMs = writeQueue.size * format.frameMs
        val bufferMs = if (track == null) 0 else track.bufferSizeInFrames * 1000 / rate
        // Unsigned: the counter is a u32 that wraps after ~24 h at 48 kHz.
        val playedFrames = track?.playbackHeadPosition?.toLong()?.and(0xFFFFFFFFL) ?: 0L
        updateProbeLocked()
        val codec = a2dpCodec()
        return mapOf(
            "route" to routeName(track),
            "device_name" to routeProductName(track),
            "track_state" to trackStateName(track),
            "buffered_ms" to bufferedMs,
            "buffer_ms" to bufferMs,
            "underruns" to (track?.underrunCount ?: 0),
            "played_ms" to playedFrames * 1000L / rate,
            "frames_written" to framesWritten.get(),
            // --- latency, per stage ------------------------------------- //
            // These five fields are what makes a latency claim checkable: the
            // queue target the phone settled on, how deep the queue actually is,
            // the track buffer the device granted, whether the fast path was
            // requested, and the one-way probe when the PC ran --latency-test.
            "queue_target_frames" to targetFrames,
            "queue_target_ms" to targetFrames * format.frameMs,
            "queue_depth" to writeQueue.size,
            "adaptive" to adaptive,
            "adapt_grow" to grewCount,
            "adapt_shrink" to shrankCount,
            "low_latency" to lowLatency,
            "track_buffer_ms" to trackBufferMs,
            "probe_state" to probeState,
            "probe_latency_ms" to probeLatencyMs,
            // Android exposes the A2DP codec and its buffer to system apps only,
            // so this is empty on a normal install and the reason says why. The
            // PC prints the honest floor instead of inventing a codec name.
            "a2dp_codec" to codec.first,
            "a2dp_codec_note" to codec.second,
        )
    }

    /**
     * The Bluetooth codec in use, when Android will admit to it.
     *
     * It normally will not: `BluetoothA2dp.getCodecStatus()` is `@SystemApi` and
     * needs BLUETOOTH_PRIVILEGED, so an ordinary app is refused. Rather than
     * print a codec name nobody measured, the phone reports an empty codec plus
     * the reason, and the PC prints the A2DP floor from docs/19 as a range. A
     * blank field would read as "no Bluetooth", which is the wrong conclusion for
     * the one route where the codec decides half the budget.
     */
    private fun a2dpCodec(): Pair<String, String> {
        if (routedDevice(playback)?.type != AudioDeviceInfo.TYPE_BLUETOOTH_A2DP) {
            return "" to "no bluetooth a2dp route (codec not applicable)"
        }
        val adapter = context.getSystemService(BluetoothManager::class.java)?.adapter
        if (adapter?.isEnabled != true) return "" to "bluetooth is off"
        // Attempted anyway: it is the only public-shaped path to the codec, and
        // a refusal is the expected answer rather than a failure to report.
        val attempt = runCatching {
            val getter = adapter.javaClass.getMethod("getCodecStatus")
            getter.isAccessible = true
            getter.invoke(adapter)
        }
        return if (attempt.isSuccess) {
            "" to "codec name not readable from the status object"
        } else {
            "" to "codec not exposed to apps on this Android version"
        }
    }

    /** A numeric RPC parameter, tolerating the JSON numbers Gson hands us. */
    private fun intParam(params: Map<*, *>?, key: String, fallback: Int): Int =
        (params?.get(key) as? Number)?.toInt() ?: fallback

    /** A boolean RPC parameter: JSON booleans arrive as Boolean or as 0/1. */
    private fun boolParam(params: Map<*, *>?, key: String, fallback: Boolean): Boolean =
        when (val raw = params?.get(key)) {
            is Boolean -> raw
            is Number -> raw.toInt() != 0
            is String -> raw.equals("true", ignoreCase = true)
            else -> fallback
        }

    /** The Android output type the track is routed to, in the PC's words. */
    private fun routeName(track: AudioTrack?): String {
        val device = routedDevice(track) ?: return "none"
        return when (device.type) {
            AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "speaker"
            AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "earpiece"
            AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> "bluetooth_a2dp"
            AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "bluetooth_sco"
            AudioDeviceInfo.TYPE_WIRED_HEADSET -> "wired_headset"
            AudioDeviceInfo.TYPE_WIRED_HEADPHONES -> "wired_headphones"
            AudioDeviceInfo.TYPE_USB_DEVICE -> "usb_device"
            AudioDeviceInfo.TYPE_USB_HEADSET -> "usb_headset"
            AudioDeviceInfo.TYPE_DOCK -> "dock"
            AudioDeviceInfo.TYPE_HDMI -> "hdmi"
            else -> "other"
        }
    }

    private fun routeProductName(track: AudioTrack?): String =
        runCatching { routedDevice(track)?.productName?.toString() ?: "" }.getOrDefault("")

    private fun routedDevice(track: AudioTrack?): AudioDeviceInfo? =
        // Throws if the track was released between the two calls; a status read
        // must never be the thing that throws.
        try {
            track?.routedDevice
        } catch (e: Exception) {
            null
        }

    private fun trackStateName(track: AudioTrack?): String {
        if (track == null) return "no_track"
        if (track.state != AudioTrack.STATE_INITIALIZED) return "uninitialised"
        return when (track.playState) {
            AudioTrack.PLAYSTATE_PLAYING -> "playing"
            AudioTrack.PLAYSTATE_PAUSED -> "paused"
            AudioTrack.PLAYSTATE_STOPPED -> "stopped"
            else -> "unknown"
        }
    }

    fun activeDirections(): Set<String> {
        val active = mutableSetOf<String>()
        if (playbackState == "active") active.add("audio.playback")
        if (micState == "active") active.add("audio.capture")
        return active
    }

    /** Stop everything and free the hardware. Safe to call more than once. */
    fun release() {
        runCatching { stopPlayback() }
        runCatching { stopMic() }
        synchronized(lock) {
            writeQueue.clear()
        }
    }

    private fun playbackResult(detail: String, fmt: AudioFrames.Format): Map<String, Any> =
        mapOf(
            "ok" to true,
            "state" to playbackState,
            "detail" to detail,
            "format" to fmt.asMap(),
        )

    private fun micResult(fmt: AudioFrames.Format): Map<String, Any> =
        mapOf("ok" to true, "state" to micState, "format" to fmt.asMap())
}

/** An RPC-level refusal that the PC shows as-is (docs/03 error codes). */
// `open`: BleFailure, CameraFailure, FileFailure, StorageFailure and SensorFailure
// all extend it, and a final class cannot be inherited from.
open class RpcFailure(val code: String, message: String) : RuntimeException(message)
