package dev.phonebridge.net

/**
 * The Phase 2 audio frame that rides inside `MSG_AUDIO` (0x30).
 *
 * Wire format, identical to `windows/phonebridge/audio/frames.py`:
 *
 * ```
 * u8  codec      1 = PCM16LE
 * u8  flags      bit0 START, bit1 STOP, bit2 MUTED
 * u16 seq        wrapping counter
 * u32 ts_ms      sender's monotonic milliseconds since the stream started
 * ..  payload    PCM16LE
 * ```
 *
 * The phone both sends (microphone) and receives (playback) this frame, so the
 * codec lives in one place and cannot drift between the two directions.
 */
object AudioFrames {

    const val CODEC_PCM16 = 1

    const val FLAG_START = 0x01
    const val FLAG_STOP = 0x02
    const val FLAG_MUTED = 0x04

    const val HEADER_SIZE = 8
    const val MAX_BODY = 512 * 1024
    const val BYTES_PER_SAMPLE = 2

    /** Stream ids are fixed by docs/03: 1 = PC to phone, 2 = phone to PC. */
    const val PLAYBACK_STREAM = 1
    const val MIC_STREAM = 2

    data class Format(
        val sampleRate: Int = 48_000,
        val channels: Int = 1,
        val frameMs: Int = 20,
        val codec: Int = CODEC_PCM16,
    ) {
        val samplesPerFrame: Int get() = sampleRate * frameMs / 1000
        val bytesPerFrame: Int get() = samplesPerFrame * channels * BYTES_PER_SAMPLE

        fun asMap(): Map<String, Any> = mapOf(
            "sample_rate" to sampleRate,
            "channels" to channels,
            "frame_ms" to frameMs,
            "codec" to codec,
        )

        companion object {
            /**
             * Parse what the PC proposed, refusing an impossible format instead of
             * silently playing it at the wrong speed.
             */
            fun fromParams(params: Map<*, *>?): Format {
                val raw = params?.get("format")
                val map = raw as? Map<*, *> ?: params ?: return Format()
                val rate = (map["sample_rate"] as? Number)?.toInt() ?: 48_000
                val channels = (map["channels"] as? Number)?.toInt() ?: 1
                val frameMs = (map["frame_ms"] as? Number)?.toInt() ?: 20
                val codec = (map["codec"] as? Number)?.toInt() ?: CODEC_PCM16
                require(codec == CODEC_PCM16) { "unsupported codec $codec" }
                require(rate in 8_000..48_000) { "sample rate out of range: $rate" }
                require(channels in 1..2) { "channels out of range: $channels" }
                require(frameMs in 5..100) { "frame length out of range: $frameMs" }
                return Format(rate, channels, frameMs, codec)
            }
        }
    }

    data class Frame(
        val seq: Int,
        val timestampMs: Long,
        val payload: ByteArray,
        val codec: Int = CODEC_PCM16,
        val flags: Int = 0,
    ) {
        val isStart: Boolean get() = flags and FLAG_START != 0
        val isStop: Boolean get() = flags and FLAG_STOP != 0
        val isMuted: Boolean get() = flags and FLAG_MUTED != 0
    }

    fun encode(frame: Frame): ByteArray {
        require(frame.payload.size <= MAX_BODY) { "audio frame too large" }
        require(frame.seq in 0..0xFFFF) { "sequence out of range: ${frame.seq}" }
        val out = ByteArray(HEADER_SIZE + frame.payload.size)
        out[0] = frame.codec.toByte()
        out[1] = frame.flags.toByte()
        out[2] = ((frame.seq ushr 8) and 0xFF).toByte()
        out[3] = (frame.seq and 0xFF).toByte()
        val ts = frame.timestampMs and 0xFFFFFFFFL
        out[4] = ((ts ushr 24) and 0xFF).toByte()
        out[5] = ((ts ushr 16) and 0xFF).toByte()
        out[6] = ((ts ushr 8) and 0xFF).toByte()
        out[7] = (ts and 0xFF).toByte()
        frame.payload.copyInto(out, HEADER_SIZE)
        return out
    }

    /** @throws IllegalArgumentException on anything that is not a legal frame. */
    fun decode(body: ByteArray): Frame {
        require(body.size >= HEADER_SIZE) { "audio frame shorter than its header" }
        require(body.size <= MAX_BODY + HEADER_SIZE) { "audio frame too large" }
        val codec = body[0].toInt() and 0xFF
        require(codec == CODEC_PCM16) { "unknown audio codec $codec" }
        val flags = body[1].toInt() and 0xFF
        val seq = ((body[2].toInt() and 0xFF) shl 8) or (body[3].toInt() and 0xFF)
        var ts = 0L
        for (i in 4..7) {
            ts = (ts shl 8) or (body[i].toLong() and 0xFF)
        }
        val payload = body.copyOfRange(HEADER_SIZE, body.size)
        require(payload.size % BYTES_PER_SAMPLE == 0) { "payload is not whole samples" }
        return Frame(seq = seq, timestampMs = ts, payload = payload, codec = codec, flags = flags)
    }
}
