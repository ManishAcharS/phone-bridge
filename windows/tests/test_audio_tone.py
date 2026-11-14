"""The test tone: a real `AudioSource` that produces a known note.

`--test-tone` exists so the phone can be tested without the PC's sound card,
without PyAudioWPatch and without anything being played. That only works if the
tone is a *proper* source: exact frames, PCM16LE, the frequency it claims, and a
documented end. A tone that is one byte off, or that silently emits silence,
would make a healthy phone look broken.
"""

from __future__ import annotations

import sys
from array import array

import pytest

from phonebridge.audio.frames import AudioFormat
from phonebridge.audio.tone import (
    DEFAULT_TONE_HZ,
    DEFAULT_TONE_SECONDS,
    ToneSource,
)

FORMAT = AudioFormat()  # 48 kHz mono 20 ms: the session's default


def samples(payload: bytes) -> list[int]:
    """The PCM16 samples in one frame, in the host's byte order."""
    values = array("h")
    values.frombytes(payload)
    if sys.byteorder == "big":  # the wire is little-endian, the array is native
        values.byteswap()
    return list(values)


def source(**kwargs) -> ToneSource:
    """A tone that does not sleep, so the tests do not either."""
    kwargs.setdefault("sleep", lambda seconds: None)
    return ToneSource(FORMAT, **kwargs)


# --------------------------------------------------------------------------- #
# the note
# --------------------------------------------------------------------------- #
def test_it_produces_the_documented_default() -> None:
    tone = source()

    assert tone.frequency == DEFAULT_TONE_HZ == 440.0
    assert tone.duration_ms == int(DEFAULT_TONE_SECONDS * 1000), "3 s, as the flag says"
    assert tone.frames_total == 150, "150 x 20 ms at 48 kHz"


def test_one_read_is_exactly_one_frame() -> None:
    tone = source()

    payload = tone.read()

    assert len(payload) == FORMAT.bytes_per_frame
    assert len(payload) == 1920


def test_the_frequency_is_the_one_it_promises() -> None:
    """440 Hz means 17.6 crossings per 20 ms frame — counted, not assumed."""
    tone = source()
    for _ in range(3):  # let the phase settle on a whole wave
        payload = tone.read()

    values = samples(payload)
    crossings = sum(1 for left, right in zip(values, values[1:], strict=False) if (left < 0) != (right < 0))
    expected = 2 * DEFAULT_TONE_HZ * FORMAT.frame_ms / 1000.0

    assert abs(crossings - expected) <= 2, f"{crossings} crossings, expected ~{expected}"


def test_the_level_is_audible_and_does_not_clip() -> None:
    tone = source()
    values = samples(tone.read()) + samples(tone.read())

    peak = max(abs(value) for value in values)
    assert peak == pytest.approx(0.25 * 32767, rel=0.01), "-12 dBFS: audible, not clips"
    assert peak < 32767


def test_a_frame_is_a_whole_number_of_samples() -> None:
    """A payload that is not a whole number of samples is rejected by the frame."""
    tone = source()
    payload = tone.read()

    assert len(payload) % 2 == 0
    assert len(payload) // 2 == FORMAT.samples_per_frame


def test_stereo_duplicates_instead_of_going_out_of_phase() -> None:
    stereo = ToneSource(AudioFormat(channels=2), sleep=lambda seconds: None)
    payload = stereo.read()

    values = samples(payload)
    assert len(values) == FORMAT.samples_per_frame * 2
    assert values[0] == values[1] and values[2] == values[3], "left == right, sample for sample"


def test_a_higher_frequency_has_more_crossings() -> None:
    low = source(frequency=220.0)
    high = source(frequency=880.0)

    def crossings(tone: ToneSource) -> int:
        values = samples(tone.read()) + samples(tone.read())
        return sum(1 for a, b in zip(values, values[1:], strict=False) if (a < 0) != (b < 0))

    assert crossings(high) > crossings(low) * 3


# --------------------------------------------------------------------------- #
# the end of the note
# --------------------------------------------------------------------------- #
def test_it_stops_after_the_requested_length() -> None:
    tone = source(seconds=0.1)  # 5 frames of 20 ms

    produced = [tone.read() for _ in range(5)]
    assert all(payload for payload in produced)
    assert tone.finished is True

    assert tone.read() == b"", "the tone has an end, and it is reported not raised"


class FakeClock:
    """A clock that only moves when the tone sleeps: pacing without waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def paced_tone(**kwargs) -> tuple[ToneSource, FakeClock]:
    clock = FakeClock()
    return ToneSource(FORMAT, clock=clock, sleep=clock.sleep, **kwargs), clock


def test_frames_are_paced_like_a_capture_device() -> None:
    """One frame per frame period — not the whole tone as fast as the loop can read.

    The phone buffers four 20 ms frames and drops the oldest when its queue is
    full, so an unpaced tone would be heard as a click at the end of the note.
    """
    tone, clock = paced_tone()

    for _ in range(5):
        tone.read()

    assert clock.now == pytest.approx(0.08), "five frames span four periods"
    assert len(clock.slept) == 4, "the first frame is due immediately"
    assert all(0 < slept <= 0.0201 for slept in clock.slept)


def test_three_seconds_of_tone_take_three_seconds() -> None:
    tone, clock = paced_tone()

    for _ in range(tone.frames_total):
        tone.read()

    assert clock.now == pytest.approx(2.98, abs=0.01), "not a burst, and not 4.7 s"
    assert max(clock.slept) <= 0.0201, "no single wait longer than a frame"


def test_a_finished_tone_still_paces_its_empty_reads() -> None:
    """The link reads again on an empty frame; an unpaced one would spin a core."""
    tone, clock = paced_tone(seconds=0.02)  # a single frame
    tone.read()

    for _ in range(3):
        assert tone.read() == b""

    assert len(clock.slept) == 3
    assert all(slept <= 0.0201 for slept in clock.slept)


def test_the_duration_is_the_number_of_frames_it_produces() -> None:
    tone = source(seconds=0.5)

    assert tone.duration_ms == 500
    assert tone.frames_total == 25
    assert abs(tone.frames_total * FORMAT.frame_ms - tone.duration_ms) == 0


def test_closing_it_is_harmless_and_stops_the_tone() -> None:
    tone = source()
    tone.close()

    assert tone.read() == b""
    assert tone.finished is True


# --------------------------------------------------------------------------- #
# refusals
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kwargs",
    [
        {"frequency": 5.0},  # below hearing, probably a mistake
        {"frequency": 30_000.0},  # above it
        {"seconds": 0.0},
        {"seconds": 120.0},
        {"amplitude": 0.0},
        {"amplitude": 2.0},
    ],
)
def test_an_impossible_tone_is_refused(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        ToneSource(FORMAT, **kwargs)


def test_the_wave_does_not_drift_over_a_long_tone() -> None:
    """Phase is carried between frames, so 150 frames are one continuous note.

    Counted over the whole tone rather than per frame: phase that was reset at
    every frame boundary would restart the wave 150 times and be audible as a
    buzz at 50 Hz.
    """
    tone = source()
    values: list[int] = []
    for _ in range(tone.frames_total):
        values.extend(samples(tone.read()))

    crossings = sum(1 for a, b in zip(values, values[1:], strict=False) if (a < 0) != (b < 0))
    expected = 2 * DEFAULT_TONE_HZ * tone.duration_ms / 1000.0

    assert abs(crossings - expected) <= 2, f"{crossings} crossings, expected {expected}"
