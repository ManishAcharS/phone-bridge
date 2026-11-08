package dev.phonebridge.net

import android.content.Context
import android.content.SharedPreferences
import dev.phonebridge.protocol.Crypto

/**
 * Pinned PCs, kept on the phone in app-private storage.
 *
 * The phone is the authority for pairing: it only accepts a PC whose identity
 * key is listed here.  Losing this file simply means re-pairing, never a
 * bypass.  Nothing is ever uploaded anywhere.
 */
class PairingStore(context: Context) {

    private val prefs: SharedPreferences =
        context.getSharedPreferences("phonebridge_pairing", Context.MODE_PRIVATE)

    data class PinnedPc(
        val deviceId: String,
        val name: String,
        val staticKeyB64: String,
        val addedAt: Long,
    )

    fun isPaired(deviceId: String): Boolean = prefs.contains(key(deviceId))

    fun pin(deviceId: String, name: String, staticKeyB64: String) {
        val existing = prefs.getString(key(deviceId), null)
        if (existing != null && existing != staticKeyB64) {
            throw IllegalStateException(
                "device $deviceId is already pinned with a different key; " +
                    "unpair it first"
            )
        }
        prefs.edit()
            .putString(key(deviceId), staticKeyB64)
            .putString("${key(deviceId)}.name", name)
            .putLong("${key(deviceId)}.at", System.currentTimeMillis())
            .apply()
    }

    /** True when the id is known *and* the key matches the pinned one. */
    fun verify(deviceId: String, staticKeyB64: String): Boolean {
        val pinned = prefs.getString(key(deviceId), null) ?: return false
        return constantTimeEquals(pinned, staticKeyB64)
    }

    fun unpair(deviceId: String) {
        prefs.edit()
            .remove(key(deviceId))
            .remove("${key(deviceId)}.name")
            .remove("${key(deviceId)}.at")
            .apply()
    }

    fun list(): List<PinnedPc> =
        prefs.all.entries
            .filter { !it.key.endsWith(".name") && !it.key.endsWith(".at") }
            .mapNotNull { entry ->
                val value = entry.value as? String ?: return@mapNotNull null
                val id = entry.key.removePrefix("pc.")
                PinnedPc(
                    deviceId = id,
                    name = prefs.getString("${entry.key}.name", "pc") ?: "pc",
                    staticKeyB64 = value,
                    addedAt = prefs.getLong("${entry.key}.at", 0L),
                )
            }

    private fun key(deviceId: String) = "pc.$deviceId"

    private fun constantTimeEquals(a: String, b: String): Boolean {
        if (a.length != b.length) return false
        var diff = 0
        for (i in a.indices) diff = diff or (a[i].code xor b[i].code)
        return diff == 0
    }

    fun sasForDisplay(sas: String): String = Crypto.formatSas(sas)
}
