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
from collections import deque
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

log = logging.getLogger(__name__)

#: How long a stalled camera keeps showing its last frame before going black.
DEFAULT_STALL_S = 2.0

#: Status is refreshed on this cadence: fast enough to look live, slow enough
#: that a one-line status does not cost more than the audio it describes.
STATUS_INTERVAL_S = 1.0


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
        if self.extra:
            payload["stats"] = dict(self.extra)
        return payload


def default_decode(frame: VideoFrame, width: int, height: int) -> Any:
    """Upright, unmirrored, letterboxed RGB image of one frame.

    Deliberately **not** mirrored: a virtual webcam feeds applications that do
    their own preview mirroring, so mirroring here would mirror it twice. See
    :func:`phonebridge.camera.preview.mirrored_for_display`.
    """
    from ..camera.preview import PreviewUnavailable, decode, render  # noqa: PLC0415

    preview = decode(frame)
    image, _, _ = render(preview, facing="", box=(width, height), mirror=False)
    image = image.convert("RGB")
    if image.size == (width, height):
        return image
    # Letterbox rather than stretch: a webcam that changes aspect ratio looks
    # like a broken camera to everyone downstream.
    canvas = image.copy()
    canvas.thumbnail((width, height))
    from PIL import Image  # noqa: PLC0415

    out = Image.new("RGB", (width, height), (0, 0, 0))
    out.paste(canvas, ((width - canvas.width) // 2, (height - canvas.height) // 2))
    del PreviewUnavailable
    return out


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._plan = plan
        self._camera_factory = camera_factory
        self._mic_factory = mic_factory
        self._loopback_factory = loopback_factory
        self._decode = decode
        self._stall_s = stall_s
        self._clock = clock

        self._camera: CameraStream | None = None
        self._camera_sink: Any = None
        self._pump: asyncio.Task[None] | None = None
        self._link: AudioLink | None = None
        self._mic_sink: CableMicSink | None = None

        self._sent = 0
        self._sent_at: deque[tuple[float, int]] = deque(maxlen=8)
        self._fps = 0.0
        self._detail = ""
        self._started_at = clock()

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
            self._camera = CameraStream(
                self._session,
                config=VideoConfig(
                    width=self._plan.camera.width,
                    height=self._plan.camera.height,
                    fps=self._plan.camera.fps,
                    quality=self._plan.camera.quality,
                    facing=self._plan.camera.facing,
                ),
            )
            await self._camera.start()

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

        if self._camera_sink is not None:
            self._pump = asyncio.get_running_loop().create_task(
                self._camera_loop(), name="phonebridge-vcam-pump"
            )

    def _open_mic(self, fmt: AudioFormat) -> Any:
        assert self._plan.mic is not None
        self._mic_sink = self._mic_factory(fmt, self._plan.mic)
        return self._mic_sink

    # ------------------------------------------------------------------ #
    # camera pump
    # ------------------------------------------------------------------ #
    async def _camera_loop(self) -> None:
        assert self._plan.camera is not None
        interval = 1.0 / max(1, self._plan.camera.fps)
        last_frame: VideoFrame | None = None
        last_sent = 0.0
        blanked = False
        loop = asyncio.get_running_loop()
        delay_s = max(0, self._plan.speaker.lipsync_ms) / 1000.0 if self._plan.speaker else 0.0
        pending: deque[tuple[float, Any]] = deque()

        while True:
            await asyncio.sleep(interval)
            now = self._clock()
            frame = self._camera.latest_frame() if self._camera else None

            if frame is not None and frame is not last_frame:
                last_frame = frame
                try:
                    image = await loop.run_in_executor(
                        None,
                        self._decode,
                        frame,
                        self._plan.camera.width,
                        self._plan.camera.height,
                    )
                    rgb = _to_array(image)
                except Exception as exc:  # noqa: BLE001 - one bad JPEG is one dropped frame
                    self._detail = f"frame dropped: {exc}"
                    continue
                pending.append((now + delay_s, rgb))
                last_sent = now
                blanked = False

            # Lip-sync: hold each frame until its due time, then release the ones
            # that are due. A positive offset delays video, never audio.
            while pending and pending[0][0] <= now:
                _, rgb = pending.popleft()
                self._camera_sink.send(rgb)
                self._sent += 1
                self._sent_at.append((now, self._sent))

            if not pending and last_sent and not blanked and now - last_sent > self._stall_s:
                # Nothing new arrived: keep the last picture, then go black so a
                # call shows "no signal" rather than a frozen face.
                with contextlib.suppress(Exception):
                    self._camera_sink.send_black()
                blanked = True

            self._refresh_fps(now)

    def _refresh_fps(self, now: float) -> None:
        while self._sent_at and now - self._sent_at[0][0] > 2.0:
            self._sent_at.popleft()
        if len(self._sent_at) >= 2:
            first_t, first_n = self._sent_at[0]
            last_t, last_n = self._sent_at[-1]
            span = last_t - first_t
            self._fps = (last_n - first_n) / span if span > 0 else 0.0
        elif not self._sent_at:
            self._fps = 0.0

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
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump
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
