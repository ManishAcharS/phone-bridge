"""The audio plane's lifecycle: start, mute, volume, backpressure, teardown.

The fake session below stands in for a real authenticated session. That boundary
is deliberate: every test in this file runs with **no sound card and no phone**,
and it still covers the things that actually break audio — a refusal, a slow
link, a duplicate frame, a session that dies mid-stream.

Nothing here is a device test. The Windows capture/playback backends themselves
are covered only by their import guards (``backend_status``), and the real phone
path is described in docs/19-audio.md.
"""

from __future__ import annotations

import asyncio

import pytest

from phonebridge.audio.backends import AudioBackendError, NullSink, NullSource
from phonebridge.audio.frames import (
    FLAG_MUTED,
    FLAG_START,
    FLAG_STOP,
    AudioFormat,
    AudioFrame,
    decode_frame,
)
from phonebridge.audio.link import (
    ACTIVE,
    ERROR,
    IDLE,
    MIC_STREAM,
    PLAYBACK_STREAM,
    AudioLink,
)
from phonebridge.protocol.messages import RpcError

SMALL = AudioFormat(sample_rate=8_000, frame_ms=20)  # 320-byte frames: fast tests


def run(coro):
    return asyncio.run(coro)


async def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


class FakeSession:
    """An authenticated session with the two methods audio uses, and a log."""

    def __init__(self, *, fmt: AudioFormat | None = None) -> None:
        self.is_open = True
        self.calls: list[tuple[str, dict | None]] = []
        self.audio: list[tuple[int, bytes]] = []
        self.queues: dict[int, asyncio.Queue] = {}
        self.refuse: dict[str, Exception] = {}
        self.adjust_format: AudioFormat | None = fmt
        self.send_delay = 0.0

    async def call(self, method: str, params: dict | None = None):
        self.calls.append((method, params))
        refusal = self.refuse.get(method)
        if refusal is not None:
            raise refusal
        if method.startswith("audio.playback") or method.startswith("audio.mic"):
            fmt = (params or {}).get("format")
            if isinstance(fmt, dict) and self.adjust_format is not None:
                return {"result": {"ok": True, "format": self.adjust_format.as_dict()}}
            return {"result": {"ok": True, "format": fmt}}
        return {"result": {"ok": True}}

    async def send_audio(self, body: bytes, stream_id: int = 1) -> None:
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        if not self.is_open:
            raise ConnectionError("session closed")
        self.audio.append((stream_id, body))

    def subscribe_blobs(self, stream_id: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        self.queues[stream_id] = queue
        return queue

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]

    def frames(self, stream: int = PLAYBACK_STREAM) -> list[AudioFrame]:
        return [decode_frame(body) for sid, body in self.audio if sid == stream]


def link(session: FakeSession, **kwargs) -> AudioLink:
    kwargs.setdefault("fmt", SMALL)
    kwargs.setdefault("source_factory", lambda fmt: NullSource(fmt))
    return AudioLink(session, **kwargs)


# --------------------------------------------------------------------------- #
# PC audio -> phone
# --------------------------------------------------------------------------- #
def test_starting_playback_negotiates_then_sends_frames() -> None:
    async def scenario():
        session = FakeSession()
        audio = link(session)
        assert await audio.start_playback() is True
        assert audio.snapshot().playback == ACTIVE
        assert session.calls[0][0] == "audio.playback.start"
        assert session.calls[0][1]["format"] == SMALL.as_dict()
        assert await wait_for(lambda: len(session.audio) >= 3)
        frames = session.frames()
        assert frames[0].start is True, "the first frame must be marked START"
        assert [f.seq for f in frames[:3]] == [0, 1, 2]
        assert all(len(f.payload) == SMALL.bytes_per_frame for f in frames)
        await audio.close()

    run(scenario())


def test_starting_twice_does_not_create_a_second_stream() -> None:
    async def scenario():
        session = FakeSession()
        audio = link(session)
        await audio.start_playback()
        await audio.start_playback()
        assert session.methods().count("audio.playback.start") == 1
        await audio.close()

    run(scenario())


def test_a_refusal_from_the_phone_is_reported_not_raised() -> None:
    async def scenario():
        session = FakeSession()
        session.refuse["audio.playback.start"] = RpcError(
            "no_output", "the phone has no audio output route"
        )
        audio = link(session)
        assert await audio.start_playback() is False
        snap = audio.snapshot()
        assert snap.playback == ERROR
        assert "no audio output route" in snap.detail
        assert "audio.playback.stop" in session.methods(), "a refusal must not leave a route open"
        assert session.audio == []

    run(scenario())


def test_a_capture_backend_that_cannot_open_stops_the_direction_cleanly() -> None:
    def broken(fmt: AudioFormat):
        raise AudioBackendError("soundcard is not installed")

    async def scenario():
        session = FakeSession()
        audio = link(session, source_factory=broken)
        assert await audio.start_playback() is False
        assert "soundcard is not installed" in audio.snapshot().detail
        assert "audio.playback.stop" in session.methods()
        await audio.close()

    run(scenario())


def test_without_a_capture_backend_the_message_says_what_to_install() -> None:
    async def scenario():
        session = FakeSession()
        audio = AudioLink(session, fmt=SMALL, source_factory=None)
        assert await audio.start_playback() is False
        assert "soundcard" in audio.snapshot().detail
        await audio.close()

    run(scenario())


def test_stop_sends_a_stop_frame_then_the_rpc_and_returns_to_idle() -> None:
    async def scenario():
        session = FakeSession()
        audio = link(session)
        await audio.start_playback()
        await wait_for(lambda: len(session.audio) >= 2)
        await audio.stop_playback("user pressed stop")
        frames = session.frames()
        assert frames[-1].stop is True
        assert "audio.playback.stop" in session.methods()
        assert audio.snapshot().playback == IDLE
        sent = len(session.audio)
        await asyncio.sleep(0.02)
        assert len(session.audio) == sent, "no frames may be sent after stop"

    run(scenario())


def test_mute_sends_silence_with_the_flag_rather_than_stopping_the_stream() -> None:
    async def scenario():
        session = FakeSession()
        audio = link(session)
        assert audio.muted is True, "conservative default: muted until asked"
        await audio.start_playback()
        await wait_for(lambda: len(session.audio) >= 1)
        assert all(f.muted for f in session.frames())
        assert all(set(f.payload) <= {0} for f in session.frames())

        await audio.set_mute(False)
        mark = len(session.audio)
        await wait_for(lambda: len(session.audio) > mark)
        newest = session.frames()[-1]
        assert newest.muted is False
        assert "audio.playback.mute" in session.methods()
        await audio.close()

    run(scenario())


def test_volume_is_clamped_and_sent_to_the_phone() -> None:
    async def scenario():
        session = FakeSession()
        audio = link(session)
        assert await audio.set_volume(250) == 100
        assert await audio.set_volume(-5) == 0
        assert await audio.set_volume(35) == 35
        sent = [p for m, p in session.calls if m == "audio.playback.volume"]
        assert [p["percent"] for p in sent] == [100, 0, 35]
        assert audio.snapshot().volume == 35
        await audio.close()

    run(scenario())


def test_a_full_queue_drops_the_oldest_frame_and_says_so() -> None:
    """The link must bound latency, not grow a buffer while the network complains."""

    async def scenario():
        session = FakeSession()
        session.send_delay = 0.01  # a link slower than the sound card
        audio = link(session, queue_size=2)
        await audio.start_playback()
        assert await wait_for(lambda: audio.stats.dropped_full > 0, timeout=3)
        assert audio.snapshot().playback == ACTIVE, "dropping a frame is not an error"
        await audio.close()

    run(scenario())


def test_the_phone_may_adjust_the_format_and_capture_follows_it() -> None:
    async def scenario():
        session = FakeSession(fmt=AudioFormat(sample_rate=16_000, frame_ms=20))
        seen: list[AudioFormat] = []

        def factory(fmt: AudioFormat):
            seen.append(fmt)
            return NullSource(fmt)

        audio = AudioLink(session, fmt=SMALL, source_factory=factory)
        await audio.start_playback()
        assert seen == [AudioFormat(sample_rate=16_000, frame_ms=20)]
        assert audio.format.sample_rate == 16_000
        assert await wait_for(lambda: len(session.audio) >= 1)
        assert len(session.frames()[0].payload) == 640
        await audio.close()

    run(scenario())


def test_a_session_that_dies_mid_stream_stops_without_raising() -> None:
    async def scenario():
        session = FakeSession()
        audio = link(session)
        await audio.start_playback()
        await wait_for(lambda: len(session.audio) >= 1)
        session.is_open = False
        assert await wait_for(lambda: audio.snapshot().playback == ERROR)
        await audio.close()

    run(scenario())


# --------------------------------------------------------------------------- #
# phone microphone -> PC
# --------------------------------------------------------------------------- #
def test_microphone_start_negotiates_and_plays_only_valid_frames() -> None:
    async def scenario():
        session = FakeSession()
        sink = NullSink()
        audio = link(session, sink_factory=lambda fmt: sink)
        assert await audio.start_mic() is True
        assert audio.snapshot().mic == ACTIVE
        assert session.calls[0][0] == "audio.mic.start"

        queue = session.queues[MIC_STREAM]
        queue.put_nowait(AudioFrame(seq=0, timestamp_ms=0, payload=b"\x01\x00" * 4).encode())
        queue.put_nowait(b"\xff" * 3)  # malformed: must be counted, not played
        queue.put_nowait(AudioFrame(seq=1, timestamp_ms=20, payload=b"\x02\x00" * 4).encode())
        assert await wait_for(lambda: sink.written == 2)
        assert audio.stats.invalid == 1
        assert audio.stats.received == 2
        assert audio.stats.bytes_received > 0
        await audio.close()

    run(scenario())


def test_out_of_order_microphone_frames_are_reordered_before_playout() -> None:
    async def scenario():
        session = FakeSession()
        played: list[bytes] = []

        class Sink:
            def write(self, payload: bytes) -> None:
                played.append(payload)

            def close(self) -> None:
                return None

        audio = link(session, sink_factory=lambda fmt: Sink())
        await audio.start_mic()
        queue = session.queues[MIC_STREAM]
        first = b"\x01\x00" * 4
        second = b"\x02\x00" * 4
        queue.put_nowait(AudioFrame(seq=0, timestamp_ms=0, payload=first).encode())
        queue.put_nowait(AudioFrame(seq=2, timestamp_ms=40, payload=b"\x03\x00" * 4).encode())
        queue.put_nowait(AudioFrame(seq=1, timestamp_ms=20, payload=second).encode())
        assert await wait_for(lambda: len(played) == 3)
        assert played[0] == first
        assert played[1] == second, "the held frame is released in order"
        await audio.close()

    run(scenario())


def test_a_permission_refusal_on_the_phone_is_shown_verbatim() -> None:
    async def scenario():
        session = FakeSession()
        session.refuse["audio.mic.start"] = RpcError(
            "mic_denied",
            "microphone permission was refused on the phone, or the PC microphone "
            "toggle is off in the PhoneBridge app",
        )
        audio = link(session)
        assert await audio.start_mic() is False
        snap = audio.snapshot()
        assert snap.mic == ERROR
        assert "microphone permission was refused" in snap.mic_detail
        assert snap.playback == IDLE, "one direction failing must not touch the other"
        await audio.close()

    run(scenario())


def test_a_microphone_that_cannot_be_monitored_is_stopped_on_the_phone() -> None:
    def broken_sink(fmt: AudioFormat):
        raise AudioBackendError("sounddevice is not installed")

    async def scenario():
        session = FakeSession()
        audio = link(session, sink_factory=broken_sink)
        assert await audio.start_mic() is False
        assert "sounddevice is not installed" in audio.snapshot().mic_detail
        assert "audio.mic.stop" in session.methods(), "never leave a live mic behind"
        await audio.close()

    run(scenario())


def test_microphone_needs_a_live_session_and_says_so() -> None:
    async def scenario():
        session = FakeSession()
        session.is_open = False
        audio = link(session)  # no sink needed: the check happens first
        assert await audio.start_mic() is False
        assert "not connected" in audio.snapshot().mic_detail
        assert session.calls == []

    run(scenario())


def test_the_end_of_the_microphone_stream_returns_the_direction_to_idle() -> None:
    async def scenario():
        session = FakeSession()
        audio = link(session, sink_factory=lambda fmt: NullSink())
        await audio.start_mic()
        session.queues[MIC_STREAM].put_nowait(None)  # documented end-of-stream marker
        assert await wait_for(lambda: audio.snapshot().mic == IDLE)
        await audio.close()

    run(scenario())


def test_stop_mic_tells_the_phone_and_closes_the_sink() -> None:
    async def scenario():
        session = FakeSession()
        sink = NullSink()
        audio = link(session, sink_factory=lambda fmt: sink)
        await audio.start_mic()
        await audio.stop_mic("user pressed stop")
        assert "audio.mic.stop" in session.methods()
        assert audio.snapshot().mic == IDLE
        assert sink.written == 0
        await audio.close()

    run(scenario())


# --------------------------------------------------------------------------- #
# both at once, and teardown
# --------------------------------------------------------------------------- #
def test_both_directions_run_at_the_same_time_without_interfering() -> None:
    async def scenario():
        session = FakeSession()
        sink = NullSink()
        audio = link(session, sink_factory=lambda fmt: sink)
        assert await audio.start_playback() is True
        assert await audio.start_mic() is True
        session.queues[MIC_STREAM].put_nowait(
            AudioFrame(seq=0, timestamp_ms=0, payload=b"\x01\x00" * 4).encode()
        )
        assert await wait_for(lambda: sink.written == 1 and len(session.audio) >= 1)
        snap = audio.snapshot()
        assert snap.playback == ACTIVE and snap.mic == ACTIVE
        assert snap.stats["sent"] >= 1 and snap.stats["received"] >= 1
        await audio.close()
        assert audio.snapshot().playback == IDLE
        assert audio.snapshot().mic == IDLE

    run(scenario())


def test_close_is_safe_when_nothing_was_ever_started() -> None:
    async def scenario():
        session = FakeSession()
        audio = link(session)
        await audio.close()
        assert audio.snapshot().playback == IDLE
        assert audio.snapshot().mic == IDLE

    run(scenario())


def test_the_snapshot_is_a_copy_the_window_can_keep() -> None:
    async def scenario():
        session = FakeSession()
        audio = link(session)
        snap = audio.snapshot()
        snap.volume = 99
        assert audio.snapshot().volume == 40
        assert audio.snapshot().to_dict()["muted"] is True
        await audio.close()

    run(scenario())


@pytest.mark.parametrize("flag", [FLAG_START, FLAG_STOP, FLAG_MUTED])
def test_every_flag_survives_the_wire(flag: int) -> None:
    frame = AudioFrame(seq=1, timestamp_ms=20, payload=b"\x00\x00", flags=flag)
    assert decode_frame(frame.encode()).flags == flag


# --------------------------------------------------------------------------- #
# a dead PC device must not cost the session
# --------------------------------------------------------------------------- #
def test_a_failing_sink_stops_the_microphone_and_leaves_the_session_open() -> None:
    """The device error the user hit must not become a dropped connection.

    Writing into a real device can fail at any moment (unplugged, taken by an
    exclusive-mode app, VB-CABLE removed). The failure is reported on the
    microphone direction; the session keeps running, so the camera and the
    speaker on the same session are untouched.
    """

    async def scenario():
        session = FakeSession()
        calls: list[str] = []

        class DyingSink:
            def write(self, payload: bytes) -> None:
                calls.append("write")
                raise AudioBackendError("writing to the microphone device failed: dtype mismatch")

            def close(self) -> None:
                return None

        audio = link(session, sink_factory=lambda fmt: DyingSink())
        await audio.start_playback()  # the other direction, on the same session
        assert await audio.start_mic() is True

        session.queues[MIC_STREAM].put_nowait(
            AudioFrame(seq=0, timestamp_ms=0, payload=b"\x01\x00" * 4).encode()
        )
        assert await wait_for(lambda: audio.snapshot().mic == ERROR)

        snapshot = audio.snapshot()
        assert "dtype mismatch" in snapshot.mic_detail
        assert snapshot.playback == ACTIVE, "the speaker direction must not be affected"
        assert session.is_open is True, "a dead device is not a dead session"

        # The phone's capture is stopped, so it is not streaming into nothing.
        await audio.stop_mic()
        assert "audio.mic.stop" in session.methods()
        await audio.close()

    run(scenario())


def test_an_unexpected_sink_error_is_reported_the_same_way() -> None:
    """A sink is third-party code: it may raise anything at all."""

    async def scenario():
        session = FakeSession()

        class ExplodingSink:
            def write(self, payload: bytes) -> None:
                raise RuntimeError("PortAudio: device invalidated")

            def close(self) -> None:
                return None

        audio = link(session, sink_factory=lambda fmt: ExplodingSink())
        await audio.start_mic()
        session.queues[MIC_STREAM].put_nowait(
            AudioFrame(seq=0, timestamp_ms=0, payload=b"\x01\x00" * 4).encode()
        )

        assert await wait_for(lambda: audio.snapshot().mic == ERROR)
        assert "device invalidated" in audio.snapshot().mic_detail
        assert session.is_open is True
        await audio.close()

    run(scenario())
