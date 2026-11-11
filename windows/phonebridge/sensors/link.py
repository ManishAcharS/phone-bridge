"""The sensor stream: one bounded queue, newest reading per sensor, nothing else.

Mirrors the camera plane deliberately, because the failure modes are the same and
one lesson was already learned there:

* **Bounded.** Readings are held in a queue of at most :data:`~.frames.QUEUE_FRAMES`
  messages and the *oldest* goes when the UI falls behind. A 100 Hz accelerometer
  must never build a backlog behind a 10 Hz screen refresh.
* **Newest per sensor.** The GUI shows one row per sensor. Keeping history would
  turn a live readout into a recorder, which this phase is not.
* **Its own stream id** (4), so a burst of sensor data cannot delay audio, video
  or a control request, and stopping sensors cannot disturb any of them.

Like the audio and camera planes, this object never opens a socket: frames arrive
on the session the connection manager authenticated, and when that session ends
the stream ends with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .frames import (
    QUEUE_FRAMES,
    SENSOR_STREAM,
    SensorFrame,
    SensorFrameError,
    SensorReading,
    SensorRegistry,
    SensorStats,
    decode_frame,
)

log = logging.getLogger(__name__)

IDLE = "idle"
STARTING = "starting"
ACTIVE = "active"
STOPPING = "stopping"
ERROR = "error"

#: Sampling intervals the GUI offers, in microseconds. Android's
#: ``SensorManager`` constants are 0/20 000/66 667/200 000; 5 000 is offered for
#: the accelerometer on phones that advertise it, and refused by the phone when a
#: sensor cannot go that fast.
INTERVAL_CHOICES_US: tuple[tuple[str, int], ...] = (
    ("200 Hz (5 ms)", 5000),
    ("100 Hz (10 ms)", 10000),
    ("50 Hz (20 ms)", 20000),
    ("15 Hz (66 ms)", 66667),
    ("5 Hz (200 ms)", 200000),
    ("1 Hz (1 s)", 1000000),
)

DEFAULT_INTERVAL_US = 20000


class SensorError(RuntimeError):
    """A sensor operation the user needs to hear about, in words."""


@dataclass
class SensorSnapshot:
    """What the GUI draws: immutable, and never the live objects."""

    state: str = IDLE
    detail: str = ""
    registry: SensorRegistry | None = None
    stats: dict[str, Any] | None = None
    #: Frames the phone sent that could not be decoded. Kept on the snapshot so a
    #: user can see a broken stream without a fabricated "sensor 0" row.
    invalid_frames: int = 0

    def to_dict(self) -> dict[str, Any]:
        registry = self.registry or SensorRegistry()
        return {
            "state": self.state,
            "detail": self.detail,
            "registry": registry.to_dict(),
            "stats": dict(self.stats or {}),
            "invalid_frames": self.invalid_frames,
        }


class SensorLink:
    """List, start, stop and read the phone's sensors."""

    def __init__(
        self,
        session: Any,
        *,
        interval_us: int = DEFAULT_INTERVAL_US,
        on_change: Callable[[SensorSnapshot], None] | None = None,
        queue_frames: int = QUEUE_FRAMES,
    ) -> None:
        self._session = session
        self._on_change = on_change
        self._interval_us = max(1000, int(interval_us))
        self._queue_frames = max(4, queue_frames)
        self._registry = SensorRegistry(interval_us=self._interval_us)
        self._state = IDLE
        self._detail = ""
        self._queue: asyncio.Queue[bytes | None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_seen: dict[int, float] = {}
        #: Frames the phone sent that could not be decoded. Counted here so no
        #: fabricated sensor row appears in the GUI or the JSON output.
        self._invalid_frames = 0
        #: Guards the registry tables against the GUI thread, which polls
        #: `snapshot()` while the frame pump inserts a newly-seen sensor id.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # observation
    # ------------------------------------------------------------------ #
    @property
    def state(self) -> str:
        return self._state

    @property
    def interval_us(self) -> int:
        return self._interval_us

    @property
    def registry(self) -> SensorRegistry:
        return self._registry

    def snapshot(self) -> SensorSnapshot:
        # Every sensor the phone listed, plus any id that produced a frame or a
        # rejected one (a frame for an unknown id is still worth counting).
        #
        # Keys are strings: the snapshot is what the GUI and the JSON output read,
        # and a dict with int keys cannot be serialised as-is.
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> SensorSnapshot:
        stats: dict[str, Any] = {
            str(info.id): self._registry.stats.get(info.id, SensorStats()).to_dict()
            for info in self._registry.sensors
        }
        for sensor_id, entry in self._registry.stats.items():
            stats.setdefault(str(sensor_id), entry.to_dict())
        return SensorSnapshot(
            state=self._state,
            detail=self._detail,
            registry=self._registry,
            stats=stats,
            invalid_frames=self._invalid_frames,
        )

    def reading(self, sensor_id: int) -> SensorReading | None:
        return self._registry.readings.get(sensor_id)

    # ------------------------------------------------------------------ #
    # configuration
    # ------------------------------------------------------------------ #
    def set_interval(self, interval_us: int) -> int:
        """Remember a sampling interval. Applies on the next start."""
        self._interval_us = max(1000, min(10_000_000, int(interval_us)))
        self._registry.interval_us = self._interval_us
        self._notify()
        return self._interval_us

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def refresh(self) -> SensorRegistry:
        """Ask the phone what it has. Safe to call while streaming."""
        if not getattr(self._session, "is_open", False):
            self._set(ERROR, "not connected: the sensor list needs a live session")
            return self._registry
        try:
            reply = await self._session.call(
                "sensor.list", {"interval_us": self._interval_us}
            )
        except Exception as exc:  # noqa: BLE001 - every failure is a message
            self._set(ERROR, _explain("the phone would not list its sensors", exc))
            return self._registry
        self._registry = SensorRegistry.from_list_reply(reply, interval_us=self._interval_us)
        if not self._registry.sensors:
            self._set(IDLE, "the phone reports no sensors")
        elif self._state not in (ACTIVE, STARTING):
            self._set(IDLE, f"{len(self._registry.sensors)} sensors reported")
        else:
            self._notify()
        return self._registry

    async def start(self, sensor_ids: list[int] | tuple[int, ...] | None = None) -> bool:
        """Start streaming the chosen sensors (all of them when none is named)."""
        if self._state in (ACTIVE, STARTING):
            return self._state == ACTIVE
        if not getattr(self._session, "is_open", False):
            self._set(ERROR, "not connected: sensor streaming needs a live session")
            return False
        if not self._registry.sensors:
            await self.refresh()
        ids = [int(i) for i in (sensor_ids or [s.id for s in self._registry.sensors])]
        if not ids:
            self._set(ERROR, "there are no sensors to start")
            return False
        unknown = [i for i in ids if self._registry.by_id(i) is None]
        if unknown:
            self._set(ERROR, f"the phone does not have sensor(s) {unknown}")
            return False

        self._set(STARTING)
        # Subscribe *before* asking: the phone registers its listeners inside
        # `sensor.start` and starts sending immediately, so a queue created after
        # the reply would drop the first readings (the file plane learned this
        # the hard way with its acks).
        self._queue, self._task = await self._open_stream()
        try:
            reply = await self._session.call(
                "sensor.start", {"ids": ids, "interval_us": self._interval_us}
            )
        except Exception as exc:  # noqa: BLE001
            await self._stop_stream()
            self._set(ERROR, _explain("the phone refused to start sensors", exc))
            await self._quiet_rpc("sensor.stop")
            return False

        started = _started_ids(reply, ids)
        applied = _applied_interval(reply, self._interval_us)
        self._interval_us = applied
        self._registry.interval_us = applied
        self._registry.active = tuple(started)
        self._registry.stats = {i: SensorStats() for i in started}
        self._registry.readings = {}
        self._last_seen.clear()
        self._set(ACTIVE, "")
        return True

    async def _stop_stream(self) -> None:
        """Drop the frame queue and its reader without touching the phone."""
        task, self._task = self._task, None
        self._queue = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def stop(self, reason: str = "stopped") -> None:
        """Stop every stream on the phone and close the local side."""
        task, self._task = self._task, None
        self._queue = None
        if self._state in (ACTIVE, STARTING) and getattr(self._session, "is_open", False):
            self._state = STOPPING
            await self._quiet_rpc("sensor.stop")
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._registry.active = ()
        for stats in self._registry.stats.values():
            stats.last_error = ""
        self._set(IDLE, reason)

    async def close(self, reason: str = "session closed") -> None:
        with contextlib.suppress(Exception):
            await self.stop(reason)

    # ------------------------------------------------------------------ #
    # receiving
    # ------------------------------------------------------------------ #
    def push(self, body: bytes | None) -> bool:
        """Feed one message (tests, and any transport that is not a session queue).

        Returns ``True`` when the queue was full and the oldest message was
        discarded, which is the documented behaviour under backpressure.
        """
        dropped = False
        queue = self._queue
        if body is None:
            self._end_stream("the phone closed the sensor stream")
            return False
        if queue is None:
            self._handle_body(body)
            return False
        try:
            queue.put_nowait(body)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            dropped = True
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(body)
        return dropped

    async def _open_stream(self) -> tuple[asyncio.Queue[bytes | None], asyncio.Task[None]]:
        subscribe = getattr(self._session, "subscribe_blobs", None)
        if callable(subscribe):
            queue = subscribe(SENSOR_STREAM, maxsize=self._queue_frames)
        else:  # pragma: no cover - a session without media plumbing
            queue = asyncio.Queue(maxsize=self._queue_frames)
        task = asyncio.get_running_loop().create_task(
            self._receive_loop(queue), name="sensors-receive"
        )
        return queue, task

    async def _receive_loop(self, queue: asyncio.Queue[bytes | None]) -> None:
        while True:
            body = await queue.get()
            if body is None:
                self._end_stream("the phone closed the sensor stream")
                return
            self._handle_body(body)

    def _end_stream(self, reason: str) -> None:
        self._registry.active = ()
        if self._state in (ACTIVE, STARTING, STOPPING):
            self._set(IDLE, reason)

    def _handle_body(self, body: bytes) -> None:
        if self._state not in (ACTIVE, STARTING):
            # A frame that arrives after stop (or before start) is not data any
            # more: counting it would make a stopped stream look alive.
            return
        try:
            frame = decode_frame(body)
        except SensorFrameError as exc:
            # Counted on the link, not on a made-up "sensor 0": a phantom row with
            # a decode error is more confusing than no row at all.
            with self._lock:
                self._invalid_frames += 1
                self._detail = f"{self._invalid_frames} malformed sensor frame(s): {exc}"
            return
        self._apply(frame)

    def _apply(self, frame: SensorFrame) -> None:
        stats = self._stats_for(frame.sensor_id)
        if stats.last_seq >= 0:
            delta = _seq_delta(stats.last_seq, frame.seq)
            if delta == 0:
                stats.duplicates += 1
                return
            if delta < 0:
                stats.out_of_order += 1
                return
            if delta > 1:
                stats.dropped += delta - 1
        stats.received += 1
        stats.last_seq = frame.seq
        stats.last_timestamp_ms = frame.timestamp_ms

        now = time.monotonic()
        previous = self._last_seen.get(frame.sensor_id)
        self._last_seen[frame.sensor_id] = now
        hz = 0.0
        if previous is not None and now > previous:
            instant = 1.0 / (now - previous)
            prior = self._registry.readings.get(frame.sensor_id)
            hz = instant if prior is None else 0.8 * prior.hz + 0.2 * instant
        elif previous is not None:
            prior = self._registry.readings.get(frame.sensor_id)
            hz = prior.hz if prior is not None else 0.0

        known = self._registry.by_id(frame.sensor_id)
        self._registry.readings[frame.sensor_id] = SensorReading(
            sensor_id=frame.sensor_id,
            kind=frame.kind or (known.kind if known is not None else 0),
            seq=frame.seq,
            timestamp_ms=frame.timestamp_ms,
            values=frame.values,
            hz=hz,
        )
        self._notify()

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _stats_for(self, sensor_id: int) -> SensorStats:
        with self._lock:
            stats = self._registry.stats.get(sensor_id)
            if stats is None:
                stats = SensorStats()
                self._registry.stats[sensor_id] = stats
            return stats

    def _set(self, state: str, detail: str = "") -> None:
        self._state = state
        self._detail = detail
        self._notify()

    def _notify(self) -> None:
        if self._on_change is None:
            return
        with contextlib.suppress(Exception):
            self._on_change(self.snapshot())

    async def _quiet_rpc(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Best-effort RPC used on the way *out* of a failure.

        A compensation call that raises would replace the real error with a
        confusing second one, so this logs and moves on.
        """
        if not getattr(self._session, "is_open", False):
            return
        try:
            await self._session.call(method, params or {}, timeout=3.0)
        except Exception as exc:  # noqa: BLE001
            log.debug("%s during cleanup failed: %s", method, exc)


def _seq_delta(previous: int, current: int) -> int:
    """Signed distance in a 16-bit wrapping sequence space."""
    delta = (current - previous) & 0xFFFF
    return delta - 0x10000 if delta > 0x8000 else delta


def _started_ids(reply: Any, requested: list[int]) -> list[int]:
    """The ids the phone says it actually started, defensively parsed."""
    if isinstance(reply, dict):
        raw = reply.get("active")
        if isinstance(raw, list):
            started = [int(v) for v in raw if isinstance(v, (int, float))]
            if started:
                return started
    return requested


def _applied_interval(reply: Any, fallback: int) -> int:
    if isinstance(reply, dict):
        value = reply.get("interval_us")
        if isinstance(value, (int, float)) and 1000 <= int(value) <= 10_000_000:
            return int(value)
    return fallback


def _explain(prefix: str, exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return f"{prefix}: {text}"
