"""Re-channelling and resampling of PCM16 — the maths behind the device edges.

Both defects these cover were invisible to the old fakes: mono written into a
stereo endpoint (half speed / refused stream) and a 44.1 kHz capture labelled as
48 kHz (9 % fast, i.e. a chipmunk). Neither raises anywhere, which is exactly why
they need arithmetic tests rather than "it did not throw" tests.
"""

from __future__ import annotations

import math
from array import array

import pytest

from phonebridge.audio.convert import LinearResampler, remix, samples


def pcm(values: list[int]) -> bytes:
    return array("h", values).tobytes()


def values(payload: bytes) -> list[int]:
    out = array("h")
    out.frombytes(payload)
    return list(out)


# --------------------------------------------------------------------------- #
# channels
# --------------------------------------------------------------------------- #
def test_mono_to_stereo_duplicates_each_sample() -> None:
    assert values(remix(pcm([1, 2, 3]), 1, 2)) == [1, 1, 2, 2, 3, 3]


def test_stereo_to_mono_averages_without_overflowing() -> None:
    left_right = pcm([32767, 32767, -32768, -32768, 100, 200])
    assert values(remix(left_right, 2, 1)) == [32767, -32768, 150]


def test_matching_channel_counts_return_the_same_bytes() -> None:
    payload = pcm([1, 2])
    assert remix(payload, 1, 1) is payload
    assert remix(payload, 2, 2) is payload


def test_a_trailing_half_sample_is_dropped_not_interpreted() -> None:
    assert values(remix(pcm([7]) + b"\x01", 1, 2)) == [7, 7]


def test_an_odd_frame_is_never_invented() -> None:
    """Five samples of "stereo" is two frames and a half: the half is dropped."""
    assert len(values(remix(pcm([1, 2, 3, 4, 5]), 2, 1))) == 2


def test_unsupported_shapes_are_passed_through_rather_than_guessed() -> None:
    payload = pcm([1, 2, 3, 4, 5, 6])
    assert remix(payload, 3, 2) is payload, "a wrong guess would be a speed defect"


def test_empty_payload_is_a_no_op() -> None:
    assert remix(b"", 1, 2) == b""
    assert samples(b"") == array("h")


# --------------------------------------------------------------------------- #
# rate
# --------------------------------------------------------------------------- #
def test_the_resampler_is_a_no_op_when_the_rates_match() -> None:
    resampler = LinearResampler(48_000, 48_000)
    payload = pcm([1, 2, 3])
    assert resampler.identity is True
    assert resampler.process(payload) is payload


def test_44k_to_48k_keeps_the_duration_and_not_the_sample_count() -> None:
    """The defect this replaces: 44.1 kHz played as 48 kHz is 9 % fast."""
    resampler = LinearResampler(44_100, 48_000)
    one_second = pcm([0] * 44_100)

    out = values(resampler.process(one_second))

    assert len(out) == pytest.approx(48_000, abs=5), f"got {len(out)}"


def test_chunked_input_produces_the_same_count_as_one_big_chunk() -> None:
    """Statefulness: 20 ms frames must not restart the interpolation each time."""
    source = [int(10_000 * math.sin(2 * math.pi * 100 * i / 44_100)) for i in range(44_100)]
    payload = pcm(source)

    whole = LinearResampler(44_100, 48_000)
    expected = len(values(whole.process(payload)))

    chunked = LinearResampler(44_100, 48_000)
    per_chunk = 44_100 // 10
    produced = 0
    for start in range(0, len(source) - per_chunk + 1, per_chunk):
        produced += len(values(chunked.process(pcm(source[start : start + per_chunk]))))

    assert produced == pytest.approx(expected, abs=2)


def test_resampled_output_stays_inside_the_pcm16_range() -> None:
    resampler = LinearResampler(44_100, 48_000)
    square = pcm([32767, -32768] * 500)

    out = values(resampler.process(square))

    assert out and all(-32768 <= value <= 32767 for value in out)


def test_a_single_sample_is_held_until_it_can_be_interpolated() -> None:
    resampler = LinearResampler(44_100, 48_000)
    assert resampler.process(pcm([123])) == b""
    out = values(resampler.process(pcm([456])))
    assert out, "the held sample must be used, not dropped"
    assert out[0] == 123


def test_zero_rates_are_refused() -> None:
    with pytest.raises(ValueError):
        LinearResampler(0, 48_000)
