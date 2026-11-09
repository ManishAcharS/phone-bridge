"""Pairing, key agreement and the encrypted channel (protocol v1).

Design in one paragraph
-----------------------
Each side owns a long-lived **identity** key: an ECDH P-256 keypair.  The
device id is the first 16 hex chars of SHA-256 over the uncompressed public
point.  A session runs a small Noise-KK-style handshake: both sides send
ephemeral P-256 public keys, then

    ikm  = ECDH(my_static_priv, peer_static_pub) || ECDH(my_eph_priv, peer_eph_pub)
    keys = HKDF-SHA256(ikm, salt = client_nonce || server_nonce)

Only an entity holding *both* private keys can derive those keys, so a
man-in-the-middle cannot complete the handshake.  Two directional AES-256-GCM
keys come out of HKDF, which means each direction has its own counter starting
at zero -- no nonce reuse is possible by construction.

First-time pairing is **trust on first use + SAS verification**: both sides
display six digits derived from `HMAC(key_c2s, "phonebridge/1 sas")`.  A relay
MITM produces different keys on the two sides, hence different digits, and the
user aborts.  The verified identity keys are then pinned in a local file.

Why P-256 + AES-GCM instead of X25519 + ChaCha20-Poly1305: P-256 ECDH is
available on every Android API level we care about (23+) through the standard
`KeyAgreement`/`KeyFactory` API, and AES-GCM is hardware-accelerated via
`AES/GCM/NoPadding`.  X25519 and ChaCha20 are not uniformly exposed by
`javax.crypto` on older Android releases, and pulling in BouncyCastle for a
personal-use app is not worth the dependency weight.

Threat model: this protects phone<->PC traffic on a shared Wi-Fi network and on
the phone's hotspot.  It does not protect a device whose OS is already
compromised, and it does not encrypt the UDP discovery beacon (which carries no
secrets).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import struct
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .framing import (
    KIND_HANDSHAKE,
    KIND_SECURE,
    PROTOCOL_VERSION,
    Envelope,
    FramingError,
    encode_envelope,
    encode_json_envelope,
)

#: Largest counter value we accept before forcing a re-handshake (~114k years
#: at 1000 frames/second, so this only exists to rule out overflow attacks).
_MAX_COUNTER = 2**63 - 1

_INFO_C2S = b"phonebridge/1 c2s"
_INFO_S2C = b"phonebridge/1 s2c"
_INFO_SAS = b"phonebridge/1 sas"


class HandshakeError(Exception):
    """Handshake failed (bad version, bad MAC, tampered transcript, ...)."""


class PeerKeyChangedError(HandshakeError):
    """The phone's identity key changed for a device we had pinned.

    Distinct from a generic handshake failure because it must **never** be
    retried in a loop: either the phone was reflashed, or someone is
    substituting a device. Retrying would silently re-establish a session with
    the wrong peer, which is exactly the attack pinning exists to stop.
    """


class ProtocolVersionError(HandshakeError):
    """The peer speaks a different protocol version.

    Also terminal: retrying cannot make the two implementations agree, and a
    reconnect loop would hide the mismatch behind "reconnecting forever".
    """


class ReplayError(HandshakeError):
    """A frame arrived with a non-increasing counter."""


class IdentityBindingError(HandshakeError):
    """The peer's claimed device id is not the hash of the key it presented.

    The id is *self-certifying*: ``sha256(static_public_key)[:16]``.  Checking
    that is what stops a peer from claiming somebody else's id -- for instance
    presenting itself under the id of a phone the PC has pinned, or of a PC the
    phone has pinned, while using a key of its own.  The pin comparison would
    also reject that, but only when the id happens to be known; for a
    first-time (trust-on-first-use) connection nothing else would.
    """


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64d(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except Exception as exc:  # noqa: BLE001 - normalised for the caller
        raise HandshakeError(f"invalid base64 field: {exc}") from exc


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=length, salt=salt, info=info
    ).derive(ikm)


#: A device id is the first 16 lowercase hex chars of SHA-256(public point).
DEVICE_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def device_id_for(public_raw: bytes) -> str:
    """The self-certifying device id for a public key.

    Must stay identical to the Kotlin ``Crypto`` implementation and to the
    ``DEVICE_ID`` regex in ``BridgeServer.kt``.
    """
    return hashlib.sha256(public_raw).hexdigest()[:16]


def check_identity_binding(claimed_id: object, public_raw: bytes) -> str:
    """Return ``claimed_id`` if it really is the id of ``public_raw``.

    Raises [IdentityBindingError] otherwise, so an unauthenticated peer cannot
    pick its own id.
    """
    if not isinstance(claimed_id, str) or not DEVICE_ID_RE.match(claimed_id):
        raise IdentityBindingError(
            f"peer sent a malformed device id: {claimed_id!r}"
        )
    expected = device_id_for(public_raw)
    if not hmac.compare_digest(claimed_id, expected):
        raise IdentityBindingError(
            "peer device id does not match its identity key "
            f"(claimed {claimed_id}, key hashes to {expected}); refusing"
        )
    return claimed_id


def public_key_from_raw(raw: bytes) -> ec.EllipticCurvePublicKey:
    if len(raw) != 65 or raw[0] != 0x04:
        raise HandshakeError("public key must be a 65-byte uncompressed P-256 point")
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)


def public_key_to_raw(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
@dataclass
class Identity:
    """A long-lived device identity (ECDH P-256)."""

    private_key: ec.EllipticCurvePrivateKey
    name: str = "phonebridge-device"

    @classmethod
    def generate(cls, name: str = "phonebridge-device") -> Identity:
        return cls(private_key=ec.generate_private_key(ec.SECP256R1()), name=name)

    @property
    def public_raw(self) -> bytes:
        return public_key_to_raw(self.private_key.public_key())

    @property
    def device_id(self) -> str:
        return device_id_for(self.public_raw)

    def public_pem(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def to_pem(self) -> bytes:
        return self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    @classmethod
    def from_pem(cls, pem: bytes, name: str = "phonebridge-device") -> Identity:
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise HandshakeError("identity file does not contain an EC private key")
        return cls(private_key=key, name=name)


# --------------------------------------------------------------------------- #
# handshake
# --------------------------------------------------------------------------- #
@dataclass
class SessionKeys:
    client_to_server: bytes
    server_to_client: bytes
    sas: str

    @property
    def sas_digits(self) -> str:
        return f"{self.sas[:3]} {self.sas[3:]}"


def sas_from_key(client_to_server_key: bytes) -> str:
    """Six-digit pairing code derived from the session key.

    Mirrors ``Crypto.sasDigits`` on Android so both sides can be tested against
    the same vector. Purely informational: the SAS is a value for a human to
    compare, never key material.
    """
    digest = hmac.new(client_to_server_key, _INFO_SAS, hashlib.sha256).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"


def _derive(
    *, static_shared: bytes, ephemeral_shared: bytes, cnonce: bytes, snonce: bytes
) -> SessionKeys:
    ikm = static_shared + ephemeral_shared
    salt = cnonce + snonce
    c2s = hkdf_sha256(ikm, salt, _INFO_C2S)
    s2c = hkdf_sha256(ikm, salt, _INFO_S2C)
    # 24 bits of SAS: enough for a human comparison, and the 1-in-16M false
    # accept only matters if the attacker gets exactly one attempt.
    return SessionKeys(client_to_server=c2s, server_to_client=s2c, sas=sas_from_key(c2s))


class HandshakeInitiator:
    """PC side.  Sends HELLO, then derives keys from HELLO_ACK."""

    def __init__(self, identity: Identity, *, expected_peer_static: bytes | None = None):
        self.identity = identity
        self.expected_peer_static = expected_peer_static
        self._eph = ec.generate_private_key(ec.SECP256R1())
        self._cnonce = os.urandom(16)
        self._peer_static: bytes | None = None
        self._peer_eph: bytes | None = None
        self._snonce: bytes | None = None
        self.peer_device_id: str | None = None
        self.keys: SessionKeys | None = None

    @property
    def peer_static(self) -> bytes | None:
        return self._peer_static

    def hello(self) -> bytes:
        return encode_json_envelope(
            KIND_HANDSHAKE,
            {
                "t": "hello",
                "v": PROTOCOL_VERSION,
                "id": self.identity.device_id,
                "name": self.identity.name,
                "static": b64e(self.identity.public_raw),
                "eph": b64e(public_key_to_raw(self._eph.public_key())),
                "nonce": b64e(self._cnonce),
            },
        )

    def on_hello_ack(
        self, env: Envelope, *, expected_peer_static: bytes | None = None
    ) -> SessionKeys:
        if env.kind != KIND_HANDSHAKE:
            raise HandshakeError(f"expected HANDSHAKE, got kind {env.kind:#04x}")
        msg = env.as_json()
        if msg.get("t") != "hello_ack":
            raise HandshakeError(f"expected hello_ack, got {msg.get('t')!r}")
        if msg.get("v") != PROTOCOL_VERSION:
            raise ProtocolVersionError(
                f"protocol version mismatch: phone speaks {msg.get('v')}, "
                f"PC speaks {PROTOCOL_VERSION}"
            )
        peer_static = b64d(msg["static"])
        # Before anything is pinned or paired: the id must certify the key.
        self.peer_device_id = check_identity_binding(msg.get("id"), peer_static)
        required = (
            expected_peer_static
            if expected_peer_static is not None
            else self.expected_peer_static
        )
        if required is not None and not hmac.compare_digest(peer_static, required):
            raise PeerKeyChangedError(
                "phone identity key does not match the pinned key. "
                "Refusing to connect: this may be a substituted device."
            )
        self._peer_static = peer_static
        self._peer_eph = b64d(msg["eph"])
        self._snonce = b64d(msg["nonce"])

        static_shared = self.identity.private_key.exchange(
            ec.ECDH(), public_key_from_raw(peer_static)
        )
        ephemeral_shared = self._eph.exchange(
            ec.ECDH(), public_key_from_raw(self._peer_eph)
        )
        self.keys = _derive(
            static_shared=static_shared,
            ephemeral_shared=ephemeral_shared,
            cnonce=self._cnonce,
            snonce=self._snonce,
        )
        return self.keys


class HandshakeResponder:
    """Android side.  Answers HELLO with HELLO_ACK and derives the same keys."""

    def __init__(self, identity: Identity):
        self.identity = identity
        self._eph = ec.generate_private_key(ec.SECP256R1())
        self._snonce = os.urandom(16)
        self._cnonce: bytes | None = None
        self._peer_static: bytes | None = None
        self._peer_eph: bytes | None = None
        self.peer_device_id: str | None = None
        self.peer_name: str | None = None
        self.keys: SessionKeys | None = None

    @property
    def peer_static(self) -> bytes | None:
        return self._peer_static

    def on_hello(self, env: Envelope, *, known_peer: bool) -> bytes:
        if env.kind != KIND_HANDSHAKE:
            raise HandshakeError(f"expected HANDSHAKE, got kind {env.kind:#04x}")
        msg = env.as_json()
        if msg.get("t") != "hello":
            raise HandshakeError(f"expected hello, got {msg.get('t')!r}")
        if msg.get("v") != PROTOCOL_VERSION:
            raise ProtocolVersionError(
                f"protocol version mismatch: PC speaks {msg.get('v')}, "
                f"phone speaks {PROTOCOL_VERSION}"
            )
        self._peer_static = b64d(msg["static"])
        self._peer_eph = b64d(msg["eph"])
        self._cnonce = b64d(msg["nonce"])
        # Trust-on-first-use still needs the id to certify the key: otherwise a
        # peer could present somebody else's id and be pinned under it.
        self.peer_device_id = check_identity_binding(msg.get("id"), self._peer_static)
        self.peer_name = str(msg.get("name", "pc"))

        static_shared = self.identity.private_key.exchange(
            ec.ECDH(), public_key_from_raw(self._peer_static)
        )
        ephemeral_shared = self._eph.exchange(
            ec.ECDH(), public_key_from_raw(self._peer_eph)
        )
        self.keys = _derive(
            static_shared=static_shared,
            ephemeral_shared=ephemeral_shared,
            cnonce=self._cnonce,
            snonce=self._snonce,
        )
        return encode_json_envelope(
            KIND_HANDSHAKE,
            {
                "t": "hello_ack",
                "v": PROTOCOL_VERSION,
                "id": self.identity.device_id,
                "name": self.identity.name,
                "static": b64e(self.identity.public_raw),
                "eph": b64e(public_key_to_raw(self._eph.public_key())),
                "nonce": b64e(self._snonce),
                "paired": known_peer,
            },
        )


# --------------------------------------------------------------------------- #
# secure channel
# --------------------------------------------------------------------------- #
_COUNTER = struct.Struct(">Q")


@dataclass
class SecureCodec:
    """AEAD wrapper around one direction-pair of session keys.

    ``seal()`` and ``unseal()`` operate on *whole envelopes* so callers can
    keep treating the link as a byte-oriented channel.
    """

    send_key: bytes
    recv_key: bytes
    _send_counter: int = 0
    _recv_counter: int = -1  # -1 => nothing received yet
    _send_aead: AESGCM = field(init=False, repr=False)
    _recv_aead: AESGCM = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if len(self.send_key) != 32 or len(self.recv_key) != 32:
            raise HandshakeError("session keys must be 32 bytes (AES-256)")
        self._send_aead = AESGCM(self.send_key)
        self._recv_aead = AESGCM(self.recv_key)

    @classmethod
    def for_client(cls, keys: SessionKeys) -> SecureCodec:
        return cls(send_key=keys.client_to_server, recv_key=keys.server_to_client)

    @classmethod
    def for_server(cls, keys: SessionKeys) -> SecureCodec:
        return cls(send_key=keys.server_to_client, recv_key=keys.client_to_server)

    @staticmethod
    def _nonce(counter: int) -> bytes:
        return b"\x00\x00\x00\x00" + _COUNTER.pack(counter)

    def seal(self, msg_type: int, stream_id: int, payload: bytes) -> bytes:
        if self._send_counter > _MAX_COUNTER:
            raise HandshakeError("send counter exhausted; re-handshake required")
        counter = self._send_counter
        self._send_counter += 1
        plaintext = struct.pack(">BI", msg_type, stream_id) + payload
        counter_bytes = _COUNTER.pack(counter)
        # AAD binds the frame to its position in the sequence, so an attacker
        # cannot renumber frames to defeat the replay window below.
        ciphertext = self._send_aead.encrypt(
            self._nonce(counter), plaintext, counter_bytes
        )
        return encode_envelope(KIND_SECURE, counter_bytes + ciphertext)

    def unseal(self, env: Envelope) -> tuple[int, int, bytes]:
        if env.kind != KIND_SECURE:
            raise HandshakeError(
                f"expected SECURE envelope, got kind {env.kind:#04x} "
                "(plaintext during an established session is refused)"
            )
        if len(env.body) < 8 + 16:
            raise HandshakeError("secure envelope too short")
        counter_bytes = env.body[:8]
        (counter,) = _COUNTER.unpack(counter_bytes)
        if counter > _MAX_COUNTER:
            raise ReplayError("counter out of range")
        try:
            plaintext = self._recv_aead.decrypt(
                self._nonce(counter), env.body[8:], counter_bytes
            )
        except Exception as exc:  # noqa: BLE001 - InvalidTag and friends
            raise HandshakeError(f"frame failed authentication: {exc}") from exc
        if counter <= self._recv_counter:
            raise ReplayError(
                f"replayed frame: counter {counter} <= {self._recv_counter}"
            )
        self._recv_counter = counter
        if len(plaintext) < 5:
            raise FramingError("decrypted frame too short")
        msg_type, stream_id = struct.unpack(">BI", plaintext[:5])
        return msg_type, stream_id, plaintext[5:]

    @property
    def sent_frames(self) -> int:
        return self._send_counter


def sas_matches(local: str, remote: str) -> bool:
    """Constant-time-ish comparison of two 6-digit SAS codes."""
    a = local.replace(" ", "")
    b = remote.replace(" ", "")
    return hmac.compare_digest(a, b)


def pairing_record(
    *, peer_id: str, peer_name: str, peer_static_raw: bytes, peer_kind: str
) -> dict[str, Any]:
    return {
        "peer_id": peer_id,
        "peer_name": peer_name,
        "static_key": b64e(peer_static_raw),
        "role": peer_kind,
        "paired_at": None,  # filled in by the store
    }
