"""The controller against the reference phone, over the real protocol.

Nothing is faked below the controller: a real WebSocket, a real P-256 handshake,
real AES-256-GCM envelopes, the real ``system.info`` from the simulator, and the
real pairing prompt round trip. What is faked is only adb (there is no USB here),
because the transport layer is unit-tested separately.

This proves the *controller*: discovery -> transport choice -> pairing -> state
machine -> capabilities -> reconnect. It does not prove the Android app or the
handset; that is the device checklist in ``docs/17``.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from phone_sim import SimulatedPhone, _identity_for

from phonebridge.controller.adb import Adb, AdbResult
from phonebridge.controller.manager import ConnectionManager, State
from phonebridge.controller.runtime import BackgroundRuntime
from phonebridge.controller.transports import CachedCandidates, UsbCandidates
from phonebridge.core.device_store import DeviceStore
from phonebridge.core.session import BackoffPolicy, BridgeSession
from phonebridge.transport.link import websocket_uri

FAST = BackoffPolicy(initial_s=0.02, max_s=0.05, jitter=0.0)


class NoDevicesAdb:
    """adb is installed, but no phone is plugged in."""

    def __call__(self, argv, timeout):  # noqa: ANN001
        del timeout
        argv = list(argv)
        if argv[0] == "devices":
            return AdbResult(0, "List of devices attached\n", "")
        if argv[0] == "forward" and argv[1] == "--list":
            return AdbResult(0, "", "")
        return AdbResult(0, "", "")


async def start_phone(sim_state, name: str = "Pixel-test", port: int = 0) -> SimulatedPhone:
    identity = _identity_for(sim_state / "phone_identity.pem", name)
    phone = SimulatedPhone(
        identity=identity,
        state_file=sim_state / "paired.json",
        host="127.0.0.1",
        port=port,
        pair_policy="auto",
        name=name,
    )
    await phone.start()
    return phone


def controller(pc_config, phone_port: int, **kwargs) -> ConnectionManager:
    store = DeviceStore(pc_config)
    store.remember_endpoint("wifi", "127.0.0.1", phone_port, None)
    providers = [
        UsbCandidates(Adb(runner=NoDevicesAdb(), executable="adb")),
        CachedCandidates(store),
    ]
    kwargs.setdefault("discovery_timeout", 0.3)
    kwargs.setdefault("connect_timeout", 8.0)
    kwargs.setdefault("policy", FAST)
    kwargs.setdefault("sleeper", lambda _delay: asyncio.sleep(0))
    return ConnectionManager(store, providers=providers, **kwargs)


@pytest.fixture()
async def phone(sim_state):
    started = await start_phone(sim_state)
    try:
        yield started
    finally:
        await started.stop()


# --------------------------------------------------------------------------- #
# first contact: discovery, pairing, capabilities
# --------------------------------------------------------------------------- #
async def test_pairing_then_connected_with_real_crypto(pc_config, phone) -> None:
    manager = controller(pc_config, phone.port)
    try:
        await manager.connect()
        assert await manager.wait_for_state(State.PAIRING_REQUIRED, timeout=10)
        code = manager.snapshot.pairing_code or ""
        # the phone shows it grouped, e.g. "196 775"; the GUI shows it verbatim
        assert re.fullmatch(r"\d{3}\s?\d{3}", code), f"expected a six-digit code, got {code!r}"

        manager.resolve_pairing(True)
        assert await manager.wait_for_state(State.CONNECTED, timeout=10)

        snapshot = manager.snapshot
        assert snapshot.device["name"] == "Pixel-test"
        assert snapshot.transport == "cached", "the saved address was the only candidate"
        assert snapshot.pairing_code is None
        assert snapshot.connected_at is not None

        capabilities = {c.key: c for c in snapshot.capabilities}
        assert len(capabilities) == 8
        # The simulator implements exactly one capability; the rest must not be
        # reported as usable even though the hardware is "present".
        assert capabilities["audio.bluetooth_sco"].available is True
        assert capabilities["camera.back"].available is False
        assert capabilities["camera.back"].present is True
        assert capabilities["audio.capture"].permitted is False
        assert all(not c.active for c in snapshot.capabilities)
    finally:
        await manager.aclose()


async def test_rejecting_the_code_never_connects(pc_config, phone) -> None:
    manager = controller(pc_config, phone.port)
    try:
        await manager.connect()
        assert await manager.wait_for_state(State.PAIRING_REQUIRED, timeout=10)
        manager.resolve_pairing(False)
        assert await manager.wait_for_state(State.ERROR, timeout=10)
        assert manager.session is None
        # and nothing was pinned, so the phone is still a stranger
        assert DeviceStore(pc_config).list_peers() == []
    finally:
        await manager.aclose()


# --------------------------------------------------------------------------- #
# the second launch: no prompt, and a drop is repaired
# --------------------------------------------------------------------------- #
async def pin_once(pc_config, phone) -> None:
    """Do the first pairing the way the CLI does, so later connects are silent."""
    store = DeviceStore(pc_config)
    identity = store.load_or_create_identity()
    session = BridgeSession(
        websocket_uri("127.0.0.1", phone.port),
        identity,
        store,
        confirm_pairing=lambda _sas: True,
    )
    await session.open()
    await session.close()


async def test_a_pinned_phone_connects_without_a_prompt(pc_config, phone) -> None:
    await pin_once(pc_config, phone)
    manager = controller(pc_config, phone.port)
    try:
        await manager.connect()
        assert await manager.wait_for_state(State.CONNECTED, timeout=10)
        assert manager.snapshot.pairing_code is None
        assert manager.snapshot.state is State.CONNECTED
    finally:
        await manager.aclose()


async def test_a_dropped_session_is_replaced_by_a_new_one(pc_config, phone) -> None:
    await pin_once(pc_config, phone)
    manager = controller(pc_config, phone.port)
    seen: list[State] = []
    manager.subscribe(lambda snapshot: seen.append(snapshot.state))
    try:
        await manager.connect()
        assert await manager.wait_for_state(State.CONNECTED, timeout=10)
        first = manager.session
        assert first is not None

        await first.close("simulated network loss")

        deadline = asyncio.get_running_loop().time() + 10
        while manager.session in (None, first) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert await manager.wait_for_state(State.CONNECTED, timeout=10)
        assert manager.session is not first, "a new session must have been opened"
        assert State.RECONNECTING in seen
        assert State.PAIRING_REQUIRED not in seen, "a pinned phone must not be re-prompted"
    finally:
        await manager.aclose()


async def test_heartbeat_proves_the_link_is_alive(pc_config, phone) -> None:
    await pin_once(pc_config, phone)
    manager = controller(
        pc_config, phone.port, heartbeat_interval=0.05, heartbeat_timeout=2.0
    )
    try:
        await manager.connect()
        assert await manager.wait_for_state(State.CONNECTED, timeout=10)
        # the simulator answers PING; two beats must not tear the session down
        await asyncio.sleep(0.4)
        assert manager.state is State.CONNECTED
        assert manager.session is not None and manager.session.is_open
    finally:
        await manager.aclose()


async def test_disconnect_leaves_nothing_behind(pc_config, phone) -> None:
    await pin_once(pc_config, phone)
    manager = controller(pc_config, phone.port)
    await manager.connect()
    assert await manager.wait_for_state(State.CONNECTED, timeout=10)
    session = manager.session
    await manager.disconnect("test")
    assert manager.state is State.DISCONNECTED
    assert session is not None and not session.is_open
    assert manager.session is None


# --------------------------------------------------------------------------- #
# the threading seam the window uses
# --------------------------------------------------------------------------- #
async def test_closing_the_window_while_connected_cleans_up(pc_config, phone) -> None:
    """Closing the window must not leave a session open on the phone."""
    await pin_once(pc_config, phone)
    runtime = BackgroundRuntime(lambda: controller(pc_config, phone.port))
    runtime.start()
    try:
        runtime.connect()
        assert await asyncio.to_thread(runtime.wait_for_state, "connected", timeout=15)
        live = runtime.manager.session
        assert live is not None and live.is_open
    finally:
        await asyncio.to_thread(runtime.stop)  # no disconnect() on purpose
    assert not runtime.running
    assert live is not None and not live.is_open


async def test_background_runtime_connects_and_reports(pc_config, phone) -> None:
    await pin_once(pc_config, phone)
    snapshots = []
    runtime = BackgroundRuntime(
        lambda: controller(pc_config, phone.port),
        on_snapshot=snapshots.append,
    )
    try:
        runtime.start()
        assert runtime.running
        runtime.connect()
        ok = await asyncio.to_thread(runtime.wait_for_state, "connected", timeout=15)
        assert ok, f"runtime never connected: {runtime.snapshot().state}"
        assert runtime.snapshot().device["name"] == "Pixel-test"
        assert snapshots, "the window would have nothing to draw"
        runtime.disconnect()
        assert await asyncio.to_thread(runtime.wait_for_state, "disconnected", timeout=10)
    finally:
        await asyncio.to_thread(runtime.stop)
    assert not runtime.running
