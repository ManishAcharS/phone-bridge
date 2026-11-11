"""Phase 5.1: interrupted transfers resume, over a real session and real frames.

The Phase 5 tests could show a transfer *completing*. They could not show one
*surviving an interruption*, because nothing in them ever dropped a connection
half way through. Every test here does the unglamorous thing:

1. open a real WebSocket, run the real P-256 handshake, real AES-256-GCM;
2. start the real :class:`~phonebridge.files.TransferManager` (or the real
   simulated sender in ``tools/simulator/phone_sim.py``);
3. kill the session in the middle of the transfer, exactly as a Wi-Fi drop or a
   closed laptop lid does;
4. connect again and let the production code decide what to do about it.

No chunk is ever injected by hand. The bytes on the wire come from
``_stream_out`` and ``_send_file_chunks``; the bytes on disk are written by
``_write_chunk``; the acknowledgements are the real ones. What the simulator
stands in for is the *handset* — SAF, its radios, its sensors — and it says so
in every reply.

The assertions are deliberately physical: a byte offset that is a multiple of
the negotiated chunk size, a total that equals the SHA-256 that was promised, a
``.part`` that is gone only after the file was verified, and a duplicate chunk
that was acknowledged but not written twice.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from phone_sim import SimulatedPhone
from test_e2e_workflows import make_session, start_phone

from phonebridge.files import COMPLETE, FAILED, TransferManager, part_path
from phonebridge.files import transfer as transfer_module

#: Not the default 64 KiB on purpose: a resume offset that happens to be a power
#: of two would still look aligned if the code used the wrong chunk size.
CHUNK = 8192


async def wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    """Poll a condition without assuming it becomes true immediately."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


@pytest.fixture()
async def phone(sim_state, tmp_path: Path):
    phone = await start_phone(sim_state)
    phone.file_dir = tmp_path / "phone"
    phone.file_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield phone
    finally:
        await phone.stop()


@pytest.fixture()
def fast_ack(monkeypatch: pytest.MonkeyPatch):
    """Shorten the acknowledgement timeout so a retry fits inside a test.

    The constant itself is the product decision (15 s, per docs/23); a test that
    waited it out four times would take a minute. Nothing else is patched.
    """
    monkeypatch.setattr(transfer_module, "ACK_TIMEOUT_S", 0.2)
    return 0.2


def _only_transfer(phone: SimulatedPhone) -> dict[str, Any]:
    entry = next(iter(phone.files.values()), None)
    assert entry is not None, "the phone was never offered a transfer"
    return entry


async def _session(phone: SimulatedPhone, pc_config):
    return await make_session(phone, pc_config).open()


async def _kill(session, manager: TransferManager, *tasks) -> None:
    """Drop the link mid-transfer, the way a radio does: no goodbye frame."""
    await session.close()
    await manager.close("the link dropped mid-transfer")
    for task in tasks:
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


# --------------------------------------------------------------------------- #
# PC -> phone
# --------------------------------------------------------------------------- #
async def test_an_interrupted_send_resumes_at_the_last_whole_chunk(
    phone: SimulatedPhone, pc_config, tmp_path: Path
):
    """Drop the link after a few chunks, reconnect, and finish the file.

    The phone keeps its partial document; the second attempt must start at the
    chunk boundary the phone reports, not at zero and not mid-chunk.
    """
    # 8 MiB: 1024 chunks at 8 KiB. Big enough that the transfer cannot possibly
    # finish between the poll that notices the third chunk and the socket closing,
    # which is what makes "interrupted" mean something.
    payload = bytes(range(256)) * 32768
    source = tmp_path / "resume.bin"
    source.write_bytes(payload)
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    session = await _session(phone, pc_config)
    manager = TransferManager(session, destination=inbox)
    task = asyncio.create_task(manager.send_file(source, chunk_size=CHUNK))
    assert await wait_for(lambda: bool(phone.files) and _only_transfer(phone)["chunk_count"] >= 3)
    await _kill(session, manager, task)

    entry = _only_transfer(phone)
    held = int(entry["chunk_count"]) * CHUNK
    assert entry["state"] != "complete", "the transfer was supposed to be interrupted"
    assert 0 < held < len(payload), "the cut has to land inside the file to prove anything"
    assert not (phone.file_dir / "resume.bin").exists(), "an interrupted file must not appear"

    session2 = await _session(phone, pc_config)
    manager2 = TransferManager(session2, destination=inbox)
    try:
        stats = await manager2.send_file(source, chunk_size=CHUNK)
        assert stats.state == COMPLETE, stats.error
        assert stats.verified is True
        assert stats.resumed_from == held, "the sender ignored the phone's resume offset"
        assert stats.resumed_from % CHUNK == 0, "resume offset is not a chunk boundary"
        # The phone refuses a chunk it did not expect, so success here *is* the
        # proof that no chunk was written twice and none was skipped.
        stored = phone.file_dir / "resume.bin"
        assert stored.read_bytes() == payload
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == stats.sha256
    finally:
        await manager2.close()
        await session2.close()


async def test_a_resume_offset_that_is_not_a_chunk_boundary_is_refused(
    phone: SimulatedPhone, pc_config
):
    """The phone must not accept an offset it cannot start streaming at."""
    phone.outbound_file = ("alignment.bin", b"x" * 200_000)
    session = await _session(phone, pc_config)
    try:
        reply = await session.call("file.request", {})
        transfer_id = reply["transfer_id"]
        chunk_size = reply["chunk_size"]
        with pytest.raises(Exception) as excinfo:
            await session.call(
                "file.resume", {"transfer_id": transfer_id, "from_bytes": chunk_size // 2}
            )
        assert "multiple" in str(excinfo.value).lower()
        with pytest.raises(Exception):
            # Past the end of the file: there is nothing there to skip.
            await session.call(
                "file.resume", {"transfer_id": transfer_id, "from_bytes": 10**9}
            )
    finally:
        await session.close()


async def test_a_lost_acknowledgement_is_retried_instead_of_failing(
    phone: SimulatedPhone, pc_config, tmp_path: Path, fast_ack
):
    """A dropped ack used to stall the window until the transfer failed.

    The phone drops the acks for chunks 1 to 8 on purpose, so the sender's window
    has not advanced by the time it reaches chunk 8. It must re-send a chunk, get
    an ack, and finish — with the file intact.
    """
    payload = bytes(range(256)) * 4096  # 1 MiB: enough chunks to fill a window
    source = tmp_path / "lossy.bin"
    source.write_bytes(payload)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    # Eight consecutive acks, so the sender's window cannot advance cumulatively:
    # at chunk 8 it is still waiting for chunk 1.
    phone.file_drop_ack_from = 1
    phone.file_drop_ack_count = 8

    session = await _session(phone, pc_config)
    manager = TransferManager(session, destination=inbox)
    try:
        stats = await manager.send_file(source, chunk_size=CHUNK)
        assert stats.state == COMPLETE, stats.error
        assert phone.file_dropped_acks == 8, "the phone was supposed to lose eight acks"
        assert stats.retries >= 1, "the sender never retried, so how did it finish?"
        stored = phone.file_dir / "lossy.bin"
        assert stored.read_bytes() == payload
    finally:
        await manager.close()
        await session.close()


# --------------------------------------------------------------------------- #
# phone -> PC
# --------------------------------------------------------------------------- #
async def test_an_interrupted_download_resumes_from_the_partial_file(
    phone: SimulatedPhone, pc_config, tmp_path: Path
):
    """The PC keeps a ``.part`` and its sidecar, then resumes into it."""
    payload = bytes(range(256)) * 8192  # 2 MiB: 32 chunks at 64 KiB
    phone.outbound_file = ("phone.bin", payload)
    phone.file_pace_s = 0.01
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    part = part_path(inbox / "phone.bin")
    sidecar = part.with_name(part.name + ".json")

    session = await _session(phone, pc_config)
    manager = TransferManager(session, destination=inbox)
    stats = await manager.request_file()
    assert await wait_for(lambda: part.exists() and part.stat().st_size >= 2 * 65536)
    await _kill(session, manager)

    held = part.stat().st_size
    assert held > 0, "the interrupted download lost the bytes it had already written"
    assert sidecar.is_file(), "the sidecar must survive so a resume knows what the part is"
    attempt = manager.get(stats.transfer_id)
    assert attempt.state == FAILED
    assert not (inbox / "phone.bin").exists(), "a half file must never look finished"

    session2 = await _session(phone, pc_config)
    manager2 = TransferManager(session2, destination=inbox)
    try:
        resumed = await manager2.request_file()
        assert resumed.resumed_from > 0, "the PC asked to start over instead of resuming"
        assert resumed.resumed_from % resumed.chunk_size == 0, "not a chunk boundary"
        assert resumed.resumed_from <= held
        assert await wait_for(
            lambda: manager2.get(resumed.transfer_id).state in {COMPLETE, FAILED}
        )
        final = manager2.get(resumed.transfer_id)
        assert final.state == COMPLETE, final.error
        assert final.verified is True
        assert final.sha256 == hashlib.sha256(payload).hexdigest()
        assert (inbox / "phone.bin").read_bytes() == payload
        assert not part.exists(), "a verified file must not leave a .part behind"
        assert not sidecar.exists(), "a verified file must not leave a sidecar behind"
    finally:
        await manager2.close()
        await session2.close()


async def test_a_second_interruption_still_resumes(
    phone: SimulatedPhone, pc_config, tmp_path: Path
):
    """Resume is not a one-shot: two drops in a row still converge on the file."""
    payload = bytes(range(256)) * 8192
    phone.outbound_file = ("twice.bin", payload)
    phone.file_pace_s = 0.01
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    part = part_path(inbox / "twice.bin")

    sizes: list[int] = []
    for _attempt in range(2):
        session = await _session(phone, pc_config)
        manager = TransferManager(session, destination=inbox)
        wanted = (len(sizes) + 1) * 65536
        await manager.request_file()
        assert await wait_for(
            lambda: part.exists() and part.stat().st_size >= wanted  # noqa: B023 - rebound below
        ), "the transfer never reached the next chunk"
        await _kill(session, manager)
        sizes.append(part.stat().st_size)

    assert sizes[1] > 0

    session = await _session(phone, pc_config)
    manager = TransferManager(session, destination=inbox)
    try:
        stats = await manager.request_file()
        assert stats.resumed_from > 0
        assert await wait_for(lambda: manager.get(stats.transfer_id).state in {COMPLETE, FAILED})
        final = manager.get(stats.transfer_id)
        assert final.state == COMPLETE, final.error
        assert (inbox / "twice.bin").read_bytes() == payload
    finally:
        await manager.close()
        await session.close()


async def test_a_phone_that_cannot_resume_restarts_without_duplicating_bytes(
    phone: SimulatedPhone, pc_config, tmp_path: Path
):
    """An older phone answers zero; the duplicates it then sends must be acked.

    This is the case that used to hang for fifteen seconds and then fail: the
    receiver skipped the chunks it already had *without acknowledging them*, so
    the sender's bounded window never advanced.
    """
    payload = bytes(range(256)) * 8192
    phone.outbound_file = ("legacy.bin", payload)
    phone.file_pace_s = 0.005
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    part = part_path(inbox / "legacy.bin")

    session = await _session(phone, pc_config)
    manager = TransferManager(session, destination=inbox)
    await manager.request_file()
    assert await wait_for(lambda: part.exists() and part.stat().st_size >= 2 * 65536)
    await _kill(session, manager)
    held = part.stat().st_size
    assert held >= 2 * 65536

    phone.file_cannot_resume = True
    session2 = await _session(phone, pc_config)
    manager2 = TransferManager(session2, destination=inbox)
    try:
        stats = await manager2.request_file()
        assert stats.resumed_from == 0, "the PC must believe the phone, not itself"
        assert await wait_for(
            lambda: manager2.get(stats.transfer_id).state in {COMPLETE, FAILED}, timeout=20.0
        )
        final = manager2.get(stats.transfer_id)
        assert final.state == COMPLETE, final.error
        assert (inbox / "legacy.bin").read_bytes() == payload
        assert not part.exists()
    finally:
        await manager2.close()
        await session2.close()


async def test_a_wrong_digest_on_a_resumed_download_removes_the_bad_file(
    phone: SimulatedPhone, pc_config, tmp_path: Path
):
    """Verification still decides. A mismatch destroys the part, not the truth."""
    payload = bytes(range(256)) * 1024
    phone.outbound_file = ("corrupt.bin", payload)
    phone.file_bad_digest = True
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    part = part_path(inbox / "corrupt.bin")

    session = await _session(phone, pc_config)
    manager = TransferManager(session, destination=inbox)
    try:
        stats = await manager.request_file()
        assert await wait_for(lambda: manager.get(stats.transfer_id).state in {COMPLETE, FAILED})
        final = manager.get(stats.transfer_id)
        assert final.state == FAILED
        assert "hash mismatch" in (final.error or "").lower()
        assert not part.exists(), "a file that failed its hash must not be resumable"
        assert not part.with_name(part.name + ".json").exists()
        assert not (inbox / "corrupt.bin").exists()
    finally:
        await manager.close()
        await session.close()


async def test_a_short_stream_keeps_its_partial_file(
    phone: SimulatedPhone, pc_config, tmp_path: Path
):
    """Bytes that are a valid prefix are worth keeping; a lie about them is not.

    The phone promises 4 KiB more than it sends. The receiver ends with a short
    file: it must fail the transfer, keep the part and say how far it got —
    deleting it here is what made an interrupted download start over.
    """
    payload = bytes(range(256)) * 8  # 2 KiB: one chunk
    phone.outbound_file = ("short.bin", payload)
    phone.file_report_size_extra = 4096
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    part = part_path(inbox / "short.bin")

    session = await _session(phone, pc_config)
    manager = TransferManager(session, destination=inbox)
    try:
        stats = await manager.request_file()
        assert stats.size == len(payload) + 4096
        assert await wait_for(lambda: manager.get(stats.transfer_id).state in {COMPLETE, FAILED})
        final = manager.get(stats.transfer_id)
        assert final.state == FAILED
        assert "bytes" in (final.error or "")
        assert part.is_file(), "a short but valid prefix must be kept for a resume"
        assert part.stat().st_size == len(payload)
        assert part.with_name(part.name + ".json").is_file()
        assert not (inbox / "short.bin").exists()
    finally:
        await manager.close()
        await session.close()


async def test_cancelling_a_download_cleans_up_the_part_and_sidecar(
    phone: SimulatedPhone, pc_config, tmp_path: Path
):
    """Cancellation is explicit, so it *is* allowed to delete partial data."""
    payload = bytes(range(256)) * 8192
    phone.outbound_file = ("cancel-me.bin", payload)
    phone.file_pace_s = 0.01
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    part = part_path(inbox / "cancel-me.bin")

    session = await _session(phone, pc_config)
    manager = TransferManager(session, destination=inbox)
    try:
        stats = await manager.request_file()
        assert await wait_for(lambda: part.exists() and part.stat().st_size > 0)
        await manager.cancel(stats.transfer_id)
        assert not part.exists()
        assert not part.with_name(part.name + ".json").exists()
        assert not (inbox / "cancel-me.bin").exists()
    finally:
        await manager.close()
        await session.close()


# --------------------------------------------------------------------------- #
# the real sender again, this time with a non-default chunk size end to end
# --------------------------------------------------------------------------- #
async def test_a_negotiated_chunk_size_is_what_both_sides_use(
    phone: SimulatedPhone, pc_config, tmp_path: Path
):
    """The phone echoes the chunk size, so it is the phone's number that counts."""
    payload = bytes(range(256)) * 300  # 76800 bytes = 3.75 chunks of 20480
    source = tmp_path / "negotiated.bin"
    source.write_bytes(payload)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    session = await _session(phone, pc_config)
    manager = TransferManager(session, destination=inbox, chunk_size=20480)
    try:
        stats = await manager.send_file(source)
        assert stats.state == COMPLETE, stats.error
        assert stats.chunk_size == 20480
        entry = _only_transfer(phone)
        assert entry["chunk_size"] == 20480, "the phone did not negotiate the size it used"
        assert (phone.file_dir / "negotiated.bin").read_bytes() == payload
    finally:
        await manager.close()
        await session.close()
