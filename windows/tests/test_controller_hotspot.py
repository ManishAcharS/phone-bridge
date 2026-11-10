"""Hotspot connectivity: why Wi-Fi worked and the phone's hotspot did not.

The cause was in the phone, not the PC. ``DiscoveryBeacon`` sent every datagram to
``255.255.255.255``, which Android routes out of the *default* interface — mobile
data, or another Wi-Fi. When the phone is the hotspot, the hotspot is not the
default interface, so the beacon never left it and the PC heard nothing. The PC
side could not tell that apart from "the phone app is not running".

Three fixes, each tested here:

1. the beacon now also sends to every interface's directed broadcast, and lists
   its own addresses in the payload so the PC can try them;
2. the PC probes the default gateway, because on a hotspot the gateway *is* the
   phone — one connect, no scanning;
3. the window separates "heard nothing", "heard it but nothing listens" and
   "Windows is dropping the datagram on a Public network".

None of this can prove a real hotspot works; it can only prove the logic is right.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path

import pytest

from phonebridge.controller import manager as manager_module
from phonebridge.controller.manager import ConnectionManager, Snapshot, State
from phonebridge.controller.netprobe import Gateway
from phonebridge.controller.transports import (
    SCORE_GATEWAY,
    TRANSPORT_GATEWAY,
    BeaconCandidates,
    Candidate,
    FixedCandidates,
    GatewayCandidates,
    rank,
)
from phonebridge.controller.view import EndpointError, build_view, parse_endpoint
from phonebridge.core.device_store import DeviceStore
from phonebridge.transport.discovery import DiscoveredPhone, parse_advertised_hosts

REPO = Path(__file__).resolve().parents[2]
BEACON_KT = REPO / "android/app/src/main/java/dev/phonebridge/net/DiscoveryBeacon.kt"


# --------------------------------------------------------------------------- #
# 1. the phone side: directed broadcast, not just 255.255.255.255
# --------------------------------------------------------------------------- #
def beacon_source() -> str:
    return BEACON_KT.read_text(encoding="utf-8")


def test_the_beacon_no_longer_relies_on_the_limited_broadcast_alone() -> None:
    source = beacon_source()
    assert "255.255.255.255" in source, "the limited broadcast is still worth sending"
    assert "broadcastAddresses" in source, "directed per-interface broadcast is the fix"


def test_the_beacon_enumerates_interfaces() -> None:
    source = beacon_source()
    assert "NetworkInterface.getNetworkInterfaces()" in source
    assert "interfaceAddresses" in source, "directed broadcast needs each interface's netmask"


def test_the_beacon_payload_lists_the_phones_own_addresses() -> None:
    source = beacon_source()
    assert '"hosts"' in source
    assert "localAddresses" in source


def test_the_beacon_keeps_the_protocol_keys_unchanged() -> None:
    source = beacon_source()
    for key in ('"app"', '"v"', '"id"', '"name"', '"port"', '"method"'):
        assert key in source


# --------------------------------------------------------------------------- #
# 2. the PC side: what an advertised address is allowed to be
# --------------------------------------------------------------------------- #
def test_advertised_hosts_are_validated_and_deduplicated() -> None:
    assert parse_advertised_hosts(
        ["192.168.43.1", "192.168.1.7", "192.168.1.7", "bad", "1.2.3", "10.0.0.1"]
    ) == ("192.168.43.1", "192.168.1.7", "10.0.0.1")


def test_advertised_hosts_reject_loopback_and_broadcast() -> None:
    assert parse_advertised_hosts(["127.0.0.1", "0.0.0.0", "255.255.255.255"]) == ()


def test_advertised_hosts_are_capped() -> None:
    many = [f"10.0.0.{index}" for index in range(1, 40)]
    assert len(parse_advertised_hosts(many)) == 8


def test_a_missing_hosts_key_is_not_an_error() -> None:
    assert parse_advertised_hosts(None) == ()


def test_a_phone_object_still_round_trips_its_hosts() -> None:
    phone = DiscoveredPhone(
        device_id="abc",
        name="Redmi",
        host="192.168.43.1",
        port=8765,
        method="hotspot",
        hosts=("10.0.0.5",),
    )
    assert phone.to_dict()["hosts"] == ["10.0.0.5"]


# --------------------------------------------------------------------------- #
# 3. the beacon provider offers advertised addresses below the datagram source
# --------------------------------------------------------------------------- #
class StubDiscovery:
    """A DiscoveryService that has already heard one phone."""

    def __init__(self, phones: list[DiscoveredPhone]) -> None:
        self.phones = phones

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def live(self, max_age_s: float = 8.0) -> list[DiscoveredPhone]:
        del max_age_s
        return list(self.phones)


async def test_advertised_addresses_are_tried_after_the_datagram_source() -> None:
    phone = DiscoveredPhone(
        device_id="abc",
        name="Redmi Note 14 Pro+",
        host="192.168.43.1",
        port=8765,
        hosts=("192.168.1.7",),
        method="hotspot",
    )
    provider = BeaconCandidates(discovery=StubDiscovery([phone]))  # type: ignore[arg-type]
    found = await provider.candidates(timeout=0.2)
    ordered = rank(found)
    assert ordered[0].host == "192.168.43.1"
    assert ordered[1].host == "192.168.1.7"
    assert ordered[0].score > ordered[1].score


async def test_the_phones_own_transport_word_is_kept() -> None:
    phone = DiscoveredPhone(
        device_id="abc", name="Redmi", host="192.168.43.1", port=8765, method="hotspot"
    )
    provider = BeaconCandidates(discovery=StubDiscovery([phone]))  # type: ignore[arg-type]
    found = await provider.candidates(timeout=0.2)
    assert found and found[0].kind == "hotspot"


# --------------------------------------------------------------------------- #
# 4. gateway candidates: one address per interface, and only if it answers
# --------------------------------------------------------------------------- #
async def test_the_gateway_that_answers_becomes_a_hotspot_candidate() -> None:
    async def probe(host: str, port: int, timeout: float) -> bool:
        del timeout
        return host == "192.168.137.1" and port == 8765

    provider = GatewayCandidates(
        gateways=lambda: [Gateway("192.168.137.1", "192.168.137.99")],
        probe=probe,
    )
    found = await provider.candidates(timeout=0.5)
    assert len(found) == 1
    assert found[0].kind == TRANSPORT_GATEWAY
    assert found[0].host == "192.168.137.1"
    assert found[0].score == SCORE_GATEWAY
    assert "hotspot" in found[0].detail


async def test_a_gateway_that_does_not_answer_is_not_offered() -> None:
    async def probe(host: str, port: int, timeout: float) -> bool:
        del host, port, timeout
        return False

    provider = GatewayCandidates(gateways=lambda: [Gateway("192.168.1.1")], probe=probe)
    found = await provider.candidates(timeout=0.5)
    assert found == []
    assert provider.last_error is not None
    assert "192.168.1.1" in provider.last_error


async def test_no_gateway_is_a_quiet_zero_not_an_error() -> None:
    provider = GatewayCandidates(gateways=lambda: [], probe=lambda *a: asyncio.sleep(0))
    assert await provider.candidates(timeout=0.5) == []
    assert provider.last_error is None


async def test_a_broken_routing_table_does_not_break_discovery() -> None:
    def gateways() -> list[Gateway]:
        raise OSError("route: command not found")

    provider = GatewayCandidates(gateways=gateways)
    assert await provider.candidates(timeout=0.5) == []
    assert provider.last_error is not None


async def test_two_interfaces_mean_two_probes_and_no_sweep() -> None:
    probed: list[str] = []

    async def probe(host: str, port: int, timeout: float) -> bool:
        del port, timeout
        probed.append(host)
        return False

    provider = GatewayCandidates(
        gateways=lambda: [Gateway("10.0.0.1"), Gateway("192.168.1.1")], probe=probe
    )
    await provider.candidates(timeout=0.5)
    assert probed == ["10.0.0.1", "192.168.1.1"]


# --------------------------------------------------------------------------- #
# 5. a typed address outranks every automatic source
# --------------------------------------------------------------------------- #
async def test_a_manual_endpoint_beats_the_beacon_and_the_gateway() -> None:
    async def probe(host: str, port: int, timeout: float) -> bool:
        del timeout
        return True

    manual = FixedCandidates("10.63.164.24", 8765, probe=probe)
    gateway = GatewayCandidates(gateways=lambda: [Gateway("10.63.164.1")], probe=probe)
    manual_candidates = await manual.candidates(timeout=0.5)
    gateway_candidates = await gateway.candidates(timeout=0.5)
    beacon = Candidate("hotspot", "192.168.43.1", 8765, 80, "beacon")
    ordered = rank([*gateway_candidates, beacon, *manual_candidates])
    assert ordered[0].host == "10.63.164.24"
    assert ordered[0].kind == "manual"


async def test_a_manual_endpoint_is_tried_even_when_the_probe_fails() -> None:
    async def probe(host: str, port: int, timeout: float) -> bool:
        del host, port, timeout
        return False

    manual = FixedCandidates("10.63.164.24", 8765, probe=probe)
    found = await manual.candidates(timeout=0.5)
    assert len(found) == 1
    assert manual.reachable is False
    assert "does not answer" in found[0].detail


def test_a_manual_address_is_trimmed() -> None:
    assert FixedCandidates("  10.63.164.24  ").host == "10.63.164.24"


# --------------------------------------------------------------------------- #
# 6. the manager reports the three failures separately
# --------------------------------------------------------------------------- #
FAST_POLICY = None


class _SilentSession:
    """A session that never opens: the phone is not there."""

    def __init__(self, uri: str, **kwargs) -> None:
        self.uri = uri
        self.kwargs = kwargs
        self.closed = asyncio.Event()
        self.is_open = False

    async def open(self):
        raise OSError("connection refused")

    async def close(self, reason: str = "closed by PC") -> None:
        del reason
        self.closed.set()


class _NoCandidates:
    name = "none"

    async def candidates(self, *, timeout: float) -> list[Candidate]:
        del timeout
        return []

    async def release(self) -> None:
        return None


def make_manager(pc_config, *providers, **kwargs) -> ConnectionManager:
    store = DeviceStore(pc_config)
    kwargs.setdefault("discovery_timeout", 0.05)
    kwargs.setdefault("connect_timeout", 0.4)
    kwargs.setdefault("max_attempts", 1)
    kwargs.setdefault("remember_endpoints", False)
    return ConnectionManager(
        store,
        providers=list(providers) or [_NoCandidates()],
        session_factory=_SilentSession,  # type: ignore[arg-type]
        **kwargs,
    )


async def test_nothing_found_records_a_discovery_error(pc_config) -> None:
    manager = make_manager(pc_config)
    try:
        await manager.connect()
        assert await manager.wait_for_state(State.ERROR, timeout=2)
        snapshot = manager.snapshot
        assert snapshot.last_discovery_error is not None
        assert "nothing answered" in snapshot.last_discovery_error
    finally:
        await manager.aclose()


async def test_window_reachable_and_silent_gateway_read_differently(pc_config) -> None:
    async def probe(host: str, port: int, timeout: float) -> bool:
        del host, port, timeout
        return False

    provider = GatewayCandidates(gateways=lambda: [Gateway("192.168.43.1")], probe=probe)
    quiet = BeaconCandidates(discovery=StubDiscovery([]))  # type: ignore[arg-type]
    manager = make_manager(pc_config, provider, quiet)
    try:
        await manager.connect()
        assert await manager.wait_for_state(State.ERROR, timeout=2)
        snapshot = manager.snapshot
        assert snapshot.server_reachable is False
        assert snapshot.last_discovery_error is not None
        assert "did not answer" in snapshot.last_discovery_error
    finally:
        await manager.aclose()


async def test_a_failed_connection_is_reported_as_a_connection_error(pc_config) -> None:
    provider = FixedCandidates(
        "10.63.164.24", 8765, probe=lambda *a: asyncio.sleep(0, result=True)
    )
    manager = make_manager(pc_config, provider)
    try:
        await manager.connect()
        assert await manager.wait_for_state(State.ERROR, timeout=2)
        assert manager.snapshot.last_connection_error is not None
        assert manager.snapshot.auth_ok is False
    finally:
        await manager.aclose()


async def test_a_public_network_profile_explains_a_silent_beacon(pc_config, monkeypatch) -> None:
    monkeypatch.setattr(manager_module, "network_profile", lambda: "Public")
    quiet = BeaconCandidates(discovery=StubDiscovery([]))  # type: ignore[arg-type]
    manager = make_manager(pc_config, quiet)
    try:
        await manager.connect()
        assert await manager.wait_for_state(State.ERROR, timeout=2)
        assert "Public" in manager.snapshot.wifi_status
        assert "firewall" in manager.snapshot.wifi_status.lower()
    finally:
        await manager.aclose()


async def test_a_private_profile_says_nothing_about_the_firewall(pc_config, monkeypatch) -> None:
    monkeypatch.setattr(manager_module, "network_profile", lambda: "Private")
    quiet = BeaconCandidates(discovery=StubDiscovery([]))  # type: ignore[arg-type]
    manager = make_manager(pc_config, quiet)
    try:
        await manager.connect()
        assert await manager.wait_for_state(State.ERROR, timeout=2)
        assert "firewall" not in manager.snapshot.wifi_status.lower()
    finally:
        await manager.aclose()


async def test_a_manual_endpoint_is_swapped_in_and_out(pc_config) -> None:
    manager = make_manager(pc_config)
    try:
        manager.set_manual_endpoint("10.63.164.24", 8765)
        assert "manual" in manager.provider_names
        manager.set_manual_endpoint("10.63.164.24")
        assert manager.provider_names.count("manual") == 1, "setting it twice must not duplicate"
        manager.set_manual_endpoint("")
        assert "manual" not in manager.provider_names
    finally:
        await manager.aclose()


# --------------------------------------------------------------------------- #
# 7. nothing here scans, and nothing assumes the old subnet
# --------------------------------------------------------------------------- #
def test_the_transport_module_does_not_contain_a_subnet_sweep() -> None:
    source = inspect.getsource(__import__(
        "phonebridge.controller.transports", fromlist=["transports"]
    ))
    assert not re.search(r"for .* in range\(0, *255\)", source)
    # The docstring may name the subnet it refuses to assume; code must not use it.
    docstring = __import__("phonebridge.controller.transports", fromlist=["x"]).__doc__ or ""
    code = source.replace(docstring, "")
    assert "192.168.43." not in code, "the hotspot subnet must never be hardcoded"


# --------------------------------------------------------------------------- #
# 8. what the window says about all of this
# --------------------------------------------------------------------------- #
def test_a_typed_address_is_parsed_or_refused_with_a_reason() -> None:
    assert parse_endpoint(" 10.63.164.24 ") == ("10.63.164.24", 8765)
    assert parse_endpoint("10.63.164.24:9000") == ("10.63.164.24", 9000)
    for bad in ["", "two words", "host:port", "host:0", "host:70000"]:
        with pytest.raises(EndpointError):
            parse_endpoint(bad)


def test_the_window_shows_reachability_separately_from_discovery() -> None:
    snapshot = Snapshot(
        state=State.ERROR,
        last_discovery_error="the gateway 192.168.43.1 did not answer on tcp:8765",
        server_reachable=False,
    )
    view = build_view(snapshot)
    assert view.server.startswith("no")
    assert "did not answer" in view.discovery_error


def test_an_unchecked_phone_is_not_reported_as_unreachable() -> None:
    view = build_view(Snapshot())
    assert view.server == "not checked yet"
    assert view.auth_ok == "not authenticated"


def test_usb_and_wifi_availability_are_words_not_booleans() -> None:
    no_adb = build_view(Snapshot(adb_available=False, adb_detail="adb not found"))
    assert "Wi-Fi unaffected" in no_adb.adb
    assert no_adb.usb.startswith("unavailable")

    installed = build_view(Snapshot(adb_available=True, usb_available=False))
    assert "plug in the cable" in installed.usb

    plugged = build_view(Snapshot(adb_available=True, usb_available=True))
    assert plugged.usb.startswith("yes")


def test_the_adb_path_is_shown_so_the_user_can_check_it() -> None:
    path = r"C:\Users\2026\AppData\Local\Android\Sdk\platform-tools\adb.exe"
    view = build_view(Snapshot(adb_available=True, adb_path=path))
    assert view.adb_path == path


def test_the_wifi_row_carries_the_firewall_hint_verbatim() -> None:
    hint = "no beacons heard (Windows treats this network as Public)"
    assert build_view(Snapshot(wifi_status=hint)).wifi == hint


def test_the_connection_error_survives_a_later_successful_discovery() -> None:
    snapshot = Snapshot(
        state=State.RECONNECTING,
        last_connection_error="the phone closed the connection",
        last_discovery_error=None,
    )
    view = build_view(snapshot)
    assert "closed the connection" in view.connection_error
    assert view.discovery_error == "none"


def test_the_gateway_provider_makes_exactly_one_probe_per_gateway() -> None:
    source = inspect.getsource(GatewayCandidates.candidates)
    assert source.count("self._probe(") == 1
