package dev.phonebridge.net

/**
 * The Phase 4 camera frame that rides inside `MSG_VIDEO` (0x40).
 *
 * Wire format, identical to `windows/phonebridge/camera/frames.py`:
 *
 * ```
 * u8  codec      1 = JPEG
 * u8  flags      bit0 START, bit1 STOP, bit2 KEY
 * u16 seq        wrapping counter
 * u32 ts_ms      sender's monotonic milliseconds since the stream started
 * u16 width      pixels, as encoded
 * u16 height     pixels, as encoded
 * u16 rotation   degrees the receiver must rotate before display (0/90/180/270)
 * ..  payload    one complete JPEG, FF D8 ... FF D9
 * ```
 *
 * One message is one whole picture. That is the point of using JPEG here: a lost
 * message is a lost frame, never a half-decoded image, and there is no codec
 * state to rebuild after a reconnect.
 */
object VideoFrames {

    const val CODEC_JPEG = 1

    const val FLAG_START = 0x01
    const val FLAG_STOP = 0x02
    const val FLAG_KEY = 0x04

    const val HEADER_SIZE = 14
    const val MAX_BODY = 2 * 1024 * 1024

    /** Fixed by docs/03: 3 = camera frames from the phone. */
    const val VIDEO_STREAM = 3

    data class Config(
        val width: Int = 1280,
        val height: Int = 720,
        val fps: Int = 15,
        val quality: Int = 70,
        val facing: String = "back",
        /**
         * Pin the exposure's frame-rate range to exactly [fps] instead of letting
         * the device pick a slower one in low light.
         *
         * A camera in a dim room trades frame rate for exposure: it drops to
         * 20-24 fps (as this Xiaomi did at 640x480) or all the way to 15, and the
         * picture is *brighter* for it. Pinning the range to (fps, fps) makes the
         * capture rate predictable and the picture darker, which is why it is a
         * choice the PC makes rather than a default nobody can undo.
         */
        val fixedFps: Boolean = true,
    ) {
        fun asMap(): Map<String, Any> = linkedMapOf(
            "width" to width,
            "height" to height,
            "fps" to fps,
            "quality" to quality,
            "facing" to facing,
            "fixed_fps" to fixedFps,
        )

        companion object {
            /** Parse what the PC asked for, refusing an impossible configuration. */
            fun fromParams(params: Map<*, *>?): Config {
                val raw = params?.get("config")
                val map = (raw as? Map<*, *>) ?: params ?: return Config()
                val width = (map["width"] as? Number)?.toInt() ?: 1280
                val height = (map["height"] as? Number)?.toInt() ?: 720
                val fps = (map["fps"] as? Number)?.toInt() ?: 15
                val quality = (map["quality"] as? Number)?.toInt() ?: 70
                val facing = (map["facing"] as? String) ?: "back"
                // Absent means the modern behaviour (fixed range); an older PC
                // sends nothing and gets the frame rate it asked for.
                val fixedFps = (map["fixed_fps"] as? Boolean)
                    ?: ((map["fixed_fps"] as? Number)?.toInt()?.let { it != 0 } ?: true)
                require(width in 320..3840 && height in 240..2160) {
                    "unsupported resolution ${width}x$height"
                }
                require(fps in 1..60) { "unsupported frame rate $fps" }
                require(quality in 20..95) { "unsupported JPEG quality $quality" }
                require(facing == "back" || facing == "front") { "unknown camera $facing" }
                return Config(width, height, fps, quality, facing, fixedFps)
            }
        }
    }

    data class Frame(
        val seq: Int,
        val timestampMs: Long,
        val payload: ByteArray,
        val width: Int,
        val height: Int,
        val rotation: Int = 0,
        val flags: Int = 0,
        val codec: Int = CODEC_JPEG,
    )

    fun encode(frame: Frame): ByteArray {
        require(frame.payload.size <= MAX_BODY) { "video frame too large" }
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
        out[8] = ((frame.width ushr 8) and 0xFF).toByte()
        out[9] = (frame.width and 0xFF).toByte()
        out[10] = ((frame.height ushr 8) and 0xFF).toByte()
        out[11] = (frame.height and 0xFF).toByte()
        out[12] = ((frame.rotation ushr 8) and 0xFF).toByte()
        out[13] = (frame.rotation and 0xFF).toByte()
        frame.payload.copyInto(out, HEADER_SIZE)
        return out
    }

    /** Build a STOP frame: the PC uses it to end a stream cleanly. */
    fun stopFrame(seq: Int): ByteArray = encode(
        Frame(seq = seq, timestampMs = 0, payload = ByteArray(0), width = 16, height = 16, flags = FLAG_STOP)
    )
}
