"""Phase 2: bidirectional audio over the existing authenticated session.

Nothing in this package opens a socket. :class:`~phonebridge.audio.link.AudioLink`
multiplexes both directions onto the session the connection manager already
authenticated, which is the property that makes "no audio without authentication"
true by construction rather than by review.
"""

from __future__ import annotations

from .backends import (
    AudioBackendError,
    AudioSink,
    AudioSource,
    NullSink,
    NullSource,
    SoundDeviceSink,
    WasapiLoopback,
    backend_status,
    default_sink_factory,
    default_source_factory,
)
from .frames import (
    CODEC_PCM16,
    FLAG_MUTED,
    FLAG_START,
    FLAG_STOP,
    MAX_AUDIO_BODY,
    AudioFormat,
    AudioFrame,
    AudioFrameError,
    AudioStats,
    SequenceGate,
    decode_frame,
)
from .link import (
    ACTIVE,
    ERROR,
    IDLE,
    MIC_STREAM,
    PLAYBACK_STREAM,
    STARTING,
    STOPPING,
    AudioError,
    AudioLink,
    AudioSnapshot,
)

__all__ = [
    "ACTIVE",
    "CODEC_PCM16",
    "ERROR",
    "FLAG_MUTED",
    "FLAG_START",
    "FLAG_STOP",
    "IDLE",
    "MAX_AUDIO_BODY",
    "MIC_STREAM",
    "PLAYBACK_STREAM",
    "STARTING",
    "STOPPING",
    "AudioBackendError",
    "AudioError",
    "AudioFormat",
    "AudioFrame",
    "AudioFrameError",
    "AudioLink",
    "AudioSink",
    "AudioSnapshot",
    "AudioSource",
    "AudioStats",
    "NullSink",
    "NullSource",
    "SequenceGate",
    "SoundDeviceSink",
    "WasapiLoopback",
    "backend_status",
    "decode_frame",
    "default_sink_factory",
    "default_source_factory",
]
