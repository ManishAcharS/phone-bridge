"""Camera framing: a lost frame must be a lost picture, never a broken one.

Every case here is a frame a real phone or a real network produces — truncated by
a closed socket, larger than a hotspot link can carry, out of order after a Wi-Fi
stall, duplicated by a retry, or with an impossible resolution in its header.
"""

from __future__ import annotations

import pytest

from phonebridge.camera.frames import (
    FLAG_KEY,
    FLAG_START,
    FLAG_STOP,
    HEADER_SIZE,
    MAX_VIDEO_BODY,
    VALID_ROTATIONS,
    FrameGate,
    VideoConfig,
    VideoFrame,
    VideoFrameError,
    decode_frame,
)

JPEG = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"


def frame(seq: int, payload: bytes = JPEG, **kwargs) -> VideoFrame:
    return VideoFrame(seq=seq, timestamp_ms=seq * 66, payload=payload, width=1280, height=720, **kwargs)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def test_the_default_configuration_is_modest_on_purpose() -> None:
    config = VideoConfig()
    assert (config.width, config.height) == (1280, 720)
    assert config.fps == 15
    assert config.facing == "back"
    assert config.label == "rear 1280x720 @ 15 fps"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"width": 100},
        {"height": 5000},
        {"fps": 0},
        {"fps": 120},
        {"quality": 5},
        {"quality": 100},
        {"facing": "sideways"},
    ],
)
def test_an_impossible_camera_configuration_is_refused(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        VideoConfig(**kwargs)


def test_a_configuration_round_trips_through_a_dict() -> None:
    config = VideoConfig(1920, 1080, 30, 80, "front")
    assert VideoConfig.from_dict(config.as_dict()) == config


def test_a_bad_configuration_raises_instead_of_being_guessed() -> None:
    with pytest.raises(VideoFrameError):
        VideoConfig.from_dict({"width": "wide"})
    with pytest.raises(VideoFrameError):
        VideoConfig.from_dict({"fps": 999})


def test_a_partial_configuration_keeps_the_fallback_values() -> None:
    parsed = VideoConfig.from_dict({"facing": "front"}, fallback=VideoConfig(640, 480, 10))
    assert (parsed.width, parsed.height, parsed.fps, parsed.facing) == (640, 480, 10, "front")


# --------------------------------------------------------------------------- #
# codec
# --------------------------------------------------------------------------- #
def test_a_frame_round_trips_with_its_metadata() -> None:
    original = frame(7, rotation=90, flags=FLAG_START | FLAG_KEY)
    decoded = decode_frame(original.encode())
    assert decoded.seq == 7
    assert decoded.rotation == 90
    assert decoded.width == 1280 and decoded.height == 720
    assert decoded.start is True and decoded.key is True
    assert decoded.stop is False
    assert decoded.payload == JPEG


def test_the_header_is_fourteen_bytes() -> None:
    assert HEADER_SIZE == 14
    assert len(frame(0).encode()) == 14 + len(JPEG)


def test_a_frame_shorter_than_its_header_is_rejected() -> None:
    with pytest.raises(VideoFrameError):
        decode_frame(b"\x01\x00\x00")


def test_a_payload_that_is_not_jpeg_is_rejected() -> None:
    body = VideoFrame(seq=0, timestamp_ms=0, payload=b"\x00" * 32, width=640, height=480).encode()
    with pytest.raises(VideoFrameError):
        decode_frame(body)


def test_a_truncated_jpeg_is_rejected_rather_than_displayed() -> None:
    with pytest.raises(VideoFrameError):
        decode_frame(frame(0, payload=b"\xff\xd8" + b"\x00" * 32).encode())


def test_an_unknown_codec_is_rejected() -> None:
    body = bytearray(frame(0).encode())
    body[0] = 9
    with pytest.raises(VideoFrameError):
        decode_frame(bytes(body))


def test_an_impossible_resolution_or_rotation_is_rejected() -> None:
    body = bytearray(frame(0).encode())
    body[8:10] = (0).to_bytes(2, "big")  # width 0
    with pytest.raises(VideoFrameError):
        decode_frame(bytes(body))

    bad_rotation = bytearray(frame(0).encode())
    bad_rotation[12:14] = (45).to_bytes(2, "big")
    with pytest.raises(VideoFrameError):
        decode_frame(bytes(bad_rotation))


def test_an_oversized_frame_is_refused_on_both_sides() -> None:
    big = b"\xff\xd8" + b"\x00" * (MAX_VIDEO_BODY + 2) + b"\xff\xd9"
    with pytest.raises(VideoFrameError):
        frame(0, payload=big).encode()
    header = b"\x01\x00\x00\x00\x00\x00\x00\x00\x05\x00\x02\xd0\x00\x00"
    with pytest.raises(VideoFrameError):
        decode_frame(header + big)


def test_out_of_range_metadata_cannot_be_encoded() -> None:
    with pytest.raises(VideoFrameError):
        VideoFrame(seq=70_000, timestamp_ms=0, payload=JPEG).encode()
    with pytest.raises(VideoFrameError):
        VideoFrame(seq=0, timestamp_ms=-1, payload=JPEG).encode()
    with pytest.raises(VideoFrameError):
        VideoFrame(seq=0, timestamp_ms=0, payload=JPEG, rotation=45).encode()


@pytest.mark.parametrize("rotation", VALID_ROTATIONS)
def test_every_legal_rotation_survives(rotation: int) -> None:
    assert decode_frame(frame(1, rotation=rotation).encode()).rotation == rotation


@pytest.mark.parametrize("flag", [FLAG_START, FLAG_STOP, FLAG_KEY])
def test_every_flag_survives(flag: int) -> None:
    assert decode_frame(frame(1, flags=flag).encode()).flags == flag


# --------------------------------------------------------------------------- #
# sequencing
# --------------------------------------------------------------------------- #
def test_frames_in_order_are_returned_immediately() -> None:
    gate = FrameGate()
    for seq in range(4):
        assert gate.offer(frame(seq)) == [frame(seq)]


def test_a_frame_arriving_early_is_held_and_released_in_order() -> None:
    gate = FrameGate()
    gate.offer(frame(0))
    assert gate.offer(frame(2)) == []
    assert [f.seq for f in gate.offer(frame(1))] == [1, 2]
    assert gate.reordered == 1


def test_a_duplicate_frame_is_not_shown_twice() -> None:
    gate = FrameGate()
    gate.offer(frame(0))
    assert gate.offer(frame(0)) == []
    assert gate.duplicates == 1


def test_a_frame_from_before_a_resync_is_late_not_shown() -> None:
    gate = FrameGate(window=2)
    gate.offer(frame(0))
    gate.offer(frame(5))  # jumped: display this, skip 1-4
    assert gate.offer(frame(2)) == []
    assert gate.late == 1


def test_a_long_gap_resynchronises_instead_of_stalling_the_preview() -> None:
    gate = FrameGate(window=4)
    gate.offer(frame(0))
    assert [f.seq for f in gate.offer(frame(400))] == [400]
    assert [f.seq for f in gate.offer(frame(401))] == [401]


def test_the_sequence_wraps_at_65535() -> None:
    gate = FrameGate()
    gate.reset(65534)
    gate.offer(frame(65534))
    gate.offer(frame(65535))
    assert [f.seq for f in gate.offer(frame(0))] == [0]


def test_reset_accepts_a_new_stream() -> None:
    gate = FrameGate()
    gate.offer(frame(500))
    gate.reset(0)
    assert [f.seq for f in gate.offer(frame(0))] == [0]
