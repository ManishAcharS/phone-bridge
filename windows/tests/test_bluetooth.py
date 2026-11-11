"""BLE scan and GATT, with no radio anywhere near the machine.

What this file can prove: the models, the UUID handling, deduplication, the scan
lifecycle and its refusals (no radio, Bluetooth off, permission denied), the GATT
request/answer path, notification collection, disconnection and the bounded
retention that stops a chatty characteristic from growing memory.

What it cannot prove: that a real phone's `BluetoothLeScanner` behaves this way.
The fake below is a *client's* view of the protocol; docs/21 §9 is the checklist
for the hardware half.
"""

from __future__ import annotations

import asyncio

import pytest

from phonebridge.bluetooth import (
    GATT_CONNECTED,
    GATT_DISCONNECTED,
    GATT_FAILED,
    MAX_NOTIFICATIONS,
    MAX_VALUE_BYTES,
    SCAN_SCANNING,
    SCAN_STOPPED,
    AdapterStatus,
    BleDevice,
    BleError,
    BluetoothError,
    BluetoothLink,
    GattTree,
    decode_value,
    encode_value,
    hex_of,
    is_mac,
    normalise_uuid,
    parse_devices,
    printable_of,
    short_uuid,
)
from phonebridge.protocol.messages import RpcError

BATTERY = normalise_uuid("180f")
BATTERY_LEVEL = normalise_uuid("2a19")
ADDRESS = "C4:7F:51:00:00:01"

DEVICE_ROWS = [
    {"address": ADDRESS, "name": "Heart Rate Band", "rssi": -54,
     "service_uuids": ["0000180d-0000-1000-8000-00805f9b34fb"], "connectable": True},
    {"address": "E1:2A:9B:00:00:02", "name": "Thermo Beacon", "rssi": -71,
     "service_uuids": [], "connectable": True},
]

SERVICES = [
    {
        "uuid": BATTERY,
        "characteristics": [
            {"uuid": BATTERY_LEVEL, "properties": ["read", "notify"],
             "descriptors": ["00002902-0000-1000-8000-00805f9b34fb"]},
        ],
    },
    {
        "uuid": normalise_uuid("ffe0"),
        "characteristics": [{"uuid": normalise_uuid("ffe1"),
                             "properties": ["write", "write_no_response"]}],
    },
]


def run(coro):
    return asyncio.run(coro)


async def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


class FakePhone:
    def __init__(self) -> None:
        self.is_open = True
        self.calls: list[tuple[str, dict | None]] = []
        self.scanning = False
        self.connected: dict[str, str] = {}
        self.refuse: dict[str, Exception] = {}
        self.adapter = {
            "supported": True,
            "enabled": True,
            "le_supported": True,
            "name": "fake-radio",
            "address": "00:11:22:33:44:55",
            "permitted": True,
            "detail": "",
        }

    async def call(self, method, params=None, timeout: float = 10.0):
        self.calls.append((method, params))
        refusal = self.refuse.get(method)
        if refusal is not None:
            raise refusal
        params = params or {}
        if method == "bluetooth.adapter.status":
            return dict(self.adapter)
        if method == "bluetooth.scan.start":
            self.scanning = True
            return {"ok": True, "scanning": True, "started_at_ms": 1234,
                    "duration_ms": params.get("duration_ms", 0)}
        if method == "bluetooth.scan.stop":
            self.scanning = False
            return {"ok": True, "scanning": False, "devices": DEVICE_ROWS}
        if method == "bluetooth.scan.results":
            return {"scanning": self.scanning, "devices": DEVICE_ROWS,
                    "started_at_ms": 1234, "duration_ms": 10000}
        if method == "bluetooth.gatt.connect":
            address = str(params.get("address") or "")
            self.connected[address] = "connected"
            return {"ok": True, "address": address, "state": "connected"}
        if method == "bluetooth.gatt.services":
            return {"address": params.get("address"), "mtu": 185, "services": SERVICES}
        if method == "bluetooth.gatt.read":
            return {"ok": True, "value_b64": encode_value(bytes([87])), "bytes": 1}
        if method == "bluetooth.gatt.write":
            return {"ok": True, "bytes": len(decode_value(str(params.get("value_b64"))))}
        if method == "bluetooth.gatt.subscribe":
            return {"ok": True, "subscribed": bool(params.get("enable", True))}
        if method == "bluetooth.gatt.disconnect":
            self.connected.pop(str(params.get("address")), None)
            return {"ok": True, "state": "disconnected"}
        if method == "bluetooth.gatt.status":
            return {
                "scanning": self.scanning,
                "connections": [
                    {"address": a, "state": s} for a, s in self.connected.items()
                ],
            }
        return {"ok": True}

    def methods(self):
        return [name for name, _ in self.calls]


# --------------------------------------------------------------------------- #
# models and values
# --------------------------------------------------------------------------- #
def test_a_short_uuid_expands_to_the_full_form_and_back() -> None:
    full = "0000180d-0000-1000-8000-00805f9b34fb"
    assert normalise_uuid("180d") == full
    assert normalise_uuid("180D") == full
    assert short_uuid(full) == "180d"
    assert normalise_uuid("0000180d0000100080000080 5f9b34fb".replace(" ", "")) == full


@pytest.mark.parametrize("bad", ["", "zzzz", "180", "180d-", "0000180d-0000"])
def test_a_non_uuid_is_refused(bad: str) -> None:
    with pytest.raises(BleError):
        normalise_uuid(bad)


def test_values_are_capped_and_round_trip() -> None:
    assert decode_value(encode_value(b"\x57")) == b"\x57"
    with pytest.raises(BleError):
        encode_value(b"x" * (MAX_VALUE_BYTES + 1))
    with pytest.raises(BleError):
        decode_value("not base64!!")
    payload = b"\x01\x02\x41"
    assert hex_of(payload) == "01 02 41"
    assert printable_of(payload) == "··A"


def test_an_address_must_look_like_a_mac() -> None:
    assert is_mac(ADDRESS)
    assert not is_mac("C4-7F-51-00-00-01")
    assert not is_mac("")


def test_a_repeat_sighting_merges_and_keeps_the_strongest_rssi() -> None:
    first = BleDevice.from_dict(DEVICE_ROWS[0])
    later = BleDevice.from_dict({**DEVICE_ROWS[0], "rssi": -60, "name": ""})
    merged = first.updated_with(later)
    assert merged.sightings == 2
    assert merged.name == "Heart Rate Band", "a name once seen is not lost"
    assert merged.rssi == -60, "the newest signal wins"
    assert merged.services == ("180d",)


def test_the_device_list_is_deduplicated_and_sorted_by_signal() -> None:
    devices = parse_devices({"devices": DEVICE_ROWS + [DEVICE_ROWS[1]]})
    assert len(devices) == 2
    ordered = sorted(devices, key=lambda d: -d.rssi)
    assert ordered[0].address == ADDRESS


def test_a_gatt_tree_finds_a_characteristic_and_lists_its_properties() -> None:
    tree = GattTree.from_dict({"address": ADDRESS, "mtu": 185, "services": SERVICES})
    entry = tree.characteristic(BATTERY, BATTERY_LEVEL)
    assert entry is not None
    assert entry.can_read and entry.can_notify and not entry.can_write
    assert entry.to_dict()["short"] == "2a19"
    writable = tree.characteristic(normalise_uuid("ffe0"), normalise_uuid("ffe1"))
    assert writable is not None and writable.can_write


def test_a_partial_gatt_tree_is_tolerated() -> None:
    tree = GattTree.from_dict(
        {"services": [{"uuid": BATTERY, "characteristics": ["junk", {"uuid": "nope"}]}, 7]}
    )
    assert len(tree.services) == 1
    assert tree.services[0].characteristics == ()


# --------------------------------------------------------------------------- #
# adapter and scan
# --------------------------------------------------------------------------- #
def test_an_adapter_is_only_usable_when_every_fact_says_so() -> None:
    good = AdapterStatus(supported=True, enabled=True, le_supported=True, permitted=True)
    assert good.usable
    assert not AdapterStatus(supported=True, enabled=False, le_supported=True, permitted=True).usable
    assert not AdapterStatus(supported=True, enabled=True, le_supported=False).usable
    assert not AdapterStatus(supported=True, enabled=True, le_supported=True,
                             permitted=False).usable


def test_refresh_reads_the_adapter_and_says_nothing_was_started() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        adapter = await link.refresh_adapter()
        assert adapter.usable
        assert phone.methods() == ["bluetooth.adapter.status"]
        assert link.snapshot().scan.state != SCAN_SCANNING

    run(scenario())


def test_start_and_stop_a_scan() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        await link.refresh_adapter()
        assert await link.start_scan(duration_ms=5000) is True
        assert link.snapshot().scan.scanning
        assert await link.stop_scan() is True
        assert link.snapshot().scan.state == SCAN_STOPPED
        assert len(link.snapshot().scan.devices) == 2

    run(scenario())


def test_a_scan_with_no_radio_is_refused_with_the_reason() -> None:
    async def scenario():
        phone = FakePhone()
        phone.refuse["bluetooth.scan.start"] = RpcError("no_bluetooth", "this device has no Bluetooth radio")
        link = BluetoothLink(phone)
        assert await link.start_scan() is False
        assert "no Bluetooth radio" in link.snapshot().detail
        assert link.snapshot().scan.state == "error"

    run(scenario())


def test_bluetooth_turned_off_is_refused_with_the_reason() -> None:
    async def scenario():
        phone = FakePhone()
        phone.refuse["bluetooth.scan.start"] = RpcError(
            "bluetooth_off", "Bluetooth is turned off on the phone"
        )
        link = BluetoothLink(phone)
        assert await link.start_scan() is False
        assert "turned off" in link.snapshot().detail

    run(scenario())


def test_a_denied_scan_permission_is_refused_with_the_reason() -> None:
    async def scenario():
        phone = FakePhone()
        phone.refuse["bluetooth.scan.start"] = RpcError(
            "bluetooth_denied", "the Bluetooth scan permission is not granted"
        )
        link = BluetoothLink(phone)
        assert await link.start_scan() is False
        assert "permission is not granted" in link.snapshot().detail

    run(scenario())


def test_a_scan_without_a_session_says_not_connected() -> None:
    async def scenario():
        phone = FakePhone()
        phone.is_open = False
        link = BluetoothLink(phone)
        assert await link.start_scan() is False
        assert "not connected" in link.snapshot().detail
        assert phone.calls == []

    run(scenario())


def test_device_events_arrive_and_are_deduplicated() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        await link.refresh_adapter()
        await link.start_scan()
        link.on_event("bluetooth.device", {**DEVICE_ROWS[0], "rssi": -50})
        link.on_event("bluetooth.device", {**DEVICE_ROWS[0], "rssi": -44})
        link.on_event("bluetooth.device", DEVICE_ROWS[1])
        devices = link.devices()
        by_address = {d.address: d for d in devices}
        assert len(by_address) == 2
        assert by_address[ADDRESS].sightings == 2
        assert by_address[ADDRESS].rssi == -44
        assert devices[0].address == ADDRESS, "strongest signal first"

    run(scenario())


def test_a_malformed_device_event_is_ignored() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        link.on_event("bluetooth.device", {"address": "", "rssi": "loud"})
        link.on_event("bluetooth.device", {})
        assert link.devices() == ()

    run(scenario())


def test_the_scan_state_event_ends_the_scan() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        await link.refresh_adapter()
        await link.start_scan()
        link.on_event("bluetooth.scan", {"state": "stopped", "detail": "duration reached"})
        assert link.snapshot().scan.state == SCAN_STOPPED
        assert "duration reached" in link.snapshot().scan.detail

    run(scenario())


def test_refresh_results_reconciles_a_missed_event() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        await link.refresh_adapter()
        await link.start_scan()
        phone.scanning = False
        state = await link.refresh_scan()
        assert state.state == SCAN_STOPPED
        assert len(state.devices) == 2

    run(scenario())


# --------------------------------------------------------------------------- #
# GATT
# --------------------------------------------------------------------------- #
def test_connect_discovers_and_reports_the_tree() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        await link.refresh_adapter()
        await link.start_scan()
        connection = await link.connect(ADDRESS)
        assert connection.state == GATT_CONNECTED
        assert connection.tree is not None
        assert len(connection.tree.services) == 2
        assert phone.methods()[-2:] == ["bluetooth.gatt.connect", "bluetooth.gatt.services"]
        await link.close()

    run(scenario())


def test_connect_refuses_a_non_mac_and_calls_nothing() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        with pytest.raises(BluetoothError):
            await link.connect("my headphones")
        assert phone.calls == []

    run(scenario())


def test_a_refused_connection_is_reported_not_raised_at_the_ui() -> None:
    async def scenario():
        phone = FakePhone()
        phone.refuse["bluetooth.gatt.connect"] = RpcError(
            "not_connectable", "the device does not accept connections"
        )
        link = BluetoothLink(phone)
        connection = await link.connect(ADDRESS)
        assert connection.state == GATT_FAILED
        assert "does not accept connections" in connection.error

    run(scenario())


def test_reading_a_characteristic_returns_its_bytes() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        await link.refresh_adapter()
        await link.connect(ADDRESS)
        value = await link.read(ADDRESS, BATTERY, BATTERY_LEVEL)
        assert value == b"\x57"
        entry = link.connection(ADDRESS).tree.characteristic(BATTERY, BATTERY_LEVEL)
        assert entry.value == b"\x57", "the last read is kept for the browser"

    run(scenario())


def test_writing_refuses_an_oversized_value_before_the_rpc() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        await link.refresh_adapter()
        await link.connect(ADDRESS)
        before = len(phone.calls)
        with pytest.raises(BleError):
            await link.write(ADDRESS, BATTERY, BATTERY_LEVEL, b"x" * (MAX_VALUE_BYTES + 1))
        assert len(phone.calls) == before
        assert await link.write(ADDRESS, BATTERY, BATTERY_LEVEL, b"\x01\x02") == 2

    run(scenario())


def test_subscribe_and_unsubscribe_are_tracked_locally() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        await link.refresh_adapter()
        await link.connect(ADDRESS)
        assert await link.subscribe(ADDRESS, BATTERY, BATTERY_LEVEL) is True
        assert link.connection(ADDRESS).subscriptions == (
            f"{BATTERY}/{BATTERY_LEVEL}",
        )
        # The return value answers "is it subscribed now?", so unsubscribing is False.
        assert await link.subscribe(ADDRESS, BATTERY, BATTERY_LEVEL, enable=False) is False
        assert link.connection(ADDRESS).subscriptions == ()

    run(scenario())


def test_notifications_are_collected_and_bounded() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        await link.refresh_adapter()
        await link.connect(ADDRESS)
        for level in range(MAX_NOTIFICATIONS + 25):
            link.on_event(
                "bluetooth.notification",
                {
                    "address": ADDRESS,
                    "service": BATTERY,
                    "characteristic": BATTERY_LEVEL,
                    "value_b64": encode_value(bytes([level % 256])),
                    "ts_ms": level,
                },
            )
        connection = link.connection(ADDRESS)
        assert len(connection.notifications) == MAX_NOTIFICATIONS
        assert connection.notifications[-1].value == bytes([(MAX_NOTIFICATIONS + 24) % 256])
        rendered = connection.to_dict()["notifications"][-1]
        assert rendered["hex"] and rendered["bytes"] == 1

    run(scenario())


def test_a_notification_for_an_unknown_device_is_ignored() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        link.on_event(
            "bluetooth.notification",
            {"address": "00:00:00:00:00:00", "service": BATTERY,
             "characteristic": BATTERY_LEVEL, "value_b64": encode_value(b"\x01")},
        )
        assert link.snapshot().connections == ()

    run(scenario())


def test_disconnect_clears_the_connection() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        await link.refresh_adapter()
        await link.connect(ADDRESS)
        assert await link.disconnect(ADDRESS) is True
        assert link.connection(ADDRESS) is None
        assert "bluetooth.gatt.disconnect" in phone.methods()

    run(scenario())


def test_a_connection_state_event_closes_the_local_view() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        await link.refresh_adapter()
        await link.connect(ADDRESS)
        link.on_event("bluetooth.gatt", {"address": ADDRESS, "state": "disconnected"})
        assert link.connection(ADDRESS).state == GATT_DISCONNECTED

    run(scenario())


def test_status_reconciliation_notices_a_device_that_vanished() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        await link.refresh_adapter()
        await link.connect(ADDRESS)
        phone.connected.clear()
        await link.refresh_status()
        assert link.connection(ADDRESS).state == GATT_DISCONNECTED

    run(scenario())


def test_a_timeout_is_reported_as_a_failed_connection() -> None:
    async def scenario():
        phone = FakePhone()

        async def slow(method, params=None, timeout: float = 10.0):
            if method == "bluetooth.gatt.connect":
                import asyncio as _asyncio

                await _asyncio.sleep(0.02)
                raise TimeoutError("the phone did not answer")
            return await FakePhone.call(phone, method, params, timeout)

        phone.call = slow
        link = BluetoothLink(phone)
        link._session = phone
        connection = await link.connect(ADDRESS)
        assert connection.state == GATT_FAILED
        assert "did not answer" in connection.error

    run(scenario())


def test_close_drops_every_connection_and_says_why() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        await link.refresh_adapter()
        await link.connect(ADDRESS)
        await link.close("session ended")
        assert link.connection(ADDRESS).state == GATT_DISCONNECTED
        assert "session ended" in link.snapshot().detail

    run(scenario())


def test_the_snapshot_is_json_safe() -> None:
    async def scenario():
        phone = FakePhone()
        link = BluetoothLink(phone)
        await link.refresh_adapter()
        await link.start_scan()
        link.on_event("bluetooth.device", DEVICE_ROWS[0])
        snapshot = link.snapshot().to_dict()
        import json

        json.dumps(snapshot)  # raises TypeError if anything is not serialisable
        assert snapshot["adapter"]["usable"] is True
        assert snapshot["scan"]["devices"][0]["address"] == ADDRESS

    run(scenario())
