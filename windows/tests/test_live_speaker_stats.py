"""Is the phone *playing*? The numbers that answer it, and the tone that proves it.

The user's report: `phonebridge speaker` printed ``speaker active 200 ms`` and
nothing else, so there was no way to tell whether the phone made a sound. It
could not: 200 ms was the buffer the PC was *configured* to send — a number that
reads the same when the phone plays, when it is muted, when Android routed the
track to a headset nobody is wearing, and when the track is not consuming at all.

Three things were added, and this file tests each one at the boundary that
matters:

* the phone reports its own output through ``audio.status`` (route, AudioTrack
  state, buffer fill, underruns, played ms) and the PC polls it once a second;
* the status line prints those numbers, and says ``pc-buf`` for the one number
  that was always about the PC;
* ``--test-tone`` sends 440 Hz instead of loopback capture, so the phone can be
  tested with no sound card, no PyAudioWPatch and nothing playing on the PC.

What this file cannot show is that a real phone makes a noise. That is
``docs/26`` step 3, and it needs the phone.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import phonebridge.live.runner as runner_module
from phonebridge.audio.backends import NullSource
from phonebridge.audio.frames import FLAG_MUTED, AudioFormat, decode_frame
from phonebridge.audio.link import ACTIVE, AudioLink
from phonebridge.audio.tone import ToneSource
from phonebridge.cli.live import _report_failures
from phonebridge.live.config import LivePlan, SpeakerPlan
from phonebridge.live.runner import LiveRunner, LiveStatus
from phonebridge.protocol.messages import RpcError

SMALL = AudioFormat(sample_rate=8_000, frame_ms=20)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# a phone that answers audio.status
# --------------------------------------------------------------------------- #
class FakePhone:
    """An authenticated session whose phone reports what its AudioTrack is doing."""

    def __init__(
        self,
        *,
        status: dict[str, Any] | None = None,
        refuse_playback: Exception | None = None,
        status_fails: bool = False,
    ) -> None:
        self.is_open = True
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.audio: list[tuple[int, bytes]] = []
        self.queues: dict[int, asyncio.Queue] = {}
        self.status = dict(status or {})
        self.refuse_playback = refuse_playback
        self.status_fails = status_fails
        self.frames_in = 0

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, params))
        if method == "audio.playback.start" and self.refuse_playback is not None:
            raise self.refuse_playback
        if method == "audio.status":
            if self.status_fails:
                raise ConnectionError("the phone stopped answering")
            # The phone's own counters advance the way a playing track's do.
            self.frames_in += 1
            payload = {"playback": "active", "frames_in": self.frames_in}
            payload.update(self.status)
            return {"result": payload}
        if method.startswith("audio."):
            return {"result": {"ok": True, "format": (params or {}).get("format")}}
        return {"result": {"ok": True}}

    async def send_audio(self, body: bytes, stream_id: int = 1) -> None:
        self.audio.append((stream_id, body))

    def subscribe_blobs(self, stream_id: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        self.queues[stream_id] = queue
        return queue

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]

    def playback_frames(self) -> list[Any]:
        return [decode_frame(body) for sid, body in self.audio if sid == 1]


def link(session: FakePhone, **kwargs: Any) -> AudioLink:
    kwargs.setdefault("fmt", SMALL)
    kwargs.setdefault("source_factory", lambda fmt: NullSource(fmt))
    kwargs.setdefault("status_interval", 0.02)
    return AudioLink(session, **kwargs)


async def wait_for(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


# --------------------------------------------------------------------------- #
# 1. the PC asks the phone, and keeps the answer
# --------------------------------------------------------------------------- #
def test_the_link_asks_the_phone_what_its_output_is_doing() -> None:
    async def scenario():
        session = FakePhone(
            status={"route": "bluetooth_a2dp", "track_state": "playing", "underruns": 0}
        )
        audio = link(session)
        assert await audio.start_playback() is True
        try:
            assert await wait_for(lambda: "audio.status" in session.methods())
            assert await wait_for(lambda: audio.phone_status != {})
            phone = audio.phone_status
        finally:
            await audio.close()
        return phone

    phone = run(scenario())

    assert phone["route"] == "bluetooth_a2dp", "the route comes from the phone, not from us"
    assert phone["track_state"] == "playing"
    assert phone["underruns"] == 0
    assert phone["frames_in"] >= 1


def test_the_answer_appears_in_the_snapshot_the_runner_reads() -> None:
    async def scenario():
        session = FakePhone(status={"route": "speaker", "track_state": "playing"})
        audio = link(session)
        await audio.start_playback()
        try:
            await wait_for(lambda: audio.snapshot().phone != {})
            return audio.snapshot()
        finally:
            await audio.close()

    snapshot = run(scenario())

    assert snapshot.playback == ACTIVE
    assert snapshot.phone["route"] == "speaker"
    assert "phone" in snapshot.to_dict(), "the GUI reads the same snapshot as JSON"


def test_two_answers_become_a_frame_rate() -> None:
    """A cumulative counter cannot tell a stalled stream from a slow one."""

    async def scenario():
        session = FakePhone(status={"route": "speaker", "frames_written": 0})
        audio = link(session)
        await audio.start_playback()
        try:
            assert await wait_for(lambda: "rx_fps" in audio.phone_status)
            return audio.phone_status
        finally:
            await audio.close()

    phone = run(scenario())

    assert phone["rx_fps"] > 0, "frames are arriving"
    assert phone["written_fps"] == 0, (
        "and none of them are reaching AudioTrack: this is what a muted stream "
        "looks like from the PC, and it used to be invisible"
    )


def test_the_phone_going_quiet_mid_stream_is_not_an_audio_failure() -> None:
    async def scenario():
        session = FakePhone(status_fails=True)
        audio = link(session)
        assert await audio.start_playback() is True
        try:
            await asyncio.sleep(0.1)
            return audio.snapshot()
        finally:
            await audio.close()

    snapshot = run(scenario())

    assert snapshot.playback == ACTIVE, "a missing status answer is not a dead stream"
    assert snapshot.phone == {}, "and it leaves the PC line showing what the PC knows"


def test_no_status_rpc_is_spent_when_nothing_is_playing() -> None:
    async def scenario():
        session = FakePhone()
        link(session)  # built, never started
        await asyncio.sleep(0.1)
        return session.methods()

    assert "audio.status" not in run(scenario())


def test_the_poll_stops_with_the_last_direction() -> None:
    async def scenario():
        session = FakePhone(status={"route": "speaker"})
        audio = link(session)
        await audio.start_playback()
        assert await wait_for(lambda: "audio.status" in session.methods())
        await audio.stop_playback()
        before = session.methods().count("audio.status")
        await asyncio.sleep(0.15)  # several intervals
        after = session.methods().count("audio.status")
        return before, after

    before, after = run(scenario())
    assert after == before, "a stopped stream must not keep spending an RPC a second"


# --------------------------------------------------------------------------- #
# 2. the status line
# --------------------------------------------------------------------------- #
def test_the_speaker_line_reports_what_the_phone_says() -> None:
    status = LiveStatus(
        connected=True,
        speaker_state="active",
        latency_ms=200,
        speaker_stats={
            "route": "bluetooth_a2dp",
            "track_state": "playing",
            "rx_fps": 50.0,
            "written_fps": 50.0,
            "played_ms": 12_400,
            "buffered_ms": 40,
            "underruns": 3,
        },
    )

    line = status.line()

    assert "speaker active" in line
    assert "pc-buf 200 ms" in line, "the one PC number is labelled as the PC's"
    assert "-> bluetooth_a2dp" in line, "the route is the answer to 'where is it playing?'"
    assert "rx 50 f/s" in line
    assert "wrote 50 f/s" in line
    assert "played 12.4 s" in line, "a climbing play position is proof the device consumed"
    assert "buf 40 ms" in line
    assert "underruns 3" in line


def test_a_track_that_is_not_consuming_says_so() -> None:
    """The muted-stream signature: frames arrive, nothing is written."""

    status = LiveStatus(
        connected=True,
        speaker_state="active",
        latency_ms=200,
        speaker_stats={"route": "speaker", "track_state": "playing", "rx_fps": 50.0, "written_fps": 0.0},
    )

    line = status.line()
    assert "rx 50 f/s" in line
    assert "wrote 0 f/s" in line


def test_a_paused_track_is_named() -> None:
    status = LiveStatus(
        connected=True,
        speaker_state="active",
        speaker_stats={"route": "speaker", "track_state": "paused", "underruns": 0},
    )
    assert "track paused" in status.line()


def test_without_an_answer_the_line_is_what_it_used_to_be() -> None:
    """An older APK must not break the line, it just says less."""
    status = LiveStatus(connected=True, speaker_state="active", latency_ms=200)
    assert status.line() == "link up | speaker active pc-buf 200 ms"


def test_the_json_carries_the_phone_numbers() -> None:
    status = LiveStatus(
        connected=True,
        speaker_state="active",
        latency_ms=200,
        speaker_stats={"route": "speaker", "underruns": 2, "buffered_ms": 60},
    )

    payload = status.to_dict()
    assert payload["speaker"]["latency_ms"] == 200
    assert payload["speaker"]["stats"]["route"] == "speaker"
    assert payload["speaker"]["stats"]["underruns"] == 2


# --------------------------------------------------------------------------- #
# 3. a refused audio.playback.start reaches the user
# --------------------------------------------------------------------------- #
def _runner(session: Any, plan: LivePlan, **kwargs: Any) -> LiveRunner:
    kwargs.setdefault("loopback_factory", lambda fmt, speaker_plan: NullSource(fmt))
    return LiveRunner(session, plan, **kwargs)


def test_a_refused_playback_start_is_the_runners_reason() -> None:
    async def scenario():
        session = FakePhone(
            refuse_playback=RpcError(
                "no_output", "the phone could not open an audio output: AudioTrack init failed"
            )
        )
        runner = _runner(session, LivePlan(speaker=SpeakerPlan()))
        await runner.start()
        try:
            return runner.status()
        finally:
            await runner.close()

    status = run(scenario())

    assert status.speaker_state == "error"
    assert "no_output" in status.speaker_detail, "the phone's code survives"
    assert "AudioTrack init failed" in status.speaker_detail, "and its words, verbatim"
    assert "the phone refused audio playback" in status.speaker_detail


def test_the_refusal_is_printed_once_with_advice(capsys: pytest.CaptureFixture[str]) -> None:
    status = LiveStatus(
        connected=True,
        speaker_state="error",
        speaker_detail=(
            "the phone refused audio playback: no_output: "
            "the phone could not open an audio output: AudioTrack init failed"
        ),
    )
    reported: dict[str, str] = {}
    for _ in range(3):
        _report_failures(status, reported)

    out = capsys.readouterr().out
    assert out.count("speaker stopped:") == 1, "a repeating line buries the message"
    assert "AudioTrack init failed" in out
    assert "--test-tone" in out, "the advice names the command that isolates the phone"
    assert "adb logcat -s BridgeServer" in out


def test_a_refused_speaker_is_a_failed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit 0 after the phone refused to play anything was half the bug."""
    import phonebridge.cli.live as live

    failing = LiveStatus(
        connected=True,
        speaker_state="error",
        speaker_detail="the phone refused audio playback: no_output: AudioTrack init failed",
    )

    async def fake_open_session(args: Any, store: Any) -> tuple[Any, Any]:
        return FakePhone(), SimpleNamespace(describe=lambda: "test phone")

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def noop_status() -> LiveStatus:
        return failing

    runner = SimpleNamespace(
        start=noop,
        status=lambda: failing,
        run=lambda *args, **kwargs: noop_status(),
        close=noop,
    )
    monkeypatch.setattr(live, "open_session", fake_open_session)
    monkeypatch.setattr(live, "_store", lambda args: None)
    monkeypatch.setattr(live, "LiveRunner", lambda session, plan, **kwargs: runner)

    args = SimpleNamespace(
        command="speaker",
        target=None,
        timeout=1.0,
        store=None,
        format="text",
        no_status=True,
        duration=0.0,
        latency_ms=200,
        lipsync_ms=0,
        loopback_device="",
        test_tone=False,
        muted=False,
    )

    # `_report_failures` is covered on its own above; here the contract is the
    # exit code — this used to be 0 after the phone refused to play anything.
    assert run(live._run(args)) == 1  # noqa: SLF001


def test_a_refused_test_tone_exits_instead_of_waiting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing else was going to happen: the tone's only job was to prove the phone."""
    import phonebridge.cli.live as live

    refused = LiveStatus(
        connected=True,
        speaker_state="error",
        speaker_detail="the phone refused audio playback: no_output: AudioTrack init failed",
    )

    async def fake_open_session(args: Any, store: Any) -> tuple[Any, Any]:
        return FakePhone(), SimpleNamespace(describe=lambda: "test phone")

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    ran: list[str] = []

    async def run_forever(*args: Any, **kwargs: Any) -> LiveStatus:
        ran.append("run")
        return refused

    runner = SimpleNamespace(start=noop, status=lambda: refused, run=run_forever, close=noop)
    monkeypatch.setattr(live, "open_session", fake_open_session)
    monkeypatch.setattr(live, "_store", lambda args: None)
    monkeypatch.setattr(live, "LiveRunner", lambda session, plan, **kwargs: runner)

    args = SimpleNamespace(
        command="speaker",
        target=None,
        timeout=1.0,
        store=None,
        format="text",
        no_status=False,
        duration=None,
        latency_ms=200,
        lipsync_ms=0,
        loopback_device="",
        test_tone=True,
        muted=False,
    )

    assert run(live._run(args)) == 1  # noqa: SLF001
    assert ran == [], "a refused tone must not enter the run loop"
    out = capsys.readouterr().out
    assert "speaker stopped:" in out
    assert "AudioTrack init failed" in out, "the phone's reason reaches the user"


# --------------------------------------------------------------------------- #
# 4. the command asks for sound
# --------------------------------------------------------------------------- #
def test_the_runner_unmutes_the_stream_it_just_started() -> None:
    """A muted stream is silence carrying FLAG_MUTED, and the phone drops it."""

    async def scenario():
        session = FakePhone(status={"route": "speaker"})
        runner = _runner(session, LivePlan(speaker=SpeakerPlan()))
        await runner.start()
        try:
            await asyncio.sleep(0.15)
            return session, runner
        finally:
            await runner.close()

    session, _ = run(scenario())

    assert ("audio.playback.mute", {"muted": False}) in session.calls, (
        "the command's whole job is to make the phone play; it must ask"
    )
    frames = session.playback_frames()
    assert frames, "frames must be flowing"
    assert not any(frame.flags & FLAG_MUTED for frame in frames), (
        "a muted frame is dropped by the phone before it reaches AudioTrack"
    )


def test_muted_is_available_when_a_user_asks_for_it() -> None:
    async def scenario():
        session = FakePhone(status={"route": "speaker"})
        runner = _runner(session, LivePlan(speaker=SpeakerPlan(muted=True)))
        await runner.start()
        try:
            await asyncio.sleep(0.15)
            return session
        finally:
            await runner.close()

    session = run(scenario())
    frames = session.playback_frames()
    assert frames and all(frame.flags & FLAG_MUTED for frame in frames)


# --------------------------------------------------------------------------- #
# 5. --test-tone
# --------------------------------------------------------------------------- #
def test_a_test_tone_needs_no_loopback_device_at_all() -> None:
    def explode(fmt: Any, plan: Any) -> Any:
        raise AssertionError("--test-tone must not open the PC's capture device")

    async def scenario():
        session = FakePhone(status={"route": "speaker", "frames_written": 1})
        runner = LiveRunner(
            session,
            LivePlan(speaker=SpeakerPlan(test_tone=True, tone_seconds=0.2)),
            loopback_factory=explode,
        )
        await runner.start()
        try:
            await asyncio.sleep(0.15)
            return session, runner
        finally:
            await runner.close()

    session, runner = run(scenario())

    assert runner._tone is not None  # noqa: SLF001
    # The session also carries the STOP marker, which has no payload on purpose.
    payloads = [frame.payload for frame in session.playback_frames() if not frame.stop]
    assert len(payloads) >= 5, "the tone actually went over the session"
    assert len({len(payload) for payload in payloads}) == 1, "one frame per read"
    assert all(payload and any(payload) for payload in payloads), "not silence"


def test_the_tone_is_audible_rather_than_silence() -> None:
    async def scenario():
        session = FakePhone(status={"route": "speaker"})
        runner = LiveRunner(
            session,
            LivePlan(speaker=SpeakerPlan(test_tone=True, tone_seconds=0.2)),
            loopback_factory=lambda fmt, plan: NullSource(fmt),
        )
        await runner.start()
        try:
            await asyncio.sleep(0.15)
            return session
        finally:
            await runner.close()

    frames = run(scenario()).playback_frames()
    peaks = [max(abs(sample) for sample in _samples(frame.payload)) for frame in frames[:3]]

    assert max(peaks) > 2_000, "a 440 Hz sine at -12 dBFS, not the silence of a muted stream"


def _samples(payload: bytes) -> list[int]:
    from array import array

    samples = array("h")
    samples.frombytes(payload[: len(payload) - (len(payload) % 2)])
    return list(samples)


def test_the_tone_ends_the_run_by_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three seconds of tone, then a sentence — not a session left on silence."""
    monkeypatch.setattr(runner_module, "STATUS_INTERVAL_S", 0.05)
    monkeypatch.setattr(runner_module, "TEST_TONE_DRAIN_S", 0.05)

    async def scenario():
        session = FakePhone(
            status={"route": "speaker", "played_ms": 2_500, "underruns": 0, "frames_written": 1}
        )
        runner = LiveRunner(
            session,
            # Long enough for one `audio.status` poll (1 s) to land before the
            # tone ends, so the final line carries the phone's own numbers.
            LivePlan(speaker=SpeakerPlan(test_tone=True, tone_seconds=1.05)),
            loopback_factory=lambda fmt, plan: NullSource(fmt),
        )
        await runner.start()
        try:
            status = await asyncio.wait_for(runner.run(duration=10.0), timeout=8.0)
            return status, session
        finally:
            await runner.close()

    status, session = run(scenario())

    assert status.speaker_state == "idle", "playback is stopped once the tone has drained"
    assert "test tone sent" in status.detail
    assert "440 Hz" in status.detail and "1.0 s" in status.detail
    # The last line carries the phone's own evidence, not just "I sent it".
    assert "route speaker" in status.detail
    assert "played 2.5 s" in status.detail
    assert "audio.playback.stop" in session.methods()


def test_the_tone_source_is_what_was_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The injected factory is honoured, so the tone length is testable."""
    seen: dict[str, Any] = {}

    def factory(fmt: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return ToneSource(fmt, sleep=lambda seconds: None, **kwargs)

    async def scenario():
        session = FakePhone()
        runner = LiveRunner(
            session,
            LivePlan(speaker=SpeakerPlan(test_tone=True, tone_seconds=0.5)),
            loopback_factory=lambda fmt, plan: NullSource(fmt),
            tone_factory=factory,
        )
        await runner.start()
        await runner.close()

    run(scenario())

    assert seen["seconds"] == 0.5
    assert seen["frequency"] == 440.0


# --------------------------------------------------------------------------- #
# underruns during silence are the feed's doing, not the link's
# --------------------------------------------------------------------------- #
def test_underruns_while_the_pc_is_silent_do_not_count_as_faults() -> None:
    """A starved track underruns by definition; that must not read as a fault.

    The reported symptom was "hundreds of underruns" from a PC that was simply
    not playing anything: loopback delivers no packets from an idle endpoint, the
    phone's AudioTrack runs dry, and the counter climbs. The counter is still
    reported — as ``underruns_silent`` — but it no longer dominates the number
    that means "this path is broken".
    """

    async def scenario():
        session = FakePhone(status={"playback": "playing"})
        audio = link(session)
        # The phone starts a fresh track at zero, so the first answer is a baseline.
        audio._absorb_phone_status({"build_id": "1.0.0 (1) abc12345", "underruns": 0})
        audio.pc_silent = True
        audio._absorb_phone_status({"underruns": 300})  # 300 while silent
        while_silent = dict(audio.phone_status)
        audio.pc_silent = False
        audio._absorb_phone_status({"underruns": 305})  # 5 while playing
        return while_silent, dict(audio.phone_status)

    while_silent, playing = run(scenario())

    assert while_silent["underruns"] == 0, "silence is not a fault"
    assert while_silent["underruns_silent"] == 300
    assert while_silent["underruns_total"] == 300, "the raw counter is not hidden"
    assert playing["underruns"] == 5, "underruns while playing are the real thing"
    assert playing["underruns_silent"] == 300


def test_the_phone_build_reaches_the_status_the_runner_reads() -> None:
    async def scenario():
        session = FakePhone(status={"build_id": "1.0.0 (1) e807c93a", "route": "speaker"})
        audio = link(session)
        await audio.start_playback()
        try:
            assert await wait_for(lambda: "build_id" in audio.phone_status)
            return audio.snapshot()
        finally:
            await audio.close()

    snapshot = run(scenario())

    assert snapshot.phone["build_id"] == "1.0.0 (1) e807c93a"
    assert "keepalive_frames" in snapshot.stats


def test_the_status_line_says_zero_when_every_underrun_was_silence() -> None:
    """`underruns 312` from a silent PC read as a broken link. It was a starved one."""
    status = LiveStatus(
        connected=True,
        speaker_state="active",
        latency_ms=60,
        speaker_stats={
            "rx_fps": 50.0,
            "written_fps": 50.0,
            "underruns": 0,
            "underruns_silent": 312,
            "underruns_total": 312,
        },
        pc_silent=True,
    )

    line = status.line()

    assert "pc silent" in line
    assert "underruns 0 (silent 312 of 312)" in line, line
    assert "rx 50 f/s" in line, "keep-alive at 20 ms shows up as 50 frames a second"


def test_a_real_underrun_is_still_printed_first() -> None:
    status = LiveStatus(
        connected=True,
        speaker_state="active",
        speaker_stats={"underruns": 5, "underruns_silent": 300, "underruns_total": 305},
    )

    line = status.line()

    assert "underruns 5 (silent 300 of 305)" in line, line
