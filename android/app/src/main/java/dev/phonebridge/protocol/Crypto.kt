package dev.phonebridge.protocol

import android.util.Base64
import java.math.BigInteger
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.AlgorithmParameters
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.PrivateKey
import java.security.spec.ECGenParameterSpec
import java.security.spec.ECParameterSpec
import java.security.spec.ECPoint
import java.security.spec.ECPublicKeySpec
import java.security.spec.PKCS8EncodedKeySpec
import javax.crypto.Cipher
import javax.crypto.KeyAgreement
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Pairing, key agreement and the encrypted channel - Kotlin side.
 *
 * MUST match `windows/phonebridge/protocol/crypto.py` exactly:
 *
 *   ikm  = ECDH(static_self, static_peer) || ECDH(eph_self, eph_peer)
 *   keys = HKDF-SHA256(ikm, salt = clientNonce || serverNonce)
 *   c2s  = HKDF-Expand(prk, "phonebridge/1 c2s", 32)
 *   s2c  = HKDF-Expand(prk, "phonebridge/1 s2c", 32)
 *   SAS  = first 4 bytes of HMAC-SHA256(c2s, "phonebridge/1 sas") mod 1e6
 *
 * Frames are AES-256-GCM: nonce = 4 zero bytes || u64 BE counter, AAD = the
 * counter bytes, plaintext = u8 type | u32 stream | payload.
 */
object Crypto {
    private const val CURVE = "secp256r1"
    private const val INFO_C2S = "phonebridge/1 c2s"
    private const val INFO_S2C = "phonebridge/1 s2c"
    private const val INFO_SAS = "phonebridge/1 sas"
    private const val GCM_TAG_BITS = 128

    class HandshakeException(message: String, cause: Throwable? = null) :
        Exception(message, cause)

    class ReplayException(message: String) : Exception(message)

    // ------------------------------------------------------------------ //
    // encoding helpers
    // ------------------------------------------------------------------ //
    fun b64e(raw: ByteArray): String = Base64.encodeToString(raw, Base64.NO_WRAP)

    fun b64d(text: String): ByteArray = try {
        Base64.decode(text, Base64.NO_WRAP)
    } catch (e: IllegalArgumentException) {
        throw HandshakeException("invalid base64 field", e)
    }

    private fun fixed32(value: BigInteger): ByteArray {
        val raw = value.toByteArray()
        return when {
            raw.size == 33 && raw[0] == 0.toByte() -> raw.copyOfRange(1, 33)
            raw.size == 32 -> raw
            raw.size < 32 -> ByteArray(32 - raw.size) + raw
            else -> raw.copyOfRange(raw.size - 32, raw.size)
        }
    }

    val ecParams: ECParameterSpec by lazy {
        AlgorithmParameters.getInstance("EC")
            .apply { init(ECGenParameterSpec(CURVE)) }
            .getParameterSpec(ECParameterSpec::class.java)
    }

    fun publicKeyFromRaw(raw: ByteArray) =
        if (raw.size != 65 || raw[0] != 0x04.toByte()) {
            throw HandshakeException("public key must be a 65-byte uncompressed P-256 point")
        } else {
            val point = ECPoint(
                BigInteger(1, raw.copyOfRange(1, 33)),
                BigInteger(1, raw.copyOfRange(33, 65)),
            )
            KeyFactory.getInstance("EC").generatePublic(ECPublicKeySpec(point, ecParams))
        }

    fun publicKeyToRaw(key: java.security.PublicKey): ByteArray {
        val ec = key as java.security.interfaces.ECPublicKey
        return ByteArray(65).also { out ->
            out[0] = 0x04
            fixed32(ec.w.affineX).copyInto(out, 1)
            fixed32(ec.w.affineY).copyInto(out, 33)
        }
    }

    fun agree(privateKey: PrivateKey, peerRaw: ByteArray): ByteArray =
        KeyAgreement.getInstance("ECDH").run {
            init(privateKey)
            doPhase(publicKeyFromRaw(peerRaw), true)
            generateSecret()
        }

    // ------------------------------------------------------------------ //
    // HKDF-SHA256 (RFC 5869)
    // ------------------------------------------------------------------ //
    fun hkdfSha256(ikm: ByteArray, salt: ByteArray, info: ByteArray, length: Int = 32): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(if (salt.isEmpty()) ByteArray(32) else salt, "HmacSHA256"))
        val prk = mac.doFinal(ikm) // extract

        val out = ByteArray(length)
        var previous = ByteArray(0)
        var offset = 0
        var counter = 1
        while (offset < length) {
            mac.init(SecretKeySpec(prk, "HmacSHA256")) // expand
            mac.update(previous)
            mac.update(info)
            mac.update(counter.toByte())
            previous = mac.doFinal()
            val take = minOf(previous.size, length - offset)
            previous.copyInto(out, offset, 0, take)
            offset += take
            counter++
            if (counter > 255) throw HandshakeException("HKDF output too long")
        }
        return out
    }

    fun sasDigits(clientToServerKey: ByteArray): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(clientToServerKey, "HmacSHA256"))
        val digest = mac.doFinal(INFO_SAS.toByteArray(Charsets.UTF_8))
        val value = ByteBuffer.wrap(digest, 0, 4).order(ByteOrder.BIG_ENDIAN).int.toLong() and 0xFFFFFFFFL
        return "%06d".format(value % 1_000_000)
    }

    fun formatSas(sas: String): String =
        if (sas.length == 6) "${sas.substring(0, 3)} ${sas.substring(3)}" else sas

    // ------------------------------------------------------------------ //
    // identity
    // ------------------------------------------------------------------ //
    class Identity private constructor(val keyPair: KeyPair, var name: String) {

        val publicRaw: ByteArray get() = Crypto.publicKeyToRaw(keyPair.public)
        val deviceId: String
            get() = MessageDigest.getInstance("SHA-256").digest(publicRaw)
                .joinToString("") { "%02x".format(it) }
                .substring(0, 16)

        fun toPkcs8(): ByteArray = keyPair.private.encoded

        companion object {
            fun generate(name: String = "android-phone"): Identity {
                val kpg = KeyPairGenerator.getInstance("EC").apply {
                    initialize(ECGenParameterSpec(CURVE))
                }
                return Identity(kpg.generateKeyPair(), name)
            }

            /**
             * Rebuild an identity from persisted material.
             *
             * The public key is supplied as a 65-byte uncompressed point rather
             * than an X.509 blob because the raw form is what goes on the wire
             * and what the pin is computed over.
             */
            fun fromPkcs8(pkcs8: ByteArray, publicRaw: ByteArray, name: String): Identity {
                val priv = KeyFactory.getInstance("EC")
                    .generatePrivate(PKCS8EncodedKeySpec(pkcs8))
                return Identity(KeyPair(Crypto.publicKeyFromRaw(publicRaw), priv), name)
            }
        }
    }

    // ------------------------------------------------------------------ //
    // handshake
    // ------------------------------------------------------------------ //
    data class SessionKeys(
        val clientToServer: ByteArray,
        val serverToClient: ByteArray,
        val sas: String,
    ) {
        val sasDigits: String get() = Crypto.formatSas(sas)
    }

    class HandshakeResponder(private val identity: Identity) {
        private val ephemeralPair: KeyPair =
            KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec(CURVE)) }
                .generateKeyPair()
        private val serverNonce: ByteArray = Crypto.randomBytes(16)

        private var clientNonce: ByteArray? = null
        private var peerStatic: ByteArray? = null

        var peerDeviceId: String? = null
            private set
        var peerName: String? = null
            private set
        var keys: SessionKeys? = null
            private set

        val peerStaticKey: ByteArray? get() = peerStatic

        fun onHello(env: Framing.Envelope, knownPeer: Boolean): ByteArray {
            val msg = env.asJson()
            if (msg["t"] != "hello") throw HandshakeException("expected hello, got ${msg["t"]}")
            val version = (msg["v"] as? Number)?.toInt()
            if (version != Framing.PROTOCOL_VERSION) {
                throw HandshakeException(
                    "protocol version mismatch: PC speaks $version, phone speaks " +
                        Framing.PROTOCOL_VERSION
                )
            }
            val static = Crypto.b64d(msg["static"] as? String ?: throw HandshakeException("missing static"))
            val eph = Crypto.b64d(msg["eph"] as? String ?: throw HandshakeException("missing eph"))
            clientNonce = Crypto.b64d(msg["nonce"] as? String ?: throw HandshakeException("missing nonce"))
            peerDeviceId = msg["id"] as? String
            peerName = msg["name"] as? String ?: "pc"
            peerStatic = static

            val staticShared = Crypto.agree(identity.keyPair.private, static)
            val ephemeralShared = Crypto.agree(ephemeralPair.private, eph)
            keys = derive(staticShared, ephemeralShared, clientNonce!!, serverNonce)

            return Framing.encodeJson(
                Framing.KIND_HANDSHAKE,
                mapOf(
                    "t" to "hello_ack",
                    "v" to Framing.PROTOCOL_VERSION,
                    "id" to identity.deviceId,
                    "name" to identity.name,
                    "static" to Crypto.b64e(identity.publicRaw),
                    "eph" to Crypto.b64e(Crypto.publicKeyToRaw(ephemeralPair.public)),
                    "nonce" to Crypto.b64e(serverNonce),
                    "paired" to knownPeer,
                ),
            )
        }

        private fun derive(
            staticShared: ByteArray,
            ephemeralShared: ByteArray,
            cNonce: ByteArray,
            sNonce: ByteArray,
        ): SessionKeys {
            val ikm = staticShared + ephemeralShared
            val salt = cNonce + sNonce
            val c2s = Crypto.hkdfSha256(ikm, salt, INFO_C2S.toByteArray(Charsets.UTF_8), 32)
            val s2c = Crypto.hkdfSha256(ikm, salt, INFO_S2C.toByteArray(Charsets.UTF_8), 32)
            return SessionKeys(c2s, s2c, Crypto.sasDigits(c2s))
        }
    }

    // ------------------------------------------------------------------ //
    // secure channel
    // ------------------------------------------------------------------ //
    class SecureCodec(sendKey: ByteArray, recvKey: ByteArray) {
        private val sendCipher = Cipher.getInstance("AES/GCM/NoPadding")
        private val recvCipher = Cipher.getInstance("AES/GCM/NoPadding")
        private val sendSpec = SecretKeySpec(sendKey, "AES")
        private val recvSpec = SecretKeySpec(recvKey, "AES")

        private var sendCounter = 0L
        private var recvCounter = -1L

        init {
            if (sendKey.size != 32 || recvKey.size != 32) {
                throw HandshakeException("session keys must be 32 bytes (AES-256)")
            }
        }

        var framesSent: Long = 0
            private set

        fun seal(msgType: Int, streamId: Int, payload: ByteArray): ByteArray {
            val counter = sendCounter++
            val counterBytes = ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN)
                .putLong(counter).array()
            val plaintext = ByteBuffer.allocate(5 + payload.size)
                .order(ByteOrder.BIG_ENDIAN)
                .put(msgType.toByte())
                .putInt(streamId)
                .put(payload)
                .array()

            synchronized(sendCipher) {
                sendCipher.init(
                    Cipher.ENCRYPT_MODE, sendSpec,
                    GCMParameterSpec(GCM_TAG_BITS, nonce(counter)),
                )
                sendCipher.updateAAD(counterBytes)
                val ciphertext = sendCipher.doFinal(plaintext)
                framesSent++
                return Framing.encode(Framing.KIND_SECURE, counterBytes + ciphertext)
            }
        }

        data class Opened(val msgType: Int, val streamId: Int, val payload: ByteArray)

        fun unseal(env: Framing.Envelope): Opened {
            if (env.kind != Framing.KIND_SECURE) {
                throw HandshakeException(
                    "expected SECURE envelope, got kind 0x%02x (plaintext during an " +
                        "established session is refused)".format(env.kind)
                )
            }
            if (env.body.size < 8 + 16) throw HandshakeException("secure envelope too short")
            val counter = ByteBuffer.wrap(env.body, 0, 8).order(ByteOrder.BIG_ENDIAN).long
            if (counter < 0) throw ReplayException("counter out of range")
            val counterBytes = env.body.copyOfRange(0, 8)

            val plaintext = synchronized(recvCipher) {
                try {
                    recvCipher.init(
                        Cipher.DECRYPT_MODE, recvSpec,
                        GCMParameterSpec(GCM_TAG_BITS, nonce(counter)),
                    )
                    recvCipher.updateAAD(counterBytes)
                    recvCipher.doFinal(env.body, 8, env.body.size - 8)
                } catch (e: Exception) {
                    throw HandshakeException("frame failed authentication: ${e.message}", e)
                }
            }
            if (counter <= recvCounter) {
                throw ReplayException("replayed frame: counter $counter <= $recvCounter")
            }
            recvCounter = counter
            if (plaintext.size < 5) throw Framing.FramingException("decrypted frame too short")
            val buf = ByteBuffer.wrap(plaintext).order(ByteOrder.BIG_ENDIAN)
            val msgType = buf.get().toInt() and 0xFF
            val streamId = buf.int
            return Opened(msgType, streamId, plaintext.copyOfRange(5, plaintext.size))
        }

        companion object {
            fun forServer(keys: SessionKeys): SecureCodec =
                SecureCodec(keys.serverToClient, keys.clientToServer)

            fun forClient(keys: SessionKeys): SecureCodec =
                SecureCodec(keys.clientToServer, keys.serverToClient)

            private fun nonce(counter: Long): ByteArray =
                ByteArray(4) + ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN)
                    .putLong(counter).array()
        }
    }

    fun randomBytes(count: Int): ByteArray = ByteArray(count).also {
        java.security.SecureRandom().nextBytes(it)
    }
}
