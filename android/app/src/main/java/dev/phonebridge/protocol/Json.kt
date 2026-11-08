package dev.phonebridge.protocol

import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject

/**
 * Minimal JSON helpers on top of Android's built-in org.json.
 *
 * Deliberately dependency-free: the control channel is a few hundred small
 * messages per session, so a hand-rolled encoder costs nothing and avoids
 * pulling kotlinx.serialization into the build for Phase 1.
 */
object Json {
    class JsonException(message: String) : Exception(message)

    fun encodeToBytes(map: Map<String, Any?>): ByteArray =
        encodeToBytes(JSONObject(map as Map<*, *>))

    fun encodeToBytes(obj: JSONObject): ByteArray = obj.toString().toByteArray(Charsets.UTF_8)

    fun toMap(bytes: ByteArray): Map<String, Any?> {
        val text = String(bytes, Charsets.UTF_8)
        val obj = try {
            JSONObject(text)
        } catch (e: JSONException) {
            throw JsonException("not a JSON object: ${e.message}")
        }
        return toMap(obj)
    }

    fun toMap(obj: JSONObject): Map<String, Any?> {
        val out = LinkedHashMap<String, Any?>(obj.length())
        for (key in obj.keys()) {
            out[key] = unwrap(obj.opt(key))
        }
        return out
    }

    private fun unwrap(value: Any?): Any? = when (value) {
        null, JSONObject.NULL -> null
        is JSONObject -> toMap(value)
        is JSONArray -> (0 until value.length()).map { unwrap(value.opt(it)) }
        is Double ->
            // Keep integers as integers: RPC ids and ports are longs on the PC.
            if (value == Math.floor(value) && !value.isInfinite()) value.toLong() else value
        else -> value
    }

    fun obj(vararg pairs: Pair<String, Any?>): JSONObject {
        val o = JSONObject()
        for ((k, v) in pairs) o.put(k, v ?: JSONObject.NULL)
        return o
    }

    fun mapToJson(map: Map<String, Any?>): JSONObject = JSONObject(map as Map<*, *>)
}
