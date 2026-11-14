"""Missing drivers, device lookup and the microphone sink — without hardware.

Nothing here opens a real device: the Windows libraries are injected, which is
also what proves they are imported lazily rather than at package import time.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from phonebridge.audio.frames import AudioFormat
from phonebridge.live import devices
from phonebridge.live.config import CameraPlan, MicPlan
from phonebridge.live.devices import (
    CableMicSink,
    DeviceUnavailable,
    SoundDeviceWriter,
    _apply_gain,
    _peak_dbfs,
    find_cable_input,
    open_cable_sink,
    open_virtual_camera,
)


class FakeDevice:
    def __init__(self, name: str, *, inputs: int = 0, outputs: int = 0) -> None:
        self.data = {
            "name": name,
            "max_input_channels": inputs,
            "max_output_channels": outputs,
        }

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


class FakeSoundDevice:
    def __init__(self, names: list[str], opened: list[bytes] | None = None) -> None:
        self.devices = [FakeDevice(name, outputs=2) for name in names]
        # A capture device with the same name sorts first in the list on purpose:
        # picking an input for output is the mistake this lookup exists to avoid.
        self.devices.insert(0, FakeDevice("Microphone", inputs=2))
        self.samples = opened if opened is not None else []

    def query_devices(self) -> list[FakeDevice]:
        return self.devices

    def OutputStream(self, **kwargs: Any) -> FakeStream:  # noqa: N802 - mirrors sounddevice
        return FakeStream(self.samples, kwargs)


class FakeStream:
    def __init__(self, sink: list[bytes], kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self._sink = sink
        self.started = False

    def start(self) -> None:
        self.started = True

    def write(self, payload: bytes) -> None:
        self._sink.append(payload)

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# lazy imports: the whole feature must not depend on Windows libraries to import
# --------------------------------------------------------------------------- #
def test_importing_the_package_pulls_in_no_windows_library() -> None:
    """The whole feature must import on a build server with none of them installed.

    Checked in a fresh interpreter: by the time this file runs, other tests may
    already have imported the modules, so an in-process ``sys.modules`` check
    would prove nothing.
    """
    code = (
        "import sys; import phonebridge.live; "
        "print(','.join(sorted(m for m in "
        "('pyvirtualcam', 'sounddevice', 'pyaudiowpatch', 'numpy') if m in sys.modules)))"
    )
    windows_dir = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(windows_dir)},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"imported eagerly: {result.stdout.strip()}"


def test_a_missing_pyvirtualcam_names_the_package_and_obs() -> None:
    with pytest.raises(DeviceUnavailable) as caught:
        devices._import_pyvirtualcam()

    message = str(caught.value)
    assert "pyvirtualcam" in message
    assert "OBS" in caught.value.hint
    assert devices.OBS_DOWNLOAD_URL in caught.value.hint


def test_no_usable_backend_reports_every_attempt() -> None:
    class FailingCameras:
        PixelFormat = type("PixelFormat", (), {"RGB": "RGB"})

        class Camera:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs
                raise RuntimeError(f"no device for {kwargs['backend']}")

    with pytest.raises(DeviceUnavailable) as caught:
        open_virtual_camera(CameraPlan(), module=FailingCameras)

    assert "obs: no device for obs" in str(caught.value)
    assert "unitycapture: no device for unitycapture" in str(caught.value)
    assert devices.OBS_DOWNLOAD_URL in caught.value.hint


def test_the_first_working_backend_wins_and_is_reported() -> None:
    opened: list[str] = []

    class WorkingCamera:
        PixelFormat = type("PixelFormat", (), {"RGB": "RGB"})

        class Camera:
            def __init__(self, **kwargs: Any) -> None:
                opened.append(kwargs["backend"])
                self.frames: list[Any] = []

            def send(self, frame: Any) -> None:
                self.frames.append(frame)

            def close(self) -> None:
                pass

    module = type(
        "M", (), {"PixelFormat": WorkingCamera.PixelFormat, "Camera": WorkingCamera.Camera}
    )
    camera = open_virtual_camera(CameraPlan(width=640, height=480, fps=15), module=module)

    assert camera.backend == "obs"
    assert opened == ["obs"]
    camera.send("frame")
    assert camera.frames_sent == 1
    camera.close()


# --------------------------------------------------------------------------- #
# VB-CABLE lookup
# --------------------------------------------------------------------------- #
def test_cable_input_is_found_by_name_and_must_be_an_output() -> None:
    sd = FakeSoundDevice(["CABLE Input (VB-Audio Virtual Cable)", "Speakers"])

    index = find_cable_input("CABLE Input", sd=sd)

    assert sd.devices[index]["name"].startswith("CABLE Input")
    assert sd.devices[index]["max_output_channels"] == 2


def test_a_missing_cable_names_vbcable_and_lists_what_was_found() -> None:
    sd = FakeSoundDevice(["Speakers", "Headphones"])

    with pytest.raises(DeviceUnavailable) as caught:
        find_cable_input("CABLE Input", sd=sd)

    assert "CABLE Input" in str(caught.value)
    assert devices.VB_CABLE_URL in caught.value.hint
    assert "Speakers" in caught.value.hint


def test_a_capture_device_is_never_chosen_as_the_output() -> None:
    sd = FakeSoundDevice([])
    sd.devices = [FakeDevice("CABLE Input", inputs=2, outputs=0)]

    with pytest.raises(DeviceUnavailable):
        find_cable_input("CABLE Input", sd=sd)


def test_open_cable_sink_opens_the_device_before_the_phone_starts() -> None:
    sd = FakeSoundDevice(["CABLE Input (VB-Audio Virtual Cable)"])

    sink = open_cable_sink(AudioFormat(), MicPlan(latency_ms=40), sd=sd)

    # The stream exists (started) before any frame arrives: a device that cannot
    # open must fail here, not on the first frame of a live call.
    assert sink._writer._stream is not None  # noqa: SLF001
    sink.close()


# --------------------------------------------------------------------------- #
# the sink itself
# --------------------------------------------------------------------------- #
class FakeWriter:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.closed = False

    def write(self, payload: bytes) -> None:
        self.frames.append(payload)

    def close(self) -> None:
        self.closed = True


def test_the_sink_writes_frames_it_receives() -> None:
    fmt = AudioFormat()
    writer = FakeWriter()
    sink = CableMicSink(fmt, device=None, latency_ms=40, writer=writer)

    silence = bytes(fmt.bytes_per_frame)
    for _ in range(3):
        sink.write(b"\x01\x00" * fmt.samples_per_frame)
    time.sleep(0.3)
    sink.close()

    assert writer.frames
    assert any(frame != silence for frame in writer.frames)
    assert writer.closed is True


def test_an_underrun_writes_silence_instead_of_stalling() -> None:
    fmt = AudioFormat()
    writer = FakeWriter()
    sink = CableMicSink(fmt, device=None, latency_ms=40, writer=writer)

    time.sleep(0.3)  # nothing was ever pushed
    sink.close()

    assert writer.frames, "the device must keep being fed, or the stream stalls"
    assert all(frame == bytes(fmt.bytes_per_frame) for frame in writer.frames)
    assert sink.stats["underruns"] > 0


def test_gain_scales_and_clips_instead_of_wrapping() -> None:
    pcm = (1000).to_bytes(2, "little", signed=True) * 3
    assert _apply_gain(pcm, 2.0) == (2000).to_bytes(2, "little", signed=True) * 3
    assert _apply_gain(pcm, 1.0) is pcm

    loud = (30000).to_bytes(2, "little", signed=True)
    assert _apply_gain(loud, 4.0) == (32767).to_bytes(2, "little", signed=True), (
        "clipping must saturate: wrapping turns a loud word into a loud click"
    )


def test_peak_level_reports_silence_and_full_scale() -> None:
    assert _peak_dbfs(b"") == float("-inf")
    assert _peak_dbfs(bytes(4)) == float("-inf")
    assert _peak_dbfs((16384).to_bytes(2, "little", signed=True)) == pytest.approx(-6.02, abs=0.1)


def test_a_writer_that_dies_stops_the_sink_and_is_reported() -> None:
    class DyingWriter(FakeWriter):
        def write(self, payload: bytes) -> None:
            raise DeviceUnavailable("device unplugged")

    fmt = AudioFormat()
    sink = CableMicSink(fmt, device=None, latency_ms=40, writer=DyingWriter())
    time.sleep(0.3)

    assert sink.error == "device unplugged"
    sink.close()


def test_sounddevice_writer_reports_an_unopenable_device() -> None:
    class Broken:
        def OutputStream(self, **kwargs: Any) -> Any:
            raise RuntimeError("device busy")

    writer = SoundDeviceWriter(AudioFormat(), 3, sd=Broken())

    with pytest.raises(DeviceUnavailable, match="could not open"):
        writer.write(b"\x00\x00")
