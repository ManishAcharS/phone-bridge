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

#: How long the loopback may produce nothing before the PC admits it is silent.
#: WASAPI loopback is driven by the endpoint: when nothing on this PC is
#: rendering, the capture stream delivers no packets, `read()` blocks, and the
#: phone reports ``rx 0 f/s`` while everything is healthy. This turns that into a
#: fact the status line can print instead of a "fault" nobody can fix.
PC_SILENT_AFTER_S = 0.6

#: How often the silence watcher looks at the capture clock.
SILENCE_TICK_S = 0.2

#: The queue target the PC asks the phone to hold, in ms. Three 20 ms frames:
#: one being written, one in the device, one for jitter. It was 200 ms.
DEFAULT_TARGET_MS = 60

#: What the Bluetooth A2DP path adds after the AudioTrack mixer, in ms, for the
#: codecs an ordinary phone negotiates. **Not measured**: Android exposes neither
#: the codec nor the Bluetooth buffer to an app (see `AudioEngine.a2dpCodec`), so
#: the honest answer is a range, printed as one.
A2DP_FLOOR_MS = (100, 250)

#: How often the PC asks the phone what its `AudioTrack` is doing. One small RPC
#: per second while a direction is running: `audio.playback.start` only says the
#: phone agreed to play, and nothing else in the protocol reports whether the
#: device ever consumed a sample. See :meth:`AudioLink.phone_status`.
PHONE_STATUS_INTERVAL_S = 1.0

#: Fields the PC keeps from `audio.status`. Anything else the phone sends is
#: ignored, so a newer APK cannot break an older PC.
_PHONE_AUDIO_KEYS = (
    "playback",
    "mic",
    "route",
    "device_name",
    "track_state",
    "buffered_ms",
    "buffer_ms",
    "underruns",
    "played_ms",
    "frames_in",
    "frames_written",
    "dropped",
    "last_error",
    # latency, per stage (docs/19): the queue the phone settled on, how deep it
    # is, whether it is adapting, the track buffer the device granted, and the
    # one-way probe when the PC armed it.
    "queue_target_frames",
    "queue_target_ms",
    "queue_depth",
    "adaptive",
    "adapt_grow",
    "adapt_shrink",
    "low_latency",
    "track_buffer_ms",
    "probe_state",
    "probe_latency_ms",
    "a2dp_codec",
    "a2dp_codec_note",
)


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
    #: The phone's own answer to `audio.status`, plus the rates the PC derives
    #: from two consecutive answers. Empty when the phone has not answered yet.
    phone: dict[str, Any] = field(default_factory=dict)
    #: True when playback is running and this PC has produced no audio for
    #: :data:`PC_SILENT_AFTER_S`. It is *not* an error: it means nothing is
    #: playing here, so the loopback device has nothing to hand over.
    pc_silent: bool = False

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
            "phone": dict(self.phone),
            "pc_silent": self.pc_silent,
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
        status_interval: float = PHONE_STATUS_INTERVAL_S,
        target_ms: int = DEFAULT_TARGET_MS,
        adaptive: bool = True,
        low_latency: bool = True,
        probe: bool = False,
        keep_alive: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._fmt = fmt or AudioFormat()
        self._source_factory = source_factory
        self._sink_factory = sink_factory
        self._on_change = on_change
        self._queue_size = max(2, queue_size)
        self._clock = clock
        # Latency policy for the phone's queue. `adaptive` and `low_latency` are
        # on because they are strictly better when they work and the phone falls
        # back safely when they do not.
        self._target_ms = max(20, int(target_ms))
        self._adaptive = bool(adaptive)
        self._low_latency = bool(low_latency)
        self._probe = bool(probe)
        self._keep_alive = bool(keep_alive)

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

        # The phone's own view of its output, refreshed once a second (see
        # `phone_status`). Rates are derived here because the phone sends
        # cumulative counters.
        self._status_interval = max(0.05, float(status_interval))
        self._status_task: asyncio.Task[None] | None = None
        self._phone: dict[str, Any] = {}
        self._phone_at: float | None = None
        self._phone_counters: dict[str, float] = {}

        # PC-side silence: when the last loopback frame arrived, and whether the
        # line is currently admitting that nothing is playing here.
        self.pc_silent = False
        self._captured_at: float | None = None
        self._silence_task: asyncio.Task[None] | None = None

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
            phone=self.phone_status,
            pc_silent=self.pc_silent,
        )

    @property
    def phone_status(self) -> dict[str, Any]:
        """What the *phone* reports about its own output.

        `playback == "active"` means the phone accepted the stream. It does not
        mean the phone played it: a muted stream, a route to a disconnected
        headset, or an `AudioTrack` that stopped consuming all look identical
        from here. This is the phone's answer to `audio.status` — its route, the
        track's state, the audio waiting in front of it, its underrun count —
        plus the two rates the PC can only compute from two answers:

        ``rx_fps``
            frames the phone received per second (what the link delivered).
        ``written_fps``
            frames it handed to `AudioTrack` per second. `rx_fps` above zero
            with `written_fps` at zero is the signature of a phone that is
            receiving a muted or unreachable stream.
        """
        return dict(self._phone)

    # ------------------------------------------------------------------ #
    # asking the phone what it is doing
    # ------------------------------------------------------------------ #
    def _ensure_status_poller(self) -> None:
        """Start the once-a-second `audio.status` poll, if it is not running."""
        if self._status_task is not None and not self._status_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop: nothing to poll on (plain construction)
            return
        self._status_task = loop.create_task(
            self._poll_phone_status(), name="audio-phone-status"
        )

    def _stop_status_poller(self) -> None:
        """Stop polling once neither direction can produce an answer."""
        if self._playback.state in (ACTIVE, STARTING) or self._mic.state in (
            ACTIVE,
            STARTING,
        ):
            return
        task, self._status_task = self._status_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _poll_phone_status(self) -> None:
        """Ask the phone for its own numbers, once a second.

        A debug aid must never break audio: every failure is swallowed and the
        loop keeps its cadence, so a phone that answers nothing simply leaves the
        PC's line showing what the PC knows.
        """
        try:
            while True:
                await asyncio.sleep(self._status_interval)
                try:
                    reply = await self._session.call("audio.status", {})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - never fatal
                    log.debug("audio.status failed: %s", exc)
                    continue
                self._absorb_phone_status(reply)
        except asyncio.CancelledError:
            raise

    def _absorb_phone_status(self, reply: Any) -> None:
        """Keep the phone's answer, and turn cumulative counters into rates."""
        payload = reply.get("result", reply) if isinstance(reply, dict) else None
        if not isinstance(payload, dict):
            return
        stats: dict[str, Any] = {
            key: payload[key]
            for key in _PHONE_AUDIO_KEYS
            if isinstance(payload.get(key), (int, float, str))
        }
        now = self._clock()
        elapsed = 0.0 if self._phone_at is None else now - self._phone_at
        if elapsed > 0:
            for name, key in (
                ("rx_fps", "frames_in"),
                ("written_fps", "frames_written"),
            ):
                value = payload.get(key)
                before = self._phone_counters.get(key)
                if not isinstance(value, (int, float)) or before is None:
                    continue
                delta = float(value) - before
                if delta < 0:  # the phone restarted its track: no rate yet
                    continue
                stats[name] = round(delta / elapsed, 1)
        for key in ("frames_in", "frames_written"):
            value = payload.get(key)
            if isinstance(value, (int, float)):
                self._phone_counters[key] = float(value)
        self._phone_at = now
        self._phone = stats
        self._notify()

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
        # The phone sizes its queue from `target_ms` and, when `probe` is set,
        # times the first frame it receives from arrival to the mixer. Older APKs
        # ignore the extra keys and keep their fixed eight-frame queue.
        params: dict[str, Any] = {
            "format": self._fmt.as_dict(),
            "target_ms": self._target_ms,
            "adaptive": self._adaptive,
            "low_latency": self._low_latency,
            "probe": self._probe,
        }
        try:
            reply = await self._session.call("audio.playback.start", params)
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
        self.pc_silent = False
        self._captured_at = self._clock()
        loop = asyncio.get_running_loop()
        self._capture = loop.create_task(self._capture_loop(), name="audio-capture")
        self._sender = loop.create_task(self._send_loop(), name="audio-send")
        self._silence_task = loop.create_task(self._silence_watch(), name="audio-silence")
        self._set_playback(ACTIVE)
        self._ensure_status_poller()
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
                self._mark_captured()
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

    def _mark_captured(self) -> None:
        """Audio moved: clear the "PC is silent" flag if it was up."""
        self._captured_at = self._clock()
        if self.pc_silent:
            self.pc_silent = False
            self._notify()

    async def _silence_watch(self) -> None:
        """Say "the PC is silent" out loud, and optionally keep the phone warm.

        WASAPI loopback capture is driven by the *endpoint*. When nothing on this
        PC is rendering audio, the loopback stream hands over no packets, so
        `read()` blocks and no frames are sent; the phone then reports ``rx 0 f/s``
        while every component is working. That is silence, not a fault, and the
        status line should say so.

        With ``keep_alive`` the watcher also sends silence at the frame cadence, so
        the phone's `AudioTrack` and the Bluetooth link stay warm and playback does
        not have to re-buffer the moment something on the PC does start playing.
        """
        try:
            while True:
                await asyncio.sleep(SILENCE_TICK_S)
                if self._playback.state not in (ACTIVE, STOPPING):
                    return
                last = self._captured_at
                if last is None:
                    continue
                if self._clock() - last < PC_SILENT_AFTER_S:
                    continue
                if not self.pc_silent:
                    self.pc_silent = True
                    log.debug(
                        "no loopback audio for %.1f s: the PC is not playing anything",
                        PC_SILENT_AFTER_S,
                    )
                    self._notify()
                if self._keep_alive:
                    await self._feed_silence(SILENCE_TICK_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a watcher may never break audio
            log.debug("silence watcher stopped: %s", exc)

    async def _feed_silence(self, window_s: float) -> None:
        """Push one silent frame per frame period for ``window_s``."""
        queue = self._queue
        if queue is None:
            return
        period = max(0.005, self._fmt.frame_ms / 1000.0)
        silence = b"\x00" * self._fmt.bytes_per_frame
        end = self._clock() + window_s
        while self._clock() < end and self._playback.state in (ACTIVE, STOPPING):
            try:
                queue.put_nowait(silence)
            except asyncio.QueueFull:
                break
            await asyncio.sleep(period)

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
        for task in (self._capture, self._sender, self._silence_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._capture = self._sender = None
        self._silence_task = None
        self._queue = None
        # A STOP frame lets the phone drain its AudioTrack instead of clicking.
        with contextlib.suppress(Exception):
            if self._session.is_open:
                await self._send_frame(b"", flags=FLAG_STOP)
        await self._quiet_rpc("audio.playback.stop")
        self._source = None
        self.pc_silent = False
        self._captured_at = None
        self._set_playback(IDLE, reason)
        self._stop_status_poller()

    # ------------------------------------------------------------------ #
    # measuring the speaker path
    # ------------------------------------------------------------------ #
    async def measure_latency(self, *, timeout_s: float = 6.0) -> dict[str, Any]:
        """Time one marked frame from this PC to the phone's mixer, per stage.

        Playback must already be running with ``probe=True`` (the CLI passes it),
        because the probe times the *first* frame after `audio.playback.start`.
        This method sends that frame itself — a short click — so the measurement
        is repeatable on a PC that happens to be silent.

        What is measured, and what is not:

        ======================  ==============================================
        stage                   how
        ======================  ==============================================
        PC capture block        one frame period, stated not guessed: the read
                                returns a whole frame, so a sound is up to
                                ``frame_ms`` old when it is sent
        PC -> phone             no jitter buffer on this path: one frame per
                                pull, sent as it is captured
        phone queue             the target the phone reports, in ms
        arrival -> mixer        the phone's own probe, on the phone's clock
        Bluetooth               **not measurable** from an app: reported as the
                                :data:`A2DP_FLOOR_MS` range, never as a number
        ======================  ==============================================

        Returns a dict of ``*_ms`` stages plus ``notes``; the CLI prints it.
        """
        if self._playback.state != ACTIVE:
            raise AudioError(
                "the speaker path is not running, so there is nothing to time: "
                "start playback (with probe) first"
            )
        click = self._click_pcm()
        sent_at = self._clock()
        await self._send_frame(click)
        probe: dict[str, Any] = {}
        deadline = sent_at + max(0.5, timeout_s)
        while self._clock() < deadline:
            try:
                reply = await self._session.call("audio.status", {})
            except Exception as exc:  # noqa: BLE001 - turned into a sentence below
                raise AudioError(_explain("the phone did not answer audio.status", exc)) from exc
            payload = reply.get("result", reply) if isinstance(reply, dict) else None
            if isinstance(payload, dict):
                self._absorb_phone_status(payload)
                done = payload.get("probe_state") == "done"
                latency = payload.get("probe_latency_ms")
                if done or (isinstance(latency, (int, float)) and latency >= 0):
                    probe = payload
                    break
            await asyncio.sleep(0.05)
        if not probe:
            raise AudioError(
                "the phone never reported the probe reaching its mixer: check that "
                "the APK is current (this needs the build with the latency probe)"
            )

        frame_ms = self._fmt.frame_ms
        capture_ms = frame_ms  # a sound is at most one read block old
        probe_ms = max(0, int(probe.get("probe_latency_ms") or 0))
        to_mixer = capture_ms + probe_ms
        route = str(probe.get("route") or "none")
        bluetooth = route == "bluetooth_a2dp"
        floor: tuple[int, int] | None = A2DP_FLOOR_MS if bluetooth else None
        report: dict[str, Any] = {
            "frame_ms": frame_ms,
            "capture_ms": capture_ms,
            "network_ms": 0,
            "queue_target_ms": int(probe.get("queue_target_ms") or 0),
            "queue_depth": int(probe.get("queue_depth") or 0),
            "arrival_to_mixer_ms": probe_ms,
            "track_buffer_ms": int(probe.get("track_buffer_ms") or probe.get("buffer_ms") or 0),
            "underruns": int(probe.get("underruns") or 0),
            "route": route,
            "device_name": str(probe.get("device_name") or ""),
            "low_latency": bool(probe.get("low_latency")),
            "adaptive": bool(probe.get("adaptive")),
            "a2dp_codec": str(probe.get("a2dp_codec") or ""),
            "a2dp_codec_note": str(probe.get("a2dp_codec_note") or ""),
            "to_mixer_ms": to_mixer,
            "bluetooth_floor_ms": list(floor) if floor else None,
            "to_ear_ms": [to_mixer + floor[0], to_mixer + floor[1]] if floor else [to_mixer, to_mixer],
        }
        notes: list[str] = []
        if not report["a2dp_codec"]:
            notes.append(
                "Android does not expose the A2DP codec or its buffer to an app, so the "
                "Bluetooth stage is a range, not a measurement"
                + (f" ({report['a2dp_codec_note']})" if report["a2dp_codec_note"] else "")
            )
        if bluetooth:
            notes.append(
                "A2DP is the floor here: even a perfect stream is re-buffered by the "
                "headphones and the phone's Bluetooth stack before it reaches the ear"
            )
        if report["underruns"]:
            notes.append(
                f"{report['underruns']} underruns so far: the adaptive queue will grow, "
                "which trades latency back for continuity"
            )
        report["notes"] = notes
        return report

    def _click_pcm(self) -> bytes:
        """One frame of a short, loud click: enough to be heard and to be timed."""
        import math
        import struct

        rate = self._fmt.sample_rate
        count = self._fmt.samples_per_frame
        # 6 ms of 1 kHz at 60 % amplitude, then silence: a click, not a tone.
        burst = max(1, int(rate * 0.006))
        samples = [
            int(0.6 * 32767 * math.sin(2 * math.pi * 1000 * i / rate)) if i < burst else 0
            for i in range(count)
        ]
        pcm = struct.pack(f"<{count}h", *samples)
        if self._fmt.channels == 2:
            pcm = b"".join(pcm[i : i + 2] * 2 for i in range(0, len(pcm), 2))
        return pcm

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
        self._ensure_status_poller()
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
        self._stop_status_poller()

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
        task, self._status_task = self._status_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


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
    """Turn an RPC/transport failure into a sentence, keeping the code visible.

    A real session turns an error reply into ``SessionError("code: message")``, so
    the code is usually already in the text. An in-process ``RpcError`` keeps it in
    an attribute instead, and a user looking the failure up in the docs/03 table
    needs it either way — so it is added when it is missing, not when it is
    convenient.
    """
    text = str(exc).strip() or type(exc).__name__
    code = getattr(exc, "code", "")
    if isinstance(code, str) and code and not text.startswith(f"{code}:"):
        text = f"{code}: {text}"
    return f"{prefix}: {text}"
