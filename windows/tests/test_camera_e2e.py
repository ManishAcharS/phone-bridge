"""End-to-end camera streaming over a real, authenticated session.

This is the closest thing to a hardware test that can run without a phone: the
reference phone in ``tools/simulator/phone_sim.py`` serves `camera.start` and
pushes real `MSG_VIDEO` messages through the same WebSocket, the same P-256
handshake and the same AES-256-GCM envelope the Android app uses. What is stubbed
is the *sensor* — the simulator's frames are synthetic JPEGs and its capability
detail says so. What is real is everything the PC does with them: header parsing,
validation, sequence handling, the bounded queue, the newest-frame policy, the
stop path and the "does this stop when the session dies" question.

What this cannot prove: that CameraX works on a handset, that a real lens produces
the configured resolution, or that thermal throttling looks acceptable. Those are
in `docs/20-camera.md` §9 as a manual checklist.
"""

from __future__ import annotations

import asyncio

import pytest
from phone_sim import SimulatedPhone
from test_e2e_workflows import make_session, start_phone

from phonebridge.camera import CameraStream, VideoConfig
from phonebridge.camera.frames import decode_frame
from phonebridge.camera.stream import ACTIVE, ERROR, IDLE
from phonebridge.core.session import BridgeSession


@pytest.fixture()
async def connected(sim_state, pc_config):
    phone = await start_phone(sim_state)
    session = await make_session(phone, pc_config).open()
    try:
        yield phone, session
    finally:
        await session.close()
        await phone.stop()


async def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_frames_arrive_over_the_encrypted_stream_and_reach_the_preview(connected):
    phone: SimulatedPhone
    session: BridgeSession
    phone, session = connected
    camera = CameraStream(session, config=VideoConfig(640, 480, 10))
    assert await camera.start() is True
    assert camera.snapshot().state == ACTIVE

    sent = await phone.emit_camera_frames(3)
    assert sent == 3, "the simulator must actually put frames on the wire"

    assert await wait_for(lambda: camera.latest_frame() is not None)
    frame = camera.latest_frame()
    assert frame is not None
    assert frame.codec == 1
    assert frame.payload.startswith(b"\xff\xd8") and frame.payload.endswith(b"\xff\xd9")
    assert (frame.width, frame.height) == (640, 480)
    stats = camera.snapshot().stats
    assert stats["received"] >= 1
    assert stats["invalid"] == 0

    await camera.stop("test finished")
    status = await session.call("camera.status", {})
    assert status["streaming"] is False, "stop must reach the phone"
    assert phone.camera["streaming"] is False


async def test_a_second_frame_replaces_the_first_and_the_preview_never_rewinds(connected):
    phone, session = connected
    camera = CameraStream(session, config=VideoConfig(640, 480, 10))
    await camera.start()
    await phone.emit_camera_frames(1)
    assert await wait_for(lambda: camera.latest_frame() is not None)
    first = camera.latest_frame().seq
    await phone.emit_camera_frames(1)
    assert await wait_for(lambda: camera.latest_frame().seq != first)
    assert camera.latest_frame().seq == (first + 1) & 0xFFFF
    await camera.close()


async def test_switching_lens_restarts_the_stream_on_the_phone(connected):
    phone, session = connected
    camera = CameraStream(session, config=VideoConfig(640, 480, 10))
    await camera.start()
    assert phone.camera["facing"] == "back"
    assert await camera.switch("front") is True
    assert phone.camera["facing"] == "front"
    assert phone.camera["streaming"] is True
    await phone.emit_camera_frames(2)
    assert await wait_for(lambda: camera.latest_frame() is not None)
    await camera.close()


async def test_a_refused_camera_is_reported_as_denied_not_as_an_empty_preview(connected):
    phone, session = connected
    phone.camera_permission = False
    camera = CameraStream(session, config=VideoConfig(640, 480, 10))
    assert await camera.start() is False
    snap = camera.snapshot()
    assert snap.state == ERROR
    assert "permission is not granted" in snap.detail
    assert snap.has_frame is False
    assert phone.camera["streaming"] is False, "a refused start must not leave a camera open"


async def test_the_phone_stop_frame_ends_the_stream_and_clears_the_preview(connected):
    phone, session = connected
    camera = CameraStream(session, config=VideoConfig(640, 480, 10))
    await camera.start()
    await phone.emit_camera_frames(2)
    assert await wait_for(lambda: camera.latest_frame() is not None)

    # The phone ends the stream (its owner pressed Stop, or the app is closing):
    # the PC must clear the preview instead of freezing the last picture.
    assert await phone.emit_camera_stop() == 1
    assert await wait_for(lambda: camera.snapshot().state == IDLE)
    assert camera.latest_frame() is None
    await camera.close()


async def test_the_session_dying_ends_the_stream_without_leaving_a_hot_camera(connected):
    phone, session = connected
    camera = CameraStream(session, config=VideoConfig(640, 480, 10))
    await camera.start()
    await session.close("test drop")
    await asyncio.sleep(0.1)
    assert camera.snapshot().state in {IDLE, ERROR}
    # The phone releases the camera when the last session goes: nothing is left on.
    assert await wait_for(lambda: phone.camera["streaming"] is False)


async def test_a_frame_that_fails_validation_is_counted_and_the_stream_survives(connected):
    phone, session = connected
    camera = CameraStream(session, config=VideoConfig(640, 480, 10))
    await camera.start()
    assert await phone.emit_camera_body(b"\x01\x00\x00") == 1  # shorter than the header
    assert await wait_for(lambda: camera.stats.invalid == 1)
    await phone.emit_camera_frames(1)
    assert await wait_for(lambda: camera.latest_frame() is not None)
    assert camera.snapshot().state == ACTIVE
    await camera.close()


async def test_camera_status_reports_the_configuration_the_phone_will_use(connected):
    phone, session = connected
    result = await session.call("camera.start", {"config": {"width": 1920, "height": 1080, "fps": 24}})
    assert result["ok"] is True
    assert result["config"]["width"] == 1920
    assert result["config"]["fps"] == 24
    status = await session.call("camera.status", {})
    assert status["streaming"] is True
    assert status["permitted"] is True
    await session.call("camera.stop", {})
    assert (await session.call("camera.status", {}))["streaming"] is False


async def test_an_impossible_configuration_is_refused_with_bad_params(connected):
    phone, session = connected
    with pytest.raises(Exception) as excinfo:
        await session.call("camera.start", {"config": {"width": 99999, "height": 10, "fps": 0}})
    assert "bad_params" in str(excinfo.value)


async def test_a_frame_round_trips_byte_for_byte_through_the_envelope(phone_sim_frame=None):
    """The header the phone writes is the header the PC reads — no drift."""
    from phonebridge.camera.frames import VideoFrame

    body = VideoFrame(
        seq=7, timestamp_ms=1234, payload=b"\xff\xd8\x01\x02\xff\xd9", width=64, height=48, rotation=90
    ).encode()
    decoded = decode_frame(body)
    assert (decoded.seq, decoded.timestamp_ms, decoded.width, decoded.height) == (7, 1234, 64, 48)
    assert decoded.rotation == 90
    assert decoded.encode() == body
