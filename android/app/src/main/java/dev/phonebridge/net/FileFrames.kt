package dev.phonebridge.net

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.Locale

/**
 * The Phase 5 file chunk that rides inside `MSG_FILE` (0x50).
 *
 * Wire format, identical to `windows/phonebridge/files/frames.py`:
 *
 * ```
 * u32 transfer_id
 * u32 chunk_index
 * u32 chunk_len     payload bytes that follow (0 for a pure control frame)
 * u8  flags         bit0 START, bit1 END, bit2 ACK, bit3 CANCEL, bit4 PAUSE, bit5 RESUME
 * u8  reserved      must be zero
 * ..  payload
 * ```
 *
 * Three details are deliberate:
 *
 *  * **`chunk_len` is 32-bit.** A 64 KiB chunk is 65 536 bytes, which does not fit
 *    in 16 bits — an off-by-one that would only appear on the last chunk of a
 *    large file.
 *  * **headers are fixed width**, so a receiver can validate a chunk before
 *    allocating anything for it.
 *  * **an ACK carries no payload** and is rejected if it does; the same for
 *    PAUSE, RESUME and CANCEL.
 */
object FileFrames {

    const val MSG_FILE = 0x50

    /** PC -> phone: the PC sends data, the phone acknowledges here. */
    const val STREAM_TO_PHONE = 5

    /** Phone -> PC: the phone sends data, the PC acknowledges here. */
    const val STREAM_TO_PC = 6

    const val HEADER_SIZE = 14

    const val FLAG_START = 0x01
    const val FLAG_END = 0x02
    const val FLAG_ACK = 0x04
    const val FLAG_CANCEL = 0x08
    const val FLAG_PAUSE = 0x10
    const val FLAG_RESUME = 0x20

    const val CHUNK_SIZE = 64 * 1024
    const val MIN_CHUNK_SIZE = 1024
    const val MAX_CHUNK_SIZE = 256 * 1024

    /** Unacknowledged chunks allowed in flight: 8 x 64 KiB = 512 KiB. */
    const val WINDOW_CHUNKS = 8

    /** Chunks between acknowledgements: every chunk would cost more than it saves. */
    const val ACK_EVERY_CHUNKS = 4

    /** Bigger than the format allows on purpose: the limit is stated to the user. */
    const val MAX_FILE_BYTES = 4L * 1024L * 1024L * 1024L

    /** How long a sender waits for an ack before failing with a reasonable sentence. */
    const val ACK_TIMEOUT_MS = 15_000L

    /** How long a GATT-style file operation may block before it is declared stuck. */
    const val IO_TIMEOUT_MS = 30_000L

    const val PART_SUFFIX = ".part"
    const val MAX_NAME_BYTES = 255

    private val RESERVED_NAMES = setOf(
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    )

    data class Chunk(
        val transferId: Int,
        val chunkIndex: Int,
        val payload: ByteArray,
        val flags: Int = 0,
    ) {
        val isAck: Boolean get() = flags and FLAG_ACK != 0
        val isEnd: Boolean get() = flags and FLAG_END != 0
        val isStart: Boolean get() = flags and FLAG_START != 0
        val isCancel: Boolean get() = flags and FLAG_CANCEL != 0
        val isPause: Boolean get() = flags and FLAG_PAUSE != 0
        val isResume: Boolean get() = flags and FLAG_RESUME != 0

        fun encode(): ByteArray {
            require(transferId >= 0) { "transfer id must be positive" }
            require(chunkIndex >= 0) { "chunk index must be positive" }
            require(payload.size <= MAX_CHUNK_SIZE) {
                "chunk of ${payload.size} bytes exceeds the $MAX_CHUNK_SIZE byte limit"
            }
            val out = ByteArray(HEADER_SIZE + payload.size)
            val buffer = ByteBuffer.wrap(out).order(ByteOrder.BIG_ENDIAN)
            buffer.putInt(transferId)
            buffer.putInt(chunkIndex)
            buffer.putInt(payload.size)
            buffer.put(flags.toByte())
            buffer.put(0)
            System.arraycopy(payload, 0, out, HEADER_SIZE, payload.size)
            return out
        }
    }

    /** Thrown for a body that cannot be trusted; always dropped and counted. */
    class BadChunk(message: String) : IllegalArgumentException(message)

    /** Parse one `MSG_FILE` payload strictly. */
    fun decode(body: ByteArray): Chunk {
        if (body.size < HEADER_SIZE) {
            throw BadChunk("file frame shorter than its $HEADER_SIZE byte header: ${body.size}")
        }
        val buffer = ByteBuffer.wrap(body).order(ByteOrder.BIG_ENDIAN)
        val transferId = buffer.int
        val chunkIndex = buffer.int
        val chunkLen = buffer.int
        buffer.get() // flags
        buffer.get() // reserved
        if (chunkLen < 0 || chunkLen > MAX_CHUNK_SIZE) {
            throw BadChunk("chunk length $chunkLen is outside 0..$MAX_CHUNK_SIZE")
        }
        if (body.size != HEADER_SIZE + chunkLen) {
            throw BadChunk("file frame is ${body.size} bytes, header says ${HEADER_SIZE + chunkLen}")
        }
        val payload = ByteArray(chunkLen)
        System.arraycopy(body, HEADER_SIZE, payload, 0, chunkLen)
        val flags = body[12].toInt() and 0xFF
        if (flags and FLAG_ACK != 0 && chunkLen != 0) {
            throw BadChunk("an acknowledgement must not carry a payload")
        }
        if (transferId < 0 || chunkIndex < 0) {
            throw BadChunk("negative transfer id or chunk index")
        }
        return Chunk(transferId = transferId, chunkIndex = chunkIndex, payload = payload, flags = flags)
    }

    /**
     * A filename that is safe to write, or null when nothing safe is left.
     *
     * This is the same rule the PC applies, exercised from the other side: no
     * separator, no traversal, no control characters, no Windows device name,
     * a bounded length, and never an empty result. A phone that wrote whatever it
     * was told would turn a file transfer into an arbitrary-write primitive.
     */
    fun safeName(raw: String?): String? {
        if (raw == null) return null
        var name = raw.replace('\\', '/').substringAfterLast('/')
        name = name.filter { ch ->
            ch.code >= 0x20 && ch.code != 0x7F && ch != '/' && ch != '\\'
        }
        name = name.trim().trimEnd('.')
        if (name.isEmpty() || name == "." || name == "..") return null
        val stem = name.substringBeforeLast('.', name).uppercase(Locale.ROOT)
        if (stem in RESERVED_NAMES) return null
        if (name.toByteArray(Charsets.UTF_8).size > MAX_NAME_BYTES) {
            val dot = name.lastIndexOf('.')
            val extension = if (dot > 0) name.substring(dot) else ""
            val keep = MAX_NAME_BYTES - extension.toByteArray(Charsets.UTF_8).size
            name = name.take(keep.coerceAtLeast(1)) + extension
        }
        return name
    }

    fun partName(name: String): String = "$name$PART_SUFFIX"

    fun chunkSize(raw: Any?): Int {
        val asked = (raw as? Number)?.toInt() ?: CHUNK_SIZE
        return asked.coerceIn(MIN_CHUNK_SIZE, MAX_CHUNK_SIZE)
    }

    fun isHexDigest(text: String?): Boolean =
        text != null && text.length == 64 && text.all { it.isDigit() || it in 'a'..'f' }

    fun hex(bytes: ByteArray): String {
        val builder = StringBuilder(bytes.size * 2)
        for (byte in bytes) builder.append("%02x".format(byte))
        return builder.toString()
    }

    fun sha256(): MessageDigest = MessageDigest.getInstance("SHA-256")

    /** Human size, used by the phone's own progress line. */
    fun humanBytes(count: Long): String {
        var size = count.toDouble()
        for (unit in listOf("B", "KiB", "MiB", "GiB")) {
            if (size < 1024.0 || unit == "GiB") {
                return if (unit == "B") "${size.toInt()} $unit" else "%.1f %s".format(size, unit)
            }
            size /= 1024.0
        }
        return "$count B"
    }
}
