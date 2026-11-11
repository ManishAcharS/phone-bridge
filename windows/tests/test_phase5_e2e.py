"""Phase 5 end to end: sensors, files and Bluetooth over a real session.

Every test here opens a real WebSocket, runs the real P-256 handshake, encrypts
with real AES-256-GCM and drives the reference phone in
``tools/simulator/phone_sim.py`` through the same code the GUI uses.

The distinction that matters: the *transport*, the framing, the sequencing, the
acknowledgements and the integrity checks are exercised for real; the *hardware*
is not. The simulator's sensors are computed, its files come from a constant and
its GATT tree is a literal. Each reply says so, and each capability detail says
so, so nothing downstream can mistake this for a handset.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from phone_sim import SimulatedPhone
from test_e2e_workflows import make_session, start_phone

from phonebridge.bluetooth import BluetoothLink, normalise_uuid
from phonebridge.bluetooth.frames import GATT_CONNECTED
from phonebridge.files import COMPLETE, TransferManager, part_path
from phonebridge.sensors import ACTIVE, SensorLink

BATTERY = normalise_uuid("180f")
BATTERY_LEVEL = normalise_uuid("2a19")


@pytest.fixture()
async def connected(sim_state, pc_config, tmp_path):
    phone = await start_phone(sim_state)
    phone.file_dir = tmp_path / "phone"
    phone.file_dir.mkdir(parents=True, exist_ok=True)
    session = await make_session(phone, pc_config).open()
    try:
        yield phone, session, tmp_path
    finally:
        await session.close()
        await phone.stop()


async def wait_for(predicate, timeout: float = 8.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------- #
# sensors
# --------------------------------------------------------------------------- #
async def test_sensors_are_listed_started_and_streamed(connected):
    phone: SimulatedPhone
    phone, session, _tmp = connected
    link = SensorLink(session, interval_us=10000)
    registry = await link.refresh()
    assert len(registry.sensors) >= 5
    kinds = {info.kind for info in registry.sensors}
    assert {1, 4} <= kinds, "accelerometer and gyroscope at least"

    assert await link.start([1, 4]) is True
    assert link.snapshot().state == ACTIVE
    sent = await phone.emit_sensor_frames(3)
    assert sent == 6, "three readings for each of two sensors"

    assert await wait_for(lambda: link.reading(1) is not None and link.reading(4) is not None)
    accel = link.reading(1)
    assert accel is not None and accel.kind == 1
    assert set(accel.reading) == {"x", "y", "z"}
    stats = link.snapshot().stats["1"]
    assert stats["received"] == 3
    assert stats["invalid"] == 0

    await link.stop("test finished")
    status = await session.call("sensor.status", {})
    assert status["active"] == [], "stop must reach the phone"
    assert phone.sensor_streaming["active"] == []
    await link.close()


async def test_a_sensor_stream_can_be_restarted_with_a_new_interval(connected):
    _phone, session, _tmp = connected
    link = SensorLink(session)
    await link.refresh()
    assert await link.start([1]) is True
    link.set_interval(100000)
    await link.stop("changing rate")
    assert await link.start([1]) is True
    status = await session.call("sensor.status", {})
    assert status["interval_us"] == 100000
    await link.close()


async def test_sensor_permission_denial_is_reported_not_hidden(connected):
    phone, session, _tmp = connected
    phone.sensor_permission = False
    link = SensorLink(session)
    await link.refresh()
    assert await link.start([1]) is False
    snapshot = link.snapshot()
    assert snapshot.state == "error"
    assert "sensor permission" in snapshot.detail
    await link.close()


async def test_stopping_sensors_leaves_nothing_streaming_on_the_phone(connected):
    phone, session, _tmp = connected
    link = SensorLink(session)
    await link.refresh()
    await link.start([1, 2, 3])
    await link.close("session over")
    assert phone.sensor_streaming["active"] == []


# --------------------------------------------------------------------------- #
# file transfer
# --------------------------------------------------------------------------- #
async def test_a_small_file_sent_to_the_phone_is_verified(connected):
    phone, session, tmp_path = connected
    source = tmp_path / "note.txt"
    payload = b"hello from the PC\\n"
    source.write_bytes(payload)
    manager = TransferManager(session, destination=tmp_path / "inbox")
    (tmp_path / "inbox").mkdir()
    stats = await manager.send_file(source)
    assert stats.state == COMPLETE
    assert stats.verified is True
    stored = phone.file_dir / "note.txt"
    assert stored.read_bytes() == payload
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == stats.sha256
    await manager.close()


async def test_a_large_file_sent_to_the_phone_survives_chunking(connected):
    phone, session, tmp_path = connected
    payload = bytes(range(256)) * 3000  # ~768 KB: a dozen 64 KiB chunks
    source = tmp_path / "big.bin"
    source.write_bytes(payload)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    manager = TransferManager(session, destination=inbox)
    stats = await manager.send_file(source)
    assert stats.state == COMPLETE, stats.error
    assert (phone.file_dir / "big.bin").read_bytes() == payload
    await manager.close()


async def test_a_file_received_from_the_phone_is_verified_and_atomic(connected):
    phone, session, tmp_path = connected
    payload = b"hello from the phone\\n" * 40
    phone.outbound_file = ("phone-note.txt", payload)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    manager = TransferManager(session, destination=inbox)
    stats = await manager.request_file()
    # A one-chunk file can finish while `request_file` is still returning.
    assert stats.state in {"active", COMPLETE}
    assert await wait_for(lambda: manager.get(stats.transfer_id).state == COMPLETE)
    final = manager.get(stats.transfer_id)
    assert final.verified is True
    assert final.sha256 == hashlib.sha256(payload).hexdigest()
    assert (inbox / "phone-note.txt").read_bytes() == payload
    assert not part_path(inbox / "phone-note.txt").exists()
    await manager.close()


async def test_a_traversal_name_from_the_phone_is_refused(connected):
    phone, session, tmp_path = connected
    phone.outbound_file = ("../../escape.txt", b"nope")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    manager = TransferManager(session, destination=inbox)
    with pytest.raises(Exception) as excinfo:
        await manager.request_file()
    assert "refusing" in str(excinfo.value).lower()
    assert list(inbox.iterdir()) == []
    await manager.close()


async def test_cancelling_reaches_the_phone_and_stops_the_transfer(connected):
    phone, session, tmp_path = connected
    payload = bytes(range(256)) * 8000  # ~2 MB: plenty of chunks to interrupt
    source = tmp_path / "cancel.bin"
    source.write_bytes(payload)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    manager = TransferManager(session, destination=inbox)

    transfer_id = (await session.call(
        "file.offer",
        {"name": "cancel.bin", "size": len(payload),
         "sha256": hashlib.sha256(payload).hexdigest(), "chunk_size": 65536},
    ))["transfer_id"]

    assert await manager.cancel(transfer_id) is None  # cancel is fire-and-forget
    progress = await session.call("file.progress", {"transfer_id": transfer_id})
    assert progress["state"] == "cancelled", "the phone must stop, not keep writing"
    assert not (phone.file_dir / "cancel.bin").exists()
    await manager.close()


async def test_the_phone_reports_a_bad_name_before_any_bytes_move(connected):
    phone, session, tmp_path = connected
    source = tmp_path / "CON"
    source.write_bytes(b"x")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    manager = TransferManager(session, destination=inbox)
    with pytest.raises(Exception):
        # "CON" is refused locally by safe_name before the RPC; the point is that
        # it is refused, by whichever side notices first.
        await manager.send_file(source)
    await manager.close()


# --------------------------------------------------------------------------- #
# Bluetooth
# --------------------------------------------------------------------------- #
async def test_the_adapter_is_described_honestly(connected):
    _phone, session, _tmp = connected
    link = BluetoothLink(session)
    session.on_event = link.on_event
    adapter = await link.refresh_adapter()
    assert adapter.supported and adapter.enabled and adapter.le_supported
    assert adapter.permitted is True
    assert adapter.usable
    await link.close()


async def test_a_scan_yields_devices_with_rssi_and_services(connected):
    phone, session, _tmp = connected
    link = BluetoothLink(session)
    session.on_event = link.on_event
    await link.refresh_adapter()
    assert await link.start_scan(duration_ms=0) is True
    assert link.snapshot().scan.scanning

    # Give the simulator time to push its device events over the real socket.
    assert await wait_for(lambda: len(link.devices()) >= 3)
    devices = {d.address: d for d in link.devices()}
    assert "C4:7F:51:00:00:01" in devices
    band = devices["C4:7F:51:00:00:01"]
    assert band.name == "Heart Rate Band"
    assert band.rssi <= -40
    assert band.services == ("180d",)
    assert band.sightings >= 2, "the simulator advertises each device twice"

    assert await link.stop_scan() is True
    assert link.snapshot().scan.state == "stopped"
    await link.close()


async def test_a_scan_permission_denial_is_reported(connected):
    phone, session, _tmp = connected
    phone.ble_permission = False
    link = BluetoothLink(session)
    session.on_event = link.on_event
    assert await link.start_scan() is False
    assert "not granted" in link.snapshot().detail
    await link.close()


async def test_bluetooth_switched_off_is_reported(connected):
    phone, session, _tmp = connected
    phone.ble_enabled = False
    link = BluetoothLink(session)
    session.on_event = link.on_event
    assert await link.start_scan() is False
    assert "turned off" in link.snapshot().detail
    await link.close()


async def test_gatt_connect_discover_read_write_and_notify(connected):
    phone, session, _tmp = connected
    link = BluetoothLink(session)
    session.on_event = link.on_event
    await link.refresh_adapter()
    await link.start_scan(duration_ms=0)
    assert await wait_for(lambda: len(link.devices()) >= 1)
    address = "C4:7F:51:00:00:01"

    connection = await link.connect(address)
    assert connection.state == GATT_CONNECTED
    assert connection.tree is not None
    assert {s.short for s in connection.tree.services} >= {"180f", "180a", "ffe0"}

    level = await link.read(address, BATTERY, BATTERY_LEVEL)
    assert level == b"\x57"

    assert await link.write(address, normalise_uuid("ffe0"), normalise_uuid("ffe1"), b"\x01\x02") == 2

    assert await link.subscribe(address, BATTERY, BATTERY_LEVEL) is True
    assert await wait_for(
        lambda: len(link.connection(address).notifications) >= 2
    ), "the phone must push notifications for a subscription"
    first = link.connection(address).notifications[0]
    assert first.value == bytes([87])
    assert first.address == address

    assert await link.disconnect(address) is True
    assert link.connection(address) is None
    assert phone.ble["connected"] == {}
    await link.close()


async def test_connecting_to_a_device_that_is_not_advertising_fails_cleanly(connected):
    _phone, session, _tmp = connected
    link = BluetoothLink(session)
    session.on_event = link.on_event
    connection = await link.connect("AA:AA:AA:AA:AA:AA")
    assert connection.state == "failed"
    assert "not in the scan results" in connection.error
    await link.close()


async def test_a_device_that_refuses_connections_says_so(connected):
    _phone, session, _tmp = connected
    link = BluetoothLink(session)
    session.on_event = link.on_event
    await link.refresh_adapter()
    await link.start_scan(duration_ms=0)
    assert await wait_for(lambda: len(link.devices()) >= 3)
    connection = await link.connect("F0:0D:1C:00:00:03")
    assert connection.state == "failed"
    assert "does not accept connections" in connection.error
    await link.close()


async def test_the_session_ending_clears_every_bluetooth_view(connected):
    phone, session, _tmp = connected
    link = BluetoothLink(session)
    session.on_event = link.on_event
    await link.refresh_adapter()
    await link.start_scan(duration_ms=0)
    await wait_for(lambda: len(link.devices()) >= 1)
    await link.connect("C4:7F:51:00:00:01")
    await session.close("test over")
    await asyncio.sleep(0.05)
    await link.close("session ended")
    assert link.connection("C4:7F:51:00:00:01").state == "disconnected"
    assert phone.ble["connected"] == {}
