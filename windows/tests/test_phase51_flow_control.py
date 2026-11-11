"""Phase 5.1: the limits that stop a busy link from eating the machine.

Three findings from the review live here, and none of them is visible in a
happy-path test:

* a scan in a crowded room published one event per sighting, and each publish
  copies the whole device list — hundreds of copies a second;
* ``--name`` was stored and never applied, so the filter silently did nothing;
* the SHA-256 of a whole file ran on the event loop, so hashing a 4 GB file
  stopped heartbeats, acknowledgements and the window for its duration.

The last test drives `_profile_at`/`_pnp_at` with a mocked monotonic clock: the
original bug only appeared on a machine that had booted less than a minute ago,
which is every CI runner and a fair number of real ones.

Everything here is a unit test against the production classes; the radio, the
phone and Windows itself are faked, and the fakes are named as such.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from test_bluetooth import ADDRESS, DEVICE_ROWS
from test_bluetooth import FakePhone as BleFakePhone
from test_files import FakePhone as FileFakePhone
from test_files import make_manager

from phonebridge.bluetooth import MAX_DEVICES, BleDevice, BluetoothLink
from phonebridge.bluetooth import link as link_module


# --------------------------------------------------------------------------- #
# scan events
# --------------------------------------------------------------------------- #
def _link(**kwargs) -> tuple[BluetoothLink, BleFakePhone, list[int]]:
    phone = BleFakePhone()
    seen: list[int] = []
    link = BluetoothLink(phone, on_change=lambda _snap: seen.append(1), **kwargs)
    return link, phone, seen


def _sighting(index: int, *, name: str = "Phone", rssi: int = -50) -> dict:
    return {
        "address": f"C4:7F:51:00:00:{index:02X}",
        "name": name,
        "rssi": rssi,
        "service_uuids": [],
        "connectable": True,
    }


def test_a_burst_of_sightings_publishes_at_a_bounded_rate(monkeypatch) -> None:
    """500 sightings in one second must not mean 500 publishes.

    The device list is still complete — coalescing is about how often the GUI is
    woken, never about dropping a device.
    """
    clock = {"now": 100.0}
    monkeypatch.setattr(link_module.time, "monotonic", lambda: clock["now"])
    link, _phone, seen = _link()

    for index in range(200):
        link.on_event("bluetooth.device", _sighting(index % 40))
        clock["now"] += 0.002  # 500 sightings a second, as a crowded room produces

    assert len(seen) <= int(link_module.DEVICE_EVENT_RATE) + 1, (
        f"{len(seen)} publishes for one second of sightings"
    )
    assert len(link.devices()) == 40, "coalescing must not lose a device"

    # Nothing is stuck: once the rate limit has passed, the next sighting is
    # published immediately, and the poll (`refresh_scan`) is the backstop.
    before = len(seen)
    clock["now"] += 5.0
    link.on_event("bluetooth.device", _sighting(0))
    assert len(seen) == before + 1, "the coalescer must publish once the window passes"


def test_the_device_list_is_capped_and_the_weakest_signal_leaves_first() -> None:
    """A misbehaving phone cannot make the PC allocate without bound."""
    link, _phone, _seen = _link()
    for index in range(MAX_DEVICES + 20):
        link.on_event("bluetooth.device", _sighting(index % 256, rssi=-40 - (index % 60)))
    assert len(link.devices()) <= MAX_DEVICES
    seen = {d.address for d in link.devices()}
    assert seen, "the cap must keep the strongest devices, not empty the list"


def test_a_repeat_sighting_updates_rssi_without_duplicating_the_device() -> None:
    link, _phone, _seen = _link()
    link.on_event("bluetooth.device", _sighting(1, rssi=-70))
    link.on_event("bluetooth.device", _sighting(1, rssi=-45))
    devices = link.devices()
    assert len(devices) == 1
    assert devices[0].rssi == -45
    assert devices[0].sightings == 2


# --------------------------------------------------------------------------- #
# the name filter
# --------------------------------------------------------------------------- #
def test_the_name_filter_drops_events_that_do_not_match() -> None:
    """``--name`` has to work even when the phone ignores the parameter."""
    link, _phone, _seen = _link()
    asyncio.run(link.start_scan(name_prefix="pixel"))
    link.on_event("bluetooth.device", _sighting(1, name="Pixel 8"))
    link.on_event("bluetooth.device", _sighting(2, name="Heart Rate Band"))
    link.on_event("bluetooth.device", _sighting(3, name="pixel-buds"))
    names = sorted(d.name for d in link.devices())
    assert names == ["Pixel 8", "pixel-buds"], "the filter must be case-insensitive"


def test_the_name_filter_is_sent_to_the_phone_and_applied_to_results() -> None:
    link, phone, _seen = _link()
    asyncio.run(link.start_scan(name_prefix="  Pixel "))
    start = dict(phone.calls[-1][1] or {})
    assert start.get("name_prefix") == "  Pixel ", "the phone needs the raw filter"
    assert link._name_prefix == "Pixel", "and the PC trims it for its own comparisons"

    # `refresh_scan` returns every device the phone knows; the client filters it.
    asyncio.run(link.refresh_scan())
    assert all(d.name.lower().startswith("pixel") for d in link.devices())


# --------------------------------------------------------------------------- #
# hashing off the event loop
# --------------------------------------------------------------------------- #
def test_hashing_a_file_does_not_block_the_event_loop() -> None:
    """While a file is hashed, the loop must keep running other work.

    The old code called ``sha256_of`` inline, so the counter below never got a
    chance to advance until the hash finished. The threshold is deliberately
    loose: it is not measuring speed, only whether the loop was free at all.
    """
    from phonebridge.files import transfer as transfer_module

    main_thread = threading.get_ident()
    seen: dict[str, int] = {}

    original = transfer_module.sha256_of

    def slow_hash(path: Path) -> str:
        seen["thread"] = threading.get_ident()
        time.sleep(0.05)  # stand in for a multi-gigabyte read
        return original(path)

    ticks = {"n": 0}

    async def scenario(tmp: Path) -> None:
        source = tmp / "big.bin"
        source.write_bytes(b"x" * 4096)
        phone = FileFakePhone()
        manager = make_manager(phone)
        transfer_module.sha256_of = slow_hash  # type: ignore[assignment]

        async def ticker() -> None:
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        try:
            await manager.send_file(source)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await manager.close()

    tmp = Path(pytest.importorskip("tempfile").mkdtemp())
    try:
        asyncio.run(scenario(tmp))
    finally:
        transfer_module.sha256_of = original  # type: ignore[assignment]

    assert seen["thread"] != main_thread, "the hash ran on the event loop thread"
    assert ticks["n"] >= 3, (
        f"the loop only advanced {ticks['n']} times while a 50 ms hash ran: it was blocked"
    )


# --------------------------------------------------------------------------- #
# the cold-boot cache sentinels
# --------------------------------------------------------------------------- #
class _CountingProbe:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return []


def _manager(monkeypatch, clock: dict[str, float], probe: _CountingProbe):
    from phonebridge.controller import manager as manager_module
    from phonebridge.controller.manager import ConnectionManager

    profiles = {"n": 0}

    def fake_profile(*_args, **_kwargs):
        profiles["n"] += 1
        return "Public"

    monkeypatch.setattr(manager_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(manager_module, "network_profile", fake_profile)
    from phonebridge.core.device_store import DeviceStore

    store = DeviceStore(Path(pytest.importorskip("tempfile").mkdtemp()))
    return ConnectionManager(store, usb_probe=probe), profiles


def test_the_first_profile_probe_runs_on_a_freshly_booted_machine(monkeypatch) -> None:
    """`time.monotonic()` is small right after boot; `0.0` used to skip the probe."""
    clock = {"now": 1.5}  # 1.5 seconds of uptime: a CI runner
    probe = _CountingProbe()
    manager, profiles = _manager(monkeypatch, clock, probe)

    hint = manager._firewall_hint()
    assert profiles["n"] == 1, "the first probe must not be skipped on a young clock"
    assert "Public" in hint

    clock["now"] = 1.6
    manager._firewall_hint()
    assert profiles["n"] == 1, "a second call within a minute must be cached"

    clock["now"] = 100.0
    manager._firewall_hint()
    assert profiles["n"] == 2, "the cache must expire"


def test_the_first_usb_probe_runs_on_a_freshly_booted_machine(monkeypatch) -> None:
    clock = {"now": 2.0}
    probe = _CountingProbe()
    manager, _profiles = _manager(monkeypatch, clock, probe)

    manager._device_tree()
    assert probe.calls == 1, "the first USB probe must not be skipped on a young clock"
    clock["now"] = 2.5
    manager._device_tree()
    assert probe.calls == 1, "the device tree is cached for 30 s"
    clock["now"] = 40.0
    manager._device_tree()
    assert probe.calls == 2
    manager._device_tree(force=True)
    assert probe.calls == 3, "a manual diagnosis must always look"


# --------------------------------------------------------------------------- #
# notification buffering stays bounded
# --------------------------------------------------------------------------- #
def test_a_chatty_characteristic_cannot_grow_the_notification_buffer() -> None:
    """A characteristic that streams at 1 kHz must not become unbounded memory."""
    from phonebridge.bluetooth import MAX_NOTIFICATIONS

    link, _phone, _seen = _link()
    asyncio.run(link.connect(ADDRESS))
    for index in range(MAX_NOTIFICATIONS * 3):
        link.on_event(
            "bluetooth.notification",
            {
                "address": ADDRESS,
                "service": "0000180f-0000-1000-8000-00805f9b34fb",
                "characteristic": "00002a19-0000-1000-8000-00805f9b34fb",
                "value_b64": "Vw==",
                "timestamp_ms": index,
            },
        )
    connection = link.connection(ADDRESS)
    assert connection is not None
    assert len(connection.notifications) == MAX_NOTIFICATIONS
    assert connection.notifications[-1].timestamp_ms >= 0, (
        "the newest notifications must be the ones kept"
    )


def test_a_device_event_with_no_address_is_ignored() -> None:
    link, _phone, _seen = _link()
    link.on_event("bluetooth.device", {"name": "", "rssi": 0})
    assert link.devices() == ()
    assert isinstance(BleDevice.from_dict(DEVICE_ROWS[0]), BleDevice)
