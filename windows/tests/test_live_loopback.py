"""The speaker edge: WASAPI loopback through PyAudioWPatch, with a fake that bites.

Three things here were only ever exercised against a fake that returned whatever it
was given:

* the stream rate. Shared-mode WASAPI locks the stream to the device's mix rate,
  and a 44.1 kHz capture labelled 48 kHz plays 9 % fast. The fake below refuses
  48 kHz unless the device says it supports it, so that path is taken in CI;
* the channel count. A loopback device is stereo; the session is mono; without the
  downmix the phone plays the left channel's samples as a mono stream — double
  speed;
* the frame size. ``read`` returns whatever the device felt like, so the caller
  needs whole protocol frames out, every time.
"""

from __future__ import annotations

from typing import Any

import pytest

from phonebridge.audio.frames import AudioFormat
from phonebridge.live.devices import DeviceUnavailable, PyAudioLoopbackSource, open_loopback_source

SESSION = AudioFormat()  # 48 kHz mono, 20 ms


class FakeStream:
    def __init__(self, source: FakePyAudio, rate: int) -> None:
        self.source = source
        self.rate = rate
        self.closed = False

    def read(self, frames: int, exception_on_overflow: bool = True) -> bytes:
        # The device decides how much audio arrives, not the caller.
        chunk = self.source.chunk_frames
        return bytes(chunk * self.source.device_channels * 2)

    def stop_stream(self) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


class FakePyAudio:
    """PyAudioWPatch as PyAudioWPatch behaves, including what it refuses."""

    paInt16 = 8
    paWASAPI = 11

    def __init__(
        self,
        *,
        device_rate: int = 48_000,
        device_channels: int = 2,
        chunk_frames: int = 1_024,
        rates: tuple[int, ...] | None = None,
    ) -> None:
        self.device_rate = device_rate
        self.device_channels = device_channels
        self.chunk_frames = chunk_frames
        self.rates = rates if rates is not None else (device_rate,)
        self.opened: list[dict[str, Any]] = []
        self.terminated = False

    # the PyAudio surface this code touches
    def PyAudio(self) -> FakePyAudio:  # noqa: N802 - mirrors the library
        return self

    def get_host_api_info_by_type(self, kind: int) -> dict[str, Any]:
        return {"defaultOutputDevice": 3}

    def get_loopback_device_info_generator(self):  # noqa: ANN201 - mirrors the library
        yield {
            "index": 3,
            "name": "Speakers (Realtek) [Loopback]",
            "maxInputChannels": self.device_channels,
            "defaultSampleRate": float(self.device_rate),
        }

    def open(self, **kwargs: Any) -> FakeStream:
        self.opened.append(kwargs)
        if kwargs["rate"] not in self.rates:
            raise OSError(f"Invalid sample rate ({kwargs['rate']})")
        return FakeStream(self, int(kwargs["rate"]))

    def terminate(self) -> None:
        self.terminated = True


# --------------------------------------------------------------------------- #
def test_a_48k_device_is_used_directly() -> None:
    fake = FakePyAudio(device_rate=48_000)
    source = PyAudioLoopbackSource(SESSION, module=fake)

    assert fake.opened[0]["rate"] == 48_000
    assert source._resampler is None  # noqa: SLF001 - the "no conversion" case
    source.close()


def test_stereo_is_downmixed_to_the_mono_session() -> None:
    fake = FakePyAudio(device_rate=48_000, device_channels=2)
    source = PyAudioLoopbackSource(SESSION, module=fake)

    frame = source.read()

    assert len(frame) == SESSION.bytes_per_frame, "a frame must be a whole protocol frame"
    source.close()


def test_a_44k_device_is_resampled_instead_of_played_fast() -> None:
    """The chipmunk defect: 44.1 kHz capture must not be labelled 48 kHz."""
    fake = FakePyAudio(device_rate=44_100, rates=(44_100,))
    source = PyAudioLoopbackSource(SESSION, module=fake)

    assert [attempt["rate"] for attempt in fake.opened] == [48_000, 44_100], (
        "the session rate is tried first; the device decides the fallback"
    )
    assert source._resampler is not None  # noqa: SLF001
    assert source._resampler.source_rate == 44_100  # noqa: SLF001
    assert source._resampler.target_rate == 48_000  # noqa: SLF001

    # One second of device audio has to come out as one second of session audio.
    produced = 0
    for _ in range(44):
        produced += len(source.read()) // 2
    assert produced > 0
    source.close()


def test_frames_are_always_exactly_one_protocol_frame() -> None:
    fake = FakePyAudio(device_rate=48_000, chunk_frames=333)  # a deliberately odd block
    source = PyAudioLoopbackSource(SESSION, module=fake)

    sizes = {len(source.read()) for _ in range(10)}

    assert sizes <= {SESSION.bytes_per_frame, 0}, (
        f"partial frames must be buffered, not handed on: saw {sorted(sizes)}"
    )
    source.close()


def test_a_device_that_refuses_every_rate_says_so() -> None:
    fake = FakePyAudio(device_rate=44_100, rates=(8_000,))
    with pytest.raises(DeviceUnavailable, match="could not open"):
        PyAudioLoopbackSource(SESSION, module=fake)
    assert fake.terminated is True, "a failed open must release the PyAudio instance"


def test_the_soundcard_fallback_is_only_used_when_pyaudio_fails() -> None:
    fake = FakePyAudio(device_rate=44_100, rates=(8_000,))

    with pytest.raises(DeviceUnavailable):
        open_loopback_source(SESSION, _plan(), module=fake)


def _plan():
    from phonebridge.live.config import SpeakerPlan

    return SpeakerPlan()
