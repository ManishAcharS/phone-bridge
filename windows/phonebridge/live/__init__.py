"""The three Windows delivery paths: webcam, microphone, speaker.

Nothing here opens a socket or re-implements the protocol. Frames arrive on the
session the connection manager already authenticated
(:mod:`phonebridge.camera.stream`, :mod:`phonebridge.audio.link`) and leave
through a Windows device (:mod:`phonebridge.live.devices`).

    phonebridge camera --virtual      phone camera -> OBS Virtual Camera
    phonebridge mic --to-cable        phone mic    -> CABLE Input (VB-CABLE)
    phonebridge speaker               PC audio     -> phone -> Bluetooth

Optional libraries (pyvirtualcam, sounddevice, PyAudioWPatch) are imported inside
the functions that need them, so importing this package on a machine without them
— or on a build server — is always safe.
"""

from __future__ import annotations

from .config import (
    DEFAULT_CABLE_DEVICE,
    CameraPlan,
    ConfigError,
    LivePlan,
    MicPlan,
    SpeakerPlan,
)
from .devices import (
    OBS_DOWNLOAD_URL,
    VB_CABLE_URL,
    CableMicSink,
    DeviceUnavailable,
    PyAudioLoopbackSource,
    VirtualCamera,
    find_cable_input,
    open_cable_sink,
    open_loopback_source,
    open_virtual_camera,
)
from .jitter import JitterBuffer, JitterStats
from .runner import LiveRunner, LiveStatus

__all__ = [
    "DEFAULT_CABLE_DEVICE",
    "OBS_DOWNLOAD_URL",
    "VB_CABLE_URL",
    "CableMicSink",
    "CameraPlan",
    "ConfigError",
    "DeviceUnavailable",
    "JitterBuffer",
    "JitterStats",
    "LivePlan",
    "LiveRunner",
    "LiveStatus",
    "MicPlan",
    "PyAudioLoopbackSource",
    "SpeakerPlan",
    "VirtualCamera",
    "find_cable_input",
    "open_cable_sink",
    "open_loopback_source",
    "open_virtual_camera",
]
