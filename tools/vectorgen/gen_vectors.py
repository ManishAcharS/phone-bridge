"""Generate cross-language known-answer vectors (Python -> Kotlin).

Writes both:

    tools/vectorgen/vectors.json
    android/app/src/main/java/dev/phonebridge/protocol/ProtocolVectors.kt

so the two can never be hand-edited out of sync. The Android self-test loads the
Kotlin constants, which lets a phone prove its crypto matches the PC's *without*
a PC attached.

Run::

    python tools/vectorgen/gen_vectors.py

The vectors cover the four places where a Kotlin port can silently diverge:
ECDH secret encoding, HKDF info/order, the SAS truncation, and the AES-GCM
nonce + AAD layout.

Design note: the AES vector deliberately uses a standalone key (``SESSION_KEY``)
rather than one of the derived session keys, because its job is to pin the AEAD
*envelope layout* (counter placement, nonce construction, type/stream header)
and keep it independent of key derivation, which the HKDF vector already covers.
The Kotlin self-test builds ``SecureCodec(aes_key, aes_key)``, so the frame must
be sealed with exactly that key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "windows"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from phonebridge.protocol.crypto import (  # noqa: E402
    Identity,
    SecureCodec,
    SessionKeys,
    hkdf_sha256,
    public_key_to_raw,
)
from phonebridge.protocol.framing import decode_envelope  # noqa: E402

# Fixed scalars so every run produces identical vectors.
SCALAR_A = 0x1A2B3C4D5E6F708192A3B4C5D6E7F8091A2B3C4D5E6F708192A3B4C5D6E7F809
SCALAR_B = 0x2B3C4D5E6F708192A3B4C5D6E7F8091A2B3C4D5E6F708192A3B4C5D6E7F8091A
CNONCE = bytes(range(16))
SNONCE = bytes(range(16, 32))
SESSION_KEY = bytes(range(32))
PAYLOAD = b"phonebridge-vector"
STREAM_ID = 7
MSG_TYPE = 0x10

KOTLIN_PATH = (
    REPO / "android/app/src/main/java/dev/phonebridge/protocol/ProtocolVectors.kt"
)
JSON_PATH = Path(__file__).parent / "vectors.json"

KOTLIN_TEMPLATE = """package dev.phonebridge.protocol

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
{constants}
    fun hexToBytes(hex: String): ByteArray =
        ByteArray(hex.length / 2) {
            ((Character.digit(hex[it * 2], 16) shl 4) or Character.digit(hex[it * 2 + 1], 16)).toByte()
        }
}
"""


def build_vectors() -> dict[str, object]:
    priv_a = ec.derive_private_key(SCALAR_A, ec.SECP256R1())
    priv_b = ec.derive_private_key(SCALAR_B, ec.SECP256R1())
    pub_a = priv_a.public_key()
    pub_b = priv_b.public_key()

    shared_a = priv_a.exchange(ec.ECDH(), pub_b)
    shared_b = priv_b.exchange(ec.ECDH(), pub_a)
    assert shared_a == shared_b, "ECDH must be symmetric"

    identity_a = Identity(priv_a, "vector-pc")
    identity_b = Identity(priv_b, "vector-phone")

    ikm = shared_a + shared_b[:32]
    salt = CNONCE + SNONCE
    c2s = hkdf_sha256(ikm, salt, b"phonebridge/1 c2s", 32)
    s2c = hkdf_sha256(ikm, salt, b"phonebridge/1 s2c", 32)
    sas_raw = hmac.new(c2s, b"phonebridge/1 sas", hashlib.sha256).digest()
    sas = f"{int.from_bytes(sas_raw[:4], 'big') % 1_000_000:06d}"

    # A full derived-session pair, checked for symmetry (not embedded as a vector).
    keys = SessionKeys(client_to_server=c2s, server_to_client=s2c, sas=sas)
    assert SecureCodec.for_client(keys).send_key == SecureCodec.for_server(keys).recv_key

    # The AES vector: standalone key, isolating the AEAD envelope layout.
    frame = SecureCodec(SESSION_KEY, SESSION_KEY).seal(MSG_TYPE, STREAM_ID, PAYLOAD)

    # Self-checks: the vector must open with the same key the self-test uses, and
    # must be byte-reproducible from scratch.
    mt, sid, pl = SecureCodec(SESSION_KEY, SESSION_KEY).unseal(decode_envelope(frame))
    assert (mt, sid, pl) == (MSG_TYPE, STREAM_ID, PAYLOAD), "AES vector does not open"
    assert frame == SecureCodec(SESSION_KEY, SESSION_KEY).seal(MSG_TYPE, STREAM_ID, PAYLOAD), (
        "AES vector is not reproducible"
    )

    der_a = priv_a.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    der_b = priv_b.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )

    return {
        "scalar_a_pkcs8_b64": base64.b64encode(der_a).decode("ascii"),
        "scalar_b_pkcs8_b64": base64.b64encode(der_b).decode("ascii"),
        "pub_a_raw_hex": public_key_to_raw(pub_a).hex(),
        "pub_b_raw_hex": public_key_to_raw(pub_b).hex(),
        "identity_a_device_id": identity_a.device_id,
        "identity_b_device_id": identity_b.device_id,
        "ecdh_shared_hex": shared_a.hex(),
        "cnonce_hex": CNONCE.hex(),
        "snonce_hex": SNONCE.hex(),
        "hkdf_ikm_hex": ikm.hex(),
        "hkdf_salt_hex": salt.hex(),
        "hkdf_c2s_hex": c2s.hex(),
        "hkdf_s2c_hex": s2c.hex(),
        "sas_expected": sas,
        "aes_key_hex": SESSION_KEY.hex(),
        "aes_frame_hex": frame.hex(),
        "aes_payload": PAYLOAD.decode("ascii"),
        "aes_stream_id": STREAM_ID,
        "aes_msg_type": MSG_TYPE,
    }


def render_kotlin(vectors: dict[str, object]) -> str:
    lines = []
    for key, value in vectors.items():
        const = key.upper()
        if isinstance(value, int):
            lines.append(f"    const val {const} = {value}")
        else:
            lines.append(f'    const val {const} = "{value}"')
    # Group readability: a blank line before each hex-payload constant.
    body = "\n".join(lines) + "\n"
    # str.format() cannot be used: the Kotlin lambda body contains braces.
    return KOTLIN_TEMPLATE.replace("{constants}", body)


def main() -> None:
    vectors = build_vectors()
    JSON_PATH.write_text(json.dumps(vectors, indent=2) + "\n", "utf-8")
    KOTLIN_PATH.write_text(render_kotlin(vectors), "utf-8")
    print(f"wrote {JSON_PATH}")
    print(f"wrote {KOTLIN_PATH}")
    print(json.dumps(vectors, indent=2))


if __name__ == "__main__":
    main()
