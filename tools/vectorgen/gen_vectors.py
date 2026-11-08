"""Generate cross-language known-answer vectors (Python -> Kotlin).

The Android self-test loads the constants printed here, so a phone can prove its
crypto matches the PC's *without* a PC attached.  Regenerate with::

    python tools/vectorgen/gen_vectors.py

The vectors cover the four places where a Kotlin port can silently diverge:
ECDH secret encoding, HKDF info/order, the SAS truncation, and the AES-GCM
nonce + AAD layout.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))

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


def hexs(raw: bytes) -> str:
    return raw.hex()


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def main() -> None:
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
    c2s = hkdf_sha256(ikm, CNONCE + SNONCE, b"phonebridge/1 c2s", 32)
    s2c = hkdf_sha256(ikm, CNONCE + SNONCE, b"phonebridge/1 s2c", 32)
    sas_raw = hmac.new(c2s, b"phonebridge/1 sas", hashlib.sha256).digest()
    sas = f"{int.from_bytes(sas_raw[:4], 'big') % 1_000_000:06d}"

    keys = SessionKeys(client_to_server=c2s, server_to_client=s2c, sas=sas)
    client_codec = SecureCodec.for_client(keys)
    frame = client_codec.seal(0x10, 7, PAYLOAD)

    pem_a = priv_a.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pem_b = priv_b.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )

    vectors = {
        "scalar_a_pkcs8_b64": b64(pem_a),
        "scalar_b_pkcs8_b64": b64(pem_b),
        "pub_a_raw_hex": hexs(public_key_to_raw(pub_a)),
        "pub_b_raw_hex": hexs(public_key_to_raw(pub_b)),
        "identity_a_device_id": identity_a.device_id,
        "identity_b_device_id": identity_b.device_id,
        "ecdh_shared_hex": hexs(shared_a),
        "cnonce_hex": hexs(CNONCE),
        "snonce_hex": hexs(SNONCE),
        "hkdf_ikm_hex": hexs(ikm),
        "hkdf_salt_hex": hexs(CNONCE + SNONCE),
        "hkdf_c2s_hex": hexs(c2s),
        "hkdf_s2c_hex": hexs(s2c),
        "hkdf_info": "phonebridge/1 c2s",
        "sas_expected": sas,
        "aes_key_hex": hexs(SESSION_KEY),
        "aes_frame_hex": hexs(frame),
        "aes_payload": PAYLOAD.decode(),
        "aes_stream_id": 7,
        "aes_msg_type": 0x10,
    }

    # verify the frame round-trips through the server side
    server_codec = SecureCodec.for_server(keys)
    mt, sid, pl = server_codec.unseal(decode_envelope(frame))
    assert (mt, sid, pl) == (0x10, 7, PAYLOAD)

    out = Path(__file__).parent / "vectors.json"
    out.write_text(json.dumps(vectors, indent=2) + "\n", "utf-8")
    print(f"wrote {out} ({len(vectors)} vectors)")
    print(json.dumps(vectors, indent=2))
    kotlin = Path(__file__).resolve().parents[2] / (
        "android/app/src/main/java/dev/phonebridge/protocol/ProtocolVectors.kt"
    )
    print(f"\nKotlin file to fill: {kotlin}")


if __name__ == "__main__":
    main()
