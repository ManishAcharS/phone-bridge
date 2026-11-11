"""Audio framing and sequencing: the parts a network makes wrong.

Every assertion here is about a frame a real network can produce — reordered,
duplicated, late, truncated, oversized, or with a sequence that wrapped — because
those are the failures that turn into a crackle the user blames on the phone.
"""

from __future__ import annotations

import pytest

from phonebridge.audio.frames import (
    FLAG_MUTED,
    FLAG_START,
    FLAG_STOP,
    HEADER_SIZE,
    MAX_AUDIO_BODY,
    AudioFormat,
    AudioFrame,
    AudioFrameError,
    SequenceGate,
    decode_frame,
)


# --------------------------------------------------------------------------- #
# format
# --------------------------------------------------------------------------- #
def test_the_default_format_is_48k_mono_20ms() -> None:
    fmt = AudioFormat()
    assert fmt.sample_rate == 48_000
    assert fmt.channels == 1
    assert fmt.frame_ms == 20
    assert fmt.samples_per_frame == 960
    assert fmt.bytes_per_frame == 1920


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sample_rate": 4_000},
        {"sample_rate": 192_000},
        {"channels": 0},
        {"channels": 3},
        {"frame_ms": 1},
        {"frame_ms": 500},
        {"codec": 9},
    ],
)
def test_an_impossible_format_is_refused_at_construction(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        AudioFormat(**kwargs)


def test_a_format_round_trips_through_a_dict() -> None:
    fmt = AudioFormat(sample_rate=16_000, channels=2, frame_ms=40)
    assert AudioFormat.from_dict(fmt.as_dict()) == fmt


def test_a_missing_or_empty_format_falls_back_to_the_default() -> None:
    assert AudioFormat.from_dict(None) == AudioFormat()
    assert AudioFormat.from_dict({}) == AudioFormat()


def test_a_hostile_format_raises_instead_of_being_guessed() -> None:
    with pytest.raises(AudioFrameError):
        AudioFormat.from_dict({"sample_rate": "fast", "channels": 1})
    with pytest.raises(AudioFrameError):
        AudioFormat.from_dict({"sample_rate": 48_000, "channels": 99})


# --------------------------------------------------------------------------- #
# encode / decode
# --------------------------------------------------------------------------- #
def test_a_frame_round_trips_with_its_header_intact() -> None:
    payload = b"\x01\x02" * 960
    frame = AudioFrame(seq=7, timestamp_ms=140, payload=payload, flags=FLAG_START)
    decoded = decode_frame(frame.encode())
    assert decoded == frame
    assert decoded.start is True
    assert decoded.stop is False
    assert decoded.samples == 960


def test_the_header_is_eight_bytes_and_the_body_is_header_plus_payload() -> None:
    frame = AudioFrame(seq=0, timestamp_ms=0, payload=b"\x00\x00" * 10)
    body = frame.encode()
    assert HEADER_SIZE == 8
    assert len(body) == HEADER_SIZE + 20


def test_flags_survive_the_round_trip_individually() -> None:
    for flag, attr in ((FLAG_START, "start"), (FLAG_STOP, "stop"), (FLAG_MUTED, "muted")):
        frame = AudioFrame(seq=1, timestamp_ms=1, payload=b"\x00\x00", flags=flag)
        assert getattr(decode_frame(frame.encode()), attr) is True


@pytest.mark.parametrize(
    "body",
    [
        b"",  # empty
        b"\x01\x00",  # shorter than the header
        b"\x01\x00\x00\x05\x00\x00\x00\x00",  # header only, no payload is legal
        b"\x09\x00\x00\x00\x00\x00\x00\x00\x00\x00",  # unknown codec 9
    ],
)
def test_a_bad_frame_is_rejected(body: bytes) -> None:
    if body == b"\x01\x00\x00\x05\x00\x00\x00\x00":
        # A header with an empty payload is a legal keep-alive/stop frame.
        assert decode_frame(body).payload == b""
        return
    with pytest.raises(AudioFrameError):
        decode_frame(body)


def test_an_odd_payload_length_is_not_whole_samples() -> None:
    body = AudioFrame(seq=0, timestamp_ms=0, payload=b"\x00\x00\x00").encode()
    with pytest.raises(AudioFrameError):
        decode_frame(body)


def test_an_oversized_frame_is_refused_on_both_sides() -> None:
    with pytest.raises(AudioFrameError):
        AudioFrame(seq=0, timestamp_ms=0, payload=b"\x00" * (MAX_AUDIO_BODY + 2)).encode()
    oversized = b"\x01\x00\x00\x05\x00\x00\x00\x00" + b"\x00" * (MAX_AUDIO_BODY + 2)
    with pytest.raises(AudioFrameError):
        decode_frame(oversized)


def test_a_sequence_or_timestamp_out_of_range_cannot_be_encoded() -> None:
    with pytest.raises(AudioFrameError):
        AudioFrame(seq=70_000, timestamp_ms=0, payload=b"").encode()
    with pytest.raises(AudioFrameError):
        AudioFrame(seq=0, timestamp_ms=2**32, payload=b"").encode()
    with pytest.raises(AudioFrameError):
        AudioFrame(seq=0, timestamp_ms=-1, payload=b"").encode()


# --------------------------------------------------------------------------- #
# sequencing
# --------------------------------------------------------------------------- #
def frame(seq: int, payload: bytes = b"\x00\x00") -> AudioFrame:
    return AudioFrame(seq=seq, timestamp_ms=seq * 20, payload=payload)


def test_frames_in_order_are_delivered_immediately() -> None:
    gate = SequenceGate()
    for seq in range(5):
        assert gate.offer(frame(seq)) == [frame(seq)]
    assert gate.reordered == 0


def test_one_frame_arriving_early_is_held_and_released_in_order() -> None:
    gate = SequenceGate()
    assert gate.offer(frame(0)) == [frame(0)]
    assert gate.offer(frame(2)) == []  # early: held
    assert gate.offer(frame(1)) == [frame(1), frame(2)]
    assert gate.reordered == 1


def test_a_duplicate_is_dropped_and_counted() -> None:
    gate = SequenceGate()
    gate.offer(frame(0))
    assert gate.offer(frame(0)) == []
    assert gate.duplicates == 1


def test_a_re_delivered_frame_is_a_duplicate_not_a_replay() -> None:
    gate = SequenceGate(window=4)
    for seq in range(10):
        gate.offer(frame(seq))
    assert gate.offer(frame(3)) == []
    assert gate.duplicates == 1
    assert gate.late == 0


def test_a_frame_skipped_by_a_resync_is_late_not_duplicate() -> None:
    """0 then 5 resynchronises; 2 was never delivered, and is now too old to play."""
    gate = SequenceGate(window=2)
    gate.offer(frame(0))
    gate.offer(frame(5))
    assert gate.offer(frame(2)) == []
    assert gate.late == 1
    assert gate.duplicates == 0


def test_a_jumped_sequence_resynchronises_instead_of_waiting_forever() -> None:
    gate = SequenceGate(window=4)
    gate.offer(frame(0))
    # The phone restarted its stream: the next seq is far ahead.
    assert gate.offer(frame(500)) == [frame(500)]
    assert gate.offer(frame(501)) == [frame(501)]


def test_the_sequence_wraps_at_65535_without_a_gap() -> None:
    gate = SequenceGate()
    gate.reset(65534)
    assert gate.offer(frame(65534)) == [frame(65534)]
    assert gate.offer(frame(65535)) == [frame(65535)]
    assert gate.offer(frame(0)) == [frame(0)]
    assert gate.offer(frame(1)) == [frame(1)]


def test_a_reset_makes_the_gate_accept_a_new_stream() -> None:
    gate = SequenceGate()
    gate.offer(frame(100))
    gate.reset(0)
    assert gate.offer(frame(0)) == [frame(0)]
