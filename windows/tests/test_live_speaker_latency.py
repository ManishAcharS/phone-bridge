"""Speaker latency: every stage measured, and the ones nobody can measure named.

The half-second delay to Bluetooth headphones was five stages added together, and
reading the code could only ever tell you the size of three of them. These tests
pin the parts that live on the PC:

* the queue target is *sent* to the phone instead of decorating the status line,
* a PC that is not playing anything reports ``pc silent`` rather than looking like
  a broken link (WASAPI loopback delivers no packets from an idle endpoint),
* ``--keep-alive`` keeps frames flowing from a silent PC,
* ``--latency-test`` measures the stages it can and prints the Bluetooth stage as
  the range it is, because Android will not hand over the codec or its buffer.

No sound card, no phone, no Bluetooth: a fake session and a fake source, exactly
like the rest of the audio suite.
"""

from __future__ import annotations

import asyncio

from phonebridge.audio.frames import AudioFormat, decode_frame
from phonebridge.audio.link import (
    A2DP_FLOOR_MS,
    ACTIVE,
    DEFAULT_TARGET_MS,
    IDLE,
    AudioError,
    AudioLink,
)
from phonebridge.live.config import DEFAULT_SPEAKER_LATENCY_MS, SpeakerPlan
from phonebridge.live.runner import LiveStatus

SMALL = AudioFormat(sample_rate=8_000, frame_ms=20)  # 320-byte frames: fast tests


def run(coro):
    return asyncio.run(coro)


class SilentSource:
    """A capture device that never produces a frame: an idle WASAPI endpoint."""

    def __init__(self) -> None:
        self.reads = 0
        self.closed = False

    def read(self) -> bytes:
        self.reads += 1
        # Blocking, like the real thing: the loopback read does not return.
        import time

        time.sleep(0.05)
        return b""

    def close(self) -> None:
        self.closed = True


class TalkingSource:
    """A capture device with something playing, then silence."""

    def __init__(self, fmt: AudioFormat, frames: int = 1000) -> None:
        self.fmt = fmt
        self.left = frames
        self.closed = False

    def read(self) -> bytes:
        import time

        time.sleep(0.002)
        if self.left <= 0:
            return b""
        self.left -= 1
        return b"\x01\x00" * (self.fmt.bytes_per_frame // 2)

    def close(self) -> None:
        self.closed = True


class PhoneSession:
    """A session that answers `audio.status` with whatever the test tells it."""

    def __init__(self, status: dict | None = None) -> None:
        self.is_open = True
        self.calls: list[tuple[str, dict | None]] = []
        self.audio: list[tuple[int, bytes]] = []
        self.status = dict(status or {})
        self.status_calls = 0

    async def call(self, method: str, params: dict | None = None):
        self.calls.append((method, params))
        if method == "audio.status":
            self.status_calls += 1
            return {"result": dict(self.status)}
        return {"result": {"ok": True, "format": (params or {}).get("format")}}

    async def send_audio(self, body: bytes, stream_id: int = 1) -> None:
        self.audio.append((stream_id, body))

    def start_params(self) -> dict:
        return next(params for method, params in self.calls if method == "audio.playback.start")


def _link(session, source=None, **kwargs) -> AudioLink:
    return AudioLink(
        session,
        fmt=SMALL,
        source_factory=(lambda fmt: source) if source is not None else None,
        clock=kwargs.pop("clock", None) or __import__("time").monotonic,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# the target the PC asks for
# --------------------------------------------------------------------------- #
def test_start_playback_states_its_latency_budget():
    """The target must reach the phone, or the phone keeps its own default."""

    async def scenario() -> None:
        session = PhoneSession()
        link = _link(session, TalkingSource(SMALL), target_ms=40)
        assert await link.start_playback()
        params = session.start_params()
        assert params["target_ms"] == 40
        assert params["adaptive"] is True
        assert params["low_latency"] is True
        assert params["probe"] is False
        await link.stop_playback()

    run(scenario())


def test_an_older_phone_still_gets_a_valid_start():
    """The extra keys are additive: the format is still where it always was."""

    async def scenario() -> None:
        session = PhoneSession()
        link = _link(session, TalkingSource(SMALL))
        await link.start_playback()
        params = session.start_params()
        assert params["format"] == SMALL.as_dict()
        assert params["target_ms"] == DEFAULT_TARGET_MS
        await link.stop_playback()

    run(scenario())


def test_the_default_budget_is_three_frames_not_ten():
    """200 ms was the number that made the delay; 60 is three 20 ms frames."""
    assert DEFAULT_TARGET_MS == 60
    assert DEFAULT_SPEAKER_LATENCY_MS == DEFAULT_TARGET_MS
    plan = SpeakerPlan()
    assert plan.latency_ms == 60
    assert plan.latency_test is False
    assert plan.keep_alive is False


def test_the_plan_carries_the_test_and_keepalive_flags():
    plan = SpeakerPlan(latency_test=True, keep_alive=True)
    payload = plan.as_dict()
    assert payload["latency_test"] is True
    assert payload["keep_alive"] is True


# --------------------------------------------------------------------------- #
# "the PC is silent" is not a fault
# --------------------------------------------------------------------------- #
def test_an_idle_loopback_reports_pc_silent_not_an_error():
    """The `rx 0 f/s` the user saw: nothing is playing here, nothing is broken."""

    async def scenario() -> None:
        session = PhoneSession()
        link = _link(session, SilentSource())
        await link.start_playback()
        assert not link.pc_silent
        for _ in range(200):
            if link.pc_silent:
                break
            await asyncio.sleep(0.02)
        snapshot = link.snapshot()
        assert snapshot.pc_silent is True
        assert snapshot.playback == ACTIVE, "silence is not an error state"
        assert snapshot.to_dict()["pc_silent"] is True
        await link.stop_playback()

    run(scenario())


def test_audio_flowing_clears_the_silent_flag():
    async def scenario() -> None:
        session = PhoneSession()
        source = TalkingSource(SMALL, frames=6)
        link = _link(session, source)
        await link.start_playback()
        for _ in range(200):
            if link.pc_silent:
                break
            await asyncio.sleep(0.02)
        assert link.pc_silent

        # Something starts playing on the PC.
        source.left = 500
        for _ in range(200):
            if not link.pc_silent:
                break
            await asyncio.sleep(0.02)
        assert link.pc_silent is False
        await link.stop_playback()

    run(scenario())


def test_stopping_playback_forgets_the_silence():
    async def scenario() -> None:
        session = PhoneSession()
        link = _link(session, SilentSource())
        await link.start_playback()
        for _ in range(200):
            if link.pc_silent:
                break
            await asyncio.sleep(0.02)
        assert link.pc_silent
        await link.stop_playback()
        assert link.snapshot().pc_silent is False

    run(scenario())


def test_keep_alive_feeds_silence_from_a_silent_pc():
    """Silence at the frame cadence: the phone's track must never run dry."""

    async def scenario() -> None:
        session = PhoneSession()
        link = _link(session, SilentSource(), keep_alive=True)
        await link.start_playback()
        for _ in range(300):
            if len(session.audio) >= 12:
                break
            await asyncio.sleep(0.02)
        assert len(session.audio) >= 12, "keep-alive sent no frames from an idle PC"
        body = session.audio[-1][1]
        assert decode_frame(body).payload == b"\x00" * SMALL.bytes_per_frame
        await link.stop_playback()

    run(scenario())


def test_without_keep_alive_nothing_is_invented():
    async def scenario() -> None:
        session = PhoneSession()
        link = _link(session, SilentSource())
        await link.start_playback()
        await asyncio.sleep(1.2)
        assert session.audio == [], "the PC must not send audio nobody asked for"
        await link.stop_playback()

    run(scenario())


# --------------------------------------------------------------------------- #
# the status line
# --------------------------------------------------------------------------- #
def test_the_status_line_says_pc_silent_next_to_rx_zero():
    status = LiveStatus(
        connected=True,
        speaker_state="active",
        latency_ms=60,
        speaker_stats={"rx_fps": 0.0, "written_fps": 0.0, "route": "bluetooth_a2dp"},
        pc_silent=True,
    )
    line = status.line()
    assert "pc silent" in line
    assert "rx 0 f/s" in line
    assert status.to_dict()["speaker"]["pc_silent"] is True


def test_pc_silent_is_only_printed_while_playback_is_active():
    """A stopped stream is not silent, it is stopped: say nothing extra."""
    status = LiveStatus(connected=True, speaker_state="idle", pc_silent=True)
    assert "pc silent" not in status.line()


def test_the_phone_target_is_printed_next_to_the_live_depth():
    status = LiveStatus(
        connected=True,
        speaker_state="active",
        latency_ms=60,
        speaker_stats={"buffered_ms": 40, "queue_target_ms": 60, "rx_fps": 48.0},
    )
    line = status.line()
    assert "pc-buf 60 ms" in line
    assert "buf 40 ms" in line
    assert "target 60 ms" in line


# --------------------------------------------------------------------------- #
# the latency test itself
# --------------------------------------------------------------------------- #
PROBE_STATUS = {
    "playback": "active",
    "probe_state": "done",
    "probe_latency_ms": 74,
    "queue_target_ms": 60,
    "queue_target_frames": 3,
    "queue_depth": 1,
    "track_buffer_ms": 40,
    "buffer_ms": 40,
    "underruns": 0,
    "route": "bluetooth_a2dp",
    "device_name": "WH-1000XM4",
    "low_latency": True,
    "adaptive": True,
    "a2dp_codec": "",
    "a2dp_codec_note": "codec not exposed to apps on this Android version",
}


def test_measure_latency_sends_a_click_and_reports_each_stage():
    async def scenario() -> None:
        session = PhoneSession(PROBE_STATUS)
        link = _link(session, TalkingSource(SMALL))
        await link.start_playback()
        report = await link.measure_latency(timeout_s=2.0)
        await link.stop_playback()

        # The click is a real frame, sent on the playback stream before the poll.
        assert session.audio, "no click was sent"
        frame = decode_frame(session.audio[0][1])
        assert len(frame.payload) == SMALL.bytes_per_frame
        assert any(b != 0 for b in frame.payload), "the click is silent"

        assert report["capture_ms"] == SMALL.frame_ms
        assert report["network_ms"] == 0
        assert report["queue_target_ms"] == 60
        assert report["arrival_to_mixer_ms"] == 74
        assert report["to_mixer_ms"] == SMALL.frame_ms + 74

    run(scenario())


def test_the_bluetooth_stage_is_a_range_never_a_number():
    async def scenario() -> dict:
        session = PhoneSession(PROBE_STATUS)
        link = _link(session, TalkingSource(SMALL))
        await link.start_playback()
        report = await link.measure_latency(timeout_s=2.0)
        await link.stop_playback()
        return report

    report = run(scenario())
    assert report["route"] == "bluetooth_a2dp"
    assert report["bluetooth_floor_ms"] == list(A2DP_FLOOR_MS)
    low, high = report["to_ear_ms"]
    assert low == report["to_mixer_ms"] + A2DP_FLOOR_MS[0]
    assert high == report["to_mixer_ms"] + A2DP_FLOOR_MS[1]
    assert high > low, "a point estimate for A2DP would be a guess"
    assert report["a2dp_codec"] == ""
    assert any("A2DP" in note for note in report["notes"])
    assert any("codec" in note for note in report["notes"])


def test_a_wired_route_has_no_bluetooth_stage():
    async def scenario() -> dict:
        session = PhoneSession({**PROBE_STATUS, "route": "wired_headphones"})
        link = _link(session, TalkingSource(SMALL))
        await link.start_playback()
        report = await link.measure_latency(timeout_s=2.0)
        await link.stop_playback()
        return report

    report = run(scenario())
    assert report["bluetooth_floor_ms"] is None
    assert report["to_ear_ms"] == [report["to_mixer_ms"], report["to_mixer_ms"]]


def test_underruns_are_reported_as_a_latency_trade():
    async def scenario() -> dict:
        session = PhoneSession({**PROBE_STATUS, "underruns": 7})
        link = _link(session, TalkingSource(SMALL))
        await link.start_playback()
        report = await link.measure_latency(timeout_s=2.0)
        await link.stop_playback()
        return report

    report = run(scenario())
    assert report["underruns"] == 7
    assert any("underruns" in note for note in report["notes"])


def test_a_probe_that_never_arrives_is_an_error_with_a_sentence():
    """An old APK has no probe: say that, do not time zero and look good."""

    async def scenario() -> str:
        session = PhoneSession({"playback": "active", "probe_state": "idle"})
        link = _link(session, TalkingSource(SMALL))
        await link.start_playback()
        try:
            await link.measure_latency(timeout_s=0.3)
        except AudioError as exc:
            return str(exc)
        finally:
            await link.stop_playback()
        raise AssertionError("measuring without a probe must not succeed")

    message = run(scenario())
    assert "probe" in message
    assert "APK" in message


def test_measuring_a_stopped_stream_is_refused():
    async def scenario() -> str:
        session = PhoneSession(PROBE_STATUS)
        link = _link(session, TalkingSource(SMALL))
        try:
            await link.measure_latency(timeout_s=0.2)
        except AudioError as exc:
            return str(exc)
        raise AssertionError("nothing is running, so nothing may be measured")

    message = run(scenario())
    assert "not running" in message


def test_the_click_is_one_frame_of_sound_then_silence():
    """A click, not a tone: it must be short enough to time and loud enough to hear."""
    link = _link(PhoneSession(), None)
    pcm = link._click_pcm()
    assert len(pcm) == SMALL.bytes_per_frame
    import struct

    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    loud = [s for s in samples if abs(s) > 1000]
    assert loud, "the click is inaudible"
    assert len(loud) < len(samples) / 2, "that is a tone, not a click"
    assert samples[-1] == 0, "the click ends in silence so it cannot click off"


def test_the_playback_stop_path_leaves_nothing_polling():
    async def scenario() -> AudioLink:
        session = PhoneSession()
        link = _link(session, TalkingSource(SMALL))
        await link.start_playback()
        await link.stop_playback()
        assert link.snapshot().playback == IDLE
        return link

    run(scenario())


# --------------------------------------------------------------------------- #
# the build id: "the APK is old" has to be readable, not guessed
# --------------------------------------------------------------------------- #
def test_the_latency_failure_names_the_phone_build():
    """The one number that turns "it does not work" into "install the new APK"."""
    import pytest

    async def scenario():
        session = PhoneSession({"build_id": "0.9.0 (7) deadbeef", "playback": "playing"})
        link = _link(session, SilentSource(), probe=True)
        await link.start_playback()
        try:
            with pytest.raises(AudioError) as caught:
                await link.measure_latency(timeout_s=0.6)
            return str(caught.value)
        finally:
            await link.stop_playback()

    message = run(scenario())

    assert "never reported the probe" in message
    assert "0.9.0 (7) deadbeef" in message, "the build is the diagnosis, so it belongs here"


def test_the_latency_failure_says_when_the_apk_reports_no_build():
    import pytest

    async def scenario():
        session = PhoneSession({"playback": "playing"})  # an APK older than the field
        link = _link(session, SilentSource(), probe=True)
        await link.start_playback()
        try:
            with pytest.raises(AudioError) as caught:
                await link.measure_latency(timeout_s=0.6)
            return str(caught.value)
        finally:
            await link.stop_playback()

    message = run(scenario())

    assert "no build id at all" in message
    assert "predates" in message, "say what an absent build id means"


def test_keep_alive_paces_at_the_frame_period_not_at_twice_it():
    """The reported bug: ~20 frames/s from a 20 ms frame, so the track starved.

    Fifty frames a second is what an AudioTrack at 20 ms consumes; anything below
    that underruns continuously during silence. The feed used to spend half of
    every cycle inside the silence watcher, which is exactly 20 a second.
    """
    import time

    async def scenario():
        session = PhoneSession()
        link = _link(session, SilentSource(), keep_alive=True)
        await link.start_playback()
        try:
            for _ in range(300):
                if link.pc_silent:
                    break
                await asyncio.sleep(0.02)
            assert link.pc_silent
            before = len(session.audio)
            started = time.monotonic()
            await asyncio.sleep(1.2)
            elapsed = time.monotonic() - started
            return len(session.audio) - before, elapsed
        finally:
            await link.stop_playback()

    sent, elapsed = run(scenario())
    rate = sent / elapsed

    assert rate >= 40, (
        f"keep-alive sent {sent} frames in {elapsed:.2f} s ({rate:.0f}/s); the phone "
        "consumes 50 a second at 20 ms and underruns below that"
    )
