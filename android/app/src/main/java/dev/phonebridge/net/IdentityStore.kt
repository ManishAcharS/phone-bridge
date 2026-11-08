package dev.phonebridge.net

import android.content.Context
import dev.phonebridge.protocol.Crypto

/**
 * The phone's long-lived identity key.
 *
 * Kept in app-private SharedPreferences: no other app on a non-rooted device can
 * read it, and it never leaves the phone.  A hardware-backed Keystore key would
 * be stronger, but `PURPOSE_AGREE_KEY` for EC keys is not consistently available
 * across OEM Android builds, and a Keystore key cannot be exported for backup.
 * For a personal-use device, app-private storage is the pragmatic choice.
 *
 * Losing this key only forces a re-pair; it never weakens the protocol.
 */
class IdentityStore(context: Context) {

    private val prefs = context.getSharedPreferences("phonebridge_identity", Context.MODE_PRIVATE)

    fun loadOrCreate(defaultName: String): Crypto.Identity {
        val pkcs8 = prefs.getString(KEY_PKCS8, null)
        val pubRaw = prefs.getString(KEY_PUBLIC_RAW, null)
        val name = prefs.getString(KEY_NAME, null) ?: defaultName

        if (pkcs8 != null && pubRaw != null) {
            try {
                return Crypto.Identity.fromPkcs8(
                    Crypto.b64d(pkcs8),
                    Crypto.b64d(pubRaw),
                    name,
                )
            } catch (e: Exception) {
                // A corrupt identity is recoverable: regenerate and re-pair.
                prefs.edit().clear().apply()
            }
        }

        val identity = Crypto.Identity.generate(name)
        prefs.edit()
            .putString(KEY_PKCS8, Crypto.b64e(identity.toPkcs8()))
            .putString(KEY_PUBLIC_RAW, Crypto.b64e(identity.publicRaw))
            .putString(KEY_NAME, identity.name)
            .apply()
        return identity
    }

    fun rename(identity: Crypto.Identity, newName: String) {
        identity.name = newName
        prefs.edit().putString(KEY_NAME, newName).apply()
    }

    fun delete() {
        prefs.edit().clear().apply()
    }

    companion object {
        private const val KEY_PKCS8 = "identity_pkcs8_b64"
        private const val KEY_PUBLIC_RAW = "identity_public_raw_b64"
        private const val KEY_NAME = "identity_name"
    }
}
