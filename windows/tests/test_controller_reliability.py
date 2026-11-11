"""What happens after the link dies — and what the user is told about it.

The hotspot report was: connect, authenticate, then *"the session later closed with
WebSocket error 1006"*. 1006 means "the TCP connection vanished"; it says nothing
about whether the phone stopped, the app was killed by a battery manager, or the PC
moved to a different network. Those need different actions, so the manager proves
two cheap facts when a session ends: does the phone still answer, and is this still
the same network?

Also here: the USB verdict reaching the snapshot, and the loopback note.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from phonebridge.controller.manager import ConnectionManager, Snapshot, State
from phonebridge.controller.netprobe import Gateway
from phonebridge.controller.transports import Candidate
from phonebridge.controller.usbcheck import PnpDevice
from phonebridge.core.device_store import DeviceStore
from phonebridge.core.session import BackoffPolicy

INFO = {
    "device_id": "abc123def4567890",
    "name": "Redmi Note 14 Pro+",
    "model": "24115RA8EI",
    "manufacturer": "Xiaomi",
    "android_release": "16",
    "sdk_int": 36,
    "app_version": "1.0.0",
    "capabilities": {"camera.back": False, "hardware.camera.back": True},
    "permissions": {},
}

FAST = BackoffPolicy(initial_s=0.01, max_s=0.02, jitter=0.0)


class DropSession:
    """Opens, answers system.info, then dies the way a hotspot drop looks."""

    def __init__(self, uri: str, behaviour=None, **kwargs) -> None:
        del kwargs
        self.uri = uri
        self.behaviour = behaviour
        self.closed = asyncio.Event()
        self.is_open = False
        self.pings = 0
        self.ping_error: Exception | None = None

    async def open(self):
        self.is_open = True
        return self

    async def call(self, method, params=None):  # noqa: ANN001
        del params
        assert method == "system.info"
        return INFO

    async def ping(self, timeout: float = 5.0) -> float:
        del timeout
        self.pings += 1
        if self.ping_error is not None:
            raise self.ping_error
        return 1.0

    async def wait_closed(self) -> str:
        await self.closed.wait()
        return "1006 abnormal closure"

    async def close(self, reason: str = "closed by PC") -> None:
        self.is_open = False
        self.closed.set()

    def drop(self) -> None:
        self.is_open = False
        self.closed.set()


class StaticProvider:
    name = "static"

    def __init__(self, candidates: list[Candidate]) -> None:
        self._candidates = candidates

    async def candidates(self, *, timeout: float) -> list[Candidate]:
        del timeout
        return list(self._candidates)

    async def release(self) -> None:
        return None


def wifi(host: str = "10.174.54.152") -> Candidate:
    return Candidate("hotspot", host, 8765, 60, "gateway answers")


async def wait_for(predicate, timeout: float = 4.0) -> bool:  # noqa: ANN001
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


class _NoDevices:
    """An adb that is installed and answers, but lists no device."""

    def __call__(self, argv, timeout):  # noqa: ANN001
        from phonebridge.controller.adb import AdbResult

        del timeout
        if argv[0] == "--version":
            return AdbResult(0, "Android Debug Bridge version 1.0.41\n", "")
        if argv[0] == "devices":
            return AdbResult(0, "List of devices attached\n", "")
        return AdbResult(0, "", "")


def _usb_only_provider():
    """A real ``UsbCandidates`` whose adb is present and device-less."""
    from phonebridge.controller.adb import Adb, AdbLocation
    from phonebridge.controller.transports import UsbCandidates

    adb = Adb(runner=_NoDevices(), locate=lambda: AdbLocation("C:/adb/adb.exe", "PATH"))
    return UsbCandidates(adb)


def make_manager(
    pc_config,
    *,
    sessions: list[DropSession] | None = None,
    probe_answers: list[bool] | None = None,
    gateways: list[str] | None = None,
    **kwargs,
) -> ConnectionManager:
    store = DeviceStore(pc_config)
    sessions = sessions if sessions is not None else []
    answers = list(probe_answers or [True])
    # Kept by reference on purpose: a test that moves the PC to another network
    # mutates this list, and the manager must see that.
    networks = gateways if gateways is not None else ["10.174.54.154"]

    fail_after_first = kwargs.pop("fail_after_first", False)

    def factory(uri, *_args, **_kwargs):  # noqa: ANN001
        if fail_after_first and sessions:
            raise OSError("connection refused")
        session = DropSession(uri)
        sessions.append(session)
        return session

    async def probe(host: str, port: int, timeout: float) -> bool:
        del host, port, timeout
        return answers.pop(0) if len(answers) > 1 else answers[0]

    providers = kwargs.pop("providers", None) or [StaticProvider([wifi()])]
    kwargs.setdefault("discovery_timeout", 0.05)
    kwargs.setdefault("connect_timeout", 0.5)
    kwargs.setdefault("policy", FAST)
    kwargs.setdefault("max_attempts", 1)
    kwargs.setdefault("remember_endpoints", False)
    kwargs.setdefault("sleeper", lambda _delay: asyncio.sleep(0))
    return ConnectionManager(
        store,
        providers=providers + [StaticProvider([wifi()])],
        session_factory=factory,
        probe=probe,
        gateways=lambda: [Gateway(address) for address in networks],
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# the drop explanation
# --------------------------------------------------------------------------- #
async def test_a_drop_while_the_phone_still_answers_is_reported_as_such(pc_config) -> None:
    sessions: list[DropSession] = []
    manager = make_manager(pc_config, sessions=sessions, probe_answers=[True], max_attempts=3)
    try:
        await manager.connect()
        assert await wait_for(lambda: sessions)
        assert await manager.wait_for_state(State.CONNECTED, timeout=2)
        sessions[-1].drop()
        assert await wait_for(
            lambda: "the phone still answers" in (manager.snapshot.last_connection_error or "")
        )

        text = manager.snapshot.last_connection_error or ""
        assert "hotspot at 10.174.54.152:8765" in text
        assert "network is unchanged" in text
        # And it really did try again rather than sitting there.
        assert await wait_for(lambda: len(sessions) >= 2)
    finally:
        await manager.aclose()


async def test_a_silent_phone_is_reported_with_the_background_restriction_hint(
    pc_config,
) -> None:
    """A killed app is the common 1006 cause on HyperOS, and it is actionable."""
    sessions: list[DropSession] = []
    manager = make_manager(pc_config, sessions=sessions, probe_answers=[False])
    try:
        await manager.connect()
        assert await wait_for(lambda: sessions)
        assert await manager.wait_for_state(State.CONNECTED, timeout=2)
        sessions[-1].drop()
        assert await wait_for(
            lambda: "nothing answers at that address now"
            in (manager.snapshot.last_connection_error or "")
        )
        assert "battery" in (manager.snapshot.last_connection_error or "").lower()
    finally:
        await manager.aclose()


async def test_a_changed_network_is_named_with_both_addresses(pc_config) -> None:
    """The hotspot case: the PC hopped back to Wi-Fi, so the gateway moved."""
    sessions: list[DropSession] = []
    networks = ["10.174.54.154"]
    manager = make_manager(pc_config, sessions=sessions, gateways=networks)
    # Move the PC to another network just before the drop.
    original_factory = manager._session_factory  # noqa: SLF001
    try:
        await manager.connect()
        assert await wait_for(lambda: sessions)
        assert await manager.wait_for_state(State.CONNECTED, timeout=2)
        networks.clear()
        networks.append("192.168.1.1")
        sessions[-1].drop()
        assert await wait_for(
            lambda: "the network changed" in (manager.snapshot.last_connection_error or "")
        )
        text = manager.snapshot.last_connection_error or ""
        assert "10.174.54.154" in text and "192.168.1.1" in text
    finally:
        assert original_factory
        await manager.aclose()


async def test_a_missed_heartbeat_is_explained_the_same_way(pc_config) -> None:
    sessions: list[DropSession] = []
    manager = make_manager(
        pc_config,
        sessions=sessions,
        probe_answers=[False],
        heartbeat_interval=0.01,
        heartbeat_timeout=0.01,
        heartbeat_failures=1,
    )
    try:
        await manager.connect()
        assert await wait_for(lambda: sessions)
        assert await manager.wait_for_state(State.CONNECTED, timeout=2)
        sessions[-1].ping_error = TimeoutError("no pong")
        assert await wait_for(
            lambda: "nothing answers" in (manager.snapshot.last_connection_error or "")
        )
        assert "reconnecting" in "\n".join(manager.snapshot.diagnostics).lower()
    finally:
        await manager.aclose()


async def test_a_drop_after_a_good_session_restarts_the_backoff(pc_config) -> None:
    """A flaky link must not inherit the delay a long outage built up."""
    sessions: list[DropSession] = []
    delays_seen: list[float] = []
    # Small enough to keep the test quick, far enough apart to tell a restarted
    # ladder (0.4 again) from one that kept climbing (0.8).
    policy = BackoffPolicy(initial_s=0.4, max_s=0.8, jitter=0.0)
    manager = make_manager(pc_config, sessions=sessions, max_attempts=6, policy=policy)
    manager.subscribe(
        lambda snap: delays_seen.append(snap.next_retry_in)
        if snap.next_retry_in is not None
        else None
    )
    try:
        await manager.connect()
        for expected_delays in (1, 2):
            before = len(sessions)
            assert await wait_for(lambda want=before: len(sessions) > want)
            assert await manager.wait_for_state(State.CONNECTED, timeout=3)
            sessions[-1].drop()
            assert await wait_for(
                lambda want=expected_delays: len(delays_seen) >= want
            ), f"no backoff was scheduled after drop {expected_delays}"
        assert delays_seen[:2] == [0.4, 0.4], f"ladder should restart, got {delays_seen[:3]}"
    finally:
        await manager.aclose()


async def test_two_sources_naming_one_socket_open_one_session(pc_config) -> None:
    """The beacon, the gateway and a saved address often agree. Try it once."""
    sessions: list[DropSession] = []
    manager = make_manager(
        pc_config,
        sessions=sessions,
        providers=[StaticProvider([wifi()]), StaticProvider([wifi()])],
    )
    try:
        await manager.connect()
        assert await manager.wait_for_state(State.CONNECTED, timeout=3)
        assert len(sessions) == 1
        found = [line for line in manager.snapshot.diagnostics if "found hotspot at" in line]
        assert len(found) == 1, found
    finally:
        await manager.aclose()


async def test_disconnecting_on_purpose_is_not_reported_as_a_failure(pc_config) -> None:
    sessions: list[DropSession] = []
    manager = make_manager(pc_config, sessions=sessions)
    try:
        await manager.connect()
        assert await wait_for(lambda: sessions)
        assert await manager.wait_for_state(State.CONNECTED, timeout=2)
        await manager.disconnect("disconnected by the user")

        snapshot = manager.snapshot
        assert snapshot.state is State.DISCONNECTED
        assert not snapshot.last_connection_error, "a deliberate exit is not a drop"
        assert snapshot.auth_ok is False
    finally:
        await manager.aclose()


async def test_giving_up_keeps_the_reason_and_the_ladder_is_bounded(pc_config) -> None:
    """Bounded retries, and the final message still says *why* it died."""
    sessions: list[DropSession] = []
    manager = make_manager(
        pc_config, sessions=sessions, probe_answers=[False], fail_after_first=True
    )
    try:
        await manager.connect()
        assert await wait_for(lambda: sessions)
        assert await manager.wait_for_state(State.CONNECTED, timeout=2)
        sessions[-1].drop()
        assert await manager.wait_for_state(State.ERROR, timeout=5)
        message = manager.snapshot.message or ""
        assert "gave up after 1 attempt" in message
        assert "nothing answers at that address now" in message
        assert len(sessions) == 1, "no second session may be opened after giving up"
    finally:
        await manager.aclose()


# --------------------------------------------------------------------------- #
# USB diagnosis reaches the snapshot
# --------------------------------------------------------------------------- #
async def test_the_snapshot_carries_the_usb_verdict_and_the_next_step(pc_config) -> None:
    rows = [
        PnpDevice(
            instance_id=r"USB\VID_2717&PID_FF48\4a2b",
            friendly_name="Mi Phone",
            status="OK",
            problem=0,
            class_name="USB",
        )
    ]
    manager = make_manager(
        pc_config,
        providers=[_usb_only_provider()],
        usb_probe=lambda: rows,
        system="Windows",
    )
    try:
        await manager.connect()
        assert await manager.wait_for_state((State.CONNECTED, State.ERROR), timeout=3)
        snapshot = manager.snapshot
        assert "Windows sees the phone" in snapshot.usb_diagnosis
        assert "USB debugging ON" in snapshot.usb_action
    finally:
        await manager.aclose()


async def test_a_failing_device_tree_query_does_not_break_discovery(pc_config) -> None:
    def broken() -> list[PnpDevice]:
        raise OSError("PowerShell is not available")

    manager = make_manager(pc_config, usb_probe=broken)
    try:
        await manager.connect()
        assert await manager.wait_for_state((State.CONNECTED, State.ERROR), timeout=3)
        assert manager.snapshot.state in {State.CONNECTED, State.ERROR}
    finally:
        await manager.aclose()


# --------------------------------------------------------------------------- #
# a typed address: accepted, but not pretended to be the phone
# --------------------------------------------------------------------------- #
def test_a_loopback_manual_endpoint_is_accepted_and_annotated(pc_config) -> None:
    manager = ConnectionManager(DeviceStore(pc_config), remember_endpoints=False)
    manager.set_manual_endpoint("127.0.0.1", 50001)
    assert "manual" in manager.provider_names
    text = "\n".join(manager.snapshot.diagnostics)
    assert "loopback address" in text
    assert "adb forward" in text


def test_a_real_lan_address_gets_no_such_warning(pc_config) -> None:
    manager = ConnectionManager(DeviceStore(pc_config), remember_endpoints=False)
    manager.set_manual_endpoint("10.174.54.152", 8765)
    assert "loopback" not in "\n".join(manager.snapshot.diagnostics)


def test_the_explanation_reuses_the_injected_probe() -> None:
    """No new socket code: the drop diagnosis calls exactly one probe."""
    source = inspect.getsource(ConnectionManager._explain_drop)  # noqa: SLF001
    assert source.count("await self._probe(") == 1
    assert "tcp_probe(" not in source


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_forms_are_recognised(host: str) -> None:
    from phonebridge.controller.endpoints import is_loopback

    assert is_loopback(host)


def test_a_lan_address_is_not_loopback() -> None:
    from phonebridge.controller.endpoints import is_loopback

    for host in ("10.174.54.152", "192.168.1.7", "100.64.0.9"):
        assert not is_loopback(host)


def test_the_default_endpoint_is_empty_not_loopback() -> None:
    snapshot = Snapshot()
    assert snapshot.endpoint is None
