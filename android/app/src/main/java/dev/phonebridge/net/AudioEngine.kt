package dev.phonebridge.net

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioFormat as AndroidAudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.os.SystemClock
import androidx.core.content.ContextCompat
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
 * 2. **Bounded everywhere.** Playback holds at most [QUEUE_FRAMES] frames and drops
 *    the *oldest* when the PC is late; latency therefore stays under ~200 ms
 *    instead of growing. The microphone sends one frame per read with no queue.
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
        /** 8 frames of 20 ms: 160 ms of slack, then the oldest frame goes. */
        const val QUEUE_FRAMES = 8
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

    // ------------------------------------------------------------------ //
    // PC audio -> this phone's speaker
    // ------------------------------------------------------------------ //
    fun startPlayback(params: Map<*, *>?): Map<String, Any> {
        val fmt = try {
            AudioFrames.Format.fromParams(params)
        } catch (e: IllegalArgumentException) {
            throw RpcFailure("bad_params", e.message ?: "unusable audio format")
        }
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
            // Four frames of device buffer: enough that a late frame does not
            // underrun, small enough that stopping is immediate.
            val buffer = maxOf(minBuffer, bytesPerFrame * 4)
            val track = try {
                AudioTrack.Builder()
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
                    .build()
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
            running.set(true)
            writer = Thread({ writeLoop(track) }, "phonebridge-audio-write").also { it.isDaemon = true; it.start() }
            log("INFO", "audio playback started at ${fmt.sampleRate} Hz, ${fmt.channels} ch, ${fmt.frameMs} ms frames")
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
            if (writeQueue.size >= QUEUE_FRAMES) {
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
                Thread.sleep(5)
                continue
            }
            try {
                track.write(chunk, 0, chunk.size)
            } catch (e: Exception) {
                lastError = "playback write failed: ${e.message}"
                log("ERROR", "playback write failed: ${e.message}")
                running.set(false)
            }
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
    fun status(): Map<String, Any> = mapOf(
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
    )

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
class RpcFailure(val code: String, message: String) : RuntimeException(message)
