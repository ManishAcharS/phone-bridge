"""Phase 4: camera streaming from the phone, over the authenticated session.

`CameraStream` owns the lifecycle and the newest frame; `frames` owns the wire
format; `preview` owns the optional JPEG decoding for the window. Nothing here
opens a socket, so a camera stream cannot exist without authentication.
"""

from __future__ import annotations

from .frames import (
    CODEC_JPEG,
    FLAG_KEY,
    FLAG_START,
    FLAG_STOP,
    MAX_VIDEO_BODY,
    VALID_ROTATIONS,
    FrameGate,
    VideoConfig,
    VideoFrame,
    VideoFrameError,
    VideoStats,
    decode_frame,
)
from .preview import Preview, PreviewUnavailable, decode, preview_status
from .stream import (
    ACTIVE,
    ERROR,
    IDLE,
    STARTING,
    STOPPING,
    VIDEO_STREAM,
    CameraError,
    CameraSnapshot,
    CameraStream,
)

__all__ = [
    "ACTIVE",
    "CODEC_JPEG",
    "ERROR",
    "FLAG_KEY",
    "FLAG_START",
    "FLAG_STOP",
    "IDLE",
    "MAX_VIDEO_BODY",
    "STARTING",
    "STOPPING",
    "VALID_ROTATIONS",
    "VIDEO_STREAM",
    "CameraError",
    "CameraSnapshot",
    "CameraStream",
    "FrameGate",
    "Preview",
    "PreviewUnavailable",
    "VideoConfig",
    "VideoFrame",
    "VideoFrameError",
    "VideoStats",
    "decode",
    "decode_frame",
    "preview_status",
]
