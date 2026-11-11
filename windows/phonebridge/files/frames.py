"""File transfer framing, integrity and path safety (Phase 5).

The wire format is deliberately the smallest thing that can carry a file safely:

* **Chunked.** One ``MSG_FILE`` message is one chunk of at most 64 KiB. A chunk is
  small enough that a bounded queue holds several without eating memory, and a
  loss costs one chunk rather than a file.
* **Acknowledged.** The receiver acknowledges the highest *contiguous* chunk it
  holds. The sender keeps at most :data:`WINDOW_CHUNKS` unacknowledged chunks in
  flight, so a slow link applies backpressure instead of filling RAM.
* **Hashed.** SHA-256 of the whole file is declared up front and re-checked by the
  receiver after the last chunk. A file is *never* reported as transferred until
  that check passes; a mismatch fails the transfer and leaves nothing behind.
* **Resumable.** A receiver can report ``resume_from`` (a byte offset it already
  holds in a ``.part`` file). Because SHA-256 cannot be resumed from a digest
  alone, the receiver re-hashes the completed file from disk; resuming is
  therefore safe without trusting a partial digest.
* **Atomic.** The receiver writes ``<name>.part`` and renames it only after the
  hash matches. A cancelled or failed transfer leaves the original untouched.

Stream ids are fixed by docs/03: **5 = PC to phone**, **6 = phone to PC**. Each
direction carries data one way and acknowledgements/cancels the other way, which
keeps both directions symmetric instead of inventing a third control stream.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

MSG_FILE = 0x50

#: PC -> phone (data this way, acknowledgements back).
FILE_STREAM_TO_PHONE = 5
#: Phone -> PC (data this way, acknowledgements back).
FILE_STREAM_TO_PC = 6

#: ``u32 transfer_id | u32 chunk_index | u32 chunk_len | u8 flags | u8 reserved``.
#:
#: The length is 32-bit because a full 64 KiB chunk is 65 536 bytes, which does
#: not fit a u16 — the kind of off-by-one that only shows up on the last chunk of
#: a large file, so it is a width choice rather than a clamp.
HEADER = struct.Struct(">IIIBB")
HEADER_SIZE = HEADER.size  # 14

FLAG_START = 0x01
FLAG_END = 0x02
FLAG_ACK = 0x04
FLAG_CANCEL = 0x08
FLAG_PAUSE = 0x10
FLAG_RESUME = 0x20

#: 64 KiB per chunk: 16 chunks per MiB, small enough for a bounded queue.
CHUNK_SIZE = 64 * 1024
MIN_CHUNK_SIZE = 1024
MAX_CHUNK_SIZE = 256 * 1024

#: Unacknowledged chunks allowed in flight. 8 x 64 KiB = 512 KiB of memory, and
#: on a slow link it is the point where the sender waits instead of buffering.
WINDOW_CHUNKS = 8

#: Chunks between acknowledgements. Every chunk on a fast link would cost more in
#: round trips than it saves in retries.
ACK_EVERY_CHUNKS = 4

#: Files larger than this are refused rather than streamed: it is not a hard
#: limit of the format, it is a limit of "nobody wants to resume a 40 GB file over
#: Wi-Fi in this release" - documented in docs/23.
MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024

#: The ``.part`` suffix a receiver writes before the rename.
PART_SUFFIX = ".part"

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class FileFrameError(ValueError):
    """A chunk that cannot be trusted."""


class UnsafeName(ValueError):
    """A file name that would escape the destination directory."""


def safe_name(name: str) -> str:
    """Reduce a peer-supplied name to one harmless file name, or refuse it.

    Deliberately strict: this is the boundary where a remote peer names a local
    file, so a name that needs "fixing" is rejected rather than repaired. A
    repaired name is how ``..\\..\\Windows\\System32`` becomes something plausible.
    """
    if not isinstance(name, str):
        raise UnsafeName("file name must be a string")
    candidate = name
    if candidate != candidate.strip():
        raise UnsafeName("file name must not have leading or trailing spaces")
    if not candidate:
        raise UnsafeName("file name is empty")
    if len(candidate) > 200:
        raise UnsafeName("file name is longer than 200 characters")
    if candidate in {".", ".."}:
        raise UnsafeName("file name may not be '.' or '..'")
    if "/" in candidate or "\\" in candidate:
        raise UnsafeName("file name may not contain a path separator")
    if ":" in candidate:
        raise UnsafeName("file name may not contain ':' (drive or stream separator)")
    if _CONTROL_CHARS.search(candidate):
        raise UnsafeName("file name may not contain control characters")
    stem = candidate.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        raise UnsafeName(f"file name {candidate!r} is reserved on Windows")
    if candidate.endswith("."):
        raise UnsafeName("file name may not end with a dot")
    return candidate


def safe_destination(directory: str | os.PathLike[str], name: str) -> Path:
    """``directory/name`` with the parent verified, so nothing can escape it."""
    clean = safe_name(name)
    root = Path(directory).expanduser().resolve()
    target = (root / clean).resolve()
    if target.parent != root:
        raise UnsafeName(f"{name!r} resolves outside {root}")
    return target


def part_path(target: Path) -> Path:
    return target.with_name(target.name + PART_SUFFIX)


@dataclass
class FileChunk:
    """One ``MSG_FILE`` message: a slice of a transfer, or a control frame."""

    transfer_id: int
    chunk_index: int
    flags: int = 0
    payload: bytes = b""

    def encode(self) -> bytes:
        if not 0 <= self.transfer_id <= 0xFFFFFFFF:
            raise FileFrameError(f"transfer id out of range: {self.transfer_id}")
        if not 0 <= self.chunk_index <= 0xFFFFFFFF:
            raise FileFrameError(f"chunk index out of range: {self.chunk_index}")
        if len(self.payload) > MAX_CHUNK_SIZE:
            raise FileFrameError(f"chunk too large: {len(self.payload)} bytes")
        header = HEADER.pack(
            self.transfer_id, self.chunk_index, len(self.payload), self.flags, 0
        )
        return header + self.payload

    @property
    def is_data(self) -> bool:
        return bool(self.payload) and not (self.flags & (FLAG_ACK | FLAG_CANCEL))

    def __repr__(self) -> str:  # keeps logs small and free of file bytes
        return (
            f"FileChunk(id={self.transfer_id}, index={self.chunk_index}, "
            f"flags=0x{self.flags:02x}, bytes={len(self.payload)})"
        )


def decode_chunk(body: bytes) -> FileChunk:
    if len(body) < HEADER_SIZE:
        raise FileFrameError(f"file chunk shorter than its header: {len(body)} bytes")
    transfer_id, chunk_index, length, flags, _reserved = HEADER.unpack_from(body, 0)
    payload = body[HEADER_SIZE:]
    if len(payload) != length:
        raise FileFrameError(
            f"file chunk declares {length} bytes but carries {len(payload)}"
        )
    if length > MAX_CHUNK_SIZE:
        raise FileFrameError(f"chunk too large: {length} bytes")
    if (flags & FLAG_ACK) and length:
        raise FileFrameError("an acknowledgement may not carry a payload")
    return FileChunk(
        transfer_id=transfer_id, chunk_index=chunk_index, flags=flags, payload=payload
    )


def ack(transfer_id: int, chunk_index: int) -> FileChunk:
    return FileChunk(transfer_id=transfer_id, chunk_index=chunk_index, flags=FLAG_ACK)


def cancel(transfer_id: int, chunk_index: int = 0) -> FileChunk:
    return FileChunk(transfer_id=transfer_id, chunk_index=chunk_index, flags=FLAG_CANCEL)


def pause(transfer_id: int, chunk_index: int) -> FileChunk:
    return FileChunk(transfer_id=transfer_id, chunk_index=chunk_index, flags=FLAG_PAUSE)


def resume(transfer_id: int, chunk_index: int) -> FileChunk:
    return FileChunk(transfer_id=transfer_id, chunk_index=chunk_index, flags=FLAG_RESUME)


def sha256_of(path: str | os.PathLike[str], *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without loading it, so a 4 GB file costs 4 MB of RAM."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def iter_chunks(handle: BinaryIO, *, limit: int | None = None) -> Any:
    """Yield ``(index, bytes)`` from an open file, starting at its position."""
    index = 0
    remaining = limit
    while True:
        if remaining is not None and remaining <= 0:
            return
        block = handle.read(CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining))
        if not block:
            return
        if remaining is not None:
            remaining -= len(block)
        yield index, block
        index += 1


@dataclass
class TransferStats:
    """What the GUI shows for one transfer."""

    transfer_id: int
    name: str = ""
    direction: str = "to_phone"
    size: int = 0
    bytes_done: int = 0
    chunk_size: int = CHUNK_SIZE
    state: str = "pending"
    error: str = ""
    sha256: str = ""
    verified: bool | None = None
    resumed_from: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    path: str = ""
    retries: int = 0
    history: list[str] = field(default_factory=list)

    @property
    def fraction(self) -> float:
        if self.size <= 0:
            return 1.0 if self.state == "complete" else 0.0
        return max(0.0, min(1.0, self.bytes_done / self.size))

    def speed_bps(self, now: float) -> float:
        elapsed = (self.finished_at or now) - self.started_at
        if elapsed <= 0:
            return 0.0
        return self.bytes_done / elapsed

    def to_dict(self, *, now: float | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "transfer_id": self.transfer_id,
            "name": self.name,
            "direction": self.direction,
            "size": self.size,
            "bytes_done": self.bytes_done,
            "chunk_size": self.chunk_size,
            "state": self.state,
            "error": self.error,
            "sha256": self.sha256,
            "verified": self.verified,
            "resumed_from": self.resumed_from,
            "path": self.path,
            "retries": self.retries,
            "fraction": round(self.fraction, 4),
        }
        if now is not None:
            payload["speed_bps"] = round(self.speed_bps(now), 1)
        return payload
