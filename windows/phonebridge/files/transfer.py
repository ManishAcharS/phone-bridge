"""Driving one file across the wire, in both directions, safely.

Two directions, one machine:

* **PC -> phone.** The PC offers a file, the phone accepts it (after the *user* on
  the phone has chosen a destination) and reports how many bytes it already holds.
  The PC streams chunks from that offset, waits for acknowledgements, and asks the
  phone to verify the finished file's SHA-256 before anything is called complete.
* **Phone -> PC.** The PC asks the phone for a file; the phone shows its own file
  picker (the PC cannot browse a phone), then streams chunks which are written to
  ``<name>.part`` and renamed only after the hash matches what the phone declared.

Rules this file exists to enforce:

1. **No success without a verified hash.** A transfer ends ``complete`` only when
   the receiver re-read the bytes it stored and got the declared SHA-256. A
   mismatch is ``failed`` with the expected and actual digests in the message.
2. **Bounded memory.** At most :data:`~.frames.WINDOW_CHUNKS` chunks are in flight
   and files are never read whole: a 4 GB file costs a couple of megabytes of RAM.
3. **Atomic on disk.** Data goes to ``<name>.part`` and is renamed into place, so a
   cancelled transfer cannot leave a truncated file pretending to be the real one.
4. **The peer names nothing local.** Every incoming name goes through
   :func:`~.frames.safe_name` and every destination through
   :func:`~.frames.safe_destination`; a traversal attempt fails the transfer
   instead of writing outside the chosen folder.
5. **Nothing runs by itself.** A send starts because the user picked a file; a
   receive starts because the user asked for one; the phone shows its own picker
   for both destinations and sources.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from .frames import (
    ACK_EVERY_CHUNKS,
    CHUNK_SIZE,
    FILE_STREAM_TO_PC,
    FILE_STREAM_TO_PHONE,
    FLAG_ACK,
    FLAG_CANCEL,
    FLAG_END,
    FLAG_START,
    MAX_CHUNK_SIZE,
    MAX_FILE_BYTES,
    MIN_CHUNK_SIZE,
    PART_SUFFIX,
    WINDOW_CHUNKS,
    FileChunk,
    FileFrameError,
    TransferStats,
    UnsafeName,
    ack,
    cancel,
    decode_chunk,
    part_path,
    safe_destination,
    safe_name,
    sha256_of,
)
from .frames import (
    pause as pause_frame,
)
from .frames import (
    resume as resume_frame,
)

log = logging.getLogger(__name__)

#: How long the sender waits for an acknowledgement before deciding the link is
#: stuck. Generous on purpose: a hotspot can pause for a second or two.
ACK_TIMEOUT_S = 15.0
#: Retries of a stalled window before the transfer fails, per docs/23.
MAX_RETRIES = 3
#: Incoming chunks buffered before the receiver's pump applies backpressure.
INCOMING_QUEUE = 8

PENDING = "pending"
ACTIVE = "active"
PAUSED = "paused"
COMPLETE = "complete"
FAILED = "failed"
CANCELLED = "cancelled"

#: Where the receiver remembers what a ``.part`` file belongs to, so a resume
#: across a session boundary cannot append to a different file's bytes.
SIDECAR_SUFFIX = ".part.json"


class TransferError(RuntimeError):
    """A transfer that cannot continue, with a message meant for a person."""


@dataclass
class _Acks:
    """Acknowledgement state for one outbound transfer."""

    acked: int = -1
    event: asyncio.Event = field(default_factory=asyncio.Event)

    def note(self, index: int) -> None:
        if index > self.acked:
            self.acked = index
            self.event.set()

    async def wait(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        self.event.clear()
        return True


@dataclass
class _Inbound:
    """State for one transfer arriving from the phone."""

    stats: TransferStats
    handle: BinaryIO
    digest: Any
    target: Path
    part: Path
    next_index: int = 0
    acked: int = -1
    last_ack_len: int = 0

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.handle.close()


class TransferManager:
    """Send, receive, pause, resume and cancel files over one session."""

    def __init__(
        self,
        session: Any,
        *,
        destination: str | os.PathLike[str] | None = None,
        on_change: Callable[[TransferStats], None] | None = None,
        chunk_size: int = CHUNK_SIZE,
    ) -> None:
        self._session = session
        self._destination = Path(destination).expanduser() if destination else None
        self._on_change = on_change
        self._chunk_size = max(MIN_CHUNK_SIZE, min(MAX_CHUNK_SIZE, int(chunk_size)))
        self._stats: dict[int, TransferStats] = {}
        self._acks: dict[int, _Acks] = {}
        self._inbound: dict[int, _Inbound] = {}
        self._paused: set[int] = set()
        self._cancelled: set[int] = set()
        self._resume_events: dict[int, asyncio.Event] = {}
        self._history: list[TransferStats] = []
        #: Chunks that arrived before their transfer was registered. A real phone
        #: starts streaming the moment it answers `file.request`, so the first
        #: chunk can beat the reply; dropping it would truncate every transfer.
        self._orphans: dict[int, list[FileChunk]] = {}
        self._orphan_bytes = 0
        self._queue: asyncio.Queue[bytes | None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._next_local_id = 1

    # ------------------------------------------------------------------ #
    # observation
    # ------------------------------------------------------------------ #
    @property
    def transfers(self) -> tuple[TransferStats, ...]:
        return tuple(self._stats.values())

    def history(self) -> tuple[TransferStats, ...]:
        return tuple(self._history)

    def get(self, transfer_id: int) -> TransferStats | None:
        return self._stats.get(transfer_id)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "active": [
                t.to_dict(now=now) for t in self._stats.values()
                if t.state in {PENDING, ACTIVE, PAUSED}
            ],
            "history": [t.to_dict(now=now) for t in self._history[-20:]],
            "destination": str(self._destination) if self._destination else "",
            "chunk_size": self._chunk_size,
        }

    def set_destination(self, directory: str | os.PathLike[str]) -> Path:
        path = Path(directory).expanduser()
        if not path.is_dir():
            raise TransferError(f"{path} is not a folder")
        self._destination = path
        return path

    # ------------------------------------------------------------------ #
    # PC -> phone
    # ------------------------------------------------------------------ #
    async def send_file(
        self, path: str | os.PathLike[str], *, chunk_size: int | None = None
    ) -> TransferStats:
        """Offer one local file to the phone and stream it.

        The returned stats are final: ``complete`` means the phone verified the
        SHA-256, anything else carries a sentence the GUI shows verbatim.
        """
        source = Path(path).expanduser()
        if not source.is_file():
            raise TransferError(f"{source} is not a file")
        size = source.stat().st_size
        if size > MAX_FILE_BYTES:
            raise TransferError(
                f"{source.name} is {size} bytes; this build transfers at most "
                f"{MAX_FILE_BYTES} bytes per file"
            )
        name = safe_name(source.name)
        digest = sha256_of(source)
        chunk_size = max(MIN_CHUNK_SIZE, min(MAX_CHUNK_SIZE, int(chunk_size or self._chunk_size)))

        if not getattr(self._session, "is_open", False):
            raise TransferError("not connected: a file needs a live session")

        try:
            reply = await self._session.call(
                "file.offer",
                {"name": name, "size": size, "sha256": digest, "chunk_size": chunk_size},
            )
        except Exception as exc:  # noqa: BLE001 - refusals are messages
            raise TransferError(_explain("the phone refused the file", exc)) from exc

        transfer_id = int(reply.get("transfer_id") or 0)
        if not transfer_id:
            raise TransferError("the phone accepted the file but sent no transfer id")
        resume_from = int(reply.get("resume_from") or 0)
        applied_chunk = int(reply.get("chunk_size") or chunk_size)
        applied_chunk = max(MIN_CHUNK_SIZE, min(MAX_CHUNK_SIZE, applied_chunk))

        stats = TransferStats(
            transfer_id=transfer_id,
            name=name,
            direction="to_phone",
            size=size,
            chunk_size=applied_chunk,
            state=ACTIVE,
            sha256=digest,
            resumed_from=resume_from,
            started_at=time.monotonic(),
            path=str(source),
        )
        self._stats[transfer_id] = stats
        self._acks[transfer_id] = _Acks(acked=resume_from // applied_chunk - 1)
        self._publish(stats)
        await self._ensure_pump()

        try:
            await self._stream_out(source, stats)
        except TransferError as exc:
            stats.state = FAILED
            stats.error = str(exc)
            stats.finished_at = time.monotonic()
            self._publish(stats)
            await self._quiet_send(cancel(transfer_id, 0), FILE_STREAM_TO_PHONE)
            raise
        finally:
            self._acks.pop(transfer_id, None)

        return stats

    async def _stream_out(self, source: Path, stats: TransferStats) -> None:
        chunk_size = stats.chunk_size
        total_chunks = max(1, -(-stats.size // chunk_size))
        start_chunk = min(stats.resumed_from // chunk_size, total_chunks - 1)
        acks = self._acks[stats.transfer_id]

        with open(source, "rb") as handle:
            handle.seek(start_chunk * chunk_size)
            stats.bytes_done = start_chunk * chunk_size
            index = start_chunk
            while index < total_chunks:
                if stats.transfer_id in self._cancelled:
                    raise TransferError("cancelled")
                await self._wait_if_paused(stats.transfer_id)
                block = handle.read(chunk_size)
                if not block and index < total_chunks - 1:
                    raise TransferError("the file changed size while it was being sent")
                flags = 0
                if index == 0:
                    flags |= FLAG_START
                if index == total_chunks - 1:
                    flags |= FLAG_END
                chunk = FileChunk(
                    transfer_id=stats.transfer_id,
                    chunk_index=index,
                    flags=flags,
                    payload=block,
                )
                await self._send(chunk, FILE_STREAM_TO_PHONE)
                stats.bytes_done = min(stats.size, (index + 1) * chunk_size)
                self._publish(stats)

                # Backpressure: never more than WINDOW_CHUNKS unacknowledged.
                if index - acks.acked >= WINDOW_CHUNKS:
                    granted = await acks.wait(ACK_TIMEOUT_S)
                    attempts = stats.retries
                    while not granted and attempts < MAX_RETRIES:
                        stats.retries = attempts = attempts + 1
                        granted = await acks.wait(ACK_TIMEOUT_S)
                    if not granted:
                        raise TransferError(
                            f"the phone stopped acknowledging after chunk {acks.acked}"
                        )
                index += 1

        # The receiver verifies; only it can say the bytes on disk are right.
        verified = await self._verify_at_phone(stats)
        stats.verified = verified
        stats.finished_at = time.monotonic()
        if verified:
            stats.state = COMPLETE
            stats.bytes_done = stats.size
        else:
            stats.state = FAILED
            stats.error = "the phone could not verify the SHA-256 of the stored file"
        self._publish(stats)
        self._record(stats)
        if not verified:
            raise TransferError(stats.error)

    async def _verify_at_phone(self, stats: TransferStats) -> bool:
        for attempt in range(2):
            try:
                reply = await self._session.call(
                    "file.complete",
                    {"transfer_id": stats.transfer_id, "sha256": stats.sha256},
                    timeout=60.0,
                )
            except Exception as exc:  # noqa: BLE001
                stats.error = _explain("the phone could not verify the file", exc)
                if attempt:
                    return False
                continue
            if isinstance(reply, dict) and reply.get("verified") is True:
                return True
            if isinstance(reply, dict) and reply.get("sha256"):
                stats.error = (
                    f"hash mismatch: sent {stats.sha256}, phone stored {reply['sha256']}"
                )
                return False
            stats.error = "the phone did not confirm the SHA-256"
            return False
        return False

    # ------------------------------------------------------------------ #
    # phone -> PC
    # ------------------------------------------------------------------ #
    async def request_file(self, name_hint: str = "") -> TransferStats:
        """Ask the phone for a file. The *phone* shows its own picker."""
        if not getattr(self._session, "is_open", False):
            raise TransferError("not connected: a file needs a live session")
        if self._destination is None:
            raise TransferError("choose a destination folder before receiving a file")

        # Subscribe *before* asking: the phone starts streaming as soon as it
        # answers, and a chunk that arrives before the pump exists would be lost.
        await self._ensure_pump()

        params: dict[str, Any] = {}
        if name_hint:
            params["name"] = name_hint
        try:
            reply = await self._session.call("file.request", params, timeout=120.0)
        except Exception as exc:  # noqa: BLE001
            raise TransferError(_explain("the phone did not send a file", exc)) from exc

        transfer_id = int(reply.get("transfer_id") or 0)
        name = str(reply.get("name") or "")
        size = int(reply.get("size") or 0)
        digest = str(reply.get("sha256") or "")
        chunk_size = int(reply.get("chunk_size") or self._chunk_size)
        if not transfer_id:
            raise TransferError("the phone sent no transfer id")
        if not digest:
            raise TransferError(
                "the phone did not provide a SHA-256, so the file cannot be verified"
            )
        if size > MAX_FILE_BYTES:
            raise TransferError(f"the phone offered {size} bytes, over this build's limit")

        try:
            target = safe_destination(self._destination, name)
        except UnsafeName as exc:
            await self._quiet_send(cancel(transfer_id, 0), FILE_STREAM_TO_PHONE)
            raise TransferError(f"refusing the offered file name: {exc}") from exc

        stats = TransferStats(
            transfer_id=transfer_id,
            name=target.name,
            direction="to_pc",
            size=size,
            chunk_size=max(MIN_CHUNK_SIZE, min(MAX_CHUNK_SIZE, chunk_size)),
            state=ACTIVE,
            sha256=digest,
            started_at=time.monotonic(),
            path=str(target),
        )
        part = part_path(target)
        resumed_from = 0
        keep_part = self._resume_offset(part, name, size, digest)
        if keep_part:
            # Only a chunk-aligned prefix can be resumed: the phone restarts at a
            # chunk boundary, so anything past one would be duplicated. Truncate
            # rather than trust a half-written chunk.
            aligned = keep_part - (keep_part % stats.chunk_size)
            if aligned != keep_part:
                log.info("truncating %s to the last whole chunk (%d bytes)", part.name, aligned)
                with contextlib.suppress(OSError), open(part, "r+b") as short:
                    short.truncate(aligned)
            keep_part = aligned
        if keep_part:
            resumed_from = keep_part
            if not await self._ask_resume(transfer_id, keep_part):
                # The phone will send from the beginning: start over rather than
                # append the beginning of the file to the end of a partial one.
                resumed_from = 0
                with contextlib.suppress(FileNotFoundError):
                    part.unlink()
        stats.resumed_from = resumed_from
        stats.bytes_done = resumed_from

        # The handle lives in `_Inbound` and is closed by `_finish_inbound` /
        # `_abort_all`, so it is deliberately not a `with` block: a chunk arriving
        # ten seconds later still needs it.
        handle: BinaryIO = open(part, "r+b" if resumed_from else "wb")  # noqa: SIM115
        if resumed_from:
            handle.seek(resumed_from)
        digest_state = hashlib.sha256()
        if resumed_from:
            # Re-hash what is already on disk: a partial digest cannot be trusted
            # across a session boundary, and re-reading the part file is cheap
            # compared with trusting the wrong bytes.
            with open(part, "rb") as existing:
                remaining = resumed_from
                while remaining > 0:
                    block = existing.read(min(1024 * 1024, remaining))
                    if not block:
                        break
                    digest_state.update(block)
                    remaining -= len(block)
        inbound = _Inbound(
            stats=stats,
            handle=handle,
            digest=digest_state,
            target=target,
            part=part,
            next_index=resumed_from // stats.chunk_size,
        )
        self._inbound[transfer_id] = inbound
        self._stats[transfer_id] = stats
        self._write_sidecar(part, name, size, digest)
        self._publish(stats)
        await self._ensure_pump()
        self._drain_orphans(transfer_id)
        return stats

    def _resume_offset(self, part: Path, name: str, size: int, digest: str) -> int:
        """Bytes already on disk that belong to this exact file, if any."""
        sidecar = part.with_name(part.name + ".json")
        if not part.is_file() or not sidecar.is_file():
            return 0
        try:
            meta = json.loads(sidecar.read_text("utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(meta, dict):
            return 0
        if meta.get("name") != name or meta.get("size") != size or meta.get("sha256") != digest:
            log.info("ignoring %s: it belongs to a different file", part.name)
            return 0
        try:
            held = part.stat().st_size
        except OSError:
            return 0
        return held if 0 < held < size else 0

    async def _ask_resume(self, transfer_id: int, from_bytes: int) -> bool:
        try:
            reply = await self._session.call(
                "file.resume", {"transfer_id": transfer_id, "from_bytes": from_bytes}
            )
        except Exception as exc:  # noqa: BLE001 - an older phone simply cannot
            log.info("the phone cannot resume (%s); starting from zero", exc)
            return False
        return isinstance(reply, dict) and reply.get("ok") is True

    # ------------------------------------------------------------------ #
    # control
    # ------------------------------------------------------------------ #
    async def cancel(self, transfer_id: int) -> None:
        self._cancelled.add(transfer_id)
        self._paused.discard(transfer_id)
        event = self._resume_events.get(transfer_id)
        if event is not None:
            event.set()
        stats = self._stats.get(transfer_id)
        direction_stream = (
            FILE_STREAM_TO_PHONE if stats and stats.direction == "to_phone" else FILE_STREAM_TO_PHONE
        )
        await self._quiet_send(cancel(transfer_id, 0), direction_stream)
        inbound = self._inbound.pop(transfer_id, None)
        if inbound is not None:
            inbound.close()
            with contextlib.suppress(OSError):
                inbound.part.unlink()
            self._drop_sidecar(inbound.part)
            inbound.stats.state = CANCELLED
            inbound.stats.finished_at = time.monotonic()
            self._publish(inbound.stats)
            self._record(inbound.stats)
        with contextlib.suppress(Exception):
            await self._session.call("file.cancel", {"transfer_id": transfer_id}, timeout=5.0)

    async def pause(self, transfer_id: int) -> bool:
        stats = self._stats.get(transfer_id)
        if stats is None or stats.state != ACTIVE:
            return False
        self._paused.add(transfer_id)
        self._resume_events.setdefault(transfer_id, asyncio.Event())
        stats.state = PAUSED
        self._publish(stats)
        await self._quiet_send(pause_frame(transfer_id, 0), FILE_STREAM_TO_PHONE)
        with contextlib.suppress(Exception):
            await self._session.call("file.pause", {"transfer_id": transfer_id}, timeout=5.0)
        return True

    async def resume(self, transfer_id: int) -> bool:
        stats = self._stats.get(transfer_id)
        if stats is None or stats.state != PAUSED:
            return False
        self._paused.discard(transfer_id)
        stats.state = ACTIVE
        event = self._resume_events.setdefault(transfer_id, asyncio.Event())
        event.set()
        self._publish(stats)
        await self._quiet_send(resume_frame(transfer_id, 0), FILE_STREAM_TO_PHONE)
        with contextlib.suppress(Exception):
            await self._session.call("file.resume", {"transfer_id": transfer_id}, timeout=5.0)
        return True

    async def _wait_if_paused(self, transfer_id: int) -> None:
        if transfer_id not in self._paused:
            return
        event = self._resume_events.setdefault(transfer_id, asyncio.Event())
        await event.wait()
        event.clear()

    # ------------------------------------------------------------------ #
    # receiving pump
    # ------------------------------------------------------------------ #
    def push(self, body: bytes | None) -> bool:
        """Feed one message from the *other* direction (tests, manual bridges)."""
        if body is None:
            self._abort_all("the session ended")
            return False
        queue = self._queue
        if queue is None:
            self._handle_body(body)
            return False
        try:
            queue.put_nowait(body)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(body)
            return True
        return False

    async def _ensure_pump(self) -> None:
        if self._task is not None and not self._task.done():
            return
        subscribe = getattr(self._session, "subscribe_blobs", None)
        if callable(subscribe):
            queue: asyncio.Queue[bytes | None] = subscribe(FILE_STREAM_TO_PC, maxsize=INCOMING_QUEUE)
            self._queue = queue
            self._task = asyncio.get_running_loop().create_task(
                self._receive_loop(queue), name="files-receive"
            )
        else:  # pragma: no cover - a session without media plumbing
            self._queue = asyncio.Queue(maxsize=INCOMING_QUEUE)

    async def _receive_loop(self, queue: asyncio.Queue[bytes | None]) -> None:
        while True:
            body = await queue.get()
            if body is None:
                self._abort_all("the session ended")
                return
            self._handle_body(body)

    def _handle_body(self, body: bytes) -> None:
        try:
            chunk = decode_chunk(body)
        except FileFrameError as exc:
            log.warning("dropping a malformed file chunk: %s", exc)
            return
        acks = self._acks.get(chunk.transfer_id)
        if chunk.flags & FLAG_ACK:
            if acks is not None:
                acks.note(chunk.chunk_index)
            return
        if chunk.flags & FLAG_CANCEL:
            stats = self._stats.get(chunk.transfer_id)
            if stats is not None and stats.state in {ACTIVE, PAUSED}:
                stats.state = CANCELLED
                stats.error = "the phone cancelled the transfer"
                stats.finished_at = time.monotonic()
                self._publish(stats)
            return
        inbound = self._inbound.get(chunk.transfer_id)
        if inbound is None:
            if self._buffer_orphan(chunk):
                return
            # Data for a transfer we are not receiving: tell the peer to stop
            # rather than silently discard a file that will never complete.
            asyncio.get_event_loop().create_task(
                self._quiet_send(cancel(chunk.transfer_id, 0), FILE_STREAM_TO_PHONE)
            )
            return
        self._write_chunk(inbound, chunk)

    def _buffer_orphan(self, chunk: FileChunk) -> bool:
        """Hold an early chunk until its transfer is registered, within limits.

        Bounded by chunk count and bytes: an unknown stream id must not be a way
        to make the PC allocate memory.
        """
        pending = self._orphans.setdefault(chunk.transfer_id, [])
        if len(pending) >= 64 or self._orphan_bytes + len(chunk.payload) > 1024 * 1024:
            self._orphans.pop(chunk.transfer_id, None)
            return False
        pending.append(chunk)
        self._orphan_bytes += len(chunk.payload)
        return True

    def _drain_orphans(self, transfer_id: int) -> None:
        pending = self._orphans.pop(transfer_id, [])
        if not pending:
            return
        inbound = self._inbound.get(transfer_id)
        if inbound is None:
            return
        for chunk in sorted(pending, key=lambda c: c.chunk_index):
            self._write_chunk(inbound, chunk)
            if transfer_id not in self._inbound:
                return  # the transfer ended (completed or failed) while draining

    def _write_chunk(self, inbound: _Inbound, chunk: FileChunk) -> None:
        stats = inbound.stats
        if chunk.chunk_index < inbound.next_index:
            return  # already have it (a retry, not an error)
        if chunk.chunk_index > inbound.next_index:
            stats.error = (
                f"chunk {chunk.chunk_index} arrived before {inbound.next_index}; "
                "the stream lost data"
            )
            stats.state = FAILED
            stats.finished_at = time.monotonic()
            inbound.close()
            self._inbound.pop(stats.transfer_id, None)
            self._publish(stats)
            self._record(stats)
            asyncio.get_event_loop().create_task(
                self._quiet_send(cancel(stats.transfer_id, 0), FILE_STREAM_TO_PHONE)
            )
            return
        try:
            inbound.handle.write(chunk.payload)
        except OSError as exc:
            stats.state = FAILED
            stats.error = f"could not write {inbound.part}: {exc}"
            inbound.close()
            self._inbound.pop(stats.transfer_id, None)
            self._publish(stats)
            self._record(stats)
            return
        inbound.digest.update(chunk.payload)
        inbound.next_index += 1
        inbound.acked = chunk.chunk_index
        stats.bytes_done = min(stats.size, stats.bytes_done + len(chunk.payload))
        self._publish(stats)

        if chunk.flags & FLAG_END:
            self._finish_inbound(inbound)
            return
        if chunk.chunk_index % ACK_EVERY_CHUNKS == 0:
            asyncio.get_event_loop().create_task(
                self._quiet_send(ack(stats.transfer_id, chunk.chunk_index), FILE_STREAM_TO_PHONE)
            )

    def _finish_inbound(self, inbound: _Inbound) -> None:
        stats = inbound.stats
        part = inbound.part
        expected = stats.sha256
        inbound.close()
        self._inbound.pop(stats.transfer_id, None)
        try:
            actual = sha256_of(part)
        except OSError as exc:
            stats.state = FAILED
            stats.error = f"could not read back {part}: {exc}"
            stats.finished_at = time.monotonic()
            self._publish(stats)
            self._record(stats)
            return

        stats.finished_at = time.monotonic()
        if actual != expected:
            stats.state = FAILED
            stats.verified = False
            stats.error = f"hash mismatch: the phone said {expected}, the file is {actual}"
            self._drop_sidecar(part)
            with contextlib.suppress(OSError):
                part.unlink()
            self._publish(stats)
            self._record(stats)
            asyncio.get_event_loop().create_task(
                self._quiet_send(cancel(stats.transfer_id, 0), FILE_STREAM_TO_PHONE)
            )
            return

        try:
            os.replace(part, inbound.target)
        except OSError as exc:
            stats.state = FAILED
            stats.error = f"could not move the finished file into place: {exc}"
            self._publish(stats)
            self._record(stats)
            return
        self._drop_sidecar(part)
        stats.verified = True
        stats.state = COMPLETE
        stats.bytes_done = stats.size
        self._publish(stats)
        self._record(stats)
        asyncio.get_event_loop().create_task(
            self._quiet_send(ack(stats.transfer_id, inbound.acked), FILE_STREAM_TO_PHONE)
        )

    def _abort_all(self, reason: str) -> None:
        self._orphans.clear()
        self._orphan_bytes = 0
        for inbound in list(self._inbound.values()):
            inbound.close()
            inbound.stats.state = FAILED
            inbound.stats.error = reason
            inbound.stats.finished_at = time.monotonic()
            self._publish(inbound.stats)
            self._record(inbound.stats)
        self._inbound.clear()

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    async def _send(self, chunk: FileChunk, stream_id: int) -> None:
        sender = getattr(self._session, "send_file_chunk", None)
        if callable(sender):
            await sender(chunk.encode(), stream_id)
            return
        # A session without file plumbing is still a session: fall back to the
        # generic blob sender so tests and diagnostics work unchanged.
        blob = getattr(self._session, "send_blob", None)
        if callable(blob):
            await blob(chunk.encode(), stream_id)
            return
        raise TransferError("this session cannot send file chunks")

    async def _quiet_send(self, chunk: FileChunk, stream_id: int) -> None:
        try:
            await self._send(chunk, stream_id)
        except Exception as exc:  # noqa: BLE001 - a control frame must never raise
            log.debug("could not send %r: %s", chunk, exc)

    async def _quiet_rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if not getattr(self._session, "is_open", False):
            return None
        try:
            return await self._session.call(method, params or {}, timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            log.debug("%s failed: %s", method, exc)
            return None

    def _write_sidecar(self, part: Path, name: str, size: int, digest: str) -> None:
        sidecar = part.with_name(part.name + ".json")
        with contextlib.suppress(OSError):
            sidecar.write_text(
                json.dumps({"name": name, "size": size, "sha256": digest}), "utf-8"
            )

    def _drop_sidecar(self, part: Path) -> None:
        with contextlib.suppress(OSError):
            part.with_name(part.name + ".json").unlink()

    def _publish(self, stats: TransferStats) -> None:
        if self._on_change is None:
            return
        with contextlib.suppress(Exception):
            self._on_change(stats)

    def _record(self, stats: TransferStats) -> None:
        self._history.append(stats)
        if len(self._history) > 50:
            del self._history[: len(self._history) - 50]

    async def close(self, reason: str = "session closed") -> None:
        self._abort_all(reason)
        task, self._task = self._task, None
        self._queue = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def _explain(prefix: str, exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return f"{prefix}: {text}"


__all__ = [
    "ACTIVE",
    "CANCELLED",
    "COMPLETE",
    "FAILED",
    "PAUSED",
    "PENDING",
    "TransferError",
    "TransferManager",
    "PART_SUFFIX",
    "SIDECAR_SUFFIX",
]
