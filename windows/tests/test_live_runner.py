"""The runner: device order, camera pumping and the status it reports.

The phone side and the devices are both fakes here. What is under test is the
wiring — that a missing device is discovered *before* the phone starts streaming,
that a stalled camera is blanked rather than left frozen, and that one dead path
does not take the others down.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import phonebridge.live.runner as runner_module
from phonebridge.live.config import CameraPlan, LivePlan, MicPlan, SpeakerPlan
from phonebridge.live.devices import DeviceUnavailable
from phonebridge.live.runner import LiveRunner, LiveStatus


class FakeCameraSink:
    def __init__(self) -> None:
        self.frames: list[Any] = []
        self.blanks = 0
        self.closed = False

    def send(self, frame: Any) -> None:
        self.frames.append(frame)

    def send_black(self) -> None:
        self.blanks += 1

    def close(self) -> None:
        self.closed = True


class FakeCameraStream:
    """The real class minus the socket: it pushes frames through ``on_change``.

    That callback is how the runner is fed in production, so the fake must call
    it too — a fake that only answered ``latest_frame()`` would pass while the
    real path delivered nothing.
    """

    instances: list[FakeCameraStream] = []

    def __init__(self, session: Any, *, config: Any, on_change: Any = None) -> None:
        self.session = session
        self.config = config
        self.on_change = on_change
        self.started = False
        self.closed = False
        self.state = "idle"
        self.detail = ""
        self.frame: Any = None
        FakeCameraStream.instances.append(self)

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change(self.snapshot())

    def push_frame(self, frame: Any = None) -> None:
        """A new frame arrives the way the real stream delivers one."""
        self.frame = object() if frame is None else frame
        self._notify()

    async def start(self) -> bool:
        self.started = True
        self.state = "active"
        self.frame = object()  # one frame waiting
        self._notify()
        return True

    def latest_frame(self) -> Any:
        return self.frame

    def snapshot(self) -> Any:
        # The real snapshot carries the newest frame; the runner publishes from it.
        return SimpleNamespace(state=self.state, detail=self.detail, frame=self.frame)

    async def close(self, reason: str = "stopped") -> None:
        self.closed = True
        self.state = "idle"


class FakeAudioLink:
    instances: list[FakeAudioLink] = []

    def __init__(self, session: Any, **kwargs: Any) -> None:
        self.session = session
        self.kwargs = kwargs
        self.mic_started = False
        self.playback_started = False
        self.mic_stopped = ""
        #: What the runner asked for after `start_playback` succeeded. `None`
        #: means it never asked, which would leave the stream muted.
        self.muted: bool | None = None
        self.closed = False
        FakeAudioLink.instances.append(self)

    async def start_mic(self) -> bool:
        if self.kwargs.get("sink_factory") is None:
            return False
        self.kwargs["sink_factory"](
            SimpleNamespace(
                frame_ms=20, bytes_per_frame=4, samples_per_frame=2, sample_rate=48_000, channels=1
            )
        )
        self.mic_started = True
        return True

    async def start_playback(self) -> bool:
        self.playback_started = True
        return True

    async def set_mute(self, muted: bool) -> bool:
        self.muted = muted
        return muted

    def snapshot(self) -> Any:
        return SimpleNamespace(
            playback="active" if self.playback_started else "idle",
            mic="active" if self.mic_started else "idle",
            detail="",
            mic_detail="",
            stats={"received": 7},
            phone={"route": "speaker", "track_state": "playing", "underruns": 0},
        )

    async def stop_mic(self, reason: str = "stopped") -> None:
        self.mic_stopped = reason
        self.mic_started = False

    async def close(self, reason: str = "stopped") -> None:
        self.closed = True
        self.mic_started = False
        self.playback_started = False


class FakeMicSink:
    def __init__(self) -> None:
        self.level_dbfs = -18.5
        self.stats = {"depth_ms": 60, "underruns": 0}
        self.error = ""
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeCameraStream.instances.clear()
    FakeAudioLink.instances.clear()
    monkeypatch.setattr(runner_module, "CameraStream", FakeCameraStream)
    monkeypatch.setattr(runner_module, "AudioLink", FakeAudioLink)
    # Sending needs numpy; the runner only forwards whatever the decoder produced.
    monkeypatch.setattr(runner_module, "_to_array", lambda image: image)


def _session(open_: bool = True) -> Any:
    return SimpleNamespace(is_open=open_)


def _runner(plan: LivePlan, **kwargs: Any) -> tuple[LiveRunner, FakeCameraSink, FakeMicSink]:
    sink = FakeCameraSink()
    mic = FakeMicSink()
    runner = LiveRunner(
        _session(),
        plan,
        camera_factory=lambda camera_plan: sink,
        mic_factory=lambda fmt, mic_plan: mic,
        loopback_factory=lambda fmt, speaker_plan: object(),
        decode=lambda frame, width, height: f"rgb-{width}x{height}",
        **kwargs,
    )
    return runner, sink, mic


def test_an_empty_plan_is_refused() -> None:
    runner, _, _ = _runner(LivePlan())

    with pytest.raises(DeviceUnavailable, match="nothing to deliver"):
        asyncio.run(runner.start())


def test_a_missing_device_fails_before_the_phone_is_asked_to_stream() -> None:
    def broken(camera_plan: CameraPlan) -> Any:
        raise DeviceUnavailable("OBS is not installed", hint="install OBS")

    runner = LiveRunner(_session(), LivePlan(camera=CameraPlan()), camera_factory=broken)

    with pytest.raises(DeviceUnavailable):
        asyncio.run(runner.start())
    assert FakeCameraStream.instances == [], "the phone must not start streaming first"


def test_camera_and_audio_are_wired_on_one_session() -> None:
    plan = LivePlan(camera=CameraPlan(fps=30), mic=MicPlan(), speaker=SpeakerPlan())
    runner, sink, mic = _runner(plan)

    async def scenario() -> None:
        await runner.start()
        try:
            status = runner.status()
            link = FakeAudioLink.instances[0]
            # Same session object: these features must not open their own link.
            assert link.session is runner._session  # noqa: SLF001
            assert link.mic_started is True
            assert link.playback_started is True
            assert status.mic_state == "active"
            assert status.speaker_state == "active"
            assert status.camera_state == "active"
            assert status.buffer_ms == 60
            assert status.mic_level_dbfs == -18.5
        finally:
            await runner.close()
        assert FakeCameraStream.instances[0].started is True

    asyncio.run(scenario())
    assert sink.closed is True
    assert mic.closed is True
    assert FakeCameraStream.instances[0].closed is True


def test_the_camera_pump_delivers_decoded_frames() -> None:
    runner, sink, _ = _runner(LivePlan(camera=CameraPlan(width=640, height=480, fps=30)))

    async def scenario() -> None:
        await runner.start()
        await asyncio.sleep(0.15)
        status = runner.status()
        await runner.close()
        assert sink.frames, "the virtual camera must receive frames"
        assert sink.frames[0] == "rgb-640x480"
        assert status.frames_sent >= 1

    asyncio.run(scenario())


def test_a_stalled_camera_is_blanked_after_the_stall_timeout() -> None:
    runner, sink, _ = _runner(LivePlan(camera=CameraPlan(fps=30)), stall_s=0.05)

    async def scenario() -> None:
        await runner.start()
        stream = FakeCameraStream.instances[0]
        await asyncio.sleep(0.2)
        stream.frame = None  # the phone stopped sending
        await asyncio.sleep(0.4)
        await runner.close()

    asyncio.run(scenario())

    assert sink.frames, "frames that did arrive must still have been sent"
    assert sink.blanks >= 1, "a dead camera must go black, not freeze on the last face"


def test_lip_sync_delays_video_not_audio() -> None:
    runner, sink, _ = _runner(
        LivePlan(camera=CameraPlan(fps=30), speaker=SpeakerPlan(lipsync_ms=400))
    )

    async def scenario() -> None:
        await runner.start()
        await asyncio.sleep(0.2)
        early = len(sink.frames)
        await asyncio.sleep(0.6)
        late = len(sink.frames)
        await runner.close()
        assert early == 0, "a 400 ms offset must hold the first frames back"
        assert late >= 1, "and release them once the offset has passed"

    asyncio.run(scenario())


def test_one_dead_path_does_not_take_the_others_down() -> None:
    plan = LivePlan(mic=MicPlan(), speaker=SpeakerPlan())
    mic = FakeMicSink()
    mic.error = "device unplugged"

    runner = LiveRunner(
        _session(),
        plan,
        mic_factory=lambda fmt, mic_plan: mic,
        loopback_factory=lambda fmt, speaker_plan: object(),
    )

    async def scenario() -> None:
        await runner.start()
        status = runner.status()
        await runner.close()
        assert status.mic_state == "error"
        assert status.mic_detail == "device unplugged"
        assert status.speaker_state == "active", "the speaker must keep running"

    asyncio.run(scenario())


def test_status_is_readable_as_a_line_and_as_json() -> None:
    status = LiveStatus(
        connected=True,
        camera_state="active",
        fps=29.7,
        frames_sent=91,
        mic_state="active",
        mic_level_dbfs=-22.0,
        buffer_ms=120,
        speaker_state="active",
        latency_ms=200,
    )

    line = status.line()
    assert line.startswith("link up")
    assert "camera active 29.7 fps" in line
    assert "mic active -22 dBFS buf 120 ms" in line
    # The buffer the PC was configured with is labelled as the PC's; everything
    # after it would come from the phone (this status has no phone answer yet).
    assert "speaker active pc-buf 200 ms" in line

    payload = status.to_dict()
    assert payload["camera"]["fps"] == 29.7
    assert payload["mic"]["level_dbfs"] == -22.0
    assert payload["speaker"]["latency_ms"] == 200


def test_a_disconnected_session_is_reported_honestly() -> None:
    runner = LiveRunner(
        _session(open_=False),
        LivePlan(camera=CameraPlan()),
        camera_factory=lambda plan: FakeCameraSink(),
    )

    assert runner.status().connected is False
    assert runner.status().line().startswith("link down")


def test_a_dead_microphone_device_stops_the_phone_capture_and_nothing_else() -> None:
    """A PC-side device failure must cost the microphone *path*, not the call."""
    plan = LivePlan(camera=CameraPlan(fps=60), mic=MicPlan(), speaker=SpeakerPlan())
    runner, camera_sink, mic = _runner(plan)

    async def scenario() -> None:
        await runner.start()
        try:
            await asyncio.sleep(0.15)  # let the camera pump deliver a frame or two
            before = len(camera_sink.frames)

            # Exactly what CableMicSink does from its pump thread when the device dies.
            runner._mic_failed(asyncio.get_running_loop(), "dtype mismatch")  # noqa: SLF001
            await asyncio.sleep(0.05)

            link = FakeAudioLink.instances[0]
            assert "dtype mismatch" in link.mic_stopped, (
                "the phone's capture must be stopped: nobody can hear it"
            )
            FakeCameraStream.instances[0].push_frame()  # the phone sends another
            await asyncio.sleep(0.15)
            assert len(camera_sink.frames) > before, "the camera pump must keep sending"
            assert runner._pump.snapshot().running  # noqa: SLF001
            assert runner.status().speaker_state == "active"
        finally:
            await runner.close()

    asyncio.run(scenario())


def test_a_two_argument_mic_factory_still_works() -> None:
    """Tests and callers inject simple factories; the callback must be optional."""
    plan = LivePlan(mic=MicPlan())
    mic = FakeMicSink()
    runner = LiveRunner(_session(), plan, mic_factory=lambda fmt, mic_plan: mic)

    async def scenario() -> None:
        await runner.start()
        await runner.close()

    asyncio.run(scenario())
    assert mic.closed is True
