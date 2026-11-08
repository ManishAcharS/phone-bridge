"""PhoneBridge wire framing (protocol v1).

One codec, two transports
-------------------------
Every PhoneBridge message travels inside an *envelope*::

    +----------------+----------------+------------------+
    | u32 length (BE)| u8 kind        | body[length - 1] |
    +----------------+----------------+------------------+

``length`` counts the kind byte plus the body, so an empty body is still a
5-byte envelope.  The same envelope is used for:

* WebSocket binary messages (one envelope per WS message), and
* raw TCP streams (a byte-stream parser reassembles envelopes).

Keeping one codec means the Phase-1 WebSocket transport can be swapped for a
raw TCP/UDP media transport later without touching the message layer.

Kinds
-----
0x01 HANDSHAKE  body = UTF-8 JSON, **plaintext**, only legal before the secure
                channel is established.
0x02 SECURE     body = u64 counter (BE) | AES-256-GCM ciphertext+tag.
                The counter is transmitted so the receiver can rebuild the
                nonce; a replayed/looped frame is rejected by monotonic
                counter checks inside :mod:`phonebridge.protocol.crypto`.
0x03 CLOSE      body = UTF-8 reason. Polite shutdown; safe to drop.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1

KIND_HANDSHAKE = 0x01
KIND_SECURE = 0x02
KIND_CLOSE = 0x03

#: Hard ceiling on a single envelope.  Media frames are 20-60 ms of audio
#: (a few hundred bytes) or a video keyframe (< 1 MiB), so 8 MiB is generous
#: while still refusing a hostile length prefix outright.
MAX_ENVELOPE_BODY = 8 * 1024 * 1024

_HEADER = struct.Struct(">I")  # u32 body length (includes kind byte)


class FramingError(Exception):
    """Raised when a received envelope cannot be parsed."""


@dataclass(frozen=True)
class Envelope:
    kind: int
    body: bytes

    def as_json(self) -> dict[str, Any]:
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FramingError(f"HANDSHAKE body is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise FramingError("HANDSHAKE body must be a JSON object")
        return data


def encode_envelope(kind: int, body: bytes) -> bytes:
    """Serialise one envelope."""
    if not 0 <= kind <= 0xFF:
        raise FramingError(f"kind out of range: {kind}")
    if len(body) + 1 > MAX_ENVELOPE_BODY:
        raise FramingError(
            f"envelope body too large: {len(body) + 1} > {MAX_ENVELOPE_BODY}"
        )
    return _HEADER.pack(len(body) + 1) + bytes((kind,)) + body


def encode_json_envelope(kind: int, payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return encode_envelope(kind, body)


def decode_envelope(data: bytes) -> Envelope:
    """Parse exactly one complete envelope (the WebSocket path)."""
    if len(data) < 5:
        raise FramingError(f"envelope too short: {len(data)} bytes")
    (body_len,) = _HEADER.unpack_from(data, 0)
    if body_len < 1:
        raise FramingError("envelope length must be >= 1")
    if body_len > MAX_ENVELOPE_BODY:
        raise FramingError(f"envelope length {body_len} exceeds limit")
    if len(data) != 4 + body_len:
        raise FramingError(
            f"envelope length mismatch: header says {body_len}, got {len(data) - 4}"
        )
    return Envelope(kind=data[4], body=data[5:])


class EnvelopeStreamParser:
    """Reassembles envelopes from a raw TCP byte stream.

    Not used by the WebSocket transport (which preserves message boundaries),
    but present now so the media transport can share the framing code.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> Iterator[Envelope]:
        self._buf.extend(chunk)
        while True:
            if len(self._buf) < 4:
                return
            (body_len,) = _HEADER.unpack_from(self._buf, 0)
            if body_len < 1 or body_len > MAX_ENVELOPE_BODY:
                raise FramingError(f"invalid stream envelope length {body_len}")
            total = 4 + body_len
            if len(self._buf) < total:
                return
            frame = bytes(self._buf[:total])
            del self._buf[:total]
            yield decode_envelope(frame)
