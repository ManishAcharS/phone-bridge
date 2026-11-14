"""The camera stream: one direction, one bounded queue, one newest frame.

The phone sends, the PC shows. The design is deliberately boring:

* **One message is one frame.** A dropped frame is a dropped picture, never a
  corrupt one, and there is nothing to reassemble after a reconnect.
* **The newest frame wins.** Frames are held in a queue of at most
  :data:`QUEUE_FRAMES`; when the user interface falls behind, the *oldest* frame
  is discarded. Latency therefore stays bounded no matter how slow the preview
  is, which is the difference between "a bit choppy" and "two seconds behind
  reality".
* **Camera traffic is its own stream id** (3), so a burst of frames cannot delay
  audio or a control request, and closing the camera cannot disturb either.

Like the audio plane, this object never opens a socket: frames arrive on the
session the connection manager authenticated. If the session ends, the stream
ends with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .frames import (
    FrameGate,
    VideoConfig,
    VideoFrame,
    VideoFrameError,
    VideoStats,
    decode_frame,
)

log = logging.getLogger(__name__)

#: Camera frames from the phone (docs/03): 3 = video, fixed.
VIDEO_STREAM = 3

#: States of the stream.
IDLE = "idle"
STARTING = "starting"
ACTIVE = "active"
STOPPING = "stopping"
ERROR = "error"

#: Five frames is ~330 ms at 15 fps: enough to absorb a hiccup, small enough
#: that a slow consumer cannot accumulate a backlog behind the user's eyes.
QUEUE_FRAMES = 5


class CameraError(RuntimeError):
    """The camera refused or failed, with a message meant for a user."""


@dataclass
class CameraSnapshot:
    """What the window shows; an immutable copy, never the live objects."""

    state: str = IDLE
    detail: str = ""
    config: dict[str, object] = field(default_factory=dict)
    stats: dict[str, int | str] = field(default_factory=dict)
    has_frame: bool = False
    #: The newest frame, for the preview. ``None`` until one arrives.
    frame: VideoFrame | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "state": self.state,
            "detail": self.detail,
            "config": dict(self.config),
            "stats": dict(self.stats),
            "has_frame": self.has_frame,
        }
        return payload


class CameraStream:
    """Start/stop/switch a camera on the phone, and hold its newest frame."""

    def __init__(
        self,
        session: Any,
        *,
        config: VideoConfig | None = None,
        on_change: Callable[[CameraSnapshot], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        queue_frames: int = QUEUE_FRAMES,
    ) -> None:
        self._session = session
        self._config = config or VideoConfig()
        self._on_change = on_change
        self._clock = clock
        self._queue_frames = max(1, queue_frames)

        self.stats = VideoStats()
        self._gate = FrameGate()
        self._state = IDLE
        self._detail = ""
        self._frame: VideoFrame | None = None
        self._queue: asyncio.Queue[bytes | None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._started_at = clock()

    # ------------------------------------------------------------------ #
    # observation
    # ------------------------------------------------------------------ #
    @property
    def config(self) -> VideoConfig:
        return self._config

    @property
    def state(self) -> str:
        return self._state

    @property
    def active(self) -> bool:
        return self._state == ACTIVE

    def snapshot(self) -> CameraSnapshot:
        return CameraSnapshot(
            state=self._state,
            detail=self._detail,
            config=self._config.as_dict(),
            stats=self.stats.to_dict(),
            has_frame=self._frame is not None,
            frame=self._frame,
        )

    def latest_frame(self) -> VideoFrame | None:
        return self._frame

    def _set(self, state: str, detail: str = "") -> None:
        self._state = state
        self._detail = detail
        if state == ERROR:
            self.stats.last_error = detail
        if self._on_change is not None:
            with contextlib.suppress(Exception):  # a UI callback must not break video
                self._on_change(self.snapshot())

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self, config: VideoConfig | None = None) -> bool:
        """Ask the phone to open a camera and begin sending frames.

        Every failure leaves a sentence in ``snapshot().detail`` and returns
        ``False``: this is called from a button, and a traceback is not a UI.
        """
        if self._state in (ACTIVE, STARTING):
            return self._state == ACTIVE
        if not getattr(self._session, "is_open", False):
            self._set(ERROR, "not connected: the camera needs a live session")
            return False
        proposal = config or self._config
        self._set(STARTING)
        try:
            reply = await self._session.call("camera.start", {"config": proposal.as_dict()})
        except Exception as exc:  # noqa: BLE001 - every failure is a message
            self._set(ERROR, _explain("the phone refused the camera", exc))
            await self._quiet_rpc("camera.stop")
            return False

        granted = _granted_config(reply, proposal)
        self._config = granted
        self.stats = VideoStats()
        # A fresh gate, not a reset to sequence 0: if the very first frame is lost
        # in transit, the preview must still start on the first one that arrives
        # instead of holding every frame until the reorder window overflows.
        self._gate = FrameGate()
        self._frame = None
        self._started_at = self._clock()
        # Subscribe to the session's video stream when there is one: the frames
        # must arrive through the *same* authenticated, encrypted connection as
        # audio and control. `push()` stays as the seam tests and manual tools use.
        subscribe = getattr(self._session, "subscribe_blobs", None)
        queue: asyncio.Queue[bytes | None]
        if callable(subscribe):
            queue = subscribe(VIDEO_STREAM, maxsize=self._queue_frames)
        else:  # pragma: no cover - a session without media plumbing
            queue = asyncio.Queue(maxsize=self._queue_frames)
        self._queue = queue
        self._task = asyncio.get_running_loop().create_task(
            self._receive_loop(queue), name="camera-receive"
        )
        self._set(ACTIVE)
        return True

    def adopt(self, session: Any) -> None:
        """Point the stream at a new session after a reconnect.

        The phone closes its camera when the session that asked for it goes away
        (``CameraEngine.onSessionClosed``), so a reconnected link is a link with
        no picture. This puts the stream back to IDLE - the receive task and the
        old queue belonged to the dead session - so :meth:`start` issues a fresh
        ``camera.start`` instead of returning "already active".
        """
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
        self._session = session
        self._queue = None
        self._state = IDLE
        self._detail = ""
        self._frame = None

    async def stop(self, reason: str = "stopped") -> None:
        """Stop the phone's camera and drop every buffered frame."""
        if self._state == IDLE:
            return
        self._set(STOPPING, reason)
        await self._quiet_rpc("camera.stop")
        await self._cancel_task()
        self._queue = None
        self._frame = None
        self._set(IDLE, reason)

    async def switch(self, facing: str) -> bool:
        """Change camera while connected, without tearing the stream down twice."""
        if facing not in ("back", "front"):
            self._set(ERROR, f"unknown camera: {facing}")
            return False
        target = VideoConfig(
            width=self._config.width,
            height=self._config.height,
            fps=self._config.fps,
            quality=self._config.quality,
            facing=facing,
            fixed_fps=self._config.fixed_fps,
        )
        if not self.active:
            # Not streaming yet: just record the choice.
            self._config = target
            return True
        self._set(STOPPING, f"switching to the {facing} camera")
        await self._cancel_task()
        self._queue = None
        self._frame = None
        self.stats = VideoStats()
        self._gate = FrameGate()
        return await self.start(target)

    async def _cancel_task(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _quiet_rpc(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not getattr(self._session, "is_open", False):
            return
        try:
            await self._session.call(method, params)
        except Exception as exc:  # noqa: BLE001
            log.info("camera control %s failed: %s", method, exc)

    # ------------------------------------------------------------------ #
    # receiving
    # ------------------------------------------------------------------ #
    async def _receive_loop(self, queue: asyncio.Queue[bytes | None]) -> None:
        try:
            while True:
                body = await queue.get()
                if body is None:  # documented end-of-stream marker
                    self._set(IDLE, "the session ended")
                    return
                self._handle_body(body)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._set(ERROR, f"camera stream stopped: {exc}")

    def _handle_body(self, body: bytes) -> None:
        """Decode one message. Exposed for tests and for the session callback."""
        try:
            frame = decode_frame(body)
        except VideoFrameError as exc:
            self.stats.invalid += 1
            self.stats.last_error = str(exc)
            return
        if frame.stop:
            # The picture on screen is now the past: clear it, or the window keeps
            # showing a frozen frame and looks like a working camera.
            self._frame = None
            self._set(IDLE, "the phone stopped the camera")
            return
        self.stats.received += 1
        self.stats.bytes_received += len(body)
        self.stats.last_seq = frame.seq
        self.stats.last_timestamp_ms = frame.timestamp_ms
        self.stats.last_rotation = frame.rotation
        for ordered in self._gate.offer(frame):
            # Newest wins: whatever was waiting is older than this frame.
            self._frame = ordered
            self.stats.dropped += 0
        self.stats.duplicates = self._gate.duplicates
        self.stats.late = self._gate.late
        self.stats.reordered = self._gate.reordered
        if self._on_change is not None:
            with contextlib.suppress(Exception):
                self._on_change(self.snapshot())

    def push(self, body: bytes | None) -> bool:
        """Feed one received message without a live queue (tests, and the pump).

        Returns ``True`` when the queue was full and the oldest frame had to go —
        the drop is what keeps the preview current rather than behind.
        """
        dropped = False
        queue = self._queue
        if queue is not None:
            try:
                queue.put_nowait(body)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                self.stats.dropped += 1
                dropped = True
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(body)
        elif body is not None:
            self._handle_body(body)
        return dropped

    async def close(self, reason: str = "session closed") -> None:
        with contextlib.suppress(Exception):
            await self.stop(reason)


def _granted_config(reply: Any, fallback: VideoConfig) -> VideoConfig:
    """Read the phone's accepted configuration out of an RPC reply, defensively."""
    if isinstance(reply, dict):
        payload = reply.get("result", reply)
        if isinstance(payload, dict):
            raw = payload.get("config")
            if isinstance(raw, dict):
                try:
                    return VideoConfig.from_dict(raw, fallback=fallback)
                except VideoFrameError:
                    log.warning("phone returned an unusable camera config: %s", raw)
    return fallback


def _explain(prefix: str, exc: Exception) -> str:
    """Turn a refusal into one sentence that keeps the phone's code verbatim.

    An RPC refusal arrives as ``SessionError("camera_busy: the camera could not
    be opened (...)")`` — the code *and* the phone's own words, which is exactly
    what the user needs to paste into a bug report. Splitting them out makes the
    sentence readable without rewriting a single character of the message.
    """
    text = str(exc).strip() or type(exc).__name__
    code = str(getattr(exc, "code", "") or "")
    detail = text
    if not code:
        head, sep, rest = text.partition(": ")
        if sep and head.isidentifier() and head.islower() and rest:
            code, detail = head, rest
    if code:
        return f"{prefix} ({code}): {detail}"
    return f"{prefix}: {text}"
