package dev.phonebridge.protocol

/**
 * Known-answer vectors produced by the Python reference implementation.
 *
 * GENERATED FILE - do not edit by hand. Regenerate with:
 *
 *     python tools/vectorgen/gen_vectors.py
 *
 * That command writes both `tools/vectorgen/vectors.json` and this file, and
 * `windows/tests/test_android_conformance.py` fails if either drifts from the
 * Python implementation.
 *
 * These prove the Kotlin crypto matches the PC's crypto. Without them, the only
 * way to find a divergence (a reordered HKDF info string, a differently padded
 * ECDH secret, a nonce with the counter at the wrong end) would be a failed
 * connection on real hardware with an opaque "authentication failed".
 */
object ProtocolVectors {
    const val SCALAR_A_PKCS8_B64 = "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgGis8TV5vcIGSo7TF1uf4CRorPE1eb3CBkqO0xdbn+AmhRANCAAS1oQS2yq36Fab7nrOTkjcoTUBOnZSGtwZBFFehbxLoShi769l+UlhnB1RCvVMOpNKe562HOoKmLoBL48CONMnW"
    const val SCALAR_B_PKCS8_B64 = "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgKzxNXm9wgZKjtMXW5/gJGis8TV5vcIGSo7TF1uf4CRqhRANCAASVNmOOLzY4ji3tM+MNJK94BVbMeanRTWblxETkk0nPFgC+/hpQqlId+WgdSyepi/2q4lMBFw/UpXk2wsjG7J04"
    const val PUB_A_RAW_HEX = "04b5a104b6caadfa15a6fb9eb3939237284d404e9d9486b706411457a16f12e84a18bbebd97e525867075442bd530ea4d29ee7ad873a82a62e804be3c08e34c9d6"
    const val PUB_B_RAW_HEX = "049536638e2f36388e2ded33e30d24af780556cc79a9d14d66e5c444e49349cf1600befe1a50aa521df9681d4b27a98bfdaae25301170fd4a57936c2c8c6ec9d38"
    const val IDENTITY_A_DEVICE_ID = "141370cea3bea3c4"
    const val IDENTITY_B_DEVICE_ID = "d7ff46e84bc65277"
    const val ECDH_SHARED_HEX = "f911d0d47216c25b4f162f749ceb928a54574d2e39448c73ca03196a5aa9bda9"
    const val CNONCE_HEX = "000102030405060708090a0b0c0d0e0f"
    const val SNONCE_HEX = "101112131415161718191a1b1c1d1e1f"
    const val HKDF_IKM_HEX = "f911d0d47216c25b4f162f749ceb928a54574d2e39448c73ca03196a5aa9bda9f911d0d47216c25b4f162f749ceb928a54574d2e39448c73ca03196a5aa9bda9"
    const val HKDF_SALT_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    const val HKDF_C2S_HEX = "16327003b43f77e2687ee8bd22d637cc5dc82cdcc9c69bd9653c4ff312d28cdc"
    const val HKDF_S2C_HEX = "4d7321bd2e2d1745ce810b941a138aaf76e2468d64d42fab8624ad279d1f01fc"
    const val SAS_EXPECTED = "697549"
    const val AES_KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    const val AES_FRAME_HEX = "000000300200000000000000001ebcb5deb25cebd266cdcb477148f6fcff3533305cee12c7d7a8f8a71ff91f34b30387b5755437"
    const val AES_PAYLOAD = "phonebridge-vector"
    const val AES_STREAM_ID = 7
    const val AES_MSG_TYPE = 16

    fun hexToBytes(hex: String): ByteArray =
        ByteArray(hex.length / 2) {
            ((Character.digit(hex[it * 2], 16) shl 4) or Character.digit(hex[it * 2 + 1], 16)).toByte()
        }
}
