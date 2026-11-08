package dev.phonebridge.protocol

import android.content.Context
import dev.phonebridge.net.AudioProbe
import java.security.KeyFactory
import java.security.spec.PKCS8EncodedKeySpec
import java.util.Base64

/**
 * On-device protocol self-test.
 *
 * Runs the four crypto primitives against the Python-generated vectors and
 * reports pass/fail per step.  This is what makes the "Run protocol self-test"
 * button meaningful: if this passes, the phone will interoperate with the PC's
 * handshake, and any remaining failure is transport/pairing rather than crypto.
 */
object SelfTest {

    data class Check(val name: String, val passed: Boolean, val detail: String)

    fun run(context: Context? = null): List<Check> {
        val checks = mutableListOf<Check>()

        fun record(name: String, block: () -> String) {
            try {
                checks += Check(name, true, block())
            } catch (e: AssertionError) {
                checks += Check(name, false, e.message ?: "assertion failed")
            } catch (e: Exception) {
                checks += Check(name, false, "${e.javaClass.simpleName}: ${e.message}")
            }
        }

        fun require(condition: Boolean, message: String) {
            if (!condition) throw AssertionError(message)
        }

        fun hex(bytes: ByteArray) =
            bytes.joinToString("") { "%02x".format(java.util.Locale.ROOT, it) }

        record("identity key load + public encoding") {
            val pkcs8 = Base64.getDecoder().decode(ProtocolVectors.SCALAR_A_PKCS8_B64)
            val ident = Crypto.Identity.fromPkcs8(
                pkcs8,
                ProtocolVectors.hexToBytes(ProtocolVectors.PUB_A_RAW_HEX),
                "vector-pc",
            )
            require(
                ident.deviceId == ProtocolVectors.IDENTITY_A_DEVICE_ID,
                "device id ${ident.deviceId} != ${ProtocolVectors.IDENTITY_A_DEVICE_ID}",
            )
            require(
                hex(ident.publicRaw) == ProtocolVectors.PUB_A_RAW_HEX,
                "public key encoding differs from Python",
            )
            "deviceId=${ident.deviceId}"
        }

        record("ECDH shared secret (X coordinate, 32-byte BE)") {
            val pkcs8 = Base64.getDecoder().decode(ProtocolVectors.SCALAR_A_PKCS8_B64)
            val priv = KeyFactory.getInstance("EC").generatePrivate(PKCS8EncodedKeySpec(pkcs8))
            val peer = ProtocolVectors.hexToBytes(ProtocolVectors.PUB_B_RAW_HEX)
            val shared = Crypto.agree(priv, peer)
            require(hex(shared) == ProtocolVectors.ECDH_SHARED_HEX, "shared secret mismatch")
            "${shared.size} bytes match"
        }

        record("HKDF-SHA256 expand (c2s / s2c)") {
            val ikm = ProtocolVectors.hexToBytes(ProtocolVectors.HKDF_IKM_HEX)
            val salt = ProtocolVectors.hexToBytes(ProtocolVectors.HKDF_SALT_HEX)
            val c2s = Crypto.hkdfSha256(ikm, salt, "phonebridge/1 c2s".toByteArray(), 32)
            val s2c = Crypto.hkdfSha256(ikm, salt, "phonebridge/1 s2c".toByteArray(), 32)
            require(hex(c2s) == ProtocolVectors.HKDF_C2S_HEX, "c2s mismatch")
            require(hex(s2c) == ProtocolVectors.HKDF_S2C_HEX, "s2c mismatch")
            "both directions match"
        }

        record("pairing SAS derivation") {
            val c2s = ProtocolVectors.hexToBytes(ProtocolVectors.HKDF_C2S_HEX)
            val sas = Crypto.sasDigits(c2s)
            require(sas == ProtocolVectors.SAS_EXPECTED, "SAS $sas != ${ProtocolVectors.SAS_EXPECTED}")
            Crypto.formatSas(sas)
        }

        record("AES-256-GCM frame seal (nonce/AAD layout)") {
            val key = ProtocolVectors.hexToBytes(ProtocolVectors.AES_KEY_HEX)
            val codec = Crypto.SecureCodec(key, key)
            val frame = codec.seal(
                ProtocolVectors.AES_MSG_TYPE,
                ProtocolVectors.AES_STREAM_ID,
                ProtocolVectors.AES_PAYLOAD.toByteArray(),
            )
            require(hex(frame) == ProtocolVectors.AES_FRAME_HEX, "sealed frame differs from Python")
            "${frame.size}-byte envelope matches"
        }

        record("AES-256-GCM frame open + replay rejection") {
            val key = ProtocolVectors.hexToBytes(ProtocolVectors.AES_KEY_HEX)
            val frame = ProtocolVectors.hexToBytes(ProtocolVectors.AES_FRAME_HEX)
            val codec = Crypto.SecureCodec(key, key)
            val opened = codec.unseal(Framing.decode(frame))
            require(opened.msgType == ProtocolVectors.AES_MSG_TYPE, "msg type mismatch")
            require(opened.streamId == ProtocolVectors.AES_STREAM_ID, "stream id mismatch")
            require(String(opened.payload) == ProtocolVectors.AES_PAYLOAD, "payload mismatch")
            var rejected = false
            try {
                codec.unseal(Framing.decode(frame))
            } catch (e: Crypto.ReplayException) {
                rejected = true
            }
            require(rejected, "replayed frame was accepted")
            "payload recovered, replay refused"
        }

        record("framing round-trip") {
            val env = Framing.encode(Framing.KIND_SECURE, byteArrayOf(1, 2, 3))
            val back = Framing.decode(env)
            require(back.kind == Framing.KIND_SECURE, "kind mismatch")
            require(back.body.contentEquals(byteArrayOf(1, 2, 3)), "body mismatch")
            "${env.size}-byte envelope"
        }

        if (context != null) {
            record("audio routing probe") {
                val headset = AudioProbe.headsetConnected(context)
                val profile = AudioProbe.bluetoothProfileName(context)
                "headset=$headset profile=$profile"
            }
        }

        return checks
    }
}
