"""Every way the camera path can fail, said out loud once.

The user's report: `phonebridge camera --virtual` connected, printed the config,
and then showed "camera error 0.0 fps" **forever with no reason** — while the
microphone path did explain itself. The phone's actual answer was
`internal: ImageCapture: Method setCurrentState must be called on the main
thread`, and the PC had it in `snapshot().detail` the whole time.

These tests use the failures that really happen: an RPC refusal carrying a code,
a frame that will not decode, a virtual camera device that closes mid-stream, and
the most common of all — `camera.start` accepted, then nothing arrives.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from phonebridge.camera.frames import VideoConfig, VideoFrame
from phonebridge.camera.stream import ERROR, CameraSnapshot, CameraStream
from phonebridge.cli.live import _report_failures
from phonebridge.live.config import CameraPlan, LivePlan, MicPlan
from phonebridge.live.runner import LiveRunner, LiveStatus
from phonebridge.protocol.messages import RpcError


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_numpy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sending needs numpy; the runner only forwards whatever the decoder made."""
    import phonebridge.live.runner as runner_module

    monkeypatch.setattr(runner_module, "_to_array", lambda image: image)


class FakeSession:
    """The phone, answering (or refusing) one method."""

    def __init__(self, *, reply: Any = None, refuse: Exception | None = None) -> None:
        self.is_open = True
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.reply = reply
        self.refuse = refuse

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, params))
        if self.refuse is not None:
            raise self.refuse
        return self.reply if self.reply is not None else {"result": {"ok": True}}

    def subscribe_blobs(self, stream_id: int, maxsize: int = 5) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.queues = getattr(self, "queues", {})
        self.queues[stream_id] = queue
        return queue

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]


# --------------------------------------------------------------------------- #
# the phone refuses camera.start
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("refusal", "code", "words"),
    [
        (
            RpcError("camera_busy", "the camera could not be opened (another app may be using it): "
                     "Method setCurrentState must be called on the main thread"),
            "camera_busy",
            "Method setCurrentState must be called on the main thread",
        ),
        (
            RpcError(
                "camera_denied",
                "camera permission is not granted: open PhoneBridge on the phone and allow Camera",
            ),
            "camera_denied",
            "allow Camera",
        ),
        (RpcError("method_not_found", "no handler for 'camera.start'"), "method_not_found", "camera.start"),
        (RpcError("no_camera", "this phone has no front camera"), "no_camera", "no front camera"),
    ],
)
def test_the_phones_refusal_is_kept_verbatim(refusal: Exception, code: str, words: str) -> None:
    async def scenario() -> CameraSnapshot:
        session = FakeSession(refuse=refusal)
        stream = CameraStream(session, config=VideoConfig())
        assert await stream.start() is False
        return stream.snapshot()

    snapshot = run(scenario())

    assert snapshot.state == ERROR
    assert f"({code})" in snapshot.detail, "the code must be visible: the PC reacts on it"
    assert words in snapshot.detail, "the phone's own words must survive untouched"
    assert "the phone refused the camera" in snapshot.detail


def test_an_rpc_refusal_in_session_error_form_is_unpacked() -> None:
    """The session turns an error reply into `SessionError("code: message")`."""
    from phonebridge.core.session import SessionError

    async def scenario() -> CameraSnapshot:
        session = FakeSession(refuse=SessionError("camera_busy: the camera is in use by com.example"))
        stream = CameraStream(session, config=VideoConfig())
        await stream.start()
        return stream.snapshot()

    detail = run(scenario()).detail
    assert "(camera_busy)" in detail
    assert "camera is in use by com.example" in detail


def test_a_timeout_says_the_phone_did_not_answer() -> None:
    from phonebridge.core.session import SessionError

    async def scenario() -> CameraSnapshot:
        session = FakeSession(refuse=SessionError("phone did not answer 'camera.start' in time"))
        stream = CameraStream(session, config=VideoConfig())
        await stream.start()
        return stream.snapshot()

    detail = run(scenario()).detail
    assert "did not answer" in detail
    assert "the phone refused the camera" in detail


# --------------------------------------------------------------------------- #
# the runner keeps that refusal and the CLI prints it once
# --------------------------------------------------------------------------- #
class FakeCameraSink:
    def __init__(self, *, fail_on_send: Exception | None = None) -> None:
        self.frames: list[Any] = []
        self.blanks = 0
        self.closed = False
        self.fail_on_send = fail_on_send

    def send(self, frame: Any) -> None:
        if self.fail_on_send is not None:
            raise self.fail_on_send
        self.frames.append(frame)

    def send_black(self) -> None:
        if self.fail_on_send is not None:
            raise self.fail_on_send
        self.blanks += 1

    def close(self) -> None:
        self.closed = True


def runner_for(
    session: Any, *, sink: Any = None, decode: Any = None, first_frame_s: float = 0.05
) -> tuple[LiveRunner, Any]:
    sink = sink or FakeCameraSink()
    runner = LiveRunner(
        session,
        LivePlan(camera=CameraPlan(width=320, height=240, fps=50)),
        camera_factory=lambda plan: sink,
        decode=decode or (lambda frame, width, height: f"rgb-{width}x{height}"),
        first_frame_s=first_frame_s,
    )
    return runner, sink


def test_a_refused_camera_start_survives_into_the_status() -> None:
    async def scenario() -> tuple[LiveStatus, Any]:
        session = FakeSession(refuse=RpcError("camera_busy", "the camera is already open in Camera"))
        runner, sink = runner_for(session)
        await runner.start()
        try:
            return runner.status(), sink
        finally:
            await runner.close()

    status, sink = run(scenario())

    assert status.camera_state == "error"
    assert "camera_busy" in status.camera_detail
    assert "already open in Camera" in status.camera_detail
    assert sink.frames == [], "nothing must be published when the phone refused"


def test_an_accepted_start_that_sends_nothing_is_reported_as_silence() -> None:
    """The user's case: `camera.start` succeeded, then 0.0 fps forever."""

    async def scenario() -> LiveStatus:
        runner, _ = runner_for(FakeSession(), first_frame_s=0.05)
        await runner.start()
        try:
            await asyncio.sleep(0.3)
            return runner.status()
        finally:
            await runner.close()

    status = run(scenario())

    assert status.camera_state == "error"
    assert "no camera frames from the phone within" in status.camera_detail
    assert "adb logcat -s CameraEngine" in status.camera_detail, (
        "the silence has to point at where the phone explains itself"
    )


def test_frames_that_cannot_be_decoded_are_named_and_counted() -> None:
    def explode(frame: Any, width: int, height: int) -> Any:
        raise ValueError("cannot identify image file")

    async def scenario() -> LiveStatus:
        session = FakeSession()
        runner, _ = runner_for(session, decode=explode, first_frame_s=0.05)
        await runner.start()
        try:
            stream = runner._camera  # noqa: SLF001
            return await _after_frames(runner, stream, count=3)
        finally:
            await runner.close()

    status = run(scenario())

    assert status.camera_state == "error"
    assert "none could be decoded" in status.camera_detail
    assert "cannot identify image file" in status.camera_detail
    assert "camera frame(s) but none could be decoded" in status.camera_detail


async def _after_frames(runner: LiveRunner, stream: CameraStream, *, count: int) -> Any:
    for seq in range(count):
        stream.push(_frame_body(seq))
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.15)
    return runner.status()


def _frame_body(seq: int) -> bytes:
    """A header plus the smallest payload that passes the frame gate."""
    return VideoFrame(
        seq=seq, timestamp_ms=seq * 20, payload=b"\xff\xd8jpeg\xff\xd9", width=320, height=240
    ).encode()


def test_one_bad_frame_is_a_notice_not_a_dead_path() -> None:
    calls = {"n": 0}

    def flaky(frame: Any, width: int, height: int) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("truncated JPEG")
        return "rgb"

    async def scenario() -> tuple[LiveStatus, Any]:
        session = FakeSession()
        sink = FakeCameraSink()
        runner, _ = runner_for(session, sink=sink, decode=flaky, first_frame_s=5.0)
        await runner.start()
        try:
            stream = runner._camera  # noqa: SLF001
            stream.push(_frame_body(0))
            await asyncio.sleep(0.1)
            mid = runner.status()
            stream.push(_frame_body(1))
            await asyncio.sleep(0.15)
            return mid, (runner.status(), sink)
        finally:
            await runner.close()

    mid, (end, sink) = run(scenario())

    assert "could not be decoded" in mid.camera_detail, "the first failure is explained"
    assert mid.camera_state != "error", "one bad frame does not kill the camera"
    assert end.camera_state == "active"
    assert sink.frames, "the next good frame still arrives"


def test_a_virtual_camera_that_stops_accepting_frames_is_reported() -> None:
    """OBS closed, or the device was taken: the session must survive it."""

    async def scenario() -> tuple[LiveStatus, FakeCameraSink]:
        session = FakeSession()
        sink = FakeCameraSink(fail_on_send=OSError("the device is no longer available"))
        runner, _ = runner_for(session, sink=sink, first_frame_s=5.0)
        await runner.start()
        try:
            runner._camera.push(_frame_body(0))  # noqa: SLF001
            await asyncio.sleep(0.2)
            runner._camera.push(_frame_body(1))  # noqa: SLF001
            await asyncio.sleep(0.1)
            return runner.status(), sink
        finally:
            await runner.close()

    status, _ = run(scenario())

    assert status.camera_state == "error"
    assert "virtual camera stopped accepting frames" in status.camera_detail
    assert "the device is no longer available" in status.camera_detail
    assert status.connected is True, "a dead device is not a dead session"


def test_the_phone_stopping_its_camera_is_not_an_error() -> None:
    async def scenario() -> LiveStatus:
        runner, _ = runner_for(FakeSession(), first_frame_s=5.0)
        await runner.start()
        try:
            stream = runner._camera  # noqa: SLF001
            stream.push(_frame_body(0))
            await asyncio.sleep(0.1)
            stream.push(None)  # the documented end-of-stream marker
            await asyncio.sleep(0.1)
            return runner.status()
        finally:
            await runner.close()

    status = run(scenario())
    assert status.camera_state == "idle"
    assert status.camera_detail, "an ended stream still says something"


# --------------------------------------------------------------------------- #
# the CLI prints each reason once
# --------------------------------------------------------------------------- #
def test_each_reason_is_printed_once_and_never_again(capsys: pytest.CaptureFixture[str]) -> None:
    reported: dict[str, str] = {}
    status = LiveStatus(
        connected=True,
        camera_state="error",
        camera_detail="the phone refused the camera (camera_busy): the camera could not be opened",
        mic_state="active",
        speaker_state="off",
    )

    for _ in range(4):
        _report_failures(status, reported)

    out = capsys.readouterr().out
    assert out.count("camera stopped:") == 1, "a repeating line buries the message it repeats"
    assert "(camera_busy)" in out, "the phone's code is part of the line"
    assert "adb logcat -s CameraEngine" in out, "the advice says where to look on the phone"


def test_a_new_reason_after_the_first_is_also_printed(capsys: pytest.CaptureFixture[str]) -> None:
    reported: dict[str, str] = {}
    _report_failures(
        LiveStatus(connected=True, camera_state="error", camera_detail="no camera frames from the phone"),
        reported,
    )
    _report_failures(
        LiveStatus(connected=True, camera_state="error", camera_detail="the phone stopped the camera"),
        reported,
    )

    out = capsys.readouterr().out
    assert "no camera frames" in out
    assert "the phone stopped the camera" in out


def test_a_notice_is_printed_without_the_stopped_wording(capsys: pytest.CaptureFixture[str]) -> None:
    reported: dict[str, str] = {}
    _report_failures(
        LiveStatus(
            connected=True,
            camera_state="active",
            camera_detail="a camera frame could not be decoded (ValueError: truncated JPEG)",
        ),
        reported,
    )

    out = capsys.readouterr().out
    assert "camera: a camera frame could not be decoded" in out
    assert "stopped" not in out


def test_the_microphone_wording_did_not_change(capsys: pytest.CaptureFixture[str]) -> None:
    """The one thing that already worked must keep working, word for word."""
    _report_failures(
        LiveStatus(connected=True, mic_state="error", mic_detail="writing to the device failed"),
        {},
    )

    out = capsys.readouterr().out
    assert "microphone stopped: writing to the device failed" in out
    assert "The session is still up".lower() in out.lower()


def test_a_camera_failure_is_a_failed_run(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 0 after delivering no picture was the other half of the bug."""
    import phonebridge.cli.live as live

    async def fake_open_session(args: Any, store: Any) -> tuple[Any, Any]:
        return FakeSession(), SimpleNamespace(describe=lambda: "test phone")

    failing = LiveStatus(
        connected=True,
        camera_state="error",
        camera_detail="no camera frames from the phone within 5 s",
    )
    runner = SimpleNamespace(
        start=_noop,
        status=lambda: failing,
        run=lambda *args, **kwargs: _noop_status(),
        close=_noop,
    )
    monkeypatch.setattr(live, "open_session", fake_open_session)
    monkeypatch.setattr(live, "_store", lambda args: None)
    monkeypatch.setattr(live, "LiveRunner", lambda session, plan: runner)

    args = SimpleNamespace(
        command="camera",
        virtual=True,
        target=None,
        timeout=1.0,
        store=None,
        format="text",
        no_status=True,
        duration=0.0,
        width=320,
        height=240,
        fps=30,
        quality=70,
        facing="back",
        backend="auto",
    )
    code = run(live._run(args))  # noqa: SLF001

    assert code == 1


async def _noop(*args: Any, **kwargs: Any) -> None:
    return None


async def _noop_status() -> LiveStatus:
    return LiveStatus(
        connected=True,
        camera_state="error",
        camera_detail="no camera frames from the phone within 5 s",
    )


def test_mic_still_needs_a_reason_to_keep_its_wording() -> None:
    """`_report_failures` must not invent a line for a healthy path."""
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        _report_failures(LiveStatus(connected=True, camera_state="active", mic_state="active"), {})
    assert buffer.getvalue() == ""


def test_mic_plan_is_untouched_by_the_camera_work() -> None:
    """Guard against wiring the camera report through the mic's fields."""
    plan = LivePlan(camera=CameraPlan(), mic=MicPlan())
    assert plan.camera is not None and plan.mic is not None
