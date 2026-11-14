"""Where the bytes actually come from and go to, and whether that is possible.

The streaming logic in :mod:`phonebridge.audio.link` never touches a sound API
itself: it reads from an :class:`AudioSource` and writes to an :class:`AudioSink`.
That split is what makes the whole audio plane testable on a machine with no
sound card (this one), and it keeps the two Windows-specific libraries — both
optional — at the edge.

What is verified and what is not
--------------------------------
The *Windows* implementations below cannot be exercised in this repository's CI
(no Windows, no sound device, and ``soundcard``/``sounddevice`` are optional
extras). They are therefore written conservatively, they fail with a sentence
instead of an exception trace, and :func:`backend_status` exists so the GUI can
say "installed but never run here" rather than pretending. Every other part of
Phase 2 — framing, sequencing, flow control, lifecycle — is covered by tests
against the fakes at the bottom of this module.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .frames import AudioFormat
from .sdout import SdOutputError, SoundDeviceOutput

log = logging.getLogger(__name__)


class AudioBackendError(RuntimeError):
    """A capture or playback path that cannot start, with a reason a user can act on."""


@runtime_checkable
class AudioSource(Protocol):
    """Blocking capture. One call returns exactly one frame's worth of PCM."""

    fmt: AudioFormat

    def read(self) -> bytes: ...

    def close(self) -> None: ...


@runtime_checkable
class AudioSink(Protocol):
    """Blocking playback of one PCM frame."""

    def write(self, payload: bytes) -> None: ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# Windows: capture what the PC is playing (WASAPI loopback)
# --------------------------------------------------------------------------- #
class WasapiLoopback:
    """PC system audio via WASAPI loopback, using the optional ``soundcard`` extra.

    ``soundcard`` is the smallest dependency that exposes loopback capture on
    Windows (``include_loopback=True``); it is imported here, not at package
    import time, so a machine without it can still run every other feature.

    Loopback capture is *shared* mode: the frame size the device hands back is not
    the frame size the protocol wants, so the reader re-chunks whatever it gets
    into exact 20 ms frames and keeps a small remainder buffer. Without that, a
    device running at 44.1 kHz against a 48 kHz session would produce a slow,
    constant drift.
    """

    def __init__(self, fmt: AudioFormat, device: str | None = None) -> None:
        self.fmt = fmt
        try:
            import soundcard  # noqa: PLC0415 - optional extra, imported on demand
        except Exception as exc:  # noqa: BLE001 - any import failure is "not installed"
            raise AudioBackendError(
                "PC audio capture needs the optional 'soundcard' package: "
                "run `pip install soundcard` in the PhoneBridge virtualenv"
            ) from exc
        self._sc = soundcard
        try:
            speaker = soundcard.default_speaker()
            microphone = soundcard.get_microphone(
                id=str(getattr(speaker, "name", "")), include_loopback=True
            )
            if device:
                microphone = soundcard.get_microphone(id=device, include_loopback=True)
            self._recorder = microphone.recorder(
                samplerate=fmt.sample_rate, channels=fmt.channels, blocksize=None
            )
            self._recorder.__enter__()
        except AudioBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AudioBackendError(f"WASAPI loopback could not open: {exc}") from exc
        self._remainder = b""

    def read(self) -> bytes:
        want = self.fmt.bytes_per_frame
        try:
            chunk = self._recorder.record(numframes=self.fmt.samples_per_frame)
        except Exception as exc:  # noqa: BLE001
            raise AudioBackendError(f"loopback capture failed: {exc}") from exc
        pcm = _to_pcm16(chunk)
        self._remainder += pcm
        if len(self._remainder) < want:
            return b""
        frame, self._remainder = self._remainder[:want], self._remainder[want:]
        return frame

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._recorder.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# Windows: play the phone's microphone into a device
# --------------------------------------------------------------------------- #
class SoundDeviceSink:
    """Plays received PCM on a Windows output device (optional ``sounddevice``).

    Default device unless one is named. This is the *in-app monitor*: it makes the
    phone's microphone audible on the PC. On its own it creates no Windows
    recording device, so a call client cannot select it; to give one a microphone,
    point this sink at VB-CABLE's "CABLE Input" (``phonebridge mic --to-cable``)
    and the app records "CABLE Output" instead. See docs/26.
    """

    def __init__(self, fmt: AudioFormat, device: str | int | None = None) -> None:
        self.fmt = fmt
        try:
            import sounddevice  # noqa: PLC0415 - optional extra
        except Exception as exc:  # noqa: BLE001
            raise AudioBackendError(
                "playing the phone's microphone on the PC needs the optional "
                "'sounddevice' package: run `pip install sounddevice`"
            ) from exc
        self._sd = sounddevice
        # RawOutputStream (bytes, no dtype conversion) with the device's own
        # channel count and rate, negotiated — see phonebridge.audio.sdout. The
        # same two mistakes (numpy-array writes, session channels on a device
        # that has its own) used to be in here and in live/devices.py.
        try:
            self._output = SoundDeviceOutput(fmt, device, sd=sounddevice)
        except SdOutputError as exc:
            raise AudioBackendError(f"the output device could not be opened: {exc}") from exc

    def write(self, payload: bytes) -> None:
        if not payload:
            return
        try:
            self._output.write(payload)
        except SdOutputError as exc:
            raise AudioBackendError(f"playback failed: {exc}") from exc

    def close(self) -> None:
        self._output.close()


def _to_pcm16(chunk: object) -> bytes:
    """``soundcard`` returns float32 numpy arrays; the wire wants PCM16LE."""
    try:
        import numpy as np  # noqa: PLC0415 - arrives with soundcard

        array = np.asarray(chunk, dtype=np.float32)
        clipped = np.clip(array, -1.0, 1.0)
        return (clipped * 32767.0).astype("<i2").tobytes()
    except Exception:  # noqa: BLE001 - bytes-like input is already PCM16
        return bytes(memoryview(chunk))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# fakes: what the tests and a headless session use
# --------------------------------------------------------------------------- #
@dataclass
class NullSource:
    """An endless stream of silence, at exactly the requested frame size."""

    fmt: AudioFormat
    limit: int | None = None
    _sent: int = 0

    def read(self) -> bytes:
        if self.limit is not None and self._sent >= self.limit:
            raise AudioBackendError("source exhausted")
        self._sent += 1
        return b"\x00\x00" * (self.fmt.samples_per_frame * self.fmt.channels)

    def close(self) -> None:
        return None


@dataclass
class NullSink:
    """Counts what it was given, so a test can assert the playout."""

    written: int = 0
    bytes_written: int = 0

    def write(self, payload: bytes) -> None:
        self.written += 1
        self.bytes_written += len(payload)

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# the defaults the controller uses
# --------------------------------------------------------------------------- #
def default_source_factory(fmt: AudioFormat) -> AudioSource:
    """The real capture path on Windows; a clear refusal anywhere else."""
    import platform  # noqa: PLC0415 - trivially cheap, kept local for symmetry

    if platform.system() != "Windows":
        raise AudioBackendError(
            "PC audio capture uses WASAPI, which exists only on Windows; "
            f"this host is {platform.system()}"
        )
    return WasapiLoopback(fmt)


def default_sink_factory(fmt: AudioFormat) -> AudioSink:
    """The in-app monitor (an output device), not a recording endpoint."""
    import platform  # noqa: PLC0415

    if platform.system() != "Windows":
        raise AudioBackendError(
            "playout of the phone's microphone needs a Windows output device; "
            f"this host is {platform.system()}"
        )
    return SoundDeviceSink(fmt)


# --------------------------------------------------------------------------- #
# honesty for the GUI and the capability table
# --------------------------------------------------------------------------- #
def _importable(name: str) -> tuple[bool, str]:
    try:
        __import__(name)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return True, ""


def backend_status() -> dict[str, object]:
    """Which optional audio libraries this PC actually has, and what each is for.

    ``verified_here`` is deliberately ``False``: this reports *installability*, not
    that a capture or playback path has been exercised on a real device.
    """
    capture_ok, capture_why = _importable("soundcard")
    playback_ok, playback_why = _importable("sounddevice")
    return {
        "capture": {
            "library": "soundcard",
            "purpose": "WASAPI loopback capture of PC system audio",
            "available": capture_ok,
            "detail": capture_why or "importable",
            "verified_here": False,
        },
        "playback": {
            "library": "sounddevice",
            "purpose": "plays the phone's microphone on a PC output device (monitor)",
            "available": playback_ok,
            "detail": playback_why or "importable",
            "verified_here": False,
        },
        "virtual_microphone": {
            "library": None,
            "purpose": (
                "exposing the phone's microphone as a Windows *recording* device "
                "needs a third-party virtual audio driver; PhoneBridge does not "
                "install one and does not claim the endpoint exists"
            ),
            "available": False,
            "detail": "not implemented - documented optional integration",
            "verified_here": False,
        },
    }
