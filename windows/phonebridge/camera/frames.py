"""Camera frames: the header that rides inside ``MSG_VIDEO`` (0x40).

One message is one complete JPEG — no fragmenting, no re-assembly, so a lost
message is a lost frame and never a corrupt picture. That is the whole reason a
still-image codec is used for the first implementation: at 720p and ~15 fps a
frame is 100-250 KB, which fits comfortably inside one encrypted envelope, and
JPEG needs no codec state to survive a reconnect.

Wire format (protocol v1, additive — this kind had no implementation before)::

    u8   codec      1 = JPEG
    u8   flags      bit0 START, bit1 STOP, bit2 KEY (a fresh full frame)
    u16  seq        wrapping frame counter
    u32  ts_ms      sender's monotonic ms since the stream started
    u16  width      pixels, as encoded
    u16  height     pixels, as encoded
    u16  rotation   degrees the *receiver* must rotate before display (0/90/180/270)
    ...  payload    JPEG bytes, starting FF D8 and ending FF D9

Validation is strict: a body that is too short, uses an unknown codec, is not a
JPEG, claims an impossible size, or exceeds :data:`MAX_VIDEO_BODY` raises
:class:`VideoFrameError`. A bad frame is counted and dropped by the stream, never
crashed on — the next frame is 66 ms away.
"""

from __future__ import annotations

import struct
from collections import deque
from dataclasses import dataclass, field
from typing import Any, cast

#: 2 MiB: a 1080p JPEG at high quality is ~600 KB, so this is generous while
#: still refusing a hostile length prefix (the envelope ceiling is 8 MiB).
MAX_VIDEO_BODY = 2 * 1024 * 1024

CODEC_JPEG = 1

FLAG_START = 0x01
FLAG_STOP = 0x02
FLAG_KEY = 0x04

#: codec, flags, seq, ts_ms, width, height, rotation — 14 bytes. The rotation is
#: a u16 because 270 does not fit in a byte and packing it as quarter-turns would
#: need a translation table at both ends for no gain.
HEADER = struct.Struct(">BBHIHHH")
HEADER_SIZE = HEADER.size  # 14

VALID_ROTATIONS = (0, 90, 180, 270)

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


class VideoFrameError(Exception):
    """A camera frame that cannot be trusted."""


def _as_bool(value: Any, fallback: bool) -> bool:
    """A JSON boolean, or the same thing a phone might send instead of one."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return fallback


@dataclass(frozen=True)
class VideoConfig:
    """The negotiated shape of a stream.

    Defaults are deliberately modest: 1280x720 at 15 fps is a usable preview on
    ordinary Wi-Fi and at hotspot distances, and a phone can encode it without
    heating up. 1080p is available but never the default.
    """

    width: int = 1280
    height: int = 720
    fps: int = 15
    quality: int = 70
    facing: str = "back"
    #: Ask the phone to pin auto-exposure to exactly :attr:`fps`
    #: (``CONTROL_AE_TARGET_FPS_RANGE`` = (fps, fps)) instead of letting it slow
    #: down in low light. The frame rate becomes predictable and the picture
    #: darker; `--fps-mode variable` is the other way round.
    fixed_fps: bool = True

    def __post_init__(self) -> None:
        if not 320 <= self.width <= 3840 or not 240 <= self.height <= 2160:
            raise ValueError(f"resolution out of range: {self.width}x{self.height}")
        if not 1 <= self.fps <= 60:
            raise ValueError(f"frame rate out of range: {self.fps}")
        if not 20 <= self.quality <= 95:
            raise ValueError(f"JPEG quality out of range: {self.quality}")
        if self.facing not in ("back", "front"):
            raise ValueError(f"unknown camera facing: {self.facing}")
        if not isinstance(self.fixed_fps, bool):
            raise ValueError(f"fixed_fps must be a boolean, got {self.fixed_fps!r}")

    @property
    def label(self) -> str:
        names = {"back": "rear", "front": "front"}
        return f"{names[self.facing]} {self.width}x{self.height} @ {self.fps} fps"

    def as_dict(self) -> dict[str, object]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "quality": self.quality,
            "facing": self.facing,
            "fixed_fps": self.fixed_fps,
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, Any] | None, *, fallback: VideoConfig | None = None
    ) -> VideoConfig:
        """Parse a negotiated config, refusing nonsense instead of guessing."""
        base = fallback or cls()
        if not data:
            return base
        try:
            return cls(
                width=int(cast(Any, data.get("width", base.width))),
                height=int(cast(Any, data.get("height", base.height))),
                fps=int(cast(Any, data.get("fps", base.fps))),
                quality=int(cast(Any, data.get("quality", base.quality))),
                facing=str(data.get("facing", base.facing)),
                fixed_fps=_as_bool(data.get("fixed_fps"), base.fixed_fps),
            )
        except (TypeError, ValueError) as exc:
            raise VideoFrameError(f"bad camera config: {exc}") from exc


@dataclass(frozen=True)
class VideoFrame:
    """One decoded header plus its JPEG payload."""

    seq: int
    timestamp_ms: int
    payload: bytes
    width: int = 0
    height: int = 0
    rotation: int = 0
    codec: int = CODEC_JPEG
    flags: int = 0

    @property
    def start(self) -> bool:
        return bool(self.flags & FLAG_START)

    @property
    def stop(self) -> bool:
        return bool(self.flags & FLAG_STOP)

    @property
    def key(self) -> bool:
        return bool(self.flags & FLAG_KEY)

    @property
    def size(self) -> int:
        return len(self.payload)

    def encode(self) -> bytes:
        if len(self.payload) > MAX_VIDEO_BODY:
            raise VideoFrameError(f"video frame too large: {len(self.payload)} bytes")
        if not 0 <= self.seq <= 0xFFFF:
            raise VideoFrameError(f"sequence out of range: {self.seq}")
        if not 0 <= self.timestamp_ms <= 0xFFFFFFFF:
            raise VideoFrameError(f"timestamp out of range: {self.timestamp_ms}")
        if not 0 <= self.width <= 0xFFFF or not 0 <= self.height <= 0xFFFF:
            raise VideoFrameError(f"resolution does not fit the header: {self.width}")
        if self.rotation not in VALID_ROTATIONS:
            raise VideoFrameError(f"rotation must be one of {VALID_ROTATIONS}")
        return (
            HEADER.pack(
                self.codec,
                self.flags,
                self.seq,
                self.timestamp_ms,
                self.width,
                self.height,
                self.rotation,
            )
            + self.payload
        )


def decode_frame(body: bytes) -> VideoFrame:
    """Parse one ``MSG_VIDEO`` payload, strictly."""
    if len(body) < HEADER_SIZE:
        raise VideoFrameError(f"video frame shorter than its header: {len(body)} bytes")
    if len(body) > MAX_VIDEO_BODY + HEADER_SIZE:
        raise VideoFrameError(f"video frame too large: {len(body)} bytes")
    codec, flags, seq, ts, width, height, rotation = HEADER.unpack_from(body, 0)
    if codec != CODEC_JPEG:
        raise VideoFrameError(f"unknown video codec {codec}")
    if not 16 <= width <= 8192 or not 16 <= height <= 8192:
        raise VideoFrameError(f"impossible frame size: {width}x{height}")
    if rotation not in VALID_ROTATIONS:
        raise VideoFrameError(f"invalid rotation {rotation}")
    payload = body[HEADER_SIZE:]
    if payload and not payload.startswith(JPEG_SOI):
        raise VideoFrameError("payload is not a JPEG (missing SOI)")
    if payload and not payload.endswith(JPEG_EOI):
        raise VideoFrameError("payload is not a complete JPEG (missing EOI)")
    return VideoFrame(
        seq=seq,
        timestamp_ms=ts,
        payload=payload,
        width=width,
        height=height,
        rotation=rotation,
        codec=codec,
        flags=flags,
    )


@dataclass
class VideoStats:
    """Counters the window shows and the tests assert."""

    received: int = 0
    decoded: int = 0
    dropped: int = 0
    duplicates: int = 0
    late: int = 0
    reordered: int = 0
    invalid: int = 0
    bytes_received: int = 0
    last_seq: int = 0
    last_timestamp_ms: int = 0
    #: What the phone said the *last* frame needed: 0/90/180/270. Shown in the
    #: window so a sideways picture can be diagnosed from the number rather than
    #: guessed at, and so a ROM reporting nonsense is visible immediately.
    last_rotation: int = 0
    last_error: str = ""

    def to_dict(self) -> dict[str, int | str]:
        return {
            "received": self.received,
            "decoded": self.decoded,
            "dropped": self.dropped,
            "duplicates": self.duplicates,
            "late": self.late,
            "reordered": self.reordered,
            "invalid": self.invalid,
            "bytes_received": self.bytes_received,
            "last_seq": self.last_seq,
            "last_timestamp_ms": self.last_timestamp_ms,
            "last_rotation": self.last_rotation,
            "last_error": self.last_error,
        }


@dataclass
class FrameGate:
    """Accept, hold or reject frames, so the preview never jitters backwards.

    Same policy as the audio sequence gate, typed for video and with one extra
    rule: video has no interest in a backlog. A frame that arrives more than
    ``window`` behind is dropped rather than played late, because showing stale
    pictures is worse than showing fewer of them.
    """

    window: int = 4
    _next: int | None = None
    _pending: dict[int, VideoFrame] = field(default_factory=dict)
    _seen: deque[int] = field(default_factory=lambda: deque(maxlen=32))
    duplicates: int = 0
    late: int = 0
    reordered: int = 0

    def reset(self, seq: int = 0) -> None:
        self._next = seq & 0xFFFF
        self._pending.clear()
        self._seen.clear()

    def _deliver(self, frame: VideoFrame, out: list[VideoFrame]) -> None:
        out.append(frame)
        self._seen.append(frame.seq)
        self._next = (frame.seq + 1) & 0xFFFF
        while self._next in self._pending:
            nxt = self._pending.pop(self._next)
            out.append(nxt)
            self._seen.append(nxt.seq)
            self._next = (nxt.seq + 1) & 0xFFFF

    def offer(self, frame: VideoFrame) -> list[VideoFrame]:
        """Return the frames now in order (possibly none)."""
        if self._next is None:
            self._next = frame.seq
        distance = (frame.seq - self._next) & 0xFFFF
        if distance == 0:
            out: list[VideoFrame] = []
            self._deliver(frame, out)
            return out
        if distance <= self.window:
            if frame.seq in self._pending:
                self.duplicates += 1
                return []
            self._pending[frame.seq] = frame
            self.reordered += 1
            return []
        if frame.seq in self._seen:
            self.duplicates += 1
            return []
        if distance > 0x8000:
            self.late += 1
            return []
        # Far ahead: the phone restarted its stream or we lost a burst. Show the
        # new frame immediately rather than waiting for pictures that never come.
        self._pending.clear()
        self.reordered += 1
        out = []
        self._deliver(frame, out)
        return out
