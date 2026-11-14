"""A bounded jitter buffer, and the honest accounting of what it threw away.

The network delivers microphone frames in bursts; a sound card consumes them at a
fixed rate and cannot be told to wait. Something has to sit between the two, and
that something decides the whole feel of the call:

* Too little buffering and every Wi-Fi hiccup is a click.
* Too much and the person you are talking to hears themselves a second late.
* Unbounded and a slow link becomes an ever-growing delay that never recovers.

This buffer is bounded in both directions. It fills to a target depth before the
first frame is released, drops the **oldest** frame when it overruns (a late frame
is worthless — the caller wants the newest), and reports an underrun so the
writer can emit silence instead of stalling the device.

Sizing is in milliseconds, not frames, because the frame length is negotiated and
a buffer expressed in frames silently changes size when it does.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class JitterStats:
    """Counters a user (or a test) can read without touching the buffer."""

    received: int = 0
    delivered: int = 0
    dropped_late: int = 0
    underruns: int = 0
    last_depth_ms: int = 0
    #: Total milliseconds of silence the writer had to invent for underruns.
    concealment_ms: int = 0
    _history: deque[int] = field(default_factory=lambda: deque(maxlen=64), repr=False)

    def to_dict(self) -> dict[str, int]:
        return {
            "received": self.received,
            "delivered": self.delivered,
            "dropped_late": self.dropped_late,
            "underruns": self.underruns,
            "depth_ms": self.last_depth_ms,
            "concealment_ms": self.concealment_ms,
        }


class JitterBuffer:
    """Thread-safe, bounded, milliseconds-sized frame buffer.

    ``push`` is called from the network side and never blocks. ``pop`` is called
    from the device side and waits at most ``timeout`` seconds for a frame,
    returning ``None`` when there is nothing — the caller writes silence. That
    asymmetry is deliberate: a blocking network read would make an underrun look
    like a hung device.
    """

    def __init__(
        self,
        *,
        frame_ms: int,
        target_ms: int,
        clock: Callable[[], float] = time.monotonic,
        capacity_frames: int | None = None,
    ) -> None:
        if frame_ms <= 0:
            raise ValueError("frame_ms must be positive")
        if target_ms < frame_ms:
            raise ValueError(f"target {target_ms} ms is shorter than one {frame_ms} ms frame")
        self._frame_ms = frame_ms
        self._target_frames = max(1, round(target_ms / frame_ms))
        # Twice the target is the hard ceiling: beyond that the buffer is holding
        # latency the user did not ask for, so the oldest frame goes.
        self._capacity = capacity_frames or self._target_frames * 2
        self._clock = clock
        self._frames: deque[bytes] = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._primed = False
        self.stats = JitterStats()

    # ------------------------------------------------------------------ #
    # writer side (network)
    # ------------------------------------------------------------------ #
    def push(self, payload: bytes) -> None:
        """Accept one frame. Drops the oldest frame if that would overrun."""
        if not payload:
            return
        with self._condition:
            if self._closed:
                return
            self.stats.received += 1
            if len(self._frames) >= self._capacity:
                self._frames.popleft()
                self.stats.dropped_late += 1
            self._frames.append(payload)
            if not self._primed and len(self._frames) >= self._target_frames:
                # Prefilled: the device may start draining now.
                self._primed = True
            self._record_depth_locked()
            self._condition.notify()

    # ------------------------------------------------------------------ #
    # device side
    # ------------------------------------------------------------------ #
    def pop(self, timeout: float | None = None) -> bytes | None:
        """Return the next frame, or ``None`` if none arrived in time."""
        deadline = None if timeout is None else self._clock() + timeout
        with self._condition:
            while True:
                if self._closed:
                    return None
                if self._frames and self._primed:
                    payload = self._frames.popleft()
                    self.stats.delivered += 1
                    self._record_depth_locked()
                    return payload
                if deadline is None:
                    self._condition.wait(timeout=None)
                    continue
                remaining = deadline - self._clock()
                if remaining <= 0:
                    self.stats.underruns += 1
                    self.stats.concealment_ms += self._frame_ms
                    self._primed = False
                    return None
                self._condition.wait(timeout=remaining)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._frames.clear()
            self._condition.notify_all()

    # ------------------------------------------------------------------ #
    # observation
    # ------------------------------------------------------------------ #
    @property
    def depth_ms(self) -> int:
        with self._condition:
            return len(self._frames) * self._frame_ms

    @property
    def target_ms(self) -> int:
        return self._target_frames * self._frame_ms

    def _record_depth_locked(self) -> None:
        self.stats.last_depth_ms = len(self._frames) * self._frame_ms
        self.stats._history.append(self.stats.last_depth_ms)
