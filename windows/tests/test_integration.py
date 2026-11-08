"""End-to-end tests: PC client <-> reference phone implementation.

These run over a real loopback WebSocket with real P-256 ECDH and AES-256-GCM.
Anything that passes here is verified protocol behaviour; the only thing that
still needs a physical phone is the Kotlin port of the same logic.
"""

import asyncio

import pytest
from phone_sim import SimulatedPhone, _identity_for

from phonebridge.core.device_store import DeviceStore
from phonebridge.core.session import (
    BridgeSession,
    PairingRejected,
    ReconnectingSession,
    SessionError,
)
from phonebridge.transport.discovery import DiscoveryService
from phonebridge.transport.link import websocket_uri


async def _start_phone(sim_state, **kwargs) -> SimulatedPhone:
    ident = _identity_for(sim_state / "phone_identity.pem", kwargs.pop("name", "Pixel-test"))
    phone = SimulatedPhone(
        identity=ident,
        state_file=sim_state / "paired.json",
        host="127.0.0.1",
        port=0,
        pair_policy="auto",
        **kwargs,
    )
    await phone.start()
    return phone


def _session(phone: SimulatedPhone, pc_config, **kwargs) -> BridgeSession:
    store = DeviceStore(pc_config)
    ident = store.load_or_create_identity()
    uri = websocket_uri("127.0.0.1", phone.port)
    kwargs.setdefault("confirm_pairing", lambda _sas: True)
    return BridgeSession(uri, ident, store, **kwargs)


# --------------------------------------------------------------------------- #
async def test_pairing_then_rpc(sim_state, pc_config):
    phone = await _start_phone(sim_state)
    try:
        session = await _session(phone, pc_config).open()
        assert session.phone is not None
        assert session.phone.paired is False  # first contact
        info = await session.call("system.info")
        assert info["protocol_version"] == 1
        assert info["capabilities"]["audio.bluetooth_sco"] is True
        # pairing must have persisted on both sides
        store = DeviceStore(pc_config)
        peers = store.list_peers()
        assert len(peers) == 1 and peers[0].peer_id == phone.identity.device_id
        assert phone.paired  # phone persisted too
        await session.close()
    finally:
        await phone.stop()


async def test_second_connection_skips_pairing(sim_state, pc_config):
    phone = await _start_phone(sim_state)
    try:
        s1 = await _session(phone, pc_config).open()
        await s1.close()
        prompts = []

        def confirm(sas: str) -> bool:
            prompts.append(sas)
            return True

        s2 = await _session(phone, pc_config, confirm_pairing=confirm).open()
        assert s2.phone.paired is True
        assert prompts == []  # no pairing prompt the second time
        await s2.close()
    finally:
        await phone.stop()


async def test_pairing_declined_stores_nothing(sim_state, pc_config):
    phone = await _start_phone(sim_state)
    try:
        with pytest.raises(PairingRejected):
            await _session(phone, pc_config, confirm_pairing=lambda _s: False).open()
        assert DeviceStore(pc_config).list_peers() == []
        assert not phone.paired
    finally:
        await phone.stop()


async def test_phone_can_refuse_pairing(sim_state, pc_config):
    phone = await _start_phone(sim_state)
    phone.pair_policy = "deny"
    try:
        with pytest.raises(PairingRejected):
            await _session(phone, pc_config).open()
        assert DeviceStore(pc_config).list_peers() == []
    finally:
        await phone.stop()


async def test_unpaired_pc_is_refused_when_phone_already_trusts_another_pc(sim_state, pc_config):
    """A second PC with a different identity must not inherit the pairing."""
    phone = await _start_phone(sim_state)
    try:
        s1 = await _session(phone, pc_config).open()
        await s1.close()

        other_store = DeviceStore(pc_config / "other")
        other_ident = other_store.load_or_create_identity()
        other = BridgeSession(
            websocket_uri("127.0.0.1", phone.port),
            other_ident,
            other_store,
            confirm_pairing=lambda _s: False,
        )
        with pytest.raises(PairingRejected):
            await other.open()
    finally:
        await phone.stop()


async def test_pc_refuses_phone_whose_pinned_key_changed(sim_state, pc_config):
    """If the key on file for a device id no longer matches, refuse to talk.

    This is the case where someone substitutes a device that claims the same id
    but holds a different private key.
    """
    phone = await _start_phone(sim_state)
    try:
        store = DeviceStore(pc_config)
        # Pin the *correct* id with a *wrong* key, as if the pin had been tampered
        # with or the phone had been reflashed under the same id.
        from phonebridge.protocol.crypto import Identity

        impostor_key = Identity.generate().public_raw
        store.pin_peer(phone.identity.device_id, "Pixel-test", impostor_key)

        with pytest.raises(SessionError, match="does not match the pinned key"):
            await _session(phone, pc_config).open()
    finally:
        await phone.stop()


async def test_phone_refuses_known_pc_whose_key_changed(sim_state, pc_config):
    phone = await _start_phone(sim_state)
    try:
        s = await _session(phone, pc_config).open()
        await s.close()
        # Re-pin the same device id to a different key on the phone side.
        from phonebridge.protocol.crypto import Identity, b64e

        those_pairs = list(phone.paired.items())
        phone.paired = {k: b64e(Identity.generate().public_raw) for k, _ in those_pairs}
        with pytest.raises(SessionError):
            await _session(phone, pc_config).open()
    finally:
        await phone.stop()


async def test_ping_and_blob_echo(sim_state, pc_config):
    phone = await _start_phone(sim_state)
    try:
        session = await _session(phone, pc_config).open()
        rtt = await session.ping()
        assert 0 <= rtt < 2000

        queue = session.subscribe_blobs(5)
        await session.send_blob(b"\x01\x02\x03\x04", stream_id=5)
        blob = await asyncio.wait_for(queue.get(), 5)
        assert blob == b"\x01\x02\x03\x04"
        await session.close()
    finally:
        await phone.stop()


async def test_large_binary_payload_roundtrip(sim_state, pc_config):
    phone = await _start_phone(sim_state)
    try:
        session = await _session(phone, pc_config).open()
        queue = session.subscribe_blobs(1)
        for size in (1, 100, 4_000, 64_000, 1_000_000):
            payload = bytes(range(256)) * (size // 256) + b"\x00" * (size % 256)
            await session.send_blob(payload, stream_id=1)
            got = await asyncio.wait_for(queue.get(), 15)
            assert got == payload, f"mismatch at {size} bytes"
        await session.close()
    finally:
        await phone.stop()


async def test_unknown_rpc_method_returns_error(sim_state, pc_config):
    phone = await _start_phone(sim_state)
    try:
        session = await _session(phone, pc_config).open()
        with pytest.raises(SessionError, match="method_not_found"):
            await session.call("does.not.exist")
        await session.close()
    finally:
        await phone.stop()


async def test_phone_can_push_events(sim_state, pc_config):
    phone = await _start_phone(sim_state)
    try:
        events: list[tuple[str, dict]] = []
        session = await _session(
            phone, pc_config, on_event=lambda n, d: events.append((n, d))
        ).open()
        await phone.push_event("bluetooth.headset", {"connected": True, "name": "WH-1000XM4"})
        await asyncio.sleep(0.2)
        assert events and events[0][0] == "bluetooth.headset"
        assert events[0][1]["name"] == "WH-1000XM4"
        await session.close()
    finally:
        await phone.stop()


async def test_session_cancellation_rejects_further_sends(sim_state, pc_config):
    phone = await _start_phone(sim_state)
    try:
        session = await _session(phone, pc_config).open()
        await session.close()
        with pytest.raises(SessionError):
            await session.call("system.info")
    finally:
        await phone.stop()


async def test_reconnect_supervisor_recovers(sim_state, pc_config):
    phone = await _start_phone(sim_state)
    store = DeviceStore(pc_config)
    ident = store.load_or_create_identity()
    states: list[tuple[str, dict]] = []
    sup = ReconnectingSession(
        websocket_uri("127.0.0.1", phone.port),
        ident,
        store,
        on_state=lambda s, d: states.append((s, d)),
        confirm_pairing=lambda _s: True,
    )
    await sup.start()
    try:
        first = await sup.wait_connected(10)
        assert first.is_open
        # simulate a phone-side drop
        await phone.stop()
        await asyncio.sleep(0.6)
        assert sup.current is None

        # bring the phone back on the same port
        ident2 = _identity_for(sim_state / "phone_identity.pem", "Pixel-test")
        revived = SimulatedPhone(
            identity=ident2,
            state_file=sim_state / "paired.json",
            host="127.0.0.1",
            port=phone.port,
            pair_policy="auto",
        )
        await revived.start()
        second = await sup.wait_connected(15)
        assert second.is_open
        assert second.phone.paired is True  # pairing survived the restart
        await revived.stop()
    finally:
        await sup.stop()
    reported = [s for s, _ in states]
    assert "connected" in reported
    assert reported.count("connected") >= 2


async def test_discovery_finds_beacon(sim_state, pc_config):
    phone = await _start_phone(sim_state, beacon=True, advertise_host="127.0.0.1")
    svc = DiscoveryService()
    try:
        await svc.start()
        found = await svc.wait_for_first(timeout=6.0)
    finally:
        await svc.stop()
        await phone.stop()
    assert found is not None, "no beacon received"
    assert found.device_id == phone.identity.device_id
    assert found.port == phone.port
    assert found.uri.startswith("ws://127.0.0.1:")


async def test_bench_reports_the_requested_frame_count(sim_state, pc_config, capsys):
    """Warm-up frames must not appear in the reported frame count.

    The bench sends 20 untimed warm-up frames before the measured window; an
    earlier version printed the total (frames + 20), which reads like a bug to
    anyone comparing it with the command they typed. Throughput was also diluted
    by the warm-up time, so the timer now starts after the warm-up is drained.
    """
    from types import SimpleNamespace

    from phonebridge.cli.main import _bench

    phone = await _start_phone(sim_state)
    try:
        args = SimpleNamespace(
            host="127.0.0.1",
            port=phone.port,
            config_dir=str(pc_config),
            auto_pair=True,
            frames=100,
            size=256,
            pipeline=16,
            expect_ms=None,
        )
        rc = await _bench(args)
    finally:
        await phone.stop()

    out = capsys.readouterr().out
    assert rc == 0
    assert "frames echoed : 100 (+20 warm-up)" in out
    assert "payload corruption detected" not in out
