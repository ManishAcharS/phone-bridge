"""The audio plane: two directions, one authenticated session, bounded everywhere.

The connection manager owns the session; this object owns the *streams* on top of
it and nothing else. It never opens a socket: every frame rides the existing
AES-256-GCM channel as ``MSG_AUDIO`` on a fixed stream id, so an audio stream can
never exist without authentication, and a dropped session takes the audio with it.

Two directions, deliberately symmetric:

===========  ======  ==============  =======================================
direction    stream  sender          receiver
===========  ======  ==============  =======================================
PC -> phone  1       WASAPI loopback Android ``AudioTrack``
phone -> PC  2       ``AudioRecord`` this module (monitor sink)
===========  ======  ==============  =======================================

Backpressure is the part that decides whether this is usable. Capture is a
*blocking* call that must run at the device's pace, and the network is slower and
burstier than the sound card, so the two are separated by a small queue that drops
the **oldest** frame when it is full. A full queue therefore costs one click and
bounded latency, instead of an ever-growing buffer that turns a slow link into a
two-second echo. The receive side applies the same idea in reverse through
:class:`~phonebridge.audio.frames.SequenceGate`.

Failure policy: an error in one direction never takes the session down; it stops
that direction, records the reason, and leaves the other one running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .backends import AudioBackendError, AudioSink, AudioSource
from .frames import (
    FLAG_MUTED,
    FLAG_START,
    FLAG_STOP,
    AudioFormat,
    AudioFrame,
    AudioFrameError,
    AudioStats,
    SequenceGate,
    decode_frame,
)

log = logging.getLogger(__name__)

#: Fixed stream ids (protocol v1, docs/03): 1 = PC audio to the phone, 2 = phone
#: microphone to the PC. Fixed rather than negotiated so a reconnecting PC can
#: discard a stale stream without asking which one it was.
PLAYBACK_STREAM = 1
MIC_STREAM = 2

#: States of one direction. ``error`` is sticky until the direction is restarted,
#: because a user needs to see *why* it stopped, not just that it did.
IDLE = "idle"
STARTING = "starting"
ACTIVE = "active"
STOPPING = "stopping"
ERROR = "error"

#: Frame header is 8 bytes; a queue of 8 frames is 160 ms of audio at 20 ms.
DEFAULT_QUEUE = 8


class AudioError(RuntimeError):
    """An audio direction refused or failed, with a message meant for a user."""


@dataclass
class Direction:
    """One direction's state, kept separate so the two cannot mask each other."""

    state: str = IDLE
    detail: str = ""
    format: AudioFormat | None = None

    @property
    def active(self) -> bool:
        return self.state == ACTIVE


@dataclass
class AudioSnapshot:
    """What the window shows. Immutable copy, never the live objects."""

    playback: str = IDLE
    mic: str = IDLE
    detail: str = ""
    mic_detail: str = ""
    volume: int = 40
    muted: bool = True
    format: dict[str, int] = field(default_factory=dict)
    stats: dict[str, int | str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "playback": self.playback,
            "mic": self.mic,
            "detail": self.detail,
            "mic_detail": self.mic_detail,
            "volume": self.volume,
            "muted": self.muted,
            "format": dict(self.format),
            "stats": dict(self.stats),
        }


class AudioLink:
    """Both audio directions over one :class:`~phonebridge.core.session.BridgeSession`."""

    def __init__(
        self,
        session: Any,
        *,
        fmt: AudioFormat | None = None,
        source_factory: Callable[[AudioFormat], AudioSource] | None = None,
        sink_factory: Callable[[AudioFormat], AudioSink] | None = None,
        on_change: Callable[[AudioSnapshot], None] | None = None,
        queue_size: int = DEFAULT_QUEUE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._fmt = fmt or AudioFormat()
        self._source_factory = source_factory
        self._sink_factory = sink_factory
        self._on_change = on_change
        self._queue_size = max(2, queue_size)
        self._clock = clock

        self.stats = AudioStats()
        self._gate = SequenceGate()
        self._playback = Direction()
        self._mic = Direction()
        self._volume = 40
        self._muted = True  # conservative default: nothing is audible until asked
        self._seq = 0
        self._started_at = clock()

        self._source: AudioSource | None = None
        self._sink: AudioSink | None = None
        self._capture: asyncio.Task[None] | None = None
        self._sender: asyncio.Task[None] | None = None
        self._receiver: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[bytes | None] | None = None

    # ------------------------------------------------------------------ #
    # observation
    # ------------------------------------------------------------------ #
    @property
    def format(self) -> AudioFormat:
        return self._fmt

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def muted(self) -> bool:
        return self._muted

    def snapshot(self) -> AudioSnapshot:
        return AudioSnapshot(
            playback=self._playback.state,
            mic=self._mic.state,
            detail=self._playback.detail,
            mic_detail=self._mic.detail,
            volume=self._volume,
            muted=self._muted,
            format=self._fmt.as_dict(),
            stats=self.stats.to_dict(),
        )

    def _notify(self) -> None:
        if self._on_change is not None:
            with contextlib.suppress(Exception):  # a UI callback must not break audio
                self._on_change(self.snapshot())

    def _set_playback(self, state: str, detail: str = "") -> None:
        self._playback.state = state
        self._playback.detail = detail
        if state == ERROR:
            self.stats.last_error = detail
        self._notify()

    def _set_mic(self, state: str, detail: str = "") -> None:
        self._mic.state = state
        self._mic.detail = detail
        if state == ERROR:
            self.stats.last_error = detail
        self._notify()

    # ------------------------------------------------------------------ #
    # PC audio -> phone
    # ------------------------------------------------------------------ #
    async def start_playback(self) -> bool:
        """Negotiate, then begin capturing the PC's audio and sending it.

        Returns ``True`` when frames are flowing. Every failure leaves a sentence
        in ``snapshot().detail`` and never raises at the caller: the GUI calls this
        from a button, and a traceback is not a user interface.
        """
        if self._playback.state in (ACTIVE, STARTING):
            return self._playback.state == ACTIVE
        self._set_playback(STARTING)
        try:
            reply = await self._session.call(
                "audio.playback.start", {"format": self._fmt.as_dict()}
            )
        except Exception as exc:  # noqa: BLE001 - every failure is a message
            self._set_playback(ERROR, _explain("the phone refused audio playback", exc))
            # The call may have started a route and then failed (a timeout after
            # the phone acted). Stop is idempotent, so ask for it either way.
            await self._quiet_rpc("audio.playback.stop")
            return False

        granted = _granted_format(reply, self._fmt)
        if granted != self._fmt:
            # The phone is allowed to pick a rate it can actually play; honour it
            # rather than resampling here.
            self._fmt = granted
        self._playback.format = granted

        if self._source_factory is None:
            self._set_playback(
                ERROR,
                "no PC audio capture is configured - install the 'soundcard' "
                "package and retry (`pip install soundcard`)",
            )
            await self._quiet_rpc("audio.playback.stop")
            return False
        try:
            self._source = self._source_factory(granted)
        except AudioBackendError as exc:
            self._set_playback(ERROR, str(exc))
            await self._quiet_rpc("audio.playback.stop")
            return False

        self._seq = 0
        self._started_at = self._clock()
        self._queue = asyncio.Queue(maxsize=self._queue_size)
        loop = asyncio.get_running_loop()
        self._capture = loop.create_task(self._capture_loop(), name="audio-capture")
        self._sender = loop.create_task(self._send_loop(), name="audio-send")
        self._set_playback(ACTIVE)
        return True

    async def _capture_loop(self) -> None:
        """Read the device at its own pace and enqueue. Drops the oldest on overflow."""
        assert self._source is not None and self._queue is not None
        loop = asyncio.get_running_loop()
        try:
            while self._playback.state in (ACTIVE, STOPPING):
                pcm = await loop.run_in_executor(None, self._source.read)
                if not pcm:
                    continue  # device produced less than a frame; read again
                if self._muted:
                    pcm = b"\x00" * len(pcm)
                try:
                    self._queue.put_nowait(pcm)
                except asyncio.QueueFull:
                    # Latency matters more than completeness: discard the stale
                    # frame and keep the newest one.
                    with contextlib.suppress(asyncio.QueueEmpty):
                        self._queue.get_nowait()
                    self.stats.dropped_full += 1
                    with contextlib.suppress(asyncio.QueueFull):
                        self._queue.put_nowait(pcm)
        except asyncio.CancelledError:
            raise
        except AudioBackendError as exc:
            self._set_playback(ERROR, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._set_playback(ERROR, f"capture stopped: {exc}")
        finally:
            if self._queue is not None:
                with contextlib.suppress(asyncio.QueueFull):
                    self._queue.put_nowait(None)

    async def _send_loop(self) -> None:
        """One frame per pull, so the queue — not the socket — bounds the latency."""
        assert self._queue is not None
        try:
            while True:
                pcm = await self._queue.get()
                if pcm is None:
                    break
                await self._send_frame(pcm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead session is not a crash
            self._set_playback(ERROR, f"sending audio stopped: {exc}")

    async def _send_frame(self, pcm: bytes, flags: int = 0) -> None:
        if self._playback.state not in (ACTIVE, STOPPING):
            return
        if self.stats.sent == 0:
            flags |= FLAG_START
        if self._muted:
            flags |= FLAG_MUTED
        frame = AudioFrame(
            seq=self._seq,
            timestamp_ms=int((self._clock() - self._started_at) * 1000),
            payload=pcm,
            flags=flags,
        )
        body = frame.encode()
        await self._session.send_audio(body, stream_id=PLAYBACK_STREAM)
        self._seq = (self._seq + 1) & 0xFFFF
        self.stats.sent += 1
        self.stats.bytes_sent += len(body)
        self.stats.last_timestamp_ms = frame.timestamp_ms

    async def stop_playback(self, reason: str = "stopped") -> None:
        """Stop capturing, tell the phone, and leave the session alone."""
        if self._playback.state == IDLE:
            return
        self._set_playback(STOPPING, reason)
        if self._source is not None:
            with contextlib.suppress(Exception):
                self._source.close()
        # Cancel the pumps *before* the STOP frame: otherwise a frame already in
        # the queue would be sent after it, and the phone would accept audio for a
        # stream it has been told to close.
        for task in (self._capture, self._sender):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._capture = self._sender = None
        self._queue = None
        # A STOP frame lets the phone drain its AudioTrack instead of clicking.
        with contextlib.suppress(Exception):
            if self._session.is_open:
                await self._send_frame(b"", flags=FLAG_STOP)
        await self._quiet_rpc("audio.playback.stop")
        self._source = None
        self._set_playback(IDLE, reason)

    # ------------------------------------------------------------------ #
    # phone microphone -> PC
    # ------------------------------------------------------------------ #
    async def start_mic(self) -> bool:
        """Ask the phone to capture, and play what arrives on a PC output device.

        The phone is the one that decides: it shows its own permission state and
        its own "allow the PC to use the microphone" toggle, and refuses here if
        either says no. That refusal is reported verbatim — guessing at it is how a
        user ends up staring at a silent stream.
        """
        if self._mic.state in (ACTIVE, STARTING):
            return self._mic.state == ACTIVE
        if not self._session.is_open:
            self._set_mic(ERROR, "not connected: the microphone needs a live session")
            return False
        self._set_mic(STARTING)
        try:
            reply = await self._session.call("audio.mic.start", {"format": self._fmt.as_dict()})
        except Exception as exc:  # noqa: BLE001
            self._set_mic(ERROR, _explain("the phone refused microphone capture", exc))
            await self._quiet_rpc("audio.mic.stop")
            return False

        granted = _granted_format(reply, self._fmt)
        self._fmt = granted
        self._mic.format = granted

        if self._sink_factory is not None:
            try:
                self._sink = self._sink_factory(granted)
            except AudioBackendError as exc:
                # The phone is already capturing; stop it rather than leaving a
                # microphone running that nobody can hear.
                await self._quiet_rpc("audio.mic.stop")
                self._set_mic(ERROR, str(exc))
                return False
        self._gate.reset(0)
        queue = self._session.subscribe_blobs(MIC_STREAM)
        self._receiver = asyncio.get_running_loop().create_task(
            self._receive_loop(queue), name="audio-receive"
        )
        self._set_mic(ACTIVE)
        return True

    async def _receive_loop(self, queue: asyncio.Queue[bytes | None]) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                body = await queue.get()
                if body is None:  # documented end-of-stream marker
                    self._set_mic(IDLE, "the session ended")
                    return
                try:
                    frame = decode_frame(body)
                except AudioFrameError as exc:
                    self.stats.invalid += 1
                    self.stats.last_error = str(exc)
                    continue
                self.stats.received += 1
                self.stats.bytes_received += len(body)
                self.stats.last_timestamp_ms = frame.timestamp_ms
                for ordered in self._gate.offer(frame):
                    if ordered.muted or not ordered.payload:
                        continue
                    if self._sink is None:
                        continue
                    try:
                        await loop.run_in_executor(None, self._sink.write, ordered.payload)
                    except AudioBackendError as exc:
                        # A dead *device* stops this direction and nothing else:
                        # the session, the camera and the speaker keep running.
                        self._set_mic(ERROR, str(exc))
                        return
                    except Exception as exc:  # noqa: BLE001
                        self._set_mic(ERROR, f"microphone device failed: {exc}")
                        return
        except asyncio.CancelledError:
            raise
        except AudioBackendError as exc:
            self._set_mic(ERROR, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._set_mic(ERROR, f"microphone stream stopped: {exc}")

    async def stop_mic(self, reason: str = "stopped") -> None:
        if self._mic.state == IDLE:
            return
        self._set_mic(STOPPING, reason)
        await self._quiet_rpc("audio.mic.stop")
        if self._receiver is not None and not self._receiver.done():
            self._receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._receiver
        self._receiver = None
        if self._sink is not None:
            with contextlib.suppress(Exception):
                self._sink.close()
            self._sink = None
        self._set_mic(IDLE, reason)

    # ------------------------------------------------------------------ #
    # controls
    # ------------------------------------------------------------------ #
    async def set_volume(self, percent: int) -> int:
        """0-100, applied on the phone (it owns the output stream)."""
        self._volume = max(0, min(100, int(percent)))
        await self._quiet_rpc("audio.playback.volume", {"percent": self._volume})
        self._notify()
        return self._volume

    async def set_mute(self, muted: bool) -> bool:
        """Mute the *outgoing* PC audio.

        Muting sends silence with the MUTED flag rather than stopping the stream, so
        timing survives and the phone can show "muted" instead of "disconnected".
        """
        self._muted = bool(muted)
        await self._quiet_rpc("audio.playback.mute", {"muted": self._muted})
        self._notify()
        return self._muted

    async def _quiet_rpc(self, method: str, params: dict[str, Any] | None = None) -> None:
        """A control RPC that must never raise: losing a call is not losing audio."""
        if not getattr(self._session, "is_open", False):
            return
        try:
            await self._session.call(method, params)
        except Exception as exc:  # noqa: BLE001
            log.info("audio control %s failed: %s", method, exc)

    # ------------------------------------------------------------------ #
    # teardown
    # ------------------------------------------------------------------ #
    async def close(self, reason: str = "session closed") -> None:
        """Stop both directions. Called by the manager on disconnect and shutdown."""
        with contextlib.suppress(Exception):
            await self.stop_playback(reason)
        with contextlib.suppress(Exception):
            await self.stop_mic(reason)


def _granted_format(reply: Any, fallback: AudioFormat) -> AudioFormat:
    """Read the phone's accepted format out of an RPC reply, defensively."""
    if isinstance(reply, dict):
        payload = reply.get("result", reply)
        if isinstance(payload, dict):
            fmt = payload.get("format")
            if isinstance(fmt, dict):
                try:
                    return AudioFormat.from_dict(fmt)
                except AudioFrameError:
                    log.warning("phone returned an unusable audio format: %s", fmt)
    return fallback


def _explain(prefix: str, exc: Exception) -> str:
    """Turn an RPC/transport failure into a sentence, keeping the code visible."""
    text = str(exc).strip() or type(exc).__name__
    return f"{prefix}: {text}"
