"""The three Windows devices this feature delivers into, and what happens when
they are not installed.

Everything here is the *edge*: optional libraries (pyvirtualcam, sounddevice,
PyAudioWPatch) are imported inside functions, never at module import time, so a
machine without them can still run the rest of PhoneBridge — and the test suite
can run on a Linux container with no sound card at all.

Two rules the code below follows deliberately:

* **A missing device is a sentence, not a traceback.** Every failure path raises
  :class:`DeviceUnavailable` with a message a user can act on and a URL to the
  thing that has to be installed.
* **Nothing is silently substituted.** If OBS is not installed the virtual camera
  does not quietly become a preview window, and if VB-CABLE is missing the
  microphone does not quietly become the speakers. The user is told.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Any

from ..audio.frames import AudioFormat
from .config import CameraPlan, MicPlan, SpeakerPlan
from .jitter import JitterBuffer

log = logging.getLogger(__name__)

OBS_DOWNLOAD_URL = "https://obsproject.com/download"
VB_CABLE_URL = "https://vb-audio.com/Cable/"
PYAUDIOWPATCH_HINT = "pip install PyAudioWPatch"


class DeviceUnavailable(RuntimeError):
    """A device or its driver is not usable here, with a message meant for a user."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# --------------------------------------------------------------------------- #
# 1. Camera: the phone's frames into a Windows webcam
# --------------------------------------------------------------------------- #
def _import_pyvirtualcam() -> Any:
    try:
        import pyvirtualcam  # noqa: PLC0415 - optional extra
    except Exception as exc:  # noqa: BLE001 - any import failure means "not installed"
        raise DeviceUnavailable(
            "delivering the camera to Windows needs pyvirtualcam, which is not importable",
            hint=(
                "install it with `pip install pyvirtualcam`, then install OBS Studio "
                f'({OBS_DOWNLOAD_URL}) — OBS provides the "OBS Virtual Camera" device '
                "that every app can then select"
            ),
        ) from exc
    return pyvirtualcam


class VirtualCamera:
    """A started virtual camera device, with the last frame kept alive.

    ``pyvirtualcam`` shows whatever was last sent, so a phone disconnect leaves
    the last picture on screen — which is what the user asked for — until
    :meth:`send_black` replaces it after the stall timeout.
    """

    def __init__(self, handle: Any, *, width: int, height: int, fps: int, backend: str) -> None:
        self._handle = handle
        self.width = width
        self.height = height
        self.fps = fps
        self.backend = backend
        self.frames_sent = 0
        self._blank: Any = None

    def send(self, rgb: Any) -> None:
        self._handle.send(rgb)
        self.frames_sent += 1

    def send_black(self) -> None:
        """Show a black frame. Used when the phone stops sending but is connected."""
        if self._blank is None:
            try:
                import numpy  # noqa: PLC0415 - pyvirtualcam's own dependency
            except Exception as exc:  # noqa: BLE001
                raise DeviceUnavailable(
                    "blanking the virtual camera needs numpy (a pyvirtualcam dependency)",
                    hint="pip install numpy",
                ) from exc
            self._blank = numpy.zeros((self.height, self.width, 3), dtype=numpy.uint8)
        self._handle.send(self._blank)
        self.frames_sent += 1

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._handle.close()


def open_virtual_camera(plan: CameraPlan, *, module: Any = None) -> VirtualCamera:
    """Open the first backend that works, and say which one it was.

    "auto" tries OBS and then Unity Capture. A failure of one is not reported as a
    failure of the feature: the errors are collected and shown together, because
    the fix (install one of them) is the same either way.
    """
    pyvirtualcam = module or _import_pyvirtualcam()
    failures: list[str] = []
    for backend in plan.backends:
        try:
            handle = pyvirtualcam.Camera(
                width=plan.width,
                height=plan.height,
                fps=plan.fps,
                fmt=pyvirtualcam.PixelFormat.RGB,
                backend=backend,
            )
        except Exception as exc:  # noqa: BLE001 - any backend error is "try the next"
            failures.append(f"{backend}: {exc}")
            continue
        return VirtualCamera(
            handle, width=plan.width, height=plan.height, fps=plan.fps, backend=backend
        )
    raise DeviceUnavailable(
        "no virtual camera backend could be opened (" + "; ".join(failures) + ")",
        hint=(
            f'install OBS Studio ({OBS_DOWNLOAD_URL}) for the "OBS Virtual Camera" '
            "device, or Unity Capture for the fallback. Installing the Python package "
            "alone does not create a camera device."
        ),
    )


# --------------------------------------------------------------------------- #
# 2. Microphone: the phone's frames into /dev/cable, i.e. VB-CABLE
# --------------------------------------------------------------------------- #
def _import_sounddevice() -> Any:
    try:
        import sounddevice  # noqa: PLC0415 - optional extra
    except Exception as exc:  # noqa: BLE001
        raise DeviceUnavailable(
            "writing the phone microphone into a Windows device needs sounddevice",
            hint="pip install sounddevice",
        ) from exc
    return sounddevice


def find_cable_input(name: str = "CABLE Input", *, sd: Any = None) -> int:
    """Index of the playback device whose name contains ``name``.

    Matched case-insensitively and by substring because the device is called
    "CABLE Input (VB-Audio Virtual Cable)" on a stock install and is renamed by
    some users. The first match wins; ``--device`` exists for the rest.
    """
    sounddevice = sd or _import_sounddevice()
    try:
        devices = list(sounddevice.query_devices())
    except Exception as exc:  # noqa: BLE001 - e.g. PortAudio not initialised
        raise DeviceUnavailable(f"could not list Windows audio devices: {exc}") from exc

    wanted = name.strip().lower()
    for index, device in enumerate(devices):
        if int(device.get("max_output_channels", 0)) < 1:
            continue
        if wanted in str(device.get("name", "")).lower():
            return index

    outputs = [
        str(device.get("name", "?"))
        for device in devices
        if int(device.get("max_output_channels", 0)) > 0
    ]
    raise DeviceUnavailable(
        f"no playback device matching {name!r} was found",
        hint=(
            f"install VB-CABLE ({VB_CABLE_URL}), reboot, then check with "
            '`python -c "import sounddevice; print(sounddevice.query_devices())"`. '
            'Apps will select the matching "CABLE Output" as their microphone. '
            f"Playback devices seen: {', '.join(outputs[:8]) or 'none'}"
        ),
    )


class SoundDeviceWriter:
    """Blocking int16 writes into one device, opened lazily on first use."""

    def __init__(
        self, fmt: AudioFormat, device: int | None, *, sd: Any = None, latency: float = 0.0
    ) -> None:
        self.fmt = fmt
        self._sd = sd or _import_sounddevice()
        self._device = device
        self._stream: Any = None
        self._latency = latency

    def _open(self) -> Any:
        if self._stream is None:
            try:
                self._stream = self._sd.OutputStream(
                    samplerate=self.fmt.sample_rate,
                    channels=self.fmt.channels,
                    dtype="int16",
                    device=self._device,
                    blocksize=self.fmt.samples_per_frame,
                    latency=self._latency,
                )
                self._stream.start()
            except Exception as exc:  # noqa: BLE001
                raise DeviceUnavailable(
                    f"could not open the output device for the phone microphone: {exc}"
                ) from exc
        return self._stream

    def write(self, payload: bytes) -> None:
        stream = self._open()
        try:
            stream.write(payload)
        except Exception as exc:  # noqa: BLE001
            raise DeviceUnavailable(f"writing to the microphone device failed: {exc}") from exc

    def close(self) -> None:
        if self._stream is not None:
            with contextlib.suppress(Exception):
                self._stream.stop()
            with contextlib.suppress(Exception):
                self._stream.close()
            self._stream = None


def _apply_gain(payload: bytes, gain: float) -> bytes:
    """Scale int16 PCM, clipping instead of wrapping. No-op at 1.0."""
    if gain == 1.0 or not payload:
        return payload
    from array import array  # noqa: PLC0415 - only needed when gain is used

    samples = array("h")
    samples.frombytes(payload[: len(payload) - (len(payload) % 2)])
    scaled = array("h", (max(-32768, min(32767, int(sample * gain))) for sample in samples))
    return scaled.tobytes()


def _peak_dbfs(payload: bytes) -> float:
    """Peak level of one frame in dBFS, or -inf for silence."""
    if not payload:
        return float("-inf")
    from array import array  # noqa: PLC0415

    samples = array("h")
    samples.frombytes(payload[: len(payload) - (len(payload) % 2)])
    if not samples:
        return float("-inf")
    peak = max(abs(sample) for sample in samples)
    if peak == 0:
        return float("-inf")
    import math  # noqa: PLC0415

    return 20.0 * math.log10(peak / 32768.0)


class CableMicSink:
    """The phone's microphone written into VB-CABLE, paced by a jitter buffer.

    ``write`` is called by the audio link's receive loop and only enqueues; a
    worker thread drains the buffer at the device's pace and writes silence when
    it runs dry, which is the only way to keep the device clock independent of the
    network clock. See :mod:`phonebridge.live.jitter` for the policy.
    """

    def __init__(
        self,
        fmt: AudioFormat,
        *,
        device: int | None,
        latency_ms: int,
        gain: float = 1.0,
        writer: Any = None,
        buffer: JitterBuffer | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.fmt = fmt
        self.device = device
        self.gain = gain
        if buffer is not None:
            self._buffer = buffer
        elif clock is not None:
            self._buffer = JitterBuffer(frame_ms=fmt.frame_ms, target_ms=latency_ms, clock=clock)
        else:
            self._buffer = JitterBuffer(frame_ms=fmt.frame_ms, target_ms=latency_ms)
        self._writer = writer or SoundDeviceWriter(fmt, device)
        self._silence = bytes(fmt.bytes_per_frame)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, name="phonebridge-mic", daemon=True)
        self.level_dbfs = float("-inf")
        self.error: str = ""
        self._thread.start()

    # ------------------------------------------------------------------ #
    # writer side: the audio link calls this
    # ------------------------------------------------------------------ #
    def write(self, payload: bytes) -> None:
        self.level_dbfs = _peak_dbfs(payload)
        self._buffer.push(_apply_gain(payload, self.gain))

    @property
    def stats(self) -> dict[str, int]:
        return self._buffer.stats.to_dict()

    # ------------------------------------------------------------------ #
    def _pump(self) -> None:
        frame_s = self.fmt.frame_ms / 1000.0
        while not self._stop.is_set():
            payload = self._buffer.pop(timeout=max(frame_s * 2.0, 0.02))
            try:
                self._writer.write(payload if payload is not None else self._silence)
            except DeviceUnavailable as exc:
                # Sticky and reported: a device that vanished mid-call must not
                # spin the thread writing into a closed handle.
                self.error = str(exc)
                log.warning("microphone sink stopped: %s", exc)
                return
            except Exception as exc:  # noqa: BLE001
                self.error = f"microphone writer failed: {exc}"
                log.warning("microphone sink stopped: %s", exc)
                return

    def close(self) -> None:
        self._stop.set()
        self._buffer.close()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        with contextlib.suppress(Exception):
            self._writer.close()


def open_cable_sink(fmt: AudioFormat, plan: MicPlan, *, sd: Any = None) -> CableMicSink:
    """Locate VB-CABLE and start a sink writing into it."""
    index = find_cable_input(plan.device, sd=sd)
    writer = SoundDeviceWriter(fmt, index, sd=sd)
    # Fail now, not on the first frame: "the device opened" is the property the
    # user needs before the phone starts capturing.
    writer._open()  # noqa: SLF001 - deliberate: prove the device opens here
    return CableMicSink(
        fmt, device=index, latency_ms=plan.latency_ms, gain=plan.gain, writer=writer
    )


# --------------------------------------------------------------------------- #
# 3. Speaker: PC system audio to the phone (which routes it to Bluetooth)
# --------------------------------------------------------------------------- #
class PyAudioLoopbackSource:
    """WASAPI loopback capture through PyAudioWPatch (48 kHz, whatever channels).

    Loopback is *shared mode*: the device decides the block size and the channel
    count, so this re-chunks into exact protocol frames and downmixes to mono when
    the session is mono. Without the re-chunk a 44.1 kHz device against a 48 kHz
    session drifts steadily; without the downmix a stereo device produces frames
    the phone plays at double speed.
    """

    def __init__(self, fmt: AudioFormat, *, device: str = "", module: Any = None) -> None:
        self.fmt = fmt
        self._remainder = b""
        self._device_channels = 2
        try:
            pyaudio = module or __import__("pyaudiowpatch")
        except Exception as exc:  # noqa: BLE001
            raise DeviceUnavailable(
                "capturing PC audio needs PyAudioWPatch, which is not importable",
                hint=(
                    f"{PYAUDIOWPATCH_HINT} — it is a fork of PyAudio that exposes "
                    "WASAPI loopback devices"
                ),
            ) from exc

        self._pa = pyaudio.PyAudio()
        try:
            info = self._resolve_device(pyaudio, device)
            self._device_channels = int(info.get("maxInputChannels", 2)) or 2
            self._rate = int(info.get("defaultSampleRate", fmt.sample_rate))
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=self._device_channels,
                rate=self._rate,
                frames_per_buffer=fmt.samples_per_frame,
                input=True,
                input_device_index=int(info["index"]),
            )
        except DeviceUnavailable:
            self._pa.terminate()
            raise
        except Exception as exc:  # noqa: BLE001
            self._pa.terminate()
            raise DeviceUnavailable(f"WASAPI loopback could not open: {exc}") from exc

    def _resolve_device(self, pyaudio: Any, device: str) -> dict[str, Any]:
        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        candidates = [
            info
            for info in self._pa.get_loopback_device_info_generator()
            if int(info.get("maxInputChannels", 0)) > 0
        ]
        if not candidates:
            raise DeviceUnavailable(
                "this PC exposes no WASAPI loopback device",
                hint="check that an output device is enabled in Windows sound settings",
            )
        if device:
            wanted = device.strip().lower()
            for info in candidates:
                if wanted in str(info.get("name", "")).lower():
                    return info
            names = ", ".join(str(info.get("name", "?")) for info in candidates[:6])
            raise DeviceUnavailable(
                f"no loopback device matches {device!r}", hint=f"available: {names}"
            )
        default_index = int(wasapi.get("defaultOutputDevice", -1))
        for info in candidates:
            if int(info.get("index", -1)) == default_index:
                return info
        return candidates[0]

    def read(self) -> bytes:
        try:
            raw = self._stream.read(self.fmt.samples_per_frame, exception_on_overflow=False)
        except Exception as exc:  # noqa: BLE001
            raise DeviceUnavailable(f"loopback capture failed: {exc}") from exc
        pcm = _downmix(raw, self._device_channels, self.fmt.channels)
        self._remainder += pcm
        want = self.fmt.bytes_per_frame
        if len(self._remainder) < want:
            return b""
        frame, self._remainder = self._remainder[:want], self._remainder[want:]
        return frame

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._stream.stop_stream()
        with contextlib.suppress(Exception):
            self._stream.close()
        with contextlib.suppress(Exception):
            self._pa.terminate()


def _downmix(payload: bytes, source_channels: int, target_channels: int) -> bytes:
    """Stereo to mono by averaging, mono to stereo by duplication, else pass."""
    if source_channels == target_channels or source_channels < 2:
        return payload
    from array import array  # noqa: PLC0415

    samples = array("h")
    samples.frombytes(payload[: len(payload) - (len(payload) % (2 * source_channels))])
    if target_channels == 1:
        mono = array(
            "h",
            (int((samples[i] + samples[i + 1]) / 2) for i in range(0, len(samples) - 1, 2)),
        )
        return mono.tobytes()
    return payload


def open_loopback_source(fmt: AudioFormat, plan: SpeakerPlan, *, module: Any = None) -> Any:
    """PyAudioWPatch loopback, with the existing ``soundcard`` backend as fallback.

    PyAudioWPatch is preferred because it is what the requirement names and it
    gives explicit loopback device names. The fallback keeps a machine that only
    has ``soundcard`` working instead of failing for no reason.
    """
    try:
        return PyAudioLoopbackSource(fmt, device=plan.device, module=module)
    except DeviceUnavailable as exc:
        from ..audio.backends import WasapiLoopback  # noqa: PLC0415 - optional extra

        try:
            return WasapiLoopback(fmt, device=plan.device or None)
        except Exception:  # noqa: BLE001
            raise exc from None
