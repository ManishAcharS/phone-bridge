"""A dropped link is not the end of a live command.

The report behind these tests: a camera run survived a ~30 s link drop with the
status line saying ``camera idle 0.0 fps`` forever, because nothing ever opened a
new session or asked the phone for the camera again. The phone cannot help — it
closes its camera when the session that asked for it goes away — so the restart
has to happen here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from phonebridge.camera.stream import ACTIVE, IDLE, CameraStream
from phonebridge.live.config import CameraPlan, LivePlan
from phonebridge.live.runner import LiveRunner


class FakeSession:
    """The minimum of a session: RPC, blob subscription, and an open/closed bit."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.is_open = True
        self.calls: list[str] = []
        self.close_reason = ""
        self.queue: asyncio.Queue | None = None

    async def call(self, method: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self.calls.append(method)
        return {"result": {"ok": True}}

    def subscribe_blobs(self, stream_id: int, maxsize: int = 2) -> asyncio.Queue:
        self.queue = asyncio.Queue(maxsize=maxsize)
        return self.queue

    async def close(self, reason: str = "") -> None:
        self.is_open = False
        self.close_reason = reason


class Sink:
    def __init__(self) -> None:
        self.closed = False

    def send(self, frame: Any) -> None:
        return None

    def send_black(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def build(session: FakeSession, factory: Any) -> LiveRunner:
    return LiveRunner(
        session,
        LivePlan(camera=CameraPlan(width=320, height=240, fps=30)),
        camera_factory=lambda plan: Sink(),
        decode=lambda frame, width, height: "rgb",
        reconnect=factory,
    )


def test_a_dropped_link_reopens_the_session_and_restarts_the_camera() -> None:
    async def scenario() -> None:
        first = FakeSession("first")
        second = FakeSession("second")
        opened: list[str] = []

        async def reconnect() -> FakeSession:
            opened.append("connect")
            return second

        runner = build(first, reconnect)
        await runner.start()
        assert first.calls == ["camera.start"]

        # The phone rebooted, Wi-Fi dropped, whatever: the session is closed.
        first.is_open = False
        runner._watch_link()
        assert runner._reconnecting is not None
        await runner._reconnecting

        assert opened == ["connect"]
        # The camera had to be asked for again on the *new* session: the phone
        # closed it along with the old one.
        assert second.calls == ["camera.start"]
        assert runner.status().connected is True
        assert runner.status().camera_state == ACTIVE
        await runner.close()

    asyncio.run(scenario())


def test_the_status_line_says_reconnecting_instead_of_idle() -> None:
    async def scenario() -> None:
        session = FakeSession("only")
        gate = asyncio.Event()

        async def reconnect() -> FakeSession:
            await gate.wait()
            return session

        runner = build(session, reconnect)
        await runner.start()
        session.is_open = False
        runner._watch_link()
        # While the supervisor is working, "idle" would be a lie: the camera is
        # not idle, its link is being rebuilt.
        status = runner.status()
        assert status.camera_state == "reconnecting"
        assert "reconnecting" in status.line()
        gate.set()
        await runner._reconnecting
        await runner.close()

    asyncio.run(scenario())


def test_a_failed_attempt_is_retried_and_keeps_the_reason() -> None:
    async def scenario() -> None:
        first = FakeSession("first")
        second = FakeSession("second")
        attempts: list[str] = []

        async def reconnect() -> FakeSession:
            attempts.append("attempt")
            if len(attempts) == 1:
                raise OSError("connection refused")
            return second

        runner = build(first, reconnect)
        await runner.start()
        first.is_open = False
        runner._watch_link()
        await runner._reconnecting

        assert len(attempts) == 2, "a transient failure must be retried"
        assert second.calls == ["camera.start"]
        assert runner._detail == ""
        await runner.close()

    asyncio.run(scenario())


def test_a_live_link_is_never_reconnected() -> None:
    async def scenario() -> None:
        session = FakeSession("only")
        calls: list[str] = []

        async def reconnect() -> FakeSession:
            calls.append("connect")
            return session

        runner = build(session, reconnect)
        await runner.start()
        runner._watch_link()
        assert runner._reconnecting is None
        assert calls == []
        await runner.close()

    asyncio.run(scenario())


def test_a_stream_can_be_pointed_at_a_new_session() -> None:
    """`adopt` is what makes the restart possible: without it `start()` would
    return "already active" and the new session would never be asked."""

    async def scenario() -> None:
        first = FakeSession("first")
        second = FakeSession("second")
        stream = CameraStream(first)
        assert await stream.start() is True
        assert stream.state == ACTIVE
        assert first.calls == ["camera.start"]

        stream.adopt(second)
        assert stream.state == IDLE

        assert await stream.start() is True
        assert second.calls == ["camera.start"]
        await stream.close()

    asyncio.run(scenario())
