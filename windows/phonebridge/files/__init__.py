"""Phase 5 file transfer: chunked, hashed, resumable, and deliberately strict.

Public surface:

* :mod:`~phonebridge.files.frames` — the ``MSG_FILE`` wire format, SHA-256 helper
  and the name/path safety rules (no I/O beyond hashing, no asyncio).
* :mod:`~phonebridge.files.transfer` — :class:`TransferManager`, which sends and
  receives files over an existing session with acknowledgements, bounded memory,
  resume, cancellation and atomic completion.

A file is reported ``complete`` only after the receiver re-read what it stored and
the SHA-256 matched. Nothing here opens a socket of its own.
"""

from __future__ import annotations

from .frames import (
    ACK_EVERY_CHUNKS,
    CHUNK_SIZE,
    FILE_STREAM_TO_PC,
    FILE_STREAM_TO_PHONE,
    FLAG_ACK,
    FLAG_CANCEL,
    FLAG_END,
    FLAG_PAUSE,
    FLAG_RESUME,
    FLAG_START,
    HEADER_SIZE,
    MAX_CHUNK_SIZE,
    MAX_FILE_BYTES,
    MIN_CHUNK_SIZE,
    MSG_FILE,
    PART_SUFFIX,
    WINDOW_CHUNKS,
    FileChunk,
    FileFrameError,
    TransferStats,
    UnsafeName,
    ack,
    cancel,
    decode_chunk,
    iter_chunks,
    part_path,
    pause,
    resume,
    safe_destination,
    safe_name,
    sha256_of,
)
from .transfer import (
    ACTIVE,
    CANCELLED,
    COMPLETE,
    FAILED,
    PAUSED,
    PENDING,
    SIDECAR_SUFFIX,
    TransferError,
    TransferManager,
)

__all__ = [
    "ACK_EVERY_CHUNKS",
    "ACTIVE",
    "CANCELLED",
    "CHUNK_SIZE",
    "COMPLETE",
    "FAILED",
    "FILE_STREAM_TO_PC",
    "FILE_STREAM_TO_PHONE",
    "FLAG_ACK",
    "FLAG_CANCEL",
    "FLAG_END",
    "FLAG_PAUSE",
    "FLAG_RESUME",
    "FLAG_START",
    "HEADER_SIZE",
    "MAX_CHUNK_SIZE",
    "MAX_FILE_BYTES",
    "MIN_CHUNK_SIZE",
    "MSG_FILE",
    "PAUSED",
    "PART_SUFFIX",
    "PENDING",
    "SIDECAR_SUFFIX",
    "TransferError",
    "TransferManager",
    "TransferStats",
    "FileChunk",
    "FileFrameError",
    "UnsafeName",
    "WINDOW_CHUNKS",
    "ack",
    "cancel",
    "decode_chunk",
    "iter_chunks",
    "part_path",
    "pause",
    "resume",
    "safe_destination",
    "safe_name",
    "sha256_of",
]
