package dev.phonebridge.protocol

import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * PhoneBridge wire framing (protocol v1) - Kotlin side.
 *
 * MUST stay byte-for-byte compatible with
 * `windows/phonebridge/protocol/framing.py`. See `docs/03-protocol.md`.
 *
 * Envelope: `u32 length (BE, includes kind byte) | u8 kind | body`.
 */
object Framing {
    const val PROTOCOL_VERSION = 1

    const val KIND_HANDSHAKE = 0x01
    const val KIND_SECURE = 0x02
    const val KIND_CLOSE = 0x03

    /** 8 MiB: generous for one video keyframe, hostile to a garbage length. */
    const val MAX_ENVELOPE_BODY = 8 * 1024 * 1024

    data class Envelope(val kind: Int, val body: ByteArray) {
        fun asJson(): Map<String, Any?> = Json.toMap(body)

        override fun equals(other: Any?): Boolean =
            other is Envelope && other.kind == kind && other.body.contentEquals(body)

        override fun hashCode(): Int = 31 * kind + body.contentHashCode()
    }

    class FramingException(message: String) : Exception(message)

    fun encode(kind: Int, body: ByteArray): ByteArray {
        require(kind in 0..0xFF) { "kind out of range: $kind" }
        val bodyLength = body.size + 1
        if (bodyLength > MAX_ENVELOPE_BODY) {
            throw FramingException("envelope body too large: $bodyLength")
        }
        return ByteBuffer.allocate(4 + bodyLength)
            .order(ByteOrder.BIG_ENDIAN)
            .putInt(bodyLength)
            .put(kind.toByte())
            .put(body)
            .array()
    }

    fun encodeJson(kind: Int, json: Map<String, Any?>): ByteArray =
        encode(kind, Json.encodeToBytes(json))

    fun decode(data: ByteArray): Envelope {
        if (data.size < 5) throw FramingException("envelope too short: ${data.size}")
        val buffer = ByteBuffer.wrap(data).order(ByteOrder.BIG_ENDIAN)
        val bodyLength = buffer.int
        if (bodyLength < 1) throw FramingException("envelope length must be >= 1")
        if (bodyLength > MAX_ENVELOPE_BODY) {
            throw FramingException("envelope length $bodyLength exceeds limit")
        }
        if (data.size != 4 + bodyLength) {
            throw FramingException(
                "envelope length mismatch: header says $bodyLength, got ${data.size - 4}"
            )
        }
        val kind = data[4].toInt() and 0xFF
        return Envelope(kind, data.copyOfRange(5, data.size))
    }
}
