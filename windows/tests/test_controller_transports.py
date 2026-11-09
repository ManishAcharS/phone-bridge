"""Transport discovery and ranking.

What is proven here: the order the manager will try (USB, then a fresh beacon,
then a validated saved address), that a dead saved address is dropped instead of
costing a full handshake, and that USB discovery never leaves a forward behind
that it did not create.
"""

from __future__ import annotations

import asyncio
import socket
import time

import pytest

from phonebridge.controller.adb import Adb, AdbResult, ForwardLease
from phonebridge.controller.transports import (
    SCORE_BEACON,
    SCORE_BEACON_PINNED,
    SCORE_CACHED,
    SCORE_USB,
    BeaconCandidates,
    CachedCandidates,
    Candidate,
    UsbCandidates,
    rank,
    select_first,
    tcp_probe,
)
from phonebridge.core.device_store import DeviceStore
from phonebridge.transport.discovery import DiscoveredPhone


class RecordingAdb:
    def __init__(self, devices: str = "", forwards: str = "", fail: bool = False) -> None:
        self.devices_output = devices
        self.forwards_output = forwards
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):  # noqa: ANN001
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "devices":
            return AdbResult(0, "List of devices attached\n" + self.devices_output, "")
        if argv[0] == "forward" and argv[1] == "--list":
            return AdbResult(0, self.forwards_output, "")
        if argv[0] == "-s" and argv[2] == "forward" and argv[3] == "--remove":
            return AdbResult(0, "", "")
        if argv[0] == "-s" and argv[2] == "forward":
            if self.fail:
                return AdbResult(1, "", "cannot bind")
            self.forwards_output += f"{argv[1]} {argv[3]} {argv[4]}\n"
            return AdbResult(0, "", "")
        return AdbResult(0, "", "")

    def mutating(self) -> list[str]:
        joined = [" ".join(c) for c in self.calls]
        return [c for c in joined if "forward" in c and "--list" not in c]


class FakeDiscovery:
    """Stands in for the UDP listener: it already 'heard' these phones."""

    def __init__(self, phones: list[DiscoveredPhone]) -> None:
        self.phones = phones
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    def live(self, max_age_s: float = 8.0) -> list[DiscoveredPhone]:
        del max_age_s
        return list(self.phones)


def phone(name: str = "Pixel-test", host: str = "192.168.1.50", port: int = 8765,
          method: str = "wifi", device_id: str = "abc123def4567890") -> DiscoveredPhone:
    return DiscoveredPhone(
        device_id=device_id, name=name, host=host, port=port, method=method, last_seen=time.time()
    )


# --------------------------------------------------------------------------- #
# ranking
# --------------------------------------------------------------------------- #
def test_usb_wins_over_beacon_and_cache() -> None:
    candidates = [
        Candidate("cached", "192.168.1.9", 8765, SCORE_CACHED, "cached"),
        Candidate("wifi", "192.168.1.50", 8765, SCORE_BEACON, "beacon"),
        Candidate("usb", "127.0.0.1", 50123, SCORE_USB, "usb"),
    ]
    assert [c.kind for c in rank(candidates)] == ["usb", "wifi", "cached"]
    assert select_first(candidates).kind == "usb"


def test_a_pinned_phone_beats_an_unknown_one_on_the_same_network() -> None:
    pinned = Candidate("wifi", "10.0.0.2", 8765, SCORE_BEACON_PINNED, "pinned", device_id="a")
    unknown = Candidate("wifi", "10.0.0.3", 8765, SCORE_BEACON, "beacon", device_id="b")
    assert [c.host for c in rank([unknown, pinned])] == ["10.0.0.2", "10.0.0.3"]


def test_ranking_is_stable_for_equal_scores() -> None:
    older = Candidate("wifi", "10.0.0.2", 8765, SCORE_BEACON, "old", discovered_at=1.0)
    newer = Candidate("wifi", "10.0.0.3", 8765, SCORE_BEACON, "new", discovered_at=2.0)
    assert rank([older, newer])[0] is newer


# --------------------------------------------------------------------------- #
# USB
# --------------------------------------------------------------------------- #
async def test_usb_candidate_is_created_and_released() -> None:
    fake = RecordingAdb(devices="R58M12345 device model:Redmi_Note_14_Pro\n")
    provider = UsbCandidates(Adb(runner=fake, executable="adb"))
    found = await provider.candidates(timeout=0.1)
    assert len(found) == 1
    candidate = found[0]
    assert candidate.kind == "usb"
    assert candidate.host == "127.0.0.1"
    assert candidate.score == SCORE_USB
    assert candidate.name == "Redmi Note 14 Pro"
    assert len(provider.leases) == 1
    await provider.release()
    assert provider.leases == []
    assert any("--remove" in call for call in fake.mutating())


async def test_usb_skips_a_device_that_is_not_authorised() -> None:
    fake = RecordingAdb(devices="ABC123 unauthorized\n")
    provider = UsbCandidates(Adb(runner=fake, executable="adb"))
    assert await provider.candidates(timeout=0.1) == []
    assert fake.mutating() == [], "no forwarding is attempted for an unusable device"


async def test_usb_reuses_the_users_own_rule_without_owning_it() -> None:
    fake = RecordingAdb(
        devices="R58M12345 device\n", forwards="R58M12345 tcp:8765 tcp:8765\n"
    )
    provider = UsbCandidates(Adb(runner=fake, executable="adb"))
    found = await provider.candidates(timeout=0.1)
    assert found and found[0].port == 8765
    assert "existing rule" in found[0].detail
    assert provider.leases == []
    await provider.release()
    assert not any("--remove" in call for call in fake.mutating()), (
        "a rule PhoneBridge did not create must never be removed"
    )


async def test_usb_provider_is_quiet_when_adb_is_absent() -> None:
    provider = UsbCandidates(Adb(runner=RecordingAdb(), executable=None))
    provider.adb._executable = None  # noqa: SLF001
    import shutil

    original = shutil.which
    shutil.which = lambda _name: None  # type: ignore[assignment]
    try:
        assert await provider.candidates(timeout=0.1) == []
    finally:
        shutil.which = original  # type: ignore[assignment]


async def test_a_failing_forward_does_not_offer_a_broken_candidate() -> None:
    fake = RecordingAdb(devices="S1 device\n", fail=True)
    provider = UsbCandidates(Adb(runner=fake, executable="adb"))
    assert await provider.candidates(timeout=0.1) == []


# --------------------------------------------------------------------------- #
# beacon
# --------------------------------------------------------------------------- #
async def test_beacon_candidates_keep_the_phones_own_transport_word(
    pc_config,
) -> None:
    store = DeviceStore(pc_config)
    discovery = FakeDiscovery([phone(method="hotspot", host="192.168.43.1")])
    found = await BeaconCandidates(discovery, store).candidates(timeout=0.2)
    assert len(found) == 1
    assert found[0].kind == "hotspot"
    assert found[0].host == "192.168.43.1"
    assert found[0].score == SCORE_BEACON


async def test_beacon_marks_a_pinned_phone(pc_config) -> None:
    store = DeviceStore(pc_config)
    store.pin_peer("abc123def4567890", "Pixel-test", b"\x04" + b"\x11" * 64)
    discovery = FakeDiscovery([phone()])
    found = await BeaconCandidates(discovery, store).candidates(timeout=0.2)
    assert found[0].score == SCORE_BEACON_PINNED
    assert "pinned" in found[0].detail


async def test_beacon_with_nothing_heard_returns_empty(pc_config) -> None:
    store = DeviceStore(pc_config)
    discovery = FakeDiscovery([])
    found = await BeaconCandidates(discovery, store).candidates(timeout=0.15)
    assert found == []


async def test_beacon_does_not_stop_a_listener_it_was_given(pc_config) -> None:
    store = DeviceStore(pc_config)
    discovery = FakeDiscovery([phone()])
    await BeaconCandidates(discovery, store).candidates(timeout=0.1)
    assert discovery.stopped == 0, "an injected listener belongs to the caller"


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #
async def test_dead_cached_address_is_dropped(pc_config) -> None:
    store = DeviceStore(pc_config)
    store.remember_endpoint("wifi", "10.1.1.1", 8765, "abc")

    async def never(_host: str, _port: int, _timeout: float = 0.5) -> bool:
        return False

    assert await CachedCandidates(store, probe=never).candidates(timeout=0.2) == []


async def test_live_cached_address_is_offered_last(pc_config) -> None:
    store = DeviceStore(pc_config)
    store.remember_endpoint("wifi", "10.1.1.1", 8765, "abc")

    async def always(_host: str, _port: int, _timeout: float = 0.5) -> bool:
        return True

    found = await CachedCandidates(store, probe=always).candidates(timeout=0.2)
    assert len(found) == 1
    assert found[0].score == SCORE_CACHED
    assert found[0].device_id == "abc"


async def test_tcp_probe_is_real(pc_config) -> None:
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await tcp_probe("127.0.0.1", port, timeout=0.5) is True
    finally:
        server.close()
        await server.wait_closed()

    with socket.socket() as sock:  # a port that is definitely closed
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    assert await tcp_probe("127.0.0.1", closed_port, timeout=0.3) is False


def test_forward_lease_argv_is_exactly_what_we_own() -> None:
    lease = ForwardLease("S1", 50123)
    assert lease.remove_argv() == ["-s", "S1", "forward", "--remove", "tcp:50123"]


@pytest.mark.parametrize("kind", ["usb", "wifi", "hotspot", "cached"])
def test_candidate_endpoint_format(kind: str) -> None:
    assert Candidate(kind, "1.2.3.4", 8765, 1, "x").endpoint == "1.2.3.4:8765"
