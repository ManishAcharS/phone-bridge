"""Regression tests for the Phase 1 reliability work.

Phase 1 covers connection lifecycle, pairing lifecycle, WebSocket stability,
reconnection, duplicate connections, timeouts/error handling, the Android
background lifecycle and Python-side cleanup. This module tests the half of
that contract that can be exercised without a phone: the Python client and the
simulated phone it talks to.

Every test names the defect it guards against. Most of them are *failed
connection* cases on purpose — a client that only behaves when everything works
is the thing Phase 1 set out to fix.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time

import pytest
from phone_sim import SimulatedPhone, _identity_for
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import ConnectionClosed

from phonebridge.core.device_store import DeviceStore
from phonebridge.core.session import (
    BackoffPolicy,
    BridgeSession,
    ReconnectingSession,
    SessionError,
    TerminalSessionError,
    _normalise_reply,
)
from phonebridge.protocol.crypto import PeerKeyChangedError, ProtocolVersionError
from phonebridge.protocol.framing import (
    KIND_CLOSE,
    encode_envelope,
    encode_json_envelope,
)
from phonebridge.protocol.messages import MSG_BYE
from phonebridge.transport.link import LinkError, WebSocketLink, websocket_uri


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _phone(sim_state, **kwargs) -> SimulatedPhone:
    ident = _identity_for(sim_state / "phone_identity.pem", "Pixel-test")
    kwargs.setdefault("pair_policy", "auto")
    phone = SimulatedPhone(
        identity=ident,
        state_file=sim_state / "paired.json",
        host="127.0.0.1",
        port=0,
        **kwargs,
    )
    await phone.start()
    return phone


def _session_for(store: DeviceStore, uri: str, **kwargs) -> BridgeSession:
    kwargs.setdefault("confirm_pairing", lambda _sas: True)
    return BridgeSession(uri, store.load_or_create_identity(), store, **kwargs)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeLink:
    """A link that yields pre-canned frames and records what was sent."""

    def __init__(self, frames: list[bytes] | None = None) -> None:
        self.frames = list(frames or [])
        self.sent: list[bytes] = []
        self.closed = False
        self.closed_reason: str | None = None

    async def open(self) -> None:
        return None

    async def send(self, data: bytes) -> None:
        if self.closed:
            raise LinkError("link is not open")
        self.sent.append(data)

    async def recv(self) -> bytes:
        if not self.frames:
            raise LinkError("link closed: no close frame received or sent")
        return self.frames.pop(0)

    async def close(self, reason: str = "client closing") -> None:
        self.closed = True
        self.closed_reason = reason

    @property
    def close_info(self) -> tuple[int | None, str]:
        return (None, "") if not self.closed else (1000, self.closed_reason or "")

    @property
    def is_open(self) -> bool:
        return not self.closed


# --------------------------------------------------------------------------- #
# 1. the handshake itself must time out
# --------------------------------------------------------------------------- #
async def test_silent_phone_cannot_hang_open_forever(pc_config):
    """Defect: every setup step had a timeout except the very first one.

    `open()` waited on HELLO_ACK with a bare `recv()`, so a peer that accepted
    the TCP connection and then said nothing held the PC forever.
    """

    release = asyncio.Event()

    async def silent(ws):
        # Accept the WebSocket, then never say anything.
        await release.wait()

    async with ws_serve(silent, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        store = DeviceStore(pc_config)
        session = _session_for(store, websocket_uri("127.0.0.1", port), request_timeout=0.3)
        started = time.monotonic()
        try:
            with pytest.raises(SessionError, match="HELLO_ACK"):
                await session.open()
            assert time.monotonic() - started < 5, "the handshake timeout did not fire"
        finally:
            # ws_serve waits for its handlers before shutting down, so the
            # deliberately silent handler must be released first.
            release.set()


# --------------------------------------------------------------------------- #
# 2. permanent disagreements must not be retried forever
# --------------------------------------------------------------------------- #
async def test_changed_phone_key_is_terminal(sim_state, pc_config):
    """Defect: a swapped identity key looked like an ordinary handshake error.

    The supervisor then retried it without limit, which is both useless and the
    wrong thing to do when someone may be impersonating the phone.
    """
    phone = await _phone(sim_state)
    try:
        store = DeviceStore(pc_config)
        session = _session_for(store, websocket_uri("127.0.0.1", phone.port))
        await session.open()
        await session.close()
        # Someone replaced the key behind the pinned device id.
        store.pin_peer(phone.identity.device_id, "Pixel-test", b"\x00" * 65)

        again = _session_for(store, websocket_uri("127.0.0.1", phone.port))
        with pytest.raises(TerminalSessionError) as info:
            await again.open()
        assert isinstance(info.value.__cause__, PeerKeyChangedError)
    finally:
        await phone.stop()


async def test_supervisor_stops_after_a_changed_key(sim_state, pc_config):
    """The supervisor must emit a terminal `closed`, not loop on `reconnecting`."""
    phone = await _phone(sim_state)
    try:
        store = DeviceStore(pc_config)
        session = await _session_for(store, websocket_uri("127.0.0.1", phone.port)).open()
        await session.close()
        store.pin_peer(phone.identity.device_id, "Pixel-test", b"\x00" * 65)

        states: list[tuple[str, dict]] = []
        sup = ReconnectingSession(
            websocket_uri("127.0.0.1", phone.port),
            store.load_or_create_identity(),
            store,
            on_state=lambda state, details: states.append((state, details)),
            policy=BackoffPolicy(initial_s=0.01, max_s=0.02, jitter=0.0),
            max_attempts=5,
            confirm_pairing=lambda _s: True,
        )
        await sup.start()
        for _ in range(100):
            if any(state == "closed" for state, _ in states):
                break
            await asyncio.sleep(0.05)
        await sup.stop()

        names = [state for state, _ in states]
        assert "closed" in names, names
        closed = next(details for state, details in states if state == "closed")
        assert closed.get("terminal") is True, closed
        assert names.count("connecting") == 1, f"retried a terminal failure: {names}"
    finally:
        await phone.stop()


async def test_protocol_version_skew_is_terminal(sim_state, pc_config):
    """Defect: a version mismatch was folded into a retryable SessionError."""
    phone = await _phone(sim_state)
    try:
        store = DeviceStore(pc_config)
        session = _session_for(store, websocket_uri("127.0.0.1", phone.port))

        async def skewed() -> None:
            raise ProtocolVersionError("phone speaks protocol 2, this client speaks 1")

        session._handshake = skewed  # type: ignore[method-assign]
        with pytest.raises(TerminalSessionError, match="protocol 2"):
            await session.open()
    finally:
        await phone.stop()


async def test_auth_failure_is_terminal(sim_state, pc_config, monkeypatch):
    """Defect: `auth_fail` was retried, so a refused PC reconnected forever."""
    import phone_sim

    from phonebridge.protocol.messages import handshake_msg

    async def refuse(self, ws, codec, responder, hello, known):
        # Read the PC's auth frame exactly like the real implementation, then
        # refuse it inside the encrypted channel.
        await self._recv_setup(ws)
        await phone_sim._send(ws, codec, handshake_msg("auth_fail", reason="not today"))
        await ws.close(code=1008, reason="auth refused")
        return False

    monkeypatch.setattr(phone_sim.SimulatedPhone, "_authenticate", refuse)
    phone = await _phone(sim_state)
    try:
        store = DeviceStore(pc_config)
        session = _session_for(store, websocket_uri("127.0.0.1", phone.port))
        with pytest.raises(TerminalSessionError, match="refused authentication"):
            await session.open()
    finally:
        await phone.stop()


async def test_declined_pairing_is_terminal_in_the_supervisor(sim_state, pc_config):
    """Defect: a refused pairing could re-prompt the user in a loop."""
    phone = await _phone(sim_state, pair_policy="deny")
    try:
        store = DeviceStore(pc_config)
        states: list[str] = []
        sup = ReconnectingSession(
            websocket_uri("127.0.0.1", phone.port),
            store.load_or_create_identity(),
            store,
            on_state=lambda state, _d: states.append(state),
            policy=BackoffPolicy(initial_s=0.01, max_s=0.02, jitter=0.0),
            max_attempts=5,
            confirm_pairing=lambda _s: True,
        )
        await sup.start()
        for _ in range(120):
            if "closed" in states:
                break
            await asyncio.sleep(0.05)
        await sup.stop()
        assert "closed" in states, states
        assert states.count("connecting") == 1, f"pairing refusal was retried: {states}"
    finally:
        await phone.stop()


# --------------------------------------------------------------------------- #
# 3. BYE is a clean shutdown, on both ends
# --------------------------------------------------------------------------- #
async def test_phone_bye_closes_the_session_with_its_reason(sim_state, pc_config):
    """Defect: the PC logged BYE and carried on with a dead session."""
    phone = await _phone(sim_state)
    try:
        store = DeviceStore(pc_config)
        session = await _session_for(store, websocket_uri("127.0.0.1", phone.port)).open()
        queue = session.subscribe_blobs(1)

        session._dispatch(MSG_BYE, 0, b"phone restarting")
        reason = await asyncio.wait_for(session.wait_closed(), 2)
        assert reason == "phone restarting"
        assert session.is_open is False
        assert queue.get_nowait() is None, "a BYE must release blob subscribers"
        with pytest.raises(SessionError):
            await session.call("audio.capabilities.last", timeout=0.1)
    finally:
        await phone.stop()


async def test_pc_answers_phone_requests_with_a_response(sim_state, pc_config):
    """Defect: a `req` from the phone was answered with an *event* envelope.

    docs/03-protocol.md §6 says a request is always answered with `res`; the
    phone waited for a response that never arrived.
    """
    phone = await _phone(sim_state)
    try:
        store = DeviceStore(pc_config)
        session = await _session_for(store, websocket_uri("127.0.0.1", phone.port)).open()
        sent: list[bytes] = []

        async def capture(payload: bytes) -> None:
            sent.append(payload)

        session._safe_send_control = capture  # type: ignore[method-assign]
        session._handle_control(
            json.dumps({"k": "req", "id": 7, "method": "no.such.method"}).encode()
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert sent, "the phone's request got no answer"
        reply = json.loads(sent[0])
        assert reply["k"] == "res"
        assert reply["id"] == 7
        assert reply["ok"] is False
        assert reply["error"]["code"] == "method_not_found"
        await session.close()
    finally:
        await phone.stop()


# --------------------------------------------------------------------------- #
# 4. a broken frame must not turn a clean close into an exception
# --------------------------------------------------------------------------- #
async def test_malformed_close_body_still_closes_the_session(pc_config):
    """Defect: a CLOSE whose body was not JSON raised out of the close path."""
    store = DeviceStore(pc_config)
    bad_body = b"not json at all"
    link = FakeLink([encode_envelope(KIND_CLOSE, bad_body)])
    session = BridgeSession("ws://fake", store.load_or_create_identity(), store, link=link)
    await asyncio.wait_for(session._read_loop(), 2)
    assert await session.wait_closed() == "phone closed"


async def test_plaintext_frame_mid_session_closes_instead_of_crashing(pc_config):
    """A plaintext frame after the handshake is a protocol violation, not a crash."""
    store = DeviceStore(pc_config)
    link = FakeLink([encode_json_envelope(KIND_CLOSE, {"reason": "bye"})])
    session = BridgeSession("ws://fake", store.load_or_create_identity(), store, link=link)
    session.opened_at = time.time()
    await asyncio.wait_for(session._read_loop(), 2)
    assert await session.wait_closed() == "bye"


# --------------------------------------------------------------------------- #
# 5. timeouts are honoured, including the explicit zero
# --------------------------------------------------------------------------- #
async def test_explicit_zero_timeout_is_not_replaced_by_the_default(sim_state, pc_config):
    """Defect: `timeout or self.request_timeout` treated 0 as "use the default"."""
    phone = await _phone(sim_state)
    try:
        store = DeviceStore(pc_config)
        session = await _session_for(
            store, websocket_uri("127.0.0.1", phone.port), request_timeout=30
        ).open()
        started = time.monotonic()
        with pytest.raises(SessionError, match="did not answer"):
            await session.call("audio.capabilities.last", timeout=0)
        assert time.monotonic() - started < 5, "timeout=0 fell back to request_timeout"
        await session.close()
    finally:
        await phone.stop()


# --------------------------------------------------------------------------- #
# 6. cleanup: no leaked subscribers, no leaked server-side connections
# --------------------------------------------------------------------------- #
async def test_close_releases_blob_subscribers(sim_state, pc_config):
    """Defect: subscribers blocked in `queue.get()` until their own timeout."""
    phone = await _phone(sim_state)
    try:
        store = DeviceStore(pc_config)
        session = await _session_for(store, websocket_uri("127.0.0.1", phone.port)).open()
        queue = session.subscribe_blobs(9)
        await session.close()
        assert queue.get_nowait() is None
        assert session._read_task is None
    finally:
        await phone.stop()


async def test_phone_drop_releases_blob_subscribers(sim_state, pc_config):
    phone = await _phone(sim_state)
    store = DeviceStore(pc_config)
    session = await _session_for(store, websocket_uri("127.0.0.1", phone.port)).open()
    queue = session.subscribe_blobs(3)
    await phone.stop()
    marker = await asyncio.wait_for(queue.get(), 5)
    assert marker is None


async def test_closing_the_session_clears_it_on_the_phone(sim_state, pc_config):
    """The phone must not keep the connection in its session list after a close."""
    phone = await _phone(sim_state)
    try:
        store = DeviceStore(pc_config)
        session = await _session_for(store, websocket_uri("127.0.0.1", phone.port)).open()
        assert len(phone.connections) == 1
        await session.close()
        for _ in range(60):
            if not phone.connections:
                break
            await asyncio.sleep(0.05)
        assert phone.connections == [], "the phone kept a closed session"
    finally:
        await phone.stop()


# --------------------------------------------------------------------------- #
# 7. duplicate connections and the session ceiling
# --------------------------------------------------------------------------- #
async def test_two_sessions_from_one_identity_are_both_tracked(sim_state, pc_config):
    """Duplicate connections are allowed up to the cap, and both are accounted."""
    phone = await _phone(sim_state, max_sessions_per_device=4)
    try:
        store = DeviceStore(pc_config)
        uri = websocket_uri("127.0.0.1", phone.port)
        first = await _session_for(store, uri).open()
        second = await _session_for(store, uri).open()
        assert len(phone.connections) == 2
        peers = set(phone._peer_by_connection.values())
        assert len(peers) == 1, "both sessions should belong to one peer"
        await first.close()
        await second.close()
    finally:
        await phone.stop()


async def test_session_ceiling_refuses_with_a_reason(sim_state, pc_config):
    """Defect: no ceiling at all, so one peer could open sessions without limit."""
    phone = await _phone(sim_state, max_sessions_per_device=1)
    try:
        store = DeviceStore(pc_config)
        uri = websocket_uri("127.0.0.1", phone.port)
        first = await _session_for(store, uri).open()
        with pytest.raises(SessionError) as info:
            await _session_for(store, uri).open()
        message = str(info.value)
        assert "1008" in message or "too many connections" in message, message
        assert len(phone.connections) == 1
        await first.close()
    finally:
        await phone.stop()


# --------------------------------------------------------------------------- #
# 8. stalled peers do not hold setup slots
# --------------------------------------------------------------------------- #
async def test_stalled_peer_is_dropped_by_the_setup_timeout(sim_state):
    """Defect: a socket that never sent HELLO held a session slot indefinitely."""
    phone = await _phone(sim_state, setup_timeout_s=0.3)
    try:
        async with ws_connect(websocket_uri("127.0.0.1", phone.port)) as ws:
            with pytest.raises(ConnectionClosed) as info:
                await asyncio.wait_for(ws.recv(), 5)
        rcvd = getattr(info.value, "rcvd", None)
        assert rcvd is not None, "the phone closed without a close frame"
        assert rcvd.code == 1002
        assert "setup" in (rcvd.reason or "").lower()
        assert phone.connections == []
    finally:
        await phone.stop()


# --------------------------------------------------------------------------- #
# 9. recovery: a failed connection that later succeeds
# --------------------------------------------------------------------------- #
async def test_supervisor_recovers_when_the_phone_comes_back(sim_state, pc_config):
    """The PC must retry a refused connection and succeed once the phone is up."""
    port = _free_port()
    uri = websocket_uri("127.0.0.1", port)
    store = DeviceStore(pc_config)
    states: list[str] = []
    sup = ReconnectingSession(
        uri,
        store.load_or_create_identity(),
        store,
        on_state=lambda state, _d: states.append(state),
        policy=BackoffPolicy(initial_s=0.05, max_s=0.1, jitter=0.0),
        confirm_pairing=lambda _s: True,
    )
    await sup.start()
    phone = None
    try:
        for _ in range(100):
            if "reconnecting" in states:
                break
            await asyncio.sleep(0.05)
        assert "reconnecting" in states, f"the failure was not reported: {states}"

        ident = _identity_for(sim_state / "phone_identity.pem", "Pixel-test")
        phone = SimulatedPhone(
            identity=ident,
            state_file=sim_state / "paired.json",
            host="127.0.0.1",
            port=port,
            pair_policy="auto",
        )
        await phone.start()
        session = await sup.wait_connected(15)
        assert session.is_open
        assert "connected" in states
    finally:
        await sup.stop()
        if phone is not None:
            await phone.stop()


# --------------------------------------------------------------------------- #
# 10. diagnosability: refusals keep their close code and reason
# --------------------------------------------------------------------------- #
async def test_close_info_exposes_the_peer_close_code(sim_state):
    """`close_info` is what turns "phone closed the connection" into a cause."""
    phone = await _phone(sim_state, setup_timeout_s=0.3)
    link = WebSocketLink(websocket_uri("127.0.0.1", phone.port))
    try:
        await link.open()
        with pytest.raises(LinkError):
            await asyncio.wait_for(link.recv(), 5)
        code, reason = link.close_info
        assert code == 1002
        assert "setup" in reason.lower()
    finally:
        await link.close()
        await phone.stop()


def test_normalise_reply_flattens_a_failed_response():
    """Small unit check for the helper the handshake relies on."""
    reply = _normalise_reply(
        {"k": "res", "ok": False, "error": {"code": "auth", "message": "nope"}}
    )
    assert reply["k"] == "auth_fail"
    assert reply["reason"] == "nope"
