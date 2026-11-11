"""End-to-end workflow tests: PC client <-> reference phone, over real crypto.

Every test here opens a real WebSocket, runs a real P-256 handshake, encrypts
with real AES-256-GCM and asserts on what came back. Nothing is stubbed on the
PC side; the phone side is the reference implementation in
``tools/simulator/phone_sim.py``, the executable copy of the protocol
(``docs/03``) that the Kotlin app implements.

What this file can prove: the PC implementation, the wire schemas, the failure
paths and the data integrity. What it cannot prove: that the Android app behaves
identically on a handset. That still needs ``docs/12`` §5 run on a device, and
the verification matrix says so instead of pretending otherwise.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from phone_sim import SimulatedPhone, _identity_for

from phonebridge.cli import selftest as st
from phonebridge.core import compat
from phonebridge.core.device_store import DeviceStore
from phonebridge.core.session import BridgeSession, SessionError, TerminalSessionError
from phonebridge.protocol.messages import ProtocolError
from phonebridge.transport.link import websocket_uri

PHONE_PERMISSIONS = {"RECORD_AUDIO", "POST_NOTIFICATIONS", "BLUETOOTH_CONNECT"}


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
async def start_phone(sim_state, name: str = "Pixel-test", **kwargs) -> SimulatedPhone:
    ident = _identity_for(sim_state / "phone_identity.pem", name)
    phone = SimulatedPhone(
        identity=ident,
        state_file=sim_state / "paired.json",
        host="127.0.0.1",
        port=0,
        pair_policy="auto",
        name=name,
        **kwargs,
    )
    await phone.start()
    return phone


def make_session(phone: SimulatedPhone, pc_config, **kwargs) -> BridgeSession:
    store = DeviceStore(pc_config)
    ident = store.load_or_create_identity()
    uri = websocket_uri("127.0.0.1", phone.port)
    kwargs.setdefault("confirm_pairing", lambda _sas: True)
    return BridgeSession(uri, ident, store, **kwargs)


@pytest.fixture()
async def connected(sim_state, pc_config):
    """One live, paired, authenticated session, closed afterwards."""
    phone = await start_phone(sim_state)
    session = await make_session(phone, pc_config).open()
    try:
        yield phone, session
    finally:
        await session.close()
        await phone.stop()


async def collect_blobs(session: BridgeSession, stream: int, count: int,
                        timeout: float = 10.0) -> list[bytes]:
    queue = session.subscribe_blobs(stream)
    return [await asyncio.wait_for(queue.get(), timeout) for _ in range(count)]


# --------------------------------------------------------------------------- #
# 1. authenticated connection
# --------------------------------------------------------------------------- #
async def test_first_connection_pairs_and_pins(sim_state, pc_config):
    phone = await start_phone(sim_state)
    try:
        session = await make_session(phone, pc_config).open()
        assert session.phone is not None
        assert session.phone.paired is False           # first contact
        assert session.phone.device_id == phone.identity.device_id
        peers = DeviceStore(pc_config).list_peers()
        assert [peer.peer_id for peer in peers] == [phone.identity.device_id]
        assert phone.paired                            # the phone pinned us too
        await session.close()
    finally:
        await phone.stop()


async def test_second_connection_authenticates_without_pairing(sim_state, pc_config):
    phone = await start_phone(sim_state)
    try:
        first = await make_session(phone, pc_config).open()
        await first.close()
        prompts: list[str] = []
        second = await make_session(
            phone, pc_config, confirm_pairing=lambda sas: prompts.append(sas) or True
        ).open()
        assert second.phone.paired is True
        assert prompts == []
        await second.close()
    finally:
        await phone.stop()


async def test_protocol_version_refusal_is_terminal(sim_state, pc_config, monkeypatch):
    """A skewed client must stop, not retry forever against a phone saying no."""
    import phonebridge.core.session as session_module

    phone = await start_phone(sim_state)
    try:
        monkeypatch.setattr(session_module, "PROTOCOL_VERSION", 2)
        with pytest.raises(TerminalSessionError) as caught:
            await make_session(phone, pc_config).open()
        assert "protocol version" in str(caught.value)
    finally:
        await phone.stop()


# --------------------------------------------------------------------------- #
# 2. phone information
# --------------------------------------------------------------------------- #
async def test_system_info_schema(connected):
    _phone, session = connected
    info = await session.call("system.info")
    st._check_types(info, st.INFO_KEYS, "system.info")
    assert info["protocol_version"] == 1
    assert info["device_id"] == session.phone.device_id
    assert info["name"] == session.phone.name


async def test_system_info_device_block_is_complete(connected):
    _phone, session = connected
    info = await session.call("system.info")
    device_block = {
        "model", "manufacturer", "brand", "device", "product", "hardware",
        "android_release", "android_version", "sdk_int", "target_sdk",
        "app_version", "app_version_code", "package", "rom", "rom_version",
        "is_emulator", "protocol_version",
    }
    missing = device_block - set(info)
    assert not missing, f"docs/03 device block keys missing from system.info: {sorted(missing)}"
    for key in ("model", "manufacturer", "android_release", "package"):
        assert isinstance(info[key], str) and info[key]
    for key in ("sdk_int", "target_sdk", "app_version_code"):
        assert isinstance(info[key], int) and not isinstance(info[key], bool)
    assert isinstance(info["is_emulator"], bool)


async def test_handshake_and_rpc_report_the_same_capabilities(connected):
    """The phone sends capabilities twice; they must not disagree."""
    _phone, session = connected
    info = await session.call("system.info")
    assert info["capabilities"] == session.phone.capabilities


# --------------------------------------------------------------------------- #
# 3. capability contract and permission handling
# --------------------------------------------------------------------------- #
async def test_capability_contract_shape(connected):
    _phone, session = connected
    caps = (await session.call("system.info"))["capabilities"]
    for key in st.CAPABILITY_KEYS:
        assert isinstance(caps.get(key), bool), f"{key} must be a boolean"
        assert isinstance(caps.get(f"hardware.{key}"), bool), f"hardware.{key} must be a boolean"
        detail = caps.get(f"detail.{key}")
        assert isinstance(detail, str) and detail.strip(), f"detail.{key} must explain itself"
        if caps[key]:
            assert caps[f"hardware.{key}"], f"{key} cannot be usable without hardware"


async def test_capability_usability_follows_the_permission(sim_state, pc_config):
    """implemented AND hardware AND permitted - and the hardware row never moves.

    The simulated phone implements audio.capture here, which is the only way to
    observe the permission term doing any work: on a phone that does not
    implement the feature yet the answer is `false` whatever the permission says.
    """
    phone = await start_phone(
        sim_state, implemented=frozenset({"audio.capture"})
    )
    try:
        session = await make_session(phone, pc_config).open()
        assert phone.permissions["RECORD_AUDIO"] == "denied"
        denied = await session.call("system.info")
        assert denied["capabilities"]["audio.capture"] is False
        assert "permission RECORD_AUDIO" in denied["capabilities"]["detail.audio.capture"]

        phone.permissions["RECORD_AUDIO"] = "granted"
        granted = await session.call("system.info")
        assert granted["capabilities"]["audio.capture"] is True
        assert granted["capabilities"]["hardware.audio.capture"] is True
        assert granted["permissions"]["RECORD_AUDIO"] == "granted"

        phone.permissions["RECORD_AUDIO"] = "permanently_denied"
        revoked = await session.call("system.info")
        assert revoked["capabilities"]["audio.capture"] is False
        assert revoked["permissions"]["RECORD_AUDIO"] == "permanently_denied"
        # A feature that is not implemented stays false even when permitted.
        assert revoked["capabilities"]["camera.back"] is False
        await session.close()
    finally:
        await phone.stop()


async def test_permissions_report_only_declared_runtime_permissions(connected):
    _phone, session = connected
    permissions = (await session.call("system.info"))["permissions"]
    assert set(permissions) <= PHONE_PERMISSIONS
    assert set(permissions.values()) <= st.PERMISSION_STATES
    # A permission the app never declared must not be invented.
    assert "CAMERA" not in permissions
    assert "BLUETOOTH_SCAN" not in permissions


async def test_permission_change_is_visible_without_reconnecting(connected):
    """Revocation while connected: the next system.info reports the new state."""
    phone, session = connected
    phone.permissions["BLUETOOTH_CONNECT"] = "permanently_denied"
    info = await session.call("system.info")
    assert info["permissions"]["BLUETOOTH_CONNECT"] == "permanently_denied"


# --------------------------------------------------------------------------- #
# 4. session stats, headset, relay, probe report
# --------------------------------------------------------------------------- #
async def test_session_stats_schema_and_live_counters(connected):
    _phone, session = connected
    before = await session.call("session.stats")
    st._check_types(before, st.STATS_KEYS, "session.stats")
    assert before["connections"] >= 1

    await session.send_blob(b"counter-food", stream_id=1)
    await collect_blobs(session, 1, 1)
    await session.ping()

    after = await session.call("session.stats")
    assert after["frames_in"] > before["frames_in"]
    assert after["bytes_in"] > before["bytes_in"]
    assert after["frames_out"] > 0 and after["bytes_out"] > 0


async def test_headset_status_schema(connected):
    _phone, session = connected
    status = await session.call("bluetooth.headset.status")
    st._check_types(status, st.HEADPHONE_KEYS, "bluetooth.headset.status")
    for device in status["devices"]:
        st._check_types(device, {"id": int, "type": str}, "headset.devices[]")
        assert isinstance(device.get("sample_rates"), list)


async def test_relay_state_is_an_honest_placeholder(connected):
    _phone, session = connected
    route = await session.call("audio.route.bluetooth")
    assert isinstance(route["supported"], bool)
    assert route["detail"], "a placeholder must explain itself"
    # Phase 2 has not landed: the phone may report *support*, never a working route.
    assert "Phase 2" in route["detail"] or "probe" in route["detail"]


async def test_probe_report_absent_then_stored_intact(connected):
    _phone, session = connected
    empty = await session.call("audio.capabilities.last")
    assert empty["available"] is False
    assert isinstance(empty.get("reason"), str)

    report = await session.call("audio.capabilities.run", {"seconds": 5}, timeout=60)
    assert compat.validate_report(report) == []

    stored = await session.call("audio.capabilities.last")
    assert stored["available"] is True
    assert stored["report"] == report, "the stored report must be the one that was measured"


# --------------------------------------------------------------------------- #
# 5. data integrity
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("size", [1, 17, 1024, 4096, 65536, 1 << 20])
async def test_blob_echo_is_byte_exact(connected, size):
    _phone, session = connected
    payload = os.urandom(size)
    queue = session.subscribe_blobs(1)
    await session.send_blob(payload, stream_id=1)
    echoed = await asyncio.wait_for(queue.get(), 20)
    assert echoed == payload
    assert len(echoed) == size


async def test_zero_length_blob_round_trips(connected):
    _phone, session = connected
    queue = session.subscribe_blobs(1)
    await session.send_blob(b"", stream_id=1)
    echoed = await asyncio.wait_for(queue.get(), 10)
    assert echoed == b""


async def test_stream_ids_are_isolated(connected):
    _phone, session = connected
    one = session.subscribe_blobs(1)
    two = session.subscribe_blobs(2)
    await session.send_blob(b"belongs-to-two", stream_id=2)
    assert await asyncio.wait_for(two.get(), 10) == b"belongs-to-two"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(one.get(), 0.3)


async def test_interleaved_streams_arrive_on_their_own_stream(connected):
    _phone, session = connected
    one = session.subscribe_blobs(1)
    three = session.subscribe_blobs(3)
    sent_one = [f"one-{index}".encode() for index in range(5)]
    sent_three = [f"three-{index}".encode() for index in range(5)]
    for a, b in zip(sent_one, sent_three, strict=True):
        await session.send_blob(a, stream_id=1)
        await session.send_blob(b, stream_id=3)
    got_one = [await asyncio.wait_for(one.get(), 10) for _ in sent_one]
    got_three = [await asyncio.wait_for(three.get(), 10) for _ in sent_three]
    assert got_one == sent_one
    assert got_three == sent_three


# --------------------------------------------------------------------------- #
# 6. failure handling
# --------------------------------------------------------------------------- #
async def test_unknown_method_returns_method_not_found_and_keeps_the_session(connected):
    _phone, session = connected
    with pytest.raises(SessionError) as caught:
        await session.call("camera.capture", {"front": True})
    assert str(caught.value).startswith("method_not_found")
    assert (await session.ping()) > 0


async def test_bad_parameters_are_reported_as_bad_params(connected):
    phone, session = connected

    def rejects(_params):
        raise ProtocolError("seconds must be 5-300")

    phone.router.register("test.bad_params", rejects)
    with pytest.raises(SessionError) as caught:
        await session.call("test.bad_params")
    assert str(caught.value).startswith("bad_params")
    assert "seconds must be 5-300" in str(caught.value)


async def test_a_crashing_handler_becomes_an_internal_error(connected):
    phone, session = connected

    def explodes(_params):
        raise ValueError("handler blew up")

    phone.router.register("test.explodes", explodes)
    with pytest.raises(SessionError) as caught:
        await session.call("test.explodes")
    assert str(caught.value).startswith("internal")
    assert "handler blew up" in str(caught.value)


async def test_a_client_timeout_fails_the_call_not_the_session(connected):
    """A sub-microsecond limit cannot be met by any round trip, so it always fires.

    (A slow *handler* cannot be used here: the reference phone runs in the same
    event loop as the client, so blocking it would block the client's clock too.)
    """
    _phone, session = connected
    with pytest.raises(SessionError) as caught:
        await session.call("system.echo", {"value": 1}, timeout=st.IMPOSSIBLE_TIMEOUT_S)
    assert "did not answer" in str(caught.value)
    await asyncio.sleep(0.2)  # the late reply must be ignored, not crash anything
    assert (await session.ping()) > 0, "a timed-out request must not kill the session"


async def test_a_dropped_link_fails_quickly_instead_of_hanging(connected):
    """After the phone goes away, a call must fail at once, not sit on a timeout.

    (An RPC that is *already* in flight when the socket dies is covered by
    tests/test_regressions.py::test_ping_fails_fast_when_session_closes; on
    loopback there is no window in which a request is still unanswered and the
    socket is still open.)
    """
    phone, session = connected
    await phone.connections[0][0].close(code=1001, reason="network gone")
    await asyncio.wait_for(session.wait_closed(), 5)
    started = time.perf_counter()
    with pytest.raises(SessionError):
        await session.call("system.info", timeout=30)
    assert time.perf_counter() - started < 1.0, "a dead session must not wait for the timeout"


async def test_calls_on_a_closed_session_are_refused(sim_state, pc_config):
    phone = await start_phone(sim_state)
    try:
        session = await make_session(phone, pc_config).open()
        await session.close()
        with pytest.raises(SessionError):
            await session.call("system.info")
    finally:
        await phone.stop()


# --------------------------------------------------------------------------- #
# 7. the verification matrix itself
# --------------------------------------------------------------------------- #
async def test_selftest_passes_every_supported_workflow(connected):
    _phone, session = connected
    rows = await st.run_selftest(session, frames=4)
    failures = [row for row in rows if row.status == st.FAIL]
    assert not failures, "\n".join(f"{row.workflow}: {row.evidence}" for row in failures)
    supported = [row for row in rows if row.category == "supported"]
    assert len(supported) == 12
    assert all(row.status == st.PASS for row in supported)
    assert all(row.evidence for row in supported), "a PASS row must say what happened"


async def test_selftest_marks_unimplemented_workflows_without_counting_them(connected):
    _phone, session = connected
    rows = await st.run_selftest(session, frames=2)
    unsupported = [row for row in rows if row.category == "unsupported"]
    assert {row.status for row in unsupported} == {st.NOT_IMPLEMENTED}
    areas = " ".join(row.workflow for row in unsupported)
    for area in ("file transfer", "sensor"):
        assert area in areas
    assert "audio" not in areas, "Phase 2 implemented audio: it is no longer unimplemented"
    assert "camera" not in areas, "Phase 4 implemented the camera: it is no longer unimplemented"
    assert st.summarise(rows)[st.NOT_IMPLEMENTED] == len(st.UNSUPPORTED_WORKFLOWS)


async def test_selftest_reports_a_phone_that_cannot_answer(connected):
    phone, session = connected
    del phone.router.handlers["session.stats"]
    rows = {row.workflow: row for row in await st.run_selftest(session, frames=2)}
    assert rows["session statistics"].status == st.FAIL
    assert "method_not_found" in rows["session statistics"].evidence
    # Everything else still ran: one broken method must not abort the matrix.
    assert rows["ping round trip"].status == st.PASS


async def test_selftest_catches_a_capability_that_contradicts_the_hardware(connected):
    phone, session = connected
    original = phone.capabilities

    def lying() -> dict:
        caps = original()
        caps["hardware.camera.back"] = False
        caps["camera.back"] = True          # usable without hardware: a lie
        return caps

    phone.capabilities = lying
    rows = {row.workflow: row for row in await st.run_selftest(session, frames=2)}
    assert rows["capability contract"].status == st.FAIL
    assert "hardware" in rows["capability contract"].evidence
