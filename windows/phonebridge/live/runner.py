"""Wiring the three delivery paths onto one authenticated session.

The runner owns no protocol and no device protocol of its own: frames arrive on
the existing :class:`~phonebridge.camera.stream.CameraStream` and
:class:`~phonebridge.audio.link.AudioLink`, which already ride the session's
AES-GCM channel, and leave through the sinks in :mod:`phonebridge.live.devices`.
That is the whole design — this file is the place where "the phone's camera" and
"the PC's webcam device" are finally the same thing, and nothing about
authentication, framing or reconnection is re-implemented to get there.

Failure policy, per path:

* camera — a stall keeps the last frame on screen (a webcam that goes black the
  moment Wi-Fi hiccups interrupts every call), then blanks after ``stall_s``;
  frames resume on their own when the phone sends again.
* microphone — a dead device stops that path and reports it; the link and the
  camera keep running.
* speaker — an unreachable loopback stops that path and reports it; nothing else
  is affected.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..audio.frames import AudioFormat
from ..audio.link import MIC_STREAM, PLAYBACK_STREAM, AudioLink  # noqa: F401 - re-exported
from ..camera.frames import VideoConfig, VideoFrame
from ..camera.stream import CameraStream
from .config import LivePlan
from .devices import (
    CableMicSink,
    DeviceUnavailable,
    open_cable_sink,
    open_loopback_source,
    open_virtual_camera,
)
from .pump import CameraPump, PumpSnapshot

log = logging.getLogger(__name__)

#: How long a stalled camera keeps showing its last frame before going black.
DEFAULT_STALL_S = 2.0

#: How long the camera may accept `camera.start` and deliver nothing before the
#: silence itself is reported. Five seconds is long enough for a slow phone to
#: open a lens and short enough that nobody stares at an empty preview.
DEFAULT_FIRST_FRAME_S = 5.0

#: Status is refreshed on this cadence: fast enough to look live, slow enough
#: that a one-line status does not cost more than the audio it describes.
STATUS_INTERVAL_S = 1.0

#: How often the camera supervisor looks at the pump. Only bookkeeping — the
#: frames themselves reach the pump from :meth:`CameraStream`'s callback, so this
#: interval never limits the frame rate.
SUPERVISOR_INTERVAL_S = 0.05


@dataclass
class LiveStatus:
    """One immutable snapshot of what the user is getting right now."""

    connected: bool = False
    camera_state: str = "off"
    camera_detail: str = ""
    fps: float = 0.0
    frames_sent: int = 0
    mic_state: str = "off"
    mic_detail: str = ""
    mic_level_dbfs: float = float("-inf")
    speaker_state: str = "off"
    speaker_detail: str = ""
    buffer_ms: int = 0
    latency_ms: int = 0
    detail: str = ""
    extra: dict[str, int] = field(default_factory=dict)
    #: ``--stats`` numbers: the PC's pump and, when asked for, the phone's own
    #: counters from its ``camera.status``. Empty unless ``--stats`` is on.
    camera_stats: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        parts: list[str] = [f"link {'up' if self.connected else 'down'}"]
        if self.camera_state != "off":
            parts.append(f"camera {self.camera_state} {self.fps:.1f} fps")
        if self.mic_state != "off":
            level = (
                "silent"
                if self.mic_level_dbfs == float("-inf")
                else (f"{self.mic_level_dbfs:.0f} dBFS")
            )
            parts.append(f"mic {self.mic_state} {level} buf {self.buffer_ms} ms")
        if self.speaker_state != "off":
            parts.append(f"speaker {self.speaker_state} {self.latency_ms} ms")
        if self.detail:
            parts.append(self.detail)
        return " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "connected": self.connected,
            "camera": {
                "state": self.camera_state,
                "detail": self.camera_detail,
                "fps": round(self.fps, 1),
                "frames_sent": self.frames_sent,
            },
            "mic": {
                "state": self.mic_state,
                "detail": self.mic_detail,
                "level_dbfs": None
                if self.mic_level_dbfs == float("-inf")
                else round(self.mic_level_dbfs, 1),
                "buffer_ms": self.buffer_ms,
            },
            "speaker": {
                "state": self.speaker_state,
                "detail": self.speaker_detail,
                "latency_ms": self.latency_ms,
            },
        }
        if self.camera_stats:
            payload["camera"]["stats"] = dict(self.camera_stats)
        if self.extra:
            payload["stats"] = dict(self.extra)
        return payload


def default_decode(frame: VideoFrame, width: int, height: int) -> Any:
    """Upright, unmirrored, letterboxed RGB image of one frame.

    Deliberately **not** mirrored: a virtual webcam feeds applications that do
    their own preview mirroring, so mirroring here would mirror it twice. See
    :func:`phonebridge.camera.preview.mirrored_for_display`.

    The work is in :func:`phonebridge.camera.preview.decode_into_box`, which
    decodes with ``draft()`` and only allocates a letterbox when the aspect
    ratios disagree — this function runs 30 times a second, so it does not copy
    anything it can avoid.
    """
    from ..camera.preview import decode_into_box  # noqa: PLC0415 - optional Pillow

    return decode_into_box(frame, width, height)


def _to_array(image: Any) -> Any:
    import numpy  # noqa: PLC0415 - pyvirtualcam's dependency, only needed to send

    return numpy.asarray(image, dtype=numpy.uint8)


class LiveRunner:
    """Start the requested paths, keep them alive, and describe what is happening."""

    def __init__(
        self,
        session: Any,
        plan: LivePlan,
        *,
        camera_factory: Callable[..., Any] = open_virtual_camera,
        mic_factory: Callable[..., Any] = open_cable_sink,
        loopback_factory: Callable[..., Any] = open_loopback_source,
        decode: Callable[[VideoFrame, int, int], Any] = default_decode,
        stall_s: float = DEFAULT_STALL_S,
        first_frame_s: float = DEFAULT_FIRST_FRAME_S,
        stats: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._plan = plan
        self._camera_factory = camera_factory
        self._mic_factory = mic_factory
        self._loopback_factory = loopback_factory
        self._decode = decode
        self._stall_s = stall_s
        self._first_frame_s = first_frame_s
        self._stats = stats
        self._clock = clock

        self._camera: CameraStream | None = None
        self._camera_sink: Any = None
        #: The worker thread that decodes, converts and sends. Frames are handed
        #: to it on arrival; nothing in the frame path touches the asyncio loop.
        self._pump: CameraPump | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._last_frame_seen: Any = None
        self._pump_snapshot = PumpSnapshot()
        self._phone_stats: dict[str, Any] = {}
        self._stats_at = 0.0
        self._link: AudioLink | None = None
        self._mic_sink: CableMicSink | None = None

        self._sent = 0
        self._fps = 0.0
        self._detail = ""
        self._started_at = clock()

        # Camera failures, each kept as the sentence the user reads. They are
        # separate because each has a different fix, and because only the first
        # two mean the path is dead.
        self._camera_error = ""
        self._camera_notice = ""
        self._sink_error = ""
        self._decode_failures = 0
        self._decode_detail = ""
        self._first_frame_deadline = clock()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Open every device *before* asking the phone for anything.

        Order matters for the user: if OBS or VB-CABLE is missing, they find out
        before the phone starts streaming (and before it shows a notification
        saying the PC is using its camera).
        """
        if self._plan.empty:
            raise DeviceUnavailable("nothing to deliver: pick --camera, --mic or --speaker")

        if self._plan.camera is not None:
            self._camera_sink = self._camera_factory(self._plan.camera)
            # The pump exists before the phone is asked, so the very first frame
            # is published instead of meeting a `None` and being dropped.
            self._pump = self._make_pump()
            self._pump.start()
            self._camera = CameraStream(
                self._session,
                config=VideoConfig(
                    width=self._plan.camera.width,
                    height=self._plan.camera.height,
                    fps=self._plan.camera.fps,
                    quality=self._plan.camera.quality,
                    facing=self._plan.camera.facing,
                ),
                # Frames reach the pump the moment they arrive, not on a timer:
                # polling once per 1/fps was the largest single cost of the old
                # frame path (see pump.py).
                on_change=self._camera_changed,
            )
            if not await self._camera.start():
                # The phone said no (or never answered). That sentence is the
                # whole reason this command exists; keep it, and do not start a
                # pump that has nothing to send.
                snapshot = self._camera.snapshot()
                self._camera_error = snapshot.detail or "the phone refused the camera"
                log.warning("camera did not start: %s", self._camera_error)
                self._pump.close()
                self._pump = None

        if self._plan.mic is not None or self._plan.speaker is not None:
            self._link = AudioLink(
                self._session,
                fmt=AudioFormat(),
                sink_factory=(lambda fmt: self._open_mic(fmt)) if self._plan.mic else None,
                # The parentheses matter: without them the conditional would sit
                # *inside* the lambda body and the factory would never be None, so
                # the link would open a capture device nobody asked for.
                source_factory=(
                    (lambda fmt: self._loopback_factory(fmt, self._plan.speaker))
                    if self._plan.speaker
                    else None
                ),
            )
            if self._plan.mic is not None:
                await self._link.start_mic()
            if self._plan.speaker is not None:
                await self._link.start_playback()

        if self._camera_sink is not None and not self._camera_error:
            self._first_frame_deadline = self._clock() + self._first_frame_s
            self._supervisor = asyncio.get_running_loop().create_task(
                self._camera_loop(), name="phonebridge-camera-supervisor"
            )

    def _make_pump(self) -> CameraPump:
        assert self._plan.camera is not None
        return CameraPump(
            decode=self._decode,
            # Late binding on purpose: tests replace `_to_array` at module level
            # and the pump must see the replacement.
            to_array=lambda image: _to_array(image),
            sink=self._camera_sink,
            width=self._plan.camera.width,
            height=self._plan.camera.height,
            fps=self._plan.camera.fps,
            stall_s=self._stall_s,
            delay_s=(max(0, self._plan.speaker.lipsync_ms) / 1000.0)
            if self._plan.speaker
            else 0.0,
            clock=self._clock,
        )

    def _camera_changed(self, snapshot: Any) -> None:
        """Called by :class:`CameraStream` for every received frame.

        Runs on the asyncio thread, so it does one thing only: hand the frame to
        the pump's single slot. Any older frame still waiting there is dropped —
        a backlog of old pictures is worse than a missing one.
        """
        pump = self._pump
        if pump is None:
            return
        frame = getattr(snapshot, "frame", None)
        if frame is None or frame is self._last_frame_seen:
            return
        self._last_frame_seen = frame
        pump.submit(frame)

    def _open_mic(self, fmt: AudioFormat) -> Any:
        assert self._plan.mic is not None
        loop = asyncio.get_running_loop()
        kwargs: dict[str, Any] = {}
        if self._mic_factory_supports_failure_callback():
            kwargs["on_failure"] = lambda message: self._mic_failed(loop, message)
        self._mic_sink = self._mic_factory(fmt, self._plan.mic, **kwargs)
        return self._mic_sink

    def _mic_factory_supports_failure_callback(self) -> bool:
        """True when the factory takes ``on_failure`` (tests inject two-arg fakes)."""
        import inspect  # noqa: PLC0415 - only needed once, and keeps import cheap

        try:
            parameters = inspect.signature(self._mic_factory).parameters
        except (TypeError, ValueError):  # builtins and C callables
            return False
        if "on_failure" in parameters:
            return True
        return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())

    def _mic_failed(self, loop: asyncio.AbstractEventLoop, message: str) -> None:
        """Called from the sink's pump thread when the device dies.

        Stops the *phone's* capture — a microphone nobody can hear is battery
        spent for nothing — and touches nothing else: the session, the camera and
        the speaker stay up, which is the difference between a dead device and a
        dead call.
        """
        link = self._link
        if link is None:
            return

        def stop() -> None:
            asyncio.ensure_future(link.stop_mic(f"microphone device failed: {message}"))

        try:
            loop.call_soon_threadsafe(stop)
        except RuntimeError:
            pass  # the loop closed first; there is nothing left to stop

    # ------------------------------------------------------------------ #
    # camera pump
    # ------------------------------------------------------------------ #
    async def _camera_loop(self) -> None:
        """Supervise the pump; never touch a frame.

        The frames are decoded and sent on :class:`CameraPump`'s thread. This
        loop exists for the things that need the session: noticing a pump error,
        reporting silence, and (with ``--stats``) asking the phone what *it*
        thinks its frame rate is. It ticks every 50 ms, which no longer limits
        anything — the frames do not come through here.
        """
        assert self._plan.camera is not None
        while True:
            await asyncio.sleep(SUPERVISOR_INTERVAL_S)
            self._sample_pump()
            self._watch_first_frame(self._clock())
            if self._stats:
                await self._poll_phone_stats()

    def _sample_pump(self) -> None:
        """Take the pump's counters, and turn its errors into the user's words."""
        pump = self._pump
        if pump is None:
            return
        snapshot = pump.snapshot()
        self._pump_snapshot = snapshot
        self._sent = snapshot.sent
        self._fps = snapshot.fps

        if snapshot.decode_failures > self._decode_failures:
            self._decode_failures = snapshot.decode_failures
            self._decode_detail = snapshot.decode_error or self._decode_detail
            if not self._camera_notice:
                # Said once, not once per frame: a wrong codec or a truncated
                # stream would otherwise print every 33 ms.
                self._camera_notice = (
                    f"a camera frame could not be decoded ({self._decode_detail}); "
                    f"further failures are counted, not printed"
                )
                log.warning("camera: %s", self._camera_notice)

        if snapshot.send_error and not self._sink_error:
            self._sink_failed(Exception(snapshot.send_error))

    async def _poll_phone_stats(self) -> None:
        """Ask the phone for its own numbers, at most once a second.

        Only with ``--stats``: this is an extra RPC per second, and it is only
        worth its cost when someone is looking at the numbers.
        """
        now = self._clock()
        if now - self._stats_at < 1.0:
            return
        self._stats_at = now
        try:
            reply = await self._session.call("camera.status", {}, timeout=2.0)
        except Exception:  # noqa: BLE001 - a debug aid must never break the run
            return
        payload = reply.get("result", reply) if isinstance(reply, dict) else {}
        stats = payload.get("stats") if isinstance(payload, dict) else None
        if isinstance(stats, dict):
            self._phone_stats = stats

    def _watch_first_frame(self, now: float) -> bool:
        """Report a camera that accepted `start` and then delivered nothing.

        `camera.start` returning true only means the phone agreed to try: the
        lens can still fail to open inside CameraX, the encoder can throw, and a
        phone that is not sending cannot say so. Silence is therefore a symptom,
        and it has to be described — otherwise the status line says "camera
        error 0.0 fps" forever and the user has nothing to act on.
        """
        if self._camera_error or self._sent or self._sink_error:
            return False
        if now < self._first_frame_deadline:
            return False
        snapshot = self._camera.snapshot() if self._camera is not None else None
        received = int(snapshot.stats.get("received", 0)) if snapshot is not None else 0
        if received:
            reason = (
                f"the phone sent {received} camera frame(s) but none could be decoded "
                f"({self._decode_detail or 'no error was recorded'})"
            )
        else:
            reason = (
                f"no camera frames from the phone within {self._first_frame_s:.0f} s: "
                "the phone accepted camera.start, so it believes the camera is on. "
                "Check the phone for a camera error banner, and run "
                "'adb logcat -s CameraEngine' on the phone for the exact refusal"
            )
        self._camera_error = reason
        log.warning("camera: %s", reason)
        return True

    def _sink_failed(self, exc: Exception) -> None:
        """The virtual camera device failed: say so, stop sending, keep the session."""
        self._sink_error = (
            f"the virtual camera stopped accepting frames "
            f"({type(exc).__name__}: {exc}); restart OBS (or whichever app owns the "
            f"camera) and re-run this command"
        )
        log.warning("camera: %s", self._sink_error)

    # ------------------------------------------------------------------ #
    # observation
    # ------------------------------------------------------------------ #
    def status(self) -> LiveStatus:
        connected = bool(getattr(self._session, "is_open", False))
        status = LiveStatus(
            connected=connected,
            detail=self._detail,
            frames_sent=self._sent,
            fps=self._fps,
            latency_ms=self._plan.speaker.latency_ms if self._plan.speaker else 0,
        )

        if self._camera is not None:
            snapshot = self._camera.snapshot()
            status.camera_state = snapshot.state
            status.camera_detail = snapshot.detail
        if self._pump is not None:
            # The pump is the only thing that knows what actually reached the
            # device, so its numbers win over the stream's.
            self._sample_pump()
            status.fps = self._fps
            status.frames_sent = self._sent
            status.camera_stats = {
                "pc": self._pump_snapshot.to_dict(),
                "phone": dict(self._phone_stats),
            }
        if self._camera_error:
            # Sticky: the path is dead until the command is restarted, so the
            # reason must not disappear the moment the phone sends a frame.
            status.camera_state = "error"
            status.camera_detail = self._camera_error
        elif self._sink_error:
            status.camera_state = "error"
            status.camera_detail = self._sink_error
        elif self._camera_notice and not status.camera_detail:
            status.camera_detail = self._camera_notice
        if self._link is not None:
            audio = self._link.snapshot()
            status.mic_state = audio.mic
            status.mic_detail = audio.mic_detail
            status.speaker_state = audio.playback
            status.speaker_detail = audio.detail
            status.extra = {
                key: value for key, value in audio.stats.items() if isinstance(value, int)
            }
        if self._mic_sink is not None:
            status.mic_level_dbfs = self._mic_sink.level_dbfs
            status.buffer_ms = int(self._mic_sink.stats.get("depth_ms", 0))
            if self._mic_sink.error:
                status.mic_detail = self._mic_sink.error
                status.mic_state = "error"
        return status

    async def run(
        self, stop: asyncio.Event | None = None, duration: float | None = None
    ) -> LiveStatus:
        """Run until stopped, then return the last status."""
        deadline = None if duration is None else self._clock() + duration
        while True:
            if stop is not None and stop.is_set():
                break
            if deadline is not None and self._clock() >= deadline:
                break
            await asyncio.sleep(STATUS_INTERVAL_S)
        return self.status()

    # ------------------------------------------------------------------ #
    async def close(self, reason: str = "stopped") -> None:
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor
            self._supervisor = None
        if self._pump is not None:
            # Joins the thread: no frame may be sent after the device is closed.
            self._pump.close()
            self._pump = None
        if self._camera is not None:
            await self._camera.close(reason)
            self._camera = None
        if self._link is not None:
            await self._link.close(reason)
            self._link = None
        if self._mic_sink is not None:
            self._mic_sink.close()
            self._mic_sink = None
        if self._camera_sink is not None:
            with contextlib.suppress(Exception):
                self._camera_sink.close()
            self._camera_sink = None
