package dev.phonebridge.protocol

import org.json.JSONObject

/**
 * Message layer inside the encrypted channel.  Mirrors
 * `windows/phonebridge/protocol/messages.py`.
 */
object Messages {
    // control
    const val MSG_CONTROL = 0x10
    const val MSG_PING = 0x11
    const val MSG_PONG = 0x12
    const val MSG_BYE = 0x7F

    // media (Phase 2+)
    const val MSG_AUDIO = 0x30
    const val MSG_VIDEO = 0x40
    const val MSG_FILE = 0x50
    const val MSG_SENSOR = 0x60

    // diagnostic
    const val MSG_BLOB = 0x21

    const val STREAM_CONTROL = 0

    const val K_REQUEST = "req"
    const val K_RESPONSE = "res"
    const val K_EVENT = "evt"

    fun responseOk(id: Any?, result: Map<String, Any?>): ByteArray =
        Json.encodeToBytes(
            Json.obj("k" to K_RESPONSE, "id" to id, "ok" to true, "result" to JSONObject(result as Map<*, *>))
        )

    fun responseErr(id: Any?, code: String, message: String): ByteArray =
        Json.encodeToBytes(
            Json.obj(
                "k" to K_RESPONSE,
                "id" to id,
                "ok" to false,
                "error" to Json.obj("code" to code, "message" to message),
            )
        )

    fun event(name: String, data: Map<String, Any?> = emptyMap()): ByteArray =
        Json.encodeToBytes(
            Json.obj("k" to K_EVENT, "event" to name, "data" to JSONObject(data as Map<*, *>))
        )

    /** Setup messages carry their own `k` and top-level fields. */
    fun handshake(kind: String, vararg fields: Pair<String, Any?>): ByteArray {
        val out = LinkedHashMap<String, Any?>(fields.size + 1)
        out["k"] = kind
        for ((key, value) in fields) out[key] = value
        return Json.encodeToBytes(out)
    }

    fun pong(echoedNanos: Long): ByteArray {
        val buf = java.nio.ByteBuffer.allocate(16).order(java.nio.ByteOrder.BIG_ENDIAN)
        buf.putLong(echoedNanos)
        buf.putLong(System.nanoTime())
        return buf.array()
    }

    fun parsePingEcho(payload: ByteArray): Long {
        if (payload.size != 8) throw IllegalArgumentException("PING payload must be 8 bytes")
        return java.nio.ByteBuffer.wrap(payload).order(java.nio.ByteOrder.BIG_ENDIAN).long
    }
}
