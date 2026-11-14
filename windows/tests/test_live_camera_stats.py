"""``--stats``: the numbers come from the two ends that actually do the work.

The user's report — 5-8 fps against a 30 fps target — could only come from the
phone's encoder, the PC's per-frame work, or the link. These tests pin how each
number is gathered, so the next slow run is attributable instead of a guess:
the PC side from the pump (frames received, bytes, decode/convert/send
milliseconds, queue depth, drops) and the phone side from its own
``camera.status`` (capture fps, YUV->JPEG milliseconds, JPEG quality and size,
send milliseconds).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from phonebridge.camera.frames import VideoFrame
from phonebridge.cli import live
from phonebridge.live.config import CameraPlan, LivePlan
from phonebridge.live.runner import LiveRunner, LiveStatus


class FakeSession:
    """Answers ``camera.status`` with the phone's measurements."""

    def __init__(self, stats: dict[str, Any] | None = None) -> None:
        self.is_open = True
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.stats = stats
        self.queue: asyncio.Queue | None = None

    async def call(self, method: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self.calls.append((method, params))
        if method == "camera.status" and self.stats is not None:
            return {"result": {"streaming": True, "stats": self.stats}}
        return {"result": {"ok": True}}

    def subscribe_blobs(self, stream_id: int, maxsize: int = 2) -> asyncio.Queue:
        self.queue = asyncio.Queue(maxsize=maxsize)
        return self.queue

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]


def frame_body(seq: int) -> bytes:
    return VideoFrame(
        seq=seq, timestamp_ms=seq * 33, payload=b"\xff\xd8jpeg\xff\xd9", width=320, height=240
    ).encode()


def build(session: FakeSession, **kwargs: Any) -> tuple[LiveRunner, Any]:
    class Sink:
        def __init__(self) -> None:
            self.frames: list[Any] = []
            self.blanks = 0

        def send(self, frame: Any) -> None:
            self.frames.append(frame)

        def send_black(self) -> None:
            self.blanks += 1

        def close(self) -> None:
            return None

    sink = Sink()
    runner = LiveRunner(
        session,
        LivePlan(camera=CameraPlan(width=320, height=240, fps=30)),
        camera_factory=lambda plan: sink,
        decode=lambda frame, width, height: f"rgb-{width}x{height}",
        **kwargs,
    )
    return runner, sink


PHONE_STATS = {
    "fps": 29.8,
    "encode_ms": 12.4,
    "send_ms": 1.9,
    "bytes_per_frame": 96000,
    "quality": 75,
    "width": 1280,
    "height": 720,
}


def test_the_pc_numbers_come_from_the_pump(monkeypatch: pytest.MonkeyPatch) -> None:
    import phonebridge.live.runner as runner_module

    monkeypatch.setattr(runner_module, "_to_array", lambda image: image)

    async def scenario() -> LiveStatus:
        session = FakeSession()
        runner, sink = build(session, stats=True)
        await runner.start()
        try:
            for seq in range(3):
                runner._camera.push(frame_body(seq))  # noqa: SLF001
                await asyncio.sleep(0.06)
            await asyncio.sleep(0.05)
            return runner.status()
        finally:
            await runner.close()

    status = asyncio.run(scenario())
    pc = status.camera_stats["pc"]

    assert pc["received"] >= 3, "every frame the phone sent is counted"
    assert pc["sent"] >= 1
    assert pc["bytes_per_frame"] > 0, "the frame size is measured, not assumed"
    assert "decode_ms" in pc and "convert_ms" in pc and "send_ms" in pc
    assert pc["depth"] in (0, 1)
    assert "dropped" in pc


def test_the_phone_numbers_come_from_its_own_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import phonebridge.live.runner as runner_module

    monkeypatch.setattr(runner_module, "_to_array", lambda image: image)

    async def scenario() -> tuple[LiveStatus, FakeSession]:
        session = FakeSession(stats=PHONE_STATS)
        runner, _ = build(session, stats=True)
        await runner.start()
        try:
            runner._camera.push(frame_body(0))  # noqa: SLF001
            await asyncio.sleep(1.3)  # the poll is once a second
            return runner.status(), session
        finally:
            await runner.close()

    status, session = asyncio.run(scenario())

    assert "camera.status" in session.methods(), "the PC asks the phone what it measured"
    phone = status.camera_stats["phone"]
    assert phone["encode_ms"] == 12.4
    assert phone["quality"] == 75
    assert phone["width"] == 1280 and phone["height"] == 720


def test_without_stats_the_phone_is_not_polled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The extra RPC costs something, so it only runs when someone is looking."""
    import phonebridge.live.runner as runner_module

    monkeypatch.setattr(runner_module, "_to_array", lambda image: image)

    async def scenario() -> FakeSession:
        session = FakeSession(stats=PHONE_STATS)
        runner, _ = build(session)
        await runner.start()
        try:
            runner._camera.push(frame_body(0))  # noqa: SLF001
            await asyncio.sleep(1.2)
        finally:
            await runner.close()
        return session

    session = asyncio.run(scenario())

    assert "camera.status" not in session.methods()
    assert session.methods().count("camera.start") == 1


def test_the_phone_stats_reach_the_status_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import phonebridge.live.runner as runner_module

    monkeypatch.setattr(runner_module, "_to_array", lambda image: image)

    async def scenario() -> dict[str, Any]:
        session = FakeSession(stats=PHONE_STATS)
        runner, _ = build(session, stats=True)
        await runner.start()
        try:
            runner._camera.push(frame_body(0))  # noqa: SLF001
            await asyncio.sleep(1.3)
            return runner.status().to_dict()
        finally:
            await runner.close()

    payload = asyncio.run(scenario())

    assert payload["camera"]["stats"]["phone"]["encode_ms"] == 12.4
    assert "pc" in payload["camera"]["stats"]


# --------------------------------------------------------------------------- #
# the CLI side
# --------------------------------------------------------------------------- #
def test_the_status_loop_prints_the_measurements(capsys: pytest.CaptureFixture[str]) -> None:
    status = LiveStatus(
        connected=True,
        camera_state="active",
        fps=29.6,
        camera_stats={
            "pc": {
                "fps": 29.6,
                "received": 30,
                "bytes_per_frame": 98304,
                "decode_ms": 6.2,
                "convert_ms": 2.1,
                "send_ms": 1.4,
                "depth": 0,
                "dropped": 3,
            },
            "phone": PHONE_STATS,
        },
    )
    runner = SimpleNamespace(status=lambda: status)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(live._status_loop(runner, stop, quiet=False, stats=True))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(scenario())
    out = capsys.readouterr().out

    assert "29.6 fps" in out and "decode   6.2 ms" in out
    assert "encode  12.4 ms" in out and "q75" in out


def test_the_status_loop_is_quiet_about_stats_unless_asked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = LiveStatus(connected=True, camera_state="active", camera_stats={"pc": {"fps": 30.0}})
    runner = SimpleNamespace(status=lambda: status)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(live._status_loop(runner, stop, quiet=True))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(scenario())

    assert capsys.readouterr().out == ""
