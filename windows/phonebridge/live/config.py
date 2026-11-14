"""What the live commands are asked for, checked before any device is opened.

Three delivery paths share one shape: a plan says what to deliver, and the runner
either honours it or refuses with a sentence. Validating here — rather than at the
first ``open()`` — is what lets ``--latency 5`` or ``--backend magic`` fail with a
readable message instead of an exception from inside a sound API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: pyvirtualcam backends this build knows how to ask for. "auto" tries them in
#: order, which matters on a PC with Unity Capture but no OBS.
VIRTUAL_CAMERA_BACKENDS = ("obs", "unitycapture")
VIRTUAL_CAMERA_AUTO = "auto"

#: Where the Windows microphone has to land so other apps can select it. VB-CABLE
#: installs a playback device called "CABLE Input"; its matching capture device
#: ("CABLE Output") is what a call client lists as a microphone.
DEFAULT_CABLE_DEVICE = "CABLE Input"

#: Defaults chosen for a call, not for a studio: 720p30 is what Meet/Zoom ask for,
#: 120 ms of jitter buffer is inaudible as delay, and 200 ms of playback buffer
#: survives a Wi-Fi hiccup without turning conversation into a walkie-talkie.
DEFAULT_CAMERA = (1280, 720, 30)

#: Named camera sizes, so a machine that cannot sustain 720p has a one-flag
#: answer rather than three numbers to remember. "low" is the documented
#: fallback: 640x480 at 30 fps costs a quarter of the pixels and a quarter of the
#: JPEG work on both ends.
CAMERA_PROFILES: dict[str, tuple[int, int, int]] = {
    "720p": (1280, 720, 30),
    "low": (640, 480, 30),
    "high": (1920, 1080, 15),
}

DEFAULT_CAMERA_PROFILE = "720p"
DEFAULT_MIC_LATENCY_MS = 120
DEFAULT_SPEAKER_LATENCY_MS = 200


def profile_size(profile: str) -> tuple[int, int, int]:
    """The ``(width, height, fps)`` a named profile asks for."""
    try:
        return CAMERA_PROFILES[profile]
    except KeyError:
        raise ConfigError(
            f"unknown camera profile {profile!r}: choose one of "
            f"{', '.join(CAMERA_PROFILES)}"
        ) from None


class ConfigError(ValueError):
    """A requested setting that cannot be honoured, with a message for a user."""


def _check_int(name: str, value: int, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{name} must be a whole number, got {value!r}")
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}")
    return value


@dataclass(frozen=True)
class CameraPlan:
    """Deliver the phone camera to a virtual camera device on the PC."""

    width: int = DEFAULT_CAMERA[0]
    height: int = DEFAULT_CAMERA[1]
    fps: int = DEFAULT_CAMERA[2]
    quality: int = 70
    facing: str = "back"
    backend: str = VIRTUAL_CAMERA_AUTO

    def __post_init__(self) -> None:
        _check_int("camera width", self.width, 320, 3840)
        _check_int("camera height", self.height, 240, 2160)
        _check_int("camera fps", self.fps, 1, 60)
        _check_int("camera quality", self.quality, 20, 95)
        if self.facing not in ("back", "front"):
            raise ConfigError(f"camera facing must be 'back' or 'front', got {self.facing!r}")
        if self.backend not in (*VIRTUAL_CAMERA_BACKENDS, VIRTUAL_CAMERA_AUTO):
            raise ConfigError(
                f"unknown virtual camera backend {self.backend!r}: "
                f"choose one of {', '.join((*VIRTUAL_CAMERA_BACKENDS, VIRTUAL_CAMERA_AUTO))}"
            )

    @property
    def label(self) -> str:
        side = {"back": "rear", "front": "front"}[self.facing]
        return f"{side} {self.width}x{self.height} @ {self.fps} fps -> {self.backend}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "quality": self.quality,
            "facing": self.facing,
            "backend": self.backend,
        }

    @property
    def backends(self) -> tuple[str, ...]:
        """The backends to try, in order. "auto" is a list, never a device."""
        if self.backend == VIRTUAL_CAMERA_AUTO:
            return VIRTUAL_CAMERA_BACKENDS
        return (self.backend,)


@dataclass(frozen=True)
class MicPlan:
    """Deliver the phone microphone into the VB-CABLE playback device."""

    device: str = DEFAULT_CABLE_DEVICE
    latency_ms: int = DEFAULT_MIC_LATENCY_MS
    gain: float = 1.0

    def __post_init__(self) -> None:
        if not self.device or not str(self.device).strip():
            raise ConfigError("the microphone needs a device name to write into")
        _check_int("microphone buffer", self.latency_ms, 20, 1000)
        if not 0.0 < float(self.gain) <= 4.0:
            raise ConfigError(f"microphone gain must be in (0, 4], got {self.gain}")

    def as_dict(self) -> dict[str, Any]:
        return {"device": self.device, "latency_ms": self.latency_ms, "gain": self.gain}


@dataclass(frozen=True)
class SpeakerPlan:
    """Deliver PC audio to the phone, which routes it to its own output.

    ``lipsync_ms`` is applied to the *video* side when both are running: a positive
    value delays the camera feed so the picture matches audio that is already
    behind it (Bluetooth adds 100-200 ms of its own).
    """

    latency_ms: int = DEFAULT_SPEAKER_LATENCY_MS
    lipsync_ms: int = 0
    device: str = ""

    def __post_init__(self) -> None:
        _check_int("speaker buffer", self.latency_ms, 20, 2000)
        _check_int("lip-sync offset", self.lipsync_ms, -500, 500)

    def as_dict(self) -> dict[str, Any]:
        return {
            "latency_ms": self.latency_ms,
            "lipsync_ms": self.lipsync_ms,
            "device": self.device or "default output (loopback)",
        }


@dataclass(frozen=True)
class LivePlan:
    """The set of paths one run delivers. Any subset may be absent."""

    camera: CameraPlan | None = None
    mic: MicPlan | None = None
    speaker: SpeakerPlan | None = None

    @property
    def empty(self) -> bool:
        return self.camera is None and self.mic is None and self.speaker is None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, part in (
                ("camera", self.camera),
                ("mic", self.mic),
                ("speaker", self.speaker),
            )
            if part is not None
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera": self.camera.as_dict() if self.camera else None,
            "mic": self.mic.as_dict() if self.mic else None,
            "speaker": self.speaker.as_dict() if self.speaker else None,
        }
