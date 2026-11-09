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

    fun encodeToBytes(map: Map<String, Any?>): ByteArray = encodeToBytes(mapToJson(map))

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
        for ((k, v) in pairs) o.put(k, wrap(v))
        return o
    }

    /**
     * Build a JSONObject from a Kotlin map, converting nested values.
     *
     * `JSONObject.put` stores a nested `Map` as an ordinary object, and the
     * encoder then prints it as `{a=1}` — a *string*, not JSON. The probe report
     * and `system.info` are nested, so they must be converted explicitly. The
     * previous implementation passed the map straight to `JSONObject(map)`, which
     * only resolved the top level.
     */
    fun mapToJson(map: Map<String, Any?>): JSONObject {
        val o = JSONObject()
        for ((k, v) in map) o.put(k, wrap(v))
        return o
    }

    private fun wrap(value: Any?): Any = when (value) {
        null -> JSONObject.NULL
        is Map<*, *> -> {
            val o = JSONObject()
            for ((k, v) in value) o.put(k.toString(), wrap(v))
            o
        }

        is Iterable<*> -> {
            val a = JSONArray()
            for (v in value) a.put(wrap(v))
            a
        }

        is Array<*> -> {
            val a = JSONArray()
            for (v in value) a.put(wrap(v))
            a
        }

        // NaN/Infinity are not valid JSON and org.json would emit them verbatim.
        is Double -> if (value.isFinite()) value else JSONObject.NULL
        is Float -> if (value.isFinite()) value else JSONObject.NULL
        else -> value
    }
}
