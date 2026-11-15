"""Daily launcher transport tests: no Windows, no adb binary, no phone required."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from phonebridge.cli.target import CliError, Target
from phonebridge.controller.adb import AdbDevice, AdbResult
from phonebridge.core.device_store import DeviceStore
from phonebridge.core.session import SessionError, TerminalSessionError
from phonebridge.live.launch import DailyLaunch, LaunchError
from phonebridge.transport.discovery import DiscoveredPhone
from phonebridge.transport.link import LinkError


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeAdb:
    def __init__(self, devices: list[AdbDevice], *, available: bool = True) -> None:
        self._devices = devices
        self._available = available
        self.forwards: list[tuple[str, int, int]] = []
        self.shells: list[tuple[str, list[str]]] = []

    def available(self) -> bool:
        return self._available

    def devices(self) -> list[AdbDevice]:
        return list(self._devices)

    def add_forward(self, serial: str, local: int, remote: int) -> object:
        self.forwards.append((serial, local, remote))
        return object()

    def shell(self, serial: str, argv: list[str]) -> AdbResult:
        self.shells.append((serial, list(argv)))
        return AdbResult(0, "Starting: Intent { cmp=dev.phonebridge/.MainActivity }", "")


class FakeDiscovery:
    def __init__(self, phones: list[DiscoveredPhone]) -> None:
        self.phones = phones
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def live(self) -> list[DiscoveredPhone]:
        return list(self.phones)


class FakeSession:
    def __init__(self, *, ping_error: bool = False) -> None:
        self.phone = SimpleNamespace(device_id="phone-1", name="Xiaomi", build="1.0.0 (1) test")
        self.ping_error = ping_error
        self.closed = False

    async def ping(self, timeout: float = 5.0) -> float:
        if self.ping_error:
            raise SessionError("phone did not answer PING")
        return 12.5

    async def close(self, reason: str = "") -> None:
        self.closed = True


def args(**overrides: object) -> argparse.Namespace:
    base = dict(
        host="",
        port=None,
        route="auto",
        adb_serial="",
        device="",
        autostart=True,
        discovery_timeout=0.01,
        startup_timeout=5.0,
        transport="auto",
        connect_timeout=6.0,
        request_timeout=15.0,
        retries=0,
        auto_pair=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def opener(*, fail_hosts: set[str] | None = None, fail_first: int = 0, ping_error: bool = False):
    calls: list[Target] = []
    sessions: list[FakeSession] = []

    async def open_session(args, store, *, quiet, target, remember):
        calls.append(target)
        if len(calls) <= fail_first or (fail_hosts and target.host in fail_hosts):
            raise LinkError(f"connection refused: {target.host}")
        session = FakeSession(ping_error=ping_error)
        sessions.append(session)
        return session, target

    return open_session, calls, sessions


def pin(store: DeviceStore, peer_id: str = "phone-1", name: str = "Xiaomi") -> None:
    store.pin_peer(peer_id, name, b"\x04" * 65)


def test_usb_forward_autostart_then_authenticated_ping(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = DeviceStore(tmp_path / "pc")
        adb = FakeAdb([AdbDevice(serial="ABC123", state="device")])
        clock = Clock()
        open_session, calls, _ = opener(fail_first=1)
        launch = DailyLaunch(
            args(route="usb"), store, adb=adb, session_opener=open_session,
            status=lambda _: None, clock=clock, sleep=clock.sleep,
        )
        session, target = await launch.open()
        assert target.host == "127.0.0.1"
        assert adb.forwards == [("ABC123", 8765, 8765)]
        assert len(adb.shells) == 1
        serial, argv = adb.shells[0]
        assert serial == "ABC123"
        assert argv[:6] == ["am", "start", "-W", "-n", "dev.phonebridge/.MainActivity", "--ez"]
        assert argv[argv.index("autostart") + 1] == "true"
        assert "autostart_token" in argv
        assert len(calls) == 2
        assert store.known_endpoints()[0]["host"] == "127.0.0.1"
        assert store.known_endpoints()[0]["device_id"] == "phone-1"
        assert session.phone.name == "Xiaomi"

    import asyncio
    asyncio.run(scenario())


def test_multiple_usb_phones_require_a_serial(tmp_path: Path) -> None:
    async def scenario() -> None:
        adb = FakeAdb([AdbDevice(serial="A", state="device"), AdbDevice(serial="B", state="device")])
        open_session, calls, _ = opener()
        launch = DailyLaunch(args(route="usb"), DeviceStore(tmp_path), adb=adb, session_opener=open_session)
        with pytest.raises(LaunchError, match="more than one USB phone"):
            await launch.open()
        assert calls == [] and adb.forwards == []

    import asyncio
    asyncio.run(scenario())


def test_unauthorized_usb_phone_names_the_fix(tmp_path: Path) -> None:
    async def scenario() -> None:
        adb = FakeAdb([AdbDevice(serial="A", state="unauthorized")])
        open_session, _, _ = opener()
        launch = DailyLaunch(args(route="usb"), DeviceStore(tmp_path), adb=adb, session_opener=open_session)
        with pytest.raises(LaunchError) as exc:
            await launch.open()
        assert "unauthorized" in str(exc.value)
        assert "USB debugging" in exc.value.hint

    import asyncio
    asyncio.run(scenario())


def test_wifi_tries_last_working_address_then_beacon_and_remembers_only_after_ping(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = DeviceStore(tmp_path / "pc")
        pin(store)
        store.remember_endpoint("wifi", "10.0.0.5", 8765, peer_id="phone-1")
        phone = DiscoveredPhone(
            device_id="phone-1", name="Xiaomi", host="10.0.0.9", port=8765,
            method="wifi", hosts=("10.0.0.9", "192.168.1.7"),
        )
        discovery = FakeDiscovery([phone])
        clock = Clock()
        open_session, calls, _ = opener(fail_hosts={"10.0.0.5"})
        launch = DailyLaunch(
            args(route="wifi"), store, discovery_factory=lambda: discovery,
            session_opener=open_session, status=lambda _: None, clock=clock, sleep=clock.sleep,
        )
        _, target = await launch.open()
        assert [t.host for t in calls] == ["10.0.0.5", "10.0.0.9"]
        assert target.host == "10.0.0.9"
        assert discovery.started and discovery.stopped
        assert store.known_endpoints()[0]["host"] == "10.0.0.9"

    import asyncio
    asyncio.run(scenario())


def test_manual_host_is_the_fallback_and_skips_discovery(tmp_path: Path) -> None:
    async def scenario() -> None:
        discovery = FakeDiscovery([])
        open_session, calls, _ = opener()
        launch = DailyLaunch(
            args(host="192.168.1.20", route="auto"), DeviceStore(tmp_path),
            discovery_factory=lambda: discovery, session_opener=open_session,
            status=lambda _: None,
        )
        _, target = await launch.open()
        assert target.host == "192.168.1.20" and target.origin == "--host"
        assert not discovery.started
        assert len(calls) == 1

    import asyncio
    asyncio.run(scenario())


def test_a_socket_without_a_ping_is_not_remembered(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = DeviceStore(tmp_path / "pc")
        clock = Clock()
        open_session, _, sessions = opener(ping_error=True)
        launch = DailyLaunch(
            args(host="192.168.1.20", startup_timeout=2.0), store,
            session_opener=open_session, status=lambda _: None, clock=clock, sleep=clock.sleep,
        )
        with pytest.raises(LaunchError, match="bridge not running"):
            await launch.open()
        assert store.known_endpoints() == []
        assert sessions and all(s.closed for s in sessions)

    import asyncio
    asyncio.run(scenario())


def test_multiple_pinned_beacons_require_device_choice(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = DeviceStore(tmp_path)
        pin(store, "phone-1", "One")
        pin(store, "phone-2", "Two")
        discovery = FakeDiscovery([
            DiscoveredPhone(device_id="phone-1", name="One", host="10.0.0.1", port=8765, method="wifi"),
            DiscoveredPhone(device_id="phone-2", name="Two", host="10.0.0.2", port=8765, method="wifi"),
        ])
        open_session, calls, _ = opener()
        launch = DailyLaunch(
            args(route="wifi"), store, discovery_factory=lambda: discovery,
            session_opener=open_session, status=lambda _: None,
        )
        with pytest.raises(LaunchError, match="more than one pinned phone"):
            await launch.open()
        assert calls == []

    import asyncio
    asyncio.run(scenario())


def test_terminal_pairing_failure_is_not_retried(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls: list[Target] = []

        async def open_session(args, store, *, quiet, target, remember):
            calls.append(target)
            cause = TerminalSessionError("pinned key changed")
            raise CliError("pinned key changed", hint="not retried") from cause

        launch = DailyLaunch(
            args(host="192.168.1.20"), DeviceStore(tmp_path),
            session_opener=open_session, status=lambda _: None,
        )
        with pytest.raises(CliError, match="pinned key changed"):
            await launch.open()
        assert len(calls) == 1

    import asyncio
    asyncio.run(scenario())
