"""Audio frames: the 8-byte header that rides inside ``MSG_AUDIO`` (0x30).

One frame carries 20 ms of mono 16-bit PCM by default (960 samples, 1920 bytes),
so a frame is small enough that losing one is a click rather than a gap, and
large enough that the per-frame envelope overhead stays under 1%.

Wire format (protocol v1, additive — this kind had no implementation before)::

    u8   codec     1 = PCM16LE
    u8   flags     bit0 START, bit1 STOP, bit2 MUTED
    u16  seq       wrapping frame counter, 0..65535
    u32  ts_ms    sender's monotonic clock, ms since the stream started
    ...  payload  codec bytes

A receiver can therefore drop a late frame, a duplicate, or a whole out-of-order
burst without decoding it, and can tell silence (a muted stream still sends
frames) from a stall (no frames at all).

Validation is strict and bounded: an unknown codec, a truncated header, a payload
that is not a whole number of samples, or a body over :data:`MAX_AUDIO_BODY`
raises :class:`AudioFrameError`. Nothing here decodes audio — that is the phone's
job — so a hostile frame costs a parse and an exception, never an allocation.
"""

from __future__ import annotations

import struct
from collections import deque
from dataclasses import dataclass, field
from typing import Any, cast

#: Largest audio body accepted: 512 KiB is 2.7 s of 48 kHz stereo PCM, far more
#: than any legal frame (20 ms). A bigger body is a bug or an attack.
MAX_AUDIO_BODY = 512 * 1024

CODEC_PCM16 = 1

FLAG_START = 0x01
FLAG_STOP = 0x02
FLAG_MUTED = 0x04

#: Fixed header length: codec, flags, seq, timestamp.
HEADER = struct.Struct(">BBHI")
HEADER_SIZE = HEADER.size  # 8

BYTES_PER_SAMPLE = 2  # PCM16LE


class AudioFrameError(Exception):
    """A frame that cannot be trusted: truncated, over-long, or impossible."""


@dataclass(frozen=True)
class AudioFormat:
    """The negotiated shape of a stream.

    Defaults are deliberately conservative: 48 kHz mono is what Android captures
    and plays natively, and 20 ms is short enough for conversational latency while
    still amortising the WebSocket/envelope overhead.
    """

    sample_rate: int = 48_000
    channels: int = 1
    frame_ms: int = 20
    codec: int = CODEC_PCM16

    def __post_init__(self) -> None:
        if not 8_000 <= self.sample_rate <= 48_000:
            raise ValueError(f"sample rate out of range: {self.sample_rate}")
        if not 1 <= self.channels <= 2:
            raise ValueError(f"channels out of range: {self.channels}")
        if not 5 <= self.frame_ms <= 100:
            raise ValueError(f"frame length out of range: {self.frame_ms} ms")
        if self.codec != CODEC_PCM16:
            raise ValueError(f"unsupported codec: {self.codec}")

    @property
    def samples_per_frame(self) -> int:
        return self.sample_rate * self.frame_ms // 1000

    @property
    def bytes_per_frame(self) -> int:
        return self.samples_per_frame * self.channels * BYTES_PER_SAMPLE

    def as_dict(self) -> dict[str, int]:
        return {
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frame_ms": self.frame_ms,
            "codec": self.codec,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AudioFormat:
        """Parse a negotiated format, refusing nonsense instead of guessing."""
        if not data:
            return cls()
        try:
            return cls(
                sample_rate=int(cast(Any, data.get("sample_rate", 48_000))),
                channels=int(cast(Any, data.get("channels", 1))),
                frame_ms=int(cast(Any, data.get("frame_ms", 20))),
                codec=int(cast(Any, data.get("codec", CODEC_PCM16))),
            )
        except (TypeError, ValueError) as exc:
            raise AudioFrameError(f"bad audio format: {exc}") from exc


@dataclass(frozen=True)
class AudioFrame:
    """One decoded frame header plus its payload."""

    seq: int
    timestamp_ms: int
    payload: bytes
    codec: int = CODEC_PCM16
    flags: int = 0

    @property
    def start(self) -> bool:
        return bool(self.flags & FLAG_START)

    @property
    def stop(self) -> bool:
        return bool(self.flags & FLAG_STOP)

    @property
    def muted(self) -> bool:
        return bool(self.flags & FLAG_MUTED)

    @property
    def samples(self) -> int:
        return len(self.payload) // BYTES_PER_SAMPLE

    def encode(self) -> bytes:
        if len(self.payload) > MAX_AUDIO_BODY:
            raise AudioFrameError(f"audio frame too large: {len(self.payload)} bytes")
        if not 0 <= self.seq <= 0xFFFF:
            raise AudioFrameError(f"sequence out of range: {self.seq}")
        if self.timestamp_ms < 0 or self.timestamp_ms > 0xFFFFFFFF:
            raise AudioFrameError(f"timestamp out of range: {self.timestamp_ms}")
        return HEADER.pack(self.codec, self.flags, self.seq, self.timestamp_ms) + self.payload


def decode_frame(body: bytes) -> AudioFrame:
    """Parse one ``MSG_AUDIO`` payload, strictly."""
    if len(body) < HEADER_SIZE:
        raise AudioFrameError(f"audio frame shorter than its header: {len(body)} bytes")
    if len(body) > MAX_AUDIO_BODY + HEADER_SIZE:
        raise AudioFrameError(f"audio frame too large: {len(body)} bytes")
    codec, flags, seq, timestamp_ms = HEADER.unpack_from(body, 0)
    if codec != CODEC_PCM16:
        raise AudioFrameError(f"unknown audio codec {codec}")
    payload = body[HEADER_SIZE:]
    if len(payload) % BYTES_PER_SAMPLE:
        raise AudioFrameError(f"payload is not whole 16-bit samples: {len(payload)} bytes")
    return AudioFrame(
        seq=seq, timestamp_ms=timestamp_ms, payload=payload, codec=codec, flags=flags
    )


# --------------------------------------------------------------------------- #
# sequencing
# --------------------------------------------------------------------------- #
@dataclass
class AudioStats:
    """Counters a user can see, and a test can assert."""

    sent: int = 0
    received: int = 0
    dropped_full: int = 0
    dropped_late: int = 0
    duplicates: int = 0
    reordered: int = 0
    invalid: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    last_timestamp_ms: int = 0
    last_error: str = ""

    def to_dict(self) -> dict[str, int | str]:
        return {
            "sent": self.sent,
            "received": self.received,
            "dropped_full": self.dropped_full,
            "dropped_late": self.dropped_late,
            "duplicates": self.duplicates,
            "reordered": self.reordered,
            "invalid": self.invalid,
            "bytes_sent": self.bytes_sent,
            "bytes_received": self.bytes_received,
            "last_timestamp_ms": self.last_timestamp_ms,
            "last_error": self.last_error,
        }


@dataclass
class SequenceGate:
    """Accept, reorder or reject frames, tolerating a bounded amount of jitter.

    A real network delivers frames out of order after a hiccup. Dropping every
    late arrival produces a permanent hole, so a small reorder window is held;
    anything older than the window is *late* and dropped, anything already seen is
    a *duplicate* and dropped. This is the whole of the receive-side policy, which
    is why it is a separate testable object rather than a branch in a callback.
    """

    window: int = 8
    _next: int | None = None
    _pending: dict[int, AudioFrame] = field(default_factory=dict)
    #: Recently delivered sequences, so "late" and "duplicate" stay distinct facts
    #: instead of one bucket the user cannot act on.
    _seen: deque[int] = field(default_factory=lambda: deque(maxlen=64))
    duplicates: int = 0
    late: int = 0
    reordered: int = 0

    def reset(self, seq: int = 0) -> None:
        self._next = seq & 0xFFFF
        self._pending.clear()
        self._seen.clear()

    def _deliver(self, frame: AudioFrame, out: list[AudioFrame]) -> None:
        out.append(frame)
        self._seen.append(frame.seq)
        self._next = (frame.seq + 1) & 0xFFFF
        while self._next in self._pending:
            nxt = self._pending.pop(self._next)
            out.append(nxt)
            self._seen.append(nxt.seq)
            self._next = (nxt.seq + 1) & 0xFFFF

    def offer(self, frame: AudioFrame) -> list[AudioFrame]:
        """Return the frames now in order (possibly none, if this one was early)."""
        if self._next is None:
            self._next = frame.seq
        distance = (frame.seq - self._next) & 0xFFFF
        if distance == 0:
            out: list[AudioFrame] = []
            self._deliver(frame, out)
            return out
        if distance <= self.window:
            # Ahead of the play position but within the window: hold it.
            if frame.seq in self._pending:
                self.duplicates += 1
                return []
            self._pending[frame.seq] = frame
            self.reordered += 1
            return []
        # Behind the play position: a duplicate we already delivered, or a frame
        # so old that playing it would only add latency.
        if frame.seq in self._seen:
            self.duplicates += 1
            return []
        if distance > 0x8000:
            self.late += 1
            return []
        # Far ahead: the sender jumped (stream restart). Resynchronise rather than
        # waiting forever for frames that were never sent.
        self._pending.clear()
        self.reordered += 1
        out = []
        self._deliver(frame, out)
        return out
