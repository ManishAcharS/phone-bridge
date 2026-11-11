"""Camera stream lifecycle with no phone and no screen.

The fake session stands in for an authenticated connection, so these tests cover
what actually breaks a preview: a refused permission, an unsupported resolution,
a burst that outruns the UI, a duplicate or stale frame, swapping cameras, and a
session that dies while streaming.

Nothing here is a hardware test. The JPEG bytes are synthetic (`authored`, not
captured), and the preview decoder's absence on this machine is itself asserted.
"""

from __future__ import annotations

import asyncio

import pytest

from phonebridge.camera.frames import FLAG_STOP, VideoConfig, VideoFrame, VideoFrameError
from phonebridge.camera.preview import PreviewUnavailable, preview_status
from phonebridge.camera.stream import (
    ACTIVE,
    ERROR,
    IDLE,
    CameraStream,
)
from phonebridge.protocol.messages import RpcError

JPEG = b"\xff\xd8" + b"\x00" * 48 + b"\xff\xd9"
SMALL = VideoConfig(width=640, height=480, fps=10)


def run(coro):
    return asyncio.run(coro)


async def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


def body(seq: int, payload: bytes = JPEG, **kwargs) -> bytes:
    return VideoFrame(
        seq=seq, timestamp_ms=seq * 66, payload=payload, width=640, height=480, **kwargs
    ).encode()


class FakeSession:
    def __init__(self, *, config: VideoConfig | None = None) -> None:
        self.is_open = True
        self.calls: list[tuple[str, dict | None]] = []
        self.queues: dict[int, asyncio.Queue] = {}
        self.refuse: dict[str, Exception] = {}
        self.granted = config

    async def call(self, method: str, params: dict | None = None):
        self.calls.append((method, params))
        refusal = self.refuse.get(method)
        if refusal is not None:
            raise refusal
        if method == "camera.start" and self.granted is not None:
            return {"result": {"ok": True, "config": self.granted.as_dict()}}
        return {"result": {"ok": True, **({"config": (params or {}).get("config")} if method == "camera.start" else {})}}

    def subscribe_blobs(self, stream_id: int, maxsize: int = 256) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.queues[stream_id] = queue
        return queue

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]


# --------------------------------------------------------------------------- #
# start / stop
# --------------------------------------------------------------------------- #
def test_start_negotiates_the_configuration_and_goes_active() -> None:
    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL)
        assert await camera.start() is True
        assert camera.snapshot().state == ACTIVE
        method, params = session.calls[0]
        assert method == "camera.start"
        assert params["config"] == SMALL.as_dict()
        await camera.close()

    run(scenario())


def test_the_phone_may_answer_with_a_different_configuration() -> None:
    async def scenario():
        granted = VideoConfig(1280, 720, 15, 60, "front")
        session = FakeSession(config=granted)
        camera = CameraStream(session, config=SMALL)
        await camera.start()
        assert camera.config.width == 1280
        assert camera.config.facing == "front"
        await camera.close()

    run(scenario())


def test_a_refused_camera_is_reported_and_stopped_compensatingly() -> None:
    async def scenario():
        session = FakeSession()
        session.refuse["camera.start"] = RpcError(
            "camera_denied", "camera permission is not granted on the phone"
        )
        camera = CameraStream(session, config=SMALL)
        assert await camera.start() is False
        snap = camera.snapshot()
        assert snap.state == ERROR
        assert "camera permission is not granted" in snap.detail
        assert "camera.stop" in session.methods(), "a refused start must not leave a camera open"

    run(scenario())


def test_starting_without_a_session_says_so_and_calls_nothing() -> None:
    async def scenario():
        session = FakeSession()
        session.is_open = False
        camera = CameraStream(session, config=SMALL)
        assert await camera.start() is False
        assert "not connected" in camera.snapshot().detail
        assert session.calls == []

    run(scenario())


def test_starting_twice_does_not_open_two_cameras() -> None:
    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL)
        await camera.start()
        await camera.start()
        assert session.methods().count("camera.start") == 1
        await camera.close()

    run(scenario())


def test_stop_tells_the_phone_and_drops_the_preview() -> None:
    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL)
        await camera.start()
        camera.push(body(0))
        assert await wait_for(lambda: camera.latest_frame() is not None)
        await camera.stop("user pressed stop")
        assert "camera.stop" in session.methods()
        assert camera.snapshot().state == IDLE
        assert camera.latest_frame() is None

    run(scenario())


# --------------------------------------------------------------------------- #
# frames
# --------------------------------------------------------------------------- #
def test_a_frame_becomes_the_preview() -> None:
    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL)
        await camera.start()
        camera.push(body(0))
        assert await wait_for(lambda: camera.latest_frame() is not None)
        snap = camera.snapshot()
        assert snap.has_frame is True
        assert snap.stats["received"] == 1
        await camera.close()

    run(scenario())


def test_an_invalid_frame_is_counted_and_ignored() -> None:
    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL)
        await camera.start()
        camera.push(b"\x00" * 4)  # shorter than a header
        assert await wait_for(lambda: camera.stats.invalid == 1)
        assert camera.latest_frame() is None
        assert camera.snapshot().state == ACTIVE, "one bad frame is not a failure"
        await camera.close()

    run(scenario())


def test_a_duplicate_frame_does_not_replace_the_newer_one() -> None:
    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL)
        await camera.start()
        camera.push(body(0))
        camera.push(body(1))
        assert await wait_for(
            lambda: camera.latest_frame() is not None and camera.latest_frame().seq == 1
        )
        camera.push(body(0))  # arrives late, must not rewind the preview
        await asyncio.sleep(0.02)
        assert camera.latest_frame().seq == 1
        assert camera.snapshot().stats["duplicates"] == 1
        await camera.close()

    run(scenario())


def test_a_full_queue_drops_the_oldest_frame_not_the_newest() -> None:
    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL, queue_frames=2)
        await camera.start()
        # Two frames fill the queue; the third must evict the first.
        assert camera.push(body(0)) is False
        assert camera.push(body(1)) is False
        assert camera.push(body(2)) is True
        assert camera.stats.dropped == 1
        assert await wait_for(lambda: camera.latest_frame() is not None, timeout=1)
        await camera.close()

    run(scenario())


def test_the_stop_frame_from_the_phone_ends_the_stream() -> None:
    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL)
        await camera.start()
        camera.push(body(0, flags=FLAG_STOP))
        assert await wait_for(lambda: camera.snapshot().state == IDLE)
        assert "stopped the camera" in camera.snapshot().detail
        await camera.close()

    run(scenario())


def test_the_end_of_the_stream_is_the_end_of_the_preview() -> None:
    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL)
        await camera.start()
        camera.push(body(0))
        assert await wait_for(lambda: camera.latest_frame() is not None)
        camera.push(None)  # documented end-of-stream marker
        assert await wait_for(lambda: camera.snapshot().state == IDLE)
        assert "session ended" in camera.snapshot().detail

    run(scenario())


# --------------------------------------------------------------------------- #
# switching cameras
# --------------------------------------------------------------------------- #
def test_switching_camera_restarts_the_stream_on_the_other_lens() -> None:
    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL)
        await camera.start()
        camera.push(body(0))
        assert await camera.switch("front") is True
        assert camera.config.facing == "front"
        assert camera.snapshot().state == ACTIVE
        starts = [p for m, p in session.calls if m == "camera.start"]
        assert len(starts) == 2
        assert starts[-1]["config"]["facing"] == "front"
        await camera.close()

    run(scenario())


def test_switching_while_idle_only_records_the_choice() -> None:
    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL)
        assert await camera.switch("front") is True
        assert camera.config.facing == "front"
        assert session.calls == [], "nothing should be opened just by picking a lens"

    run(scenario())


def test_an_unknown_lens_is_refused() -> None:
    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL)
        assert await camera.switch("wide-angle") is False
        assert camera.snapshot().state == ERROR
        assert session.calls == []

    run(scenario())


# --------------------------------------------------------------------------- #
# failures and teardown
# --------------------------------------------------------------------------- #
def test_a_session_that_dies_mid_stream_returns_to_idle_or_error() -> None:
    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL)
        await camera.start()
        session.is_open = False
        await camera.stop("session ended")
        assert camera.snapshot().state == IDLE
        assert "camera.stop" not in session.methods()[1:], "a closed session gets no RPC"

    run(scenario())


def test_close_is_safe_when_nothing_started() -> None:
    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL)
        await camera.close()
        assert camera.snapshot().state == IDLE

    run(scenario())


def test_the_snapshot_is_a_copy_and_serialises_without_the_image() -> None:
    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL)
        await camera.start()
        camera.push(body(0))
        snapshot = camera.snapshot()
        snapshot.state = "tampered"
        assert camera.snapshot().state == ACTIVE
        payload = camera.snapshot().to_dict()
        assert "has_frame" in payload and "frame" not in payload
        await camera.close()

    run(scenario())


def test_a_broken_callback_does_not_break_the_stream() -> None:
    def exploding(_snapshot) -> None:
        raise RuntimeError("the UI thread is gone")

    async def scenario():
        session = FakeSession()
        camera = CameraStream(session, config=SMALL, on_change=exploding)
        await camera.start()
        camera.push(body(0))
        assert await wait_for(lambda: camera.latest_frame() is not None)
        await camera.close()

    run(scenario())


# --------------------------------------------------------------------------- #
# preview honesty
# --------------------------------------------------------------------------- #
def test_the_preview_reports_whether_it_can_draw_anything() -> None:
    status = preview_status()
    assert status["library"] == "Pillow"
    assert status["verified_here"] is False, "importability is not a rendering test"
    if not status["available"]:
        assert "pip install pillow" in str(status["detail"])


def test_decoding_without_pillow_explains_instead_of_failing_obscurely() -> None:
    from phonebridge.camera import preview

    if preview_status()["available"]:
        pytest.skip("Pillow is installed here, so the missing-library path is unreachable")
    frame = VideoFrame(seq=0, timestamp_ms=0, payload=JPEG, width=640, height=480)
    with pytest.raises(PreviewUnavailable) as excinfo:
        preview.decode(frame)
    assert "pip install pillow" in str(excinfo.value)


def test_a_configuration_error_is_never_a_silent_default() -> None:
    with pytest.raises(VideoFrameError):
        VideoConfig.from_dict({"width": 0})
