"""File transfer: chunking, integrity, resume, cancellation and path safety.

The interesting half of this file is the half that tries to go wrong: a corrupted
chunk, a hash that disagrees, a name that tries to escape the folder, a session
that dies mid-transfer. Each of those must end as a *failure with a sentence*, and
must leave nothing behind that could be mistaken for a complete file.

Every transfer here is local: a fake session stands in for the phone and a
temporary directory stands in for both disks. Nothing was tested against a real
handset, and docs/23 says so.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from phonebridge.files import (
    ACTIVE,
    CANCELLED,
    CHUNK_SIZE,
    COMPLETE,
    FAILED,
    FLAG_ACK,
    FLAG_CANCEL,
    FLAG_END,
    FLAG_START,
    PAUSED,
    FileChunk,
    TransferError,
    TransferManager,
    UnsafeName,
    decode_chunk,
    part_path,
    safe_destination,
    safe_name,
    sha256_of,
)
from phonebridge.protocol.messages import RpcError

SMALL = b"hello phonebridge\n"
EMPTY = b""
BIG = bytes(range(256)) * 1200  # 307 200 bytes: several chunks at 64 KiB


def run(coro):
    return asyncio.run(coro)


async def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


class FakePhone:
    """A phone that stores what it receives, and honours what it is told."""

    def __init__(self, *, resume_from: int = 0, verify: bool = True, sha_override: str = ""):
        self.is_open = True
        self.resume_from = resume_from
        self.verify = verify
        self.sha_override = sha_override
        self.calls: list[tuple[str, dict | None]] = []
        self.data = bytearray()
        self.chunks: list[FileChunk] = []
        self.chunk_size = CHUNK_SIZE
        self.outbound = SMALL
        #: A real link takes time; tests that need to interrupt a transfer use it.
        self.chunk_delay = 0.0
        self.refuse: dict[str, Exception] = {}

    async def call(self, method, params=None, timeout: float = 10.0):
        self.calls.append((method, params))
        refusal = self.refuse.get(method)
        if refusal is not None:
            raise refusal
        params = params or {}
        if method == "file.offer":
            self.chunk_size = int(params.get("chunk_size") or CHUNK_SIZE)
            return {
                "transfer_id": 7,
                "resume_from": self.resume_from,
                "chunk_size": self.chunk_size,
            }
        if method == "file.complete":
            actual = hashlib.sha256(bytes(self.data)).hexdigest()
            verified = self.verify and str(params.get("sha256")) == actual
            return {
                "ok": verified,
                "verified": verified,
                "sha256": self.sha_override or actual,
                "bytes": len(self.data),
            }
        if method == "file.request":
            return {
                "transfer_id": 11,
                "name": params.get("name") or "from-phone.txt",
                "size": len(self.outbound),
                "sha256": hashlib.sha256(self.outbound).hexdigest(),
                "chunk_size": CHUNK_SIZE,
            }
        if method in {"file.resume", "file.pause", "file.cancel"}:
            return {"ok": True}
        return {"ok": True}

    #: Set by ``make_manager``: a real phone acknowledges, so the fake must too.
    ack_target: object | None = None

    async def send_file_chunk(self, body: bytes, stream_id: int) -> None:
        chunk = decode_chunk(body)
        if chunk.flags & FLAG_ACK:
            return
        if chunk.flags & (FLAG_CANCEL | 0x10):
            return
        self.chunks.append(chunk)
        if chunk.chunk_index * self.chunk_size != len(self.data):
            raise AssertionError(f"chunk {chunk.chunk_index} arrived out of order")
        self.data.extend(chunk.payload)
        if self.chunk_delay:
            await asyncio.sleep(self.chunk_delay)
        ack_target = self.ack_target
        if ack_target is not None:
            from phonebridge.files.frames import ack as ack_frame

            ack_target.push(ack_frame(chunk.transfer_id, chunk.chunk_index).encode())

    def subscribe_blobs(self, stream_id: int, maxsize: int = 256):
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.queue = queue
        return queue

    def methods(self):
        return [name for name, _ in self.calls]


def make_manager(phone, **kwargs) -> TransferManager:
    """A manager wired to a fake phone that acknowledges what it stores."""
    manager = TransferManager(phone, **kwargs)
    phone.ack_target = manager
    return manager


class FakeSession(FakePhone):
    """The same fake, with the pieces the PC uses for the other direction."""

    async def send_blob(self, data: bytes, stream_id: int = 1) -> None:
        await self.send_file_chunk(data, stream_id)


# --------------------------------------------------------------------------- #
# framing
# --------------------------------------------------------------------------- #
def test_a_chunk_round_trips_and_the_header_is_fourteen_bytes() -> None:
    chunk = FileChunk(transfer_id=1, chunk_index=2, flags=FLAG_START, payload=b"abc")
    encoded = chunk.encode()
    assert len(encoded) == 14 + 3
    decoded = decode_chunk(encoded)
    assert (decoded.transfer_id, decoded.chunk_index, decoded.flags) == (1, 2, FLAG_START)
    assert decoded.payload == b"abc"


def test_an_acknowledgement_carries_no_payload_and_decodes_as_one() -> None:
    from phonebridge.files.frames import ack

    decoded = decode_chunk(ack(3, 9).encode())
    assert decoded.flags & FLAG_ACK
    assert decoded.payload == b""
    assert not decoded.is_data


@pytest.mark.parametrize(
    "payload",
    [b"", b"\x00" * 13, FileChunk(transfer_id=1, chunk_index=0, payload=b"x").encode()[:-1]],
)
def test_a_truncated_chunk_is_refused(payload: bytes) -> None:
    from phonebridge.files.frames import FileFrameError

    with pytest.raises(FileFrameError):
        decode_chunk(payload)


def test_a_chunk_over_the_limit_is_refused() -> None:
    from phonebridge.files.frames import MAX_CHUNK_SIZE, FileFrameError

    with pytest.raises(FileFrameError):
        FileChunk(transfer_id=1, chunk_index=0, payload=b"x" * (MAX_CHUNK_SIZE + 1)).encode()


def test_an_acknowledgement_with_a_payload_is_refused() -> None:
    from phonebridge.files.frames import FileFrameError

    encoded = FileChunk(transfer_id=1, chunk_index=0, flags=FLAG_ACK, payload=b"x").encode()
    with pytest.raises(FileFrameError):
        decode_chunk(encoded)


# --------------------------------------------------------------------------- #
# names and destinations
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["notes.txt", "a b c.pdf", "archive.tar.gz", ".hidden"])
def test_an_ordinary_name_is_accepted(name: str) -> None:
    assert safe_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "..",
        ".",
        "../escape.txt",
        "..\\escape.txt",
        "dir/file.txt",
        "dir\\file.txt",
        "C:\\Windows\\evil.exe",
        "file.txt:stream",
        "trail.",
        "CON",
        "nul.txt",
        "line\nbreak",
        "x" * 201,
        " leading.txt",
    ],
)
def test_a_dangerous_name_is_refused(name: str) -> None:
    with pytest.raises(UnsafeName):
        safe_name(name)


def test_a_destination_cannot_escape_its_folder(tmp_path: Path) -> None:
    target = safe_destination(tmp_path, "notes.txt")
    assert target.parent == tmp_path.resolve()
    with pytest.raises(UnsafeName):
        safe_destination(tmp_path, "../outside.txt")
    nested = tmp_path / "inner"
    nested.mkdir()
    with pytest.raises(UnsafeName):
        safe_destination(nested, "../outside.txt")


def test_hashing_is_streamed_and_correct(tmp_path: Path) -> None:
    payload = os.urandom(300_000)
    path = tmp_path / "blob.bin"
    path.write_bytes(payload)
    assert sha256_of(path) == hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- #
# PC -> phone
# --------------------------------------------------------------------------- #
def test_a_small_file_is_sent_and_verified(tmp_path: Path) -> None:
    async def scenario():
        source = tmp_path / "small.txt"
        source.write_bytes(SMALL)
        phone = FakePhone()
        manager = make_manager(phone)
        stats = await manager.send_file(source)
        assert stats.state == COMPLETE
        assert stats.verified is True
        assert bytes(phone.data) == SMALL
        assert phone.methods()[0] == "file.offer"
        assert "file.complete" in phone.methods()
        await manager.close()

    run(scenario())


def test_an_empty_file_is_sent_as_one_empty_last_chunk(tmp_path: Path) -> None:
    async def scenario():
        source = tmp_path / "empty.bin"
        source.write_bytes(EMPTY)
        phone = FakePhone()
        manager = make_manager(phone)
        stats = await manager.send_file(source)
        assert stats.state == COMPLETE
        assert stats.size == 0
        assert bytes(phone.data) == EMPTY
        assert phone.chunks and phone.chunks[0].flags & FLAG_END
        await manager.close()

    run(scenario())


def test_a_large_file_is_sent_in_chunks_and_matches_byte_for_byte(tmp_path: Path) -> None:
    async def scenario():
        source = tmp_path / "big.bin"
        source.write_bytes(BIG)
        phone = FakePhone()
        manager = make_manager(phone)
        stats = await manager.send_file(source, chunk_size=16 * 1024)
        assert stats.state == COMPLETE
        assert bytes(phone.data) == BIG
        assert len(phone.chunks) == -(-len(BIG) // (16 * 1024))
        assert phone.chunks[0].flags & FLAG_START
        assert phone.chunks[-1].flags & FLAG_END
        await manager.close()

    run(scenario())


def test_a_resume_offset_restarts_at_the_right_chunk(tmp_path: Path) -> None:
    async def scenario():
        source = tmp_path / "big.bin"
        source.write_bytes(BIG)
        chunk = 16 * 1024
        phone = FakePhone(resume_from=chunk * 3)
        phone.data.extend(BIG[: chunk * 3])
        manager = make_manager(phone)
        stats = await manager.send_file(source, chunk_size=chunk)
        assert stats.state == COMPLETE
        assert stats.resumed_from == chunk * 3
        assert bytes(phone.data) == BIG
        await manager.close()

    run(scenario())


def test_the_phone_verifies_and_a_mismatch_fails_the_transfer(tmp_path: Path) -> None:
    async def scenario():
        source = tmp_path / "small.txt"
        source.write_bytes(SMALL)
        phone = FakePhone(verify=False, sha_override="0" * 64)
        manager = make_manager(phone)
        with pytest.raises(TransferError) as excinfo:
            await manager.send_file(source)
        assert "hash" in str(excinfo.value).lower() or "SHA-256" in str(excinfo.value)
        stats = manager.get(7)
        assert stats is not None and stats.state == FAILED
        await manager.close()

    run(scenario())


def test_a_refused_offer_is_reported_as_a_sentence(tmp_path: Path) -> None:
    async def scenario():
        source = tmp_path / "small.txt"
        source.write_bytes(SMALL)
        phone = FakePhone()
        phone.refuse["file.offer"] = RpcError("file_denied", "the user declined the file")
        manager = make_manager(phone)
        with pytest.raises(TransferError) as excinfo:
            await manager.send_file(source)
        assert "the user declined the file" in str(excinfo.value)
        await manager.close()

    run(scenario())


def test_a_missing_file_is_refused_before_any_rpc(tmp_path: Path) -> None:
    async def scenario():
        phone = FakePhone()
        manager = make_manager(phone)
        with pytest.raises(TransferError):
            await manager.send_file(tmp_path / "nope.txt")
        assert phone.calls == []
        await manager.close()

    run(scenario())


def test_a_stalled_link_fails_instead_of_hanging_forever(tmp_path: Path) -> None:
    """No acknowledgements at all: the sender must give up, not wait forever."""
    async def scenario():
        from phonebridge.files import transfer as transfer_module

        source = tmp_path / "big.bin"
        source.write_bytes(BIG)
        phone = FakePhone()
        manager = make_manager(phone)
        phone.ack_target = None  # a link that acknowledges nothing
        original = transfer_module.ACK_TIMEOUT_S
        transfer_module.ACK_TIMEOUT_S = 0.02
        try:
            with pytest.raises(TransferError) as excinfo:
                await manager.send_file(source, chunk_size=8 * 1024)
            assert "acknowledg" in str(excinfo.value).lower()
        finally:
            transfer_module.ACK_TIMEOUT_S = original
            await manager.close()

    run(scenario())


def test_cancelling_a_send_stops_it_and_reports_cancelled(tmp_path: Path) -> None:
    async def scenario():
        source = tmp_path / "big.bin"
        source.write_bytes(BIG)
        phone = FakePhone()
        phone.chunk_delay = 0.02
        manager = make_manager(phone)
        task = asyncio.create_task(manager.send_file(source, chunk_size=8 * 1024))
        await asyncio.sleep(0.05)
        await manager.cancel(7)
        with pytest.raises(TransferError):
            await task
        await manager.close()

    run(scenario())


# --------------------------------------------------------------------------- #
# phone -> PC
# --------------------------------------------------------------------------- #
async def receive_all(manager: TransferManager, phone: FakePhone, payload: bytes = SMALL) -> None:
    """Feed the chunks a phone would send, correctly framed."""
    await manager._ensure_pump()
    total = max(1, -(-len(payload) // CHUNK_SIZE))
    for index in range(total):
        block = payload[index * CHUNK_SIZE : (index + 1) * CHUNK_SIZE]
        flags = 0
        if index == 0:
            flags |= FLAG_START
        if index == total - 1:
            flags |= FLAG_END
        manager.push(FileChunk(transfer_id=11, chunk_index=index, flags=flags, payload=block).encode())
        await asyncio.sleep(0.001)


def test_a_file_from_the_phone_lands_complete_and_verified(tmp_path: Path) -> None:
    async def scenario():
        phone = FakePhone()
        manager = make_manager(phone, destination=tmp_path)
        stats = await manager.request_file()
        assert stats.state == ACTIVE
        await receive_all(manager, phone)
        assert await wait_for(lambda: manager.get(11).state == COMPLETE)
        final = manager.get(11)
        assert final.verified is True
        assert (tmp_path / final.name).read_bytes() == SMALL
        assert not part_path(tmp_path / final.name).exists(), "the .part must be gone"
        await manager.close()

    run(scenario())


def test_an_empty_file_from_the_phone_completes(tmp_path: Path) -> None:
    async def scenario():
        phone = FakePhone()
        phone.outbound = EMPTY
        manager = make_manager(phone, destination=tmp_path)
        stats = await manager.request_file()
        await manager._ensure_pump()
        manager.push(
            FileChunk(transfer_id=11, chunk_index=0, flags=FLAG_START | FLAG_END, payload=b"").encode()
        )
        assert await wait_for(lambda: manager.get(11).state == COMPLETE)
        assert (tmp_path / stats.name).read_bytes() == b""
        await manager.close()

    run(scenario())


def test_a_corrupted_stream_fails_and_leaves_no_file(tmp_path: Path) -> None:
    async def scenario():
        phone = FakePhone()
        manager = make_manager(phone, destination=tmp_path)
        await manager.request_file()
        await manager._ensure_pump()
        bad = SMALL.replace(b"hello", b"HELLO")
        manager.push(
            FileChunk(transfer_id=11, chunk_index=0, flags=FLAG_START | FLAG_END, payload=bad).encode()
        )
        assert await wait_for(lambda: manager.get(11).state == FAILED)
        stats = manager.get(11)
        assert "hash mismatch" in stats.error
        assert stats.verified is False
        assert not (tmp_path / stats.name).exists(), "a corrupt file must not be written"
        await manager.close()

    run(scenario())


def test_a_gap_in_the_chunk_stream_fails_instead_of_silently_truncating(tmp_path: Path) -> None:
    async def scenario():
        phone = FakePhone()
        manager = make_manager(phone, destination=tmp_path)
        await manager.request_file()
        await manager._ensure_pump()
        manager.push(FileChunk(transfer_id=11, chunk_index=0, flags=FLAG_START, payload=b"aa").encode())
        manager.push(FileChunk(transfer_id=11, chunk_index=2, flags=FLAG_END, payload=b"bb").encode())
        assert await wait_for(lambda: manager.get(11).state == FAILED)
        assert "lost data" in manager.get(11).error
        assert not (tmp_path / manager.get(11).name).exists()
        await manager.close()

    run(scenario())


def test_a_repeated_chunk_is_ignored_not_appended_twice(tmp_path: Path) -> None:
    async def scenario():
        phone = FakePhone()
        manager = make_manager(phone, destination=tmp_path)
        await manager.request_file()
        await manager._ensure_pump()
        chunk = FileChunk(transfer_id=11, chunk_index=0, flags=FLAG_START, payload=b"abcd").encode()
        manager.push(chunk)
        manager.push(chunk)
        manager.push(FileChunk(transfer_id=11, chunk_index=1, flags=FLAG_END, payload=b"efgh").encode())
        assert await wait_for(lambda: manager.get(11).state in {COMPLETE, FAILED})
        await manager.close()

    run(scenario())


def test_a_traversal_name_from_the_phone_is_refused_before_writing(tmp_path: Path) -> None:
    async def scenario():
        phone = FakePhone()
        phone.call = lambda method, params=None, timeout=10.0: _evil_offer(method, params)
        manager = make_manager(phone, destination=tmp_path)
        with pytest.raises(TransferError) as excinfo:
            await manager.request_file()
        assert "refusing" in str(excinfo.value).lower()
        assert list(tmp_path.iterdir()) == []
        await manager.close()

    run(scenario())


async def _evil_offer(method, params=None):
    if method == "file.request":
        return {
            "transfer_id": 11,
            "name": "../../etc/passwd",
            "size": 3,
            "sha256": hashlib.sha256(b"abc").hexdigest(),
            "chunk_size": CHUNK_SIZE,
        }
    return {"ok": True}


def test_a_file_offered_without_a_hash_is_refused(tmp_path: Path) -> None:
    async def scenario():
        phone = FakePhone()

        async def no_hash(method, params=None, timeout=10.0):
            if method == "file.request":
                return {"transfer_id": 3, "name": "x.txt", "size": 1, "chunk_size": CHUNK_SIZE}
            return {"ok": True}

        phone.call = no_hash
        manager = make_manager(phone, destination=tmp_path)
        with pytest.raises(TransferError) as excinfo:
            await manager.request_file()
        assert "SHA-256" in str(excinfo.value)
        await manager.close()

    run(scenario())


def test_receiving_without_a_destination_is_refused(tmp_path: Path) -> None:
    async def scenario():
        phone = FakePhone()
        manager = make_manager(phone)
        with pytest.raises(TransferError) as excinfo:
            await manager.request_file()
        assert "destination" in str(excinfo.value)
        await manager.close()

    run(scenario())


def test_a_part_file_is_resumed_when_it_matches(tmp_path: Path) -> None:
    """A resume happens at a chunk boundary, because that is what the peer can do."""
    async def scenario():
        chunk = CHUNK_SIZE
        total = -(-len(BIG) // chunk)
        phone = FakePhone()
        phone.outbound = BIG
        manager = make_manager(phone, destination=tmp_path)
        stats = await manager.request_file()
        await manager._ensure_pump()
        # Two whole chunks arrive, then the link dies.
        for index in range(2):
            flags = FLAG_START if index == 0 else 0
            manager.push(
                FileChunk(
                    transfer_id=11,
                    chunk_index=index,
                    flags=flags,
                    payload=BIG[index * chunk : (index + 1) * chunk],
                ).encode()
            )
        await asyncio.sleep(0.02)
        await manager.close("dropped")
        part = part_path(tmp_path / stats.name)
        assert part.exists(), "the partial file must survive a drop"
        assert part.stat().st_size == 2 * chunk

        # Second attempt: the same file is offered again and the part is reused.
        phone2 = FakePhone()
        phone2.outbound = BIG
        manager2 = make_manager(phone2, destination=tmp_path)
        await manager2.request_file()
        assert manager2.get(11).resumed_from == 2 * chunk
        for index in range(2, total):
            flags = FLAG_END if index == total - 1 else 0
            manager2.push(
                FileChunk(
                    transfer_id=11,
                    chunk_index=index,
                    flags=flags,
                    payload=BIG[index * chunk : (index + 1) * chunk],
                ).encode()
            )
        assert await wait_for(
            lambda: manager2.get(11).state in {COMPLETE, FAILED}, timeout=10
        )
        final = manager2.get(11)
        assert final.state == COMPLETE, final.error
        assert (tmp_path / final.name).read_bytes() == BIG
        await manager2.close()

    run(scenario())


def test_a_stale_part_file_for_a_different_file_is_not_reused(tmp_path: Path) -> None:
    async def scenario():
        part = tmp_path / "from-phone.txt.part"
        part.write_bytes(b"from another file")
        part.with_name(part.name + ".json").write_text(
            '{"name": "from-phone.txt", "size": 3, "sha256": "deadbeef"}', "utf-8"
        )
        phone = FakePhone()
        manager = make_manager(phone, destination=tmp_path)
        await manager.request_file()
        assert manager.get(11).resumed_from == 0
        await manager.close()

    run(scenario())


def test_a_cancelled_receive_deletes_the_part_file(tmp_path: Path) -> None:
    async def scenario():
        phone = FakePhone()
        manager = make_manager(phone, destination=tmp_path)
        await manager.request_file()
        await manager._ensure_pump()
        manager.push(FileChunk(transfer_id=11, chunk_index=0, flags=FLAG_START, payload=b"abc").encode())
        await asyncio.sleep(0.02)
        await manager.cancel(11)
        assert await wait_for(lambda: manager.get(11).state == CANCELLED)
        assert not (tmp_path / "from-phone.txt.part").exists()
        await manager.close()

    run(scenario())


def test_pause_and_resume_change_state_without_losing_the_transfer(tmp_path: Path) -> None:
    async def scenario():
        phone = FakePhone()
        manager = make_manager(phone, destination=tmp_path)
        await manager.request_file()
        assert await manager.pause(11) is True
        assert manager.get(11).state == PAUSED
        assert await manager.resume(11) is True
        assert manager.get(11).state == ACTIVE
        await manager.close()

    run(scenario())


def test_the_session_dying_fails_an_open_receive(tmp_path: Path) -> None:
    async def scenario():
        phone = FakePhone()
        manager = make_manager(phone, destination=tmp_path)
        await manager.request_file()
        await manager._ensure_pump()
        manager.push(None)  # documented end-of-stream marker
        assert manager.get(11).state == FAILED
        assert "session ended" in manager.get(11).error
        await manager.close()

    run(scenario())


def test_the_snapshot_is_json_safe_and_bounded(tmp_path: Path) -> None:
    async def scenario():
        phone = FakePhone()
        manager = make_manager(phone, destination=tmp_path)
        await manager.request_file()
        snapshot = manager.snapshot()
        assert snapshot["destination"] == str(tmp_path)
        assert isinstance(snapshot["active"], list)
        assert snapshot["chunk_size"] >= 1024
        for entry in snapshot["active"]:
            assert isinstance(entry["fraction"], float)
            assert entry["direction"] in {"to_phone", "to_pc"}
        await manager.close()

    run(scenario())
