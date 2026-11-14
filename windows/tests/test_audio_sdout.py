"""The ``sounddevice`` contract, faked the way the library really behaves.

The fake that used to guard this code accepted ``write(bytes)``, so the microphone
sink passed CI and then failed on the first real frame with

    ValueError: dtype mismatch: 'bytes15360' vs 'int16'

because ``sounddevice.OutputStream`` converts its argument with numpy and refuses
anything whose dtype is not exactly its own. This file reproduces *that*: the
numpy-style stream rejects bytes, the raw stream rejects numpy arrays, and only
``RawOutputStream`` — which is what the production code must use — takes the
bytes that come off the wire.

Nothing here opens a device, needs numpy, or needs Windows.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from phonebridge.audio.backends import AudioBackendError, SoundDeviceSink
from phonebridge.audio.frames import AudioFormat
from phonebridge.audio.sdout import SdOutputError, SoundDeviceOutput, negotiate
from phonebridge.live.config import MicPlan
from phonebridge.live.devices import DeviceUnavailable, SoundDeviceWriter, open_cable_sink

CABLE = "CABLE Input (VB-Audio Virtual Cable)"


# --------------------------------------------------------------------------- #
# a fake with the library's checks
# --------------------------------------------------------------------------- #
def _dtype_name(data: Any) -> str:
    """What numpy would call it: bytes become ``bytes<len>``, arrays their dtype."""
    if isinstance(data, (bytes, bytearray, memoryview)):
        return f"bytes{len(data)}"
    dtype = getattr(data, "dtype", None)
    if dtype is not None:
        return str(getattr(dtype, "name", dtype))
    return type(data).__name__


class FakeStream:
    def __init__(self, fake: FakeSoundDevice, kwargs: dict[str, Any], *, raw: bool) -> None:
        self._fake = fake
        self.kwargs = kwargs
        self.raw = raw
        self.frames: list[bytes] = []
        self.started = False
        self.stopped = False
        self.closed = False
        self.failure: Exception | None = None

    # the bits of the sounddevice stream API the code touches
    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def write(self, data: Any) -> None:
        if self.failure is not None:
            raise self.failure
        if self.raw:
            if not isinstance(data, (bytes, bytearray, memoryview)):
                raise TypeError(
                    f"a RawOutputStream writes bytes, not {type(data).__name__}"
                )
        else:
            # numpy semantics: an exact dtype match or nothing.
            name = _dtype_name(data)
            if name != "int16":
                raise ValueError(f"dtype mismatch: '{name}' vs 'int16'")
        self.frames.append(bytes(data))


class FakeSoundDevice:
    """Enough of sounddevice to be wrong in the same places as the real one."""

    def __init__(
        self,
        *,
        outputs: int = 2,
        rates: tuple[int, ...] = (48_000,),
        name: str = CABLE,
        default_rate: int = 48_000,
    ) -> None:
        self.device = {
            "name": name,
            "max_input_channels": 0,
            "max_output_channels": outputs,
            "default_samplerate": float(default_rate),
        }
        self.rates = rates
        self.raw_streams: list[FakeStream] = []
        self.numpy_streams: list[FakeStream] = []
        self.checked: list[tuple[int, int]] = []

    # used by find_cable_input (no arguments) and by negotiate (device, kind)
    def query_devices(self, device: Any = None, kind: str | None = None) -> Any:
        if device is None and kind is None:
            return [self.device]
        if device not in (None, 0):
            raise ValueError(f"no device {device!r}")
        return dict(self.device)

    def check_output_settings(
        self, *, device: Any = None, samplerate: int, channels: int, dtype: str = "int16"
    ) -> None:
        self.checked.append((samplerate, channels))
        if samplerate not in self.rates:
            raise ValueError(f"Invalid sample rate: {samplerate}")
        if channels < 1 or channels > max(1, int(self.device["max_output_channels"])):
            raise ValueError(f"Invalid number of channels: {channels}")

    def RawOutputStream(self, **kwargs: Any) -> FakeStream:  # noqa: N802 - mirrors sounddevice
        stream = FakeStream(self, kwargs, raw=True)
        self.raw_streams.append(stream)
        return stream

    def OutputStream(self, **kwargs: Any) -> FakeStream:  # noqa: N802 - mirrors sounddevice
        stream = FakeStream(self, kwargs, raw=False)
        self.numpy_streams.append(stream)
        return stream


# --------------------------------------------------------------------------- #
# the regression itself
# --------------------------------------------------------------------------- #
def test_the_numpy_stream_rejects_the_wire_bytes_exactly_as_the_library_does() -> None:
    """Pins the fake: if this passes trivially, every test below proves nothing."""
    sd = FakeSoundDevice()
    stream = sd.OutputStream(dtype="int16", channels=2, samplerate=48_000)

    with pytest.raises(ValueError) as caught:
        stream.write(b"\x00\x00" * 3_840)  # one 80 ms mono frame, straight off the wire

    assert str(caught.value) == "dtype mismatch: 'bytes7680' vs 'int16'"


def test_the_writer_writes_the_wire_bytes_without_converting_them() -> None:
    sd = FakeSoundDevice(outputs=1)  # mono endpoint: this path converts nothing at all
    fmt = AudioFormat()
    writer = SoundDeviceWriter(fmt, 0, sd=sd)

    frame = bytes(range(256)) * 15  # 3_840 bytes: one 20 ms mono frame
    writer.write(frame)
    writer.close()

    assert sd.numpy_streams == [], "OutputStream would convert (and refuse) the bytes"
    stream = sd.raw_streams[0]
    assert stream.kwargs["dtype"] == "int16"
    assert stream.started is True
    assert stream.frames and stream.frames[0][: len(frame)] == frame, (
        "the samples must arrive unchanged: this path does no conversion at all"
    )


def test_a_stereo_endpoint_gets_stereo_frames() -> None:
    """VB-CABLE's "CABLE Input" is stereo; the phone's microphone is mono."""
    sd = FakeSoundDevice(outputs=2)
    fmt = AudioFormat()  # mono
    writer = SoundDeviceWriter(fmt, 0, sd=sd)

    mono = (1_000).to_bytes(2, "little", signed=True) * 100
    writer.write(mono)
    writer.close()

    stream = sd.raw_streams[0]
    assert stream.kwargs["channels"] == 2
    written = stream.frames[0]
    assert len(written) == len(mono) * 2, "each mono sample must feed both channels"
    assert written[:4] == (1_000).to_bytes(2, "little", signed=True) * 2
    # Same duration, which is the whole point: 100 samples either way.
    assert len(written) // 2 // 2 == 100


def test_a_mono_endpoint_is_left_alone() -> None:
    sd = FakeSoundDevice(outputs=1)
    writer = SoundDeviceWriter(AudioFormat(), 0, sd=sd)

    mono = b"\x01\x00" * 50
    writer.write(mono)
    writer.close()

    assert sd.raw_streams[0].kwargs["channels"] == 1
    assert sd.raw_streams[0].frames[0] == mono


def test_a_device_without_48k_is_opened_at_its_own_rate_and_resampled() -> None:
    """VB-CABLE on a 44.1 kHz system: same duration, so no chipmunk microphone."""
    sd = FakeSoundDevice(outputs=2, rates=(44_100,), default_rate=44_100)
    fmt = AudioFormat()  # 48 kHz mono
    writer = SoundDeviceWriter(fmt, 0, sd=sd)

    writer.write(bytes(2 * 4_800))  # 100 ms of 48 kHz mono
    writer.close()

    stream = sd.raw_streams[0]
    assert stream.kwargs["samplerate"] == 44_100
    assert stream.kwargs["blocksize"] == 0, "a resampled write is not block-aligned"
    written_samples = len(stream.frames[0]) // 2 // 2  # bytes -> int16 -> stereo frames
    assert written_samples == pytest.approx(4_410, abs=20), (
        f"100 ms at 44.1 kHz is 4410 samples, got {written_samples}"
    )


def test_a_device_that_accepts_no_format_says_what_it_tried() -> None:
    class RefusesEveryFormat(FakeSoundDevice):
        def check_output_settings(self, **kwargs: Any) -> None:
            raise ValueError("Device unavailable")

    sd = RefusesEveryFormat(outputs=2, rates=(48_000,), default_rate=44_100)
    with pytest.raises(SdOutputError) as caught:
        SoundDeviceOutput(AudioFormat(), 0, sd=sd)

    message = str(caught.value)
    assert "48000 Hz/2 ch" in message
    assert "48 kHz" in message, "the fix has to be in the message, not just the fact"


def test_a_22k_device_is_still_usable_by_resampling() -> None:
    sd = FakeSoundDevice(outputs=2, rates=(22_050,), default_rate=22_050)
    writer = SoundDeviceWriter(AudioFormat(), 0, sd=sd)

    writer.write(bytes(2 * 4_800))  # 100 ms
    writer.close()

    stream = sd.raw_streams[0]
    assert stream.kwargs["samplerate"] == 22_050
    assert len(stream.frames[0]) // 4 == pytest.approx(2_205, abs=20)


def test_writing_after_close_is_a_sentence_not_an_attribute_error() -> None:
    sd = FakeSoundDevice()
    writer = SoundDeviceWriter(AudioFormat(), 0, sd=sd)
    writer.close()

    with pytest.raises(DeviceUnavailable, match="closed"):
        writer.write(b"\x00\x00")


def test_an_odd_length_payload_is_trimmed_rather_than_crashing() -> None:
    sd = FakeSoundDevice()
    writer = SoundDeviceWriter(AudioFormat(), 0, sd=sd)

    writer.write(b"\x01\x02\x03")  # one and a half samples
    writer.close()

    assert len(sd.raw_streams[0].frames[0]) % 2 == 0


def test_an_empty_payload_writes_nothing() -> None:
    sd = FakeSoundDevice()
    writer = SoundDeviceWriter(AudioFormat(), 0, sd=sd)
    writer.write(b"")
    writer.close()

    assert sd.raw_streams[0].frames == []


def test_a_write_failure_becomes_a_user_sentence() -> None:
    sd = FakeSoundDevice()
    writer = SoundDeviceWriter(AudioFormat(), 0, sd=sd)
    sd.raw_streams[0].failure = RuntimeError("the device was removed")

    with pytest.raises(DeviceUnavailable) as caught:
        writer.write(b"\x00\x00")

    assert "writing to the microphone device failed" in str(caught.value)
    assert "the device was removed" in str(caught.value)


def test_an_unopenable_device_fails_before_any_frame_arrives() -> None:
    class Refusing(FakeSoundDevice):
        def RawOutputStream(self, **kwargs: Any) -> FakeStream:  # noqa: N802
            raise RuntimeError("device busy")

    with pytest.raises(DeviceUnavailable, match="could not open"):
        SoundDeviceWriter(AudioFormat(), 0, sd=Refusing())


def test_open_cable_sink_opens_the_stream_up_front() -> None:
    sd = FakeSoundDevice()
    sink = open_cable_sink(AudioFormat(), MicPlan(latency_ms=40), sd=sd)

    assert sd.raw_streams and sd.raw_streams[0].started is True, (
        "the device must be open before the phone is asked to capture"
    )
    sink.close()
    assert sd.raw_streams[0].closed is True


def test_the_in_app_monitor_uses_the_same_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """The GUI path writes into the default device with the same library."""
    sd = FakeSoundDevice(outputs=2)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)

    sink = SoundDeviceSink(AudioFormat())  # the default device: no index given

    sink.write(bytes(2 * 960))  # 20 ms of 48 kHz mono into a stereo endpoint
    sink.close()

    assert sd.numpy_streams == []
    assert sd.raw_streams[0].kwargs["channels"] == 2
    assert len(sd.raw_streams[0].frames[0]) == 960 * 2 * 2


def test_the_in_app_monitor_reports_a_refused_device(monkeypatch: pytest.MonkeyPatch) -> None:
    class Refusing(FakeSoundDevice):
        def RawOutputStream(self, **kwargs: Any) -> FakeStream:  # noqa: N802
            raise RuntimeError("device busy")

    monkeypatch.setitem(sys.modules, "sounddevice", Refusing())

    with pytest.raises(AudioBackendError, match="could not be opened"):
        SoundDeviceSink(AudioFormat())


def test_negotiate_prefers_the_session_rate_when_the_device_accepts_it() -> None:
    sd = FakeSoundDevice(outputs=2, rates=(48_000, 44_100), default_rate=44_100)

    assert negotiate(sd, 0, AudioFormat()) == (48_000, 2)
    assert sd.checked[0] == (48_000, 2)
