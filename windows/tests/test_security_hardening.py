"""Security regressions: pairing, authentication, exposure, resource limits.

Every test here exists because of a specific finding in the security review
(``docs/13-security-review.md``) and fails against the code as it was before the
fix. Nothing here tests "the code looks right": each one drives the real
implementation -- a real WebSocket, a real P-256 handshake, real AES-GCM -- and
asserts on the behaviour that must not regress.

Two kinds of test live here:

* **behavioural** (most of them): they attack the PC client or the reference
  phone and watch the attack fail;
* **source-level** (the ``test_android_*`` ones): the Kotlin app cannot be built
  or run on this machine, so the Android-side hardening is pinned by asserting
  the guard exists in the right function -- and, where the property is shared,
  the behavioural test above proves the *protocol* side of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import pytest
from phone_sim import SimulatedPhone, _identity_for
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from phonebridge.core.device_store import DeviceStore
from phonebridge.core.sanitize import device_label
from phonebridge.core.session import (
    BridgeSession,
    PairingRejected,
    SessionError,
    TerminalSessionError,
)
from phonebridge.protocol.crypto import (
    HandshakeInitiator,
    HandshakeResponder,
    Identity,
    IdentityBindingError,
    SecureCodec,
    device_id_for,
)
from phonebridge.protocol.framing import (
    KIND_HANDSHAKE,
    MAX_ENVELOPE_BODY,
    decode_envelope,
    encode_json_envelope,
)
from phonebridge.protocol.messages import (
    MSG_CONTROL,
    control_event,
    handshake_msg,
)
from phonebridge.transport.discovery import _BeaconProtocol
from phonebridge.transport.link import websocket_uri

REPO = Path(__file__).resolve().parents[2]
KOTLIN = REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
async def start_phone(sim_state, *args, **kwargs) -> SimulatedPhone:
    ident = _identity_for(sim_state / "phone_identity.pem", kwargs.pop("name", "Pixel-test"))
    phone = SimulatedPhone(
        identity=ident,
        state_file=sim_state / "paired.json",
        host="127.0.0.1",
        port=0,
        **kwargs,
    )
    await phone.start()
    return phone


def make_session(phone: SimulatedPhone, pc_config, **kwargs) -> BridgeSession:
    store = DeviceStore(pc_config)
    ident = store.load_or_create_identity()
    kwargs.setdefault("confirm_pairing", lambda _sas: True)
    return BridgeSession(websocket_uri("127.0.0.1", phone.port), ident, store, **kwargs)


@dataclass
class FakePhone:
    """A phone that speaks the handshake and then breaks one rule on purpose.

    Used for the checks the PC must perform *itself*: a hostile peer is exactly
    the peer that will not hold up its end of the protocol.
    """

    identity: Identity
    ack_id: str | None = None       # lie about the id in HELLO_ACK
    reply_id: str | None = None     # lie about the id in the authenticated reply
    chatter: int = 0                # sealed junk frames before the real reply
    reply_kind: str = "pair_required"
    port: int = 0
    _server: object | None = None

    async def start(self) -> None:
        self._server = await ws_serve(
            self._handle, "127.0.0.1", 0, max_size=MAX_ENVELOPE_BODY + 64
        )
        for sock in self._server.sockets:
            self.port = sock.getsockname()[1]
            break

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, ws) -> None:  # noqa: ANN001 - websockets handler
        responder = HandshakeResponder(self.identity)
        env = decode_envelope(await ws.recv())
        ack = responder.on_hello(env, known_peer=False)
        if self.ack_id is not None:
            msg = decode_envelope(ack).as_json()
            msg["id"] = self.ack_id
            ack = encode_json_envelope(KIND_HANDSHAKE, msg)
        await ws.send(ack)
        assert responder.keys is not None
        codec = SecureCodec.for_server(responder.keys)
        codec.unseal(decode_envelope(await ws.recv()))  # the PC's auth message
        for _ in range(self.chatter):
            await ws.send(codec.seal(MSG_CONTROL, 0, control_event("noise", {})))
        await ws.send(
            codec.seal(
                MSG_CONTROL,
                0,
                handshake_msg(
                    self.reply_kind,
                    id=self.reply_id or self.identity.device_id,
                    name="impostor-phone",
                    sas=responder.keys.sas,
                    capabilities={},
                ),
            )
        )
        try:  # noqa: SIM105 - hold the socket open until the client gives up
            await asyncio.wait_for(ws.recv(), 5.0)
        except (asyncio.TimeoutError, ConnectionClosed, Exception):  # noqa: BLE001
            pass


async def fake_phone(**kwargs) -> FakePhone:
    phone = FakePhone(identity=Identity.generate("impostor-phone"), **kwargs)
    await phone.start()
    return phone


# --------------------------------------------------------------------------- #
# 1. the device id must certify the key (no impersonation)
# --------------------------------------------------------------------------- #
def test_a_device_id_is_the_hash_of_its_public_key() -> None:
    ident = Identity.generate("pc")
    assert ident.device_id == device_id_for(ident.public_raw)
    assert len(ident.device_id) == 16
    assert ident.device_id == ident.device_id.lower()


def test_hello_ack_with_someone_elses_id_is_refused() -> None:
    """A phone claiming an id that is not the hash of its key is impersonating.

    Before the fix the PC looked the id up, found no pin, and went on to trust
    on first use -- so a fresh peer could present itself under any id it liked,
    including one the user had pinned on the *phone* side.
    """
    phone = Identity.generate("phone")
    initiator = HandshakeInitiator(Identity.generate("pc"))
    responder = HandshakeResponder(phone)
    ack = decode_envelope(responder.on_hello(decode_envelope(initiator.hello()), known_peer=False)).as_json()
    ack["id"] = "0123456789abcdef"  # valid shape, wrong key
    with pytest.raises(IdentityBindingError, match="does not match its identity key"):
        initiator.on_hello_ack(decode_envelope(encode_json_envelope(KIND_HANDSHAKE, ack)))


@pytest.mark.parametrize("bad_id", ["", "NOTAHASH", "0123456789abcde", "0123456789abcdef0", 12345, None])
def test_hello_ack_with_a_malformed_id_is_refused(bad_id: object) -> None:
    phone = Identity.generate("phone")
    initiator = HandshakeInitiator(Identity.generate("pc"))
    responder = HandshakeResponder(phone)
    ack = decode_envelope(responder.on_hello(decode_envelope(initiator.hello()), known_peer=False)).as_json()
    ack["id"] = bad_id
    with pytest.raises(IdentityBindingError, match="malformed device id"):
        initiator.on_hello_ack(decode_envelope(encode_json_envelope(KIND_HANDSHAKE, ack)))


def test_the_phone_side_also_requires_the_id_to_certify_the_key() -> None:
    """HELLO from a PC that claims another PC's id must be refused."""
    pc = Identity.generate("pc")
    initiator = HandshakeInitiator(pc)
    hello = decode_envelope(initiator.hello()).as_json()
    hello["id"] = "ffffffffffffffff"
    responder = HandshakeResponder(Identity.generate("phone"))
    with pytest.raises(IdentityBindingError, match="does not match its identity key"):
        responder.on_hello(
            decode_envelope(encode_json_envelope(KIND_HANDSHAKE, hello)), known_peer=False
        )


# --------------------------------------------------------------------------- #
# 2. the peer must stay the same device for the whole handshake
# --------------------------------------------------------------------------- #
async def test_a_phone_that_changes_identity_mid_handshake_is_refused(pc_config) -> None:
    """HELLO_ACK and the authenticated reply must name the same device."""
    phone = await fake_phone(reply_id="fedcba9876543210")
    store = DeviceStore(pc_config)
    session = BridgeSession(
        websocket_uri("127.0.0.1", phone.port),
        store.load_or_create_identity(),
        store,
        confirm_pairing=lambda _sas: True,
        request_timeout=5.0,
    )
    try:
        with pytest.raises(SessionError, match="identified itself inconsistently"):
            await session.open()
    finally:
        await session.close()
        await phone.stop()


# --------------------------------------------------------------------------- #
# 3. unauthenticated chatter cannot cost unbounded work
# --------------------------------------------------------------------------- #
async def test_flooding_the_handshake_fails_fast(pc_config) -> None:
    """Everything before auth is unauthenticated: it gets a fixed budget.

    The budget is what stops a peer that opens a socket and streams frames from
    buying CPU for free -- and it must trip *well* before the request timeout.
    """
    phone = await fake_phone(chatter=200, reply_kind="pair_required")
    store = DeviceStore(pc_config)
    session = BridgeSession(
        websocket_uri("127.0.0.1", phone.port),
        store.load_or_create_identity(),
        store,
        confirm_pairing=lambda _sas: True,
        request_timeout=20.0,  # the budget must bite long before this
    )
    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises(SessionError, match="without finishing the handshake"):
            await session.open()
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 10.0, f"the flood was tolerated for {elapsed:.1f}s"
    finally:
        await session.close()
        await phone.stop()


# --------------------------------------------------------------------------- #
# 4. the pin list must fail closed
# --------------------------------------------------------------------------- #
async def test_a_corrupt_pin_store_refuses_to_connect(sim_state, pc_config) -> None:
    """An unreadable peers.json used to mean "no phones are pinned".

    That silently dropped every pin and fell back to trust-on-first-use for a
    phone that had already been verified -- a fail-open security control. It now
    refuses, before any handshake message is sent.
    """
    phone = await start_phone(sim_state)
    (pc_config / "peers.json").write_text("{ this is not json", "utf-8")
    session = make_session(phone, pc_config)
    try:
        with pytest.raises(TerminalSessionError, match="corrupt"):
            await session.open()
        assert phone.pairing_requests == 0, "pairing must not start without a pin store"
    finally:
        await session.close()
        await phone.stop()


def test_pin_store_rejects_a_truncated_key(pc_config) -> None:
    store = DeviceStore(pc_config)
    with pytest.raises(ValueError, match="65-byte"):
        store.pin_peer("0123456789abcdef", "phone", b"\x04" * 10)


async def test_a_paired_phone_is_repinned_if_the_local_pin_was_lost(sim_state, pc_config) -> None:
    """Losing peers.json must not leave the PC trusting the phone unpinned.

    The phone answers ``auth_ok`` only when it has *us* pinned, so the key we
    just proved belongs to a phone that already trusts this PC; recording it
    restores mutual pinning instead of silently running unpinned for ever.
    """
    phone = await start_phone(sim_state)
    first = await make_session(phone, pc_config).open()
    pinned = DeviceStore(pc_config).find_peer(phone.identity.device_id)
    assert pinned is not None, "pairing must have pinned the phone"
    await first.close()

    (pc_config / "peers.json").unlink()  # as if the profile was restored/erased
    second = await make_session(phone, pc_config).open()
    try:
        assert second.phone is not None and second.phone.paired is True
        again = DeviceStore(pc_config).find_peer(phone.identity.device_id)
        assert again is not None, "the authenticated key must be re-pinned"
        assert again.static_key == pinned.static_key
    finally:
        await second.close()
        await phone.stop()


# --------------------------------------------------------------------------- #
# 5. the pairing code is for the screen, not the log
# --------------------------------------------------------------------------- #
async def test_the_pairing_code_never_reaches_the_log(sim_state, pc_config, caplog) -> None:
    phone = await start_phone(sim_state)
    session = make_session(phone, pc_config)
    with caplog.at_level(logging.DEBUG, logger="phonebridge.session"):
        try:
            await session.open()
            assert session.phone is not None
            digits = session.phone.sas_digits.replace(" ", "")
            assert len(digits) == 6
            assert digits not in caplog.text, "the six-digit code was written to the log"
        finally:
            await session.close()
            await phone.stop()


# --------------------------------------------------------------------------- #
# 6. attacker-chosen text is neutralised before it is displayed or stored
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Pixel 7", "Pixel 7"),
        ("\x1b[2J\x1b[31mred\x1b[0m", "red"),
        ("line1\r\nline2", "line1line2"),
        ("a\x00b\x07c", "abc"),
        ("   padded   name   ", "padded name"),
        ("x" * 500, "x" * 64),
        ("\x1b[2J", "unnamed"),
        (None, "unnamed"),
        (12345, "12345"),
    ],
)
def test_device_label_is_safe_for_a_terminal_and_a_log(raw: object, expected: str) -> None:
    assert device_label(raw) == expected


async def test_a_hostile_phone_name_is_stored_clean(sim_state, pc_config) -> None:
    """The name a phone reports is displayed by every later ``peers`` command."""
    hostile = "\x1b[2J\x1b[31mEvil\r\nINFO fake log line\x00"
    phone = await start_phone(sim_state, name=hostile)
    session = await make_session(phone, pc_config).open()
    try:
        assert session.phone is not None
        assert "\x1b" not in session.phone.name
        assert "\n" not in session.phone.name and "\r" not in session.phone.name
        stored = DeviceStore(pc_config).find_peer(phone.identity.device_id)
        assert stored is not None
        assert "\x1b" not in stored.peer_name and "\n" not in stored.peer_name
    finally:
        await session.close()
        await phone.stop()


# --------------------------------------------------------------------------- #
# 7. the discovery beacon is unauthenticated input
# --------------------------------------------------------------------------- #
def test_beacons_with_hostile_fields_are_ignored_or_cleaned() -> None:
    table: dict[str, object] = {}
    proto = _BeaconProtocol(table, lambda _phone: None)

    def beacon(payload: dict) -> bytes:
        return json.dumps(payload).encode("utf-8")

    good_id = "0123456789abcdef"
    # oversized datagram
    proto.datagram_received(b"x" * 4096, ("10.0.0.1", 47823))
    # wrong magic / version
    proto.datagram_received(beacon({"app": "not-phonebridge", "v": 1, "id": good_id}), ("10.0.0.1", 1))
    proto.datagram_received(beacon({"app": "phonebridge", "v": 2, "id": good_id}), ("10.0.0.1", 1))
    # malformed ids and ports
    for bad in (
        {"app": "phonebridge", "v": 1, "id": "short", "port": 8765},
        {"app": "phonebridge", "v": 1, "id": good_id.upper(), "port": 8765},
        {"app": "phonebridge", "v": 1, "id": good_id, "port": 0},
        {"app": "phonebridge", "v": 1, "id": good_id, "port": True},
        {"app": "phonebridge", "v": 1, "id": good_id, "port": "8765"},
    ):
        proto.datagram_received(beacon(bad), ("10.0.0.1", 47823))
    assert table == {}, f"hostile beacons were accepted: {table}"

    # a real beacon is accepted, with its display text cleaned
    proto.datagram_received(
        beacon(
            {
                "app": "phonebridge",
                "v": 1,
                "id": good_id,
                "name": "\x1b[31mPixel\x1b[0m\n7",
                "port": 8765,
                "method": "wifi" * 20,
            }
        ),
        ("10.0.0.7", 47823),
    )
    assert list(table) == [good_id]
    found = table[good_id]
    assert found.name == "Pixel7", found.name
    assert "\n" not in found.name
    assert len(found.method) <= 16
    assert found.host == "10.0.0.7", "the source address must be used, not the claimed one"


# --------------------------------------------------------------------------- #
# 8. the phone side: browser origins, size, pairing hygiene, probe gating
# --------------------------------------------------------------------------- #
async def test_the_phone_refuses_a_browser_origin(sim_state) -> None:
    """A web page must not be able to drive the bridge.

    WebSocket bypasses the same-origin policy, so any page could otherwise open
    ``ws://<phone>:8765``, hand over its own key pair and make the phone show a
    pairing prompt for the attacker. Browsers always send ``Origin``; native
    clients never do.
    """
    phone = await start_phone(sim_state)
    try:
        async with ws_connect(
            websocket_uri("127.0.0.1", phone.port), origin="http://evil.example"
        ) as ws:
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 5.0)
            assert ws.close_code == 1008
            assert "origin" in (ws.close_reason or "").lower()
        assert phone.rejected_origins == 1
        assert phone.handshakes == 0, "a rejected origin must not reach the handshake"

        # ...and the same phone still accepts a native client (no Origin header).
        session = await make_session(phone, sim_state).open()
        try:
            assert session.phone is not None
            assert session.phone.device_id == phone.identity.device_id
        finally:
            await session.close()
    finally:
        await phone.stop()


async def test_the_phone_refuses_an_oversized_frame(sim_state) -> None:
    """One frame must never cost more than the framing ceiling."""
    phone = await start_phone(sim_state)
    ws = await ws_connect(websocket_uri("127.0.0.1", phone.port), close_timeout=1)
    try:
        with pytest.raises((ConnectionClosed, OSError)):
            await ws.send(b"\x00" * (MAX_ENVELOPE_BODY + 1024))
            await asyncio.wait_for(ws.recv(), 5.0)
        assert ws.close_code == 1009, f"expected 1009 (too big), got {ws.close_code}"
        assert phone.frames_in == 0, "an oversized frame must not be counted as traffic"
    finally:
        # Only close a connection that is still open: closing one whose
        # transport is already gone raises inside asyncio, not in our code.
        if ws.state is not State.CLOSED:
            await asyncio.wait_for(ws.close(), 2)
        await phone.stop()


async def test_a_second_probe_is_refused_as_busy_not_queued(sim_state, pc_config) -> None:
    phone = await start_phone(sim_state)
    session = await make_session(phone, pc_config).open()
    try:
        phone._probe_in_flight = True
        with pytest.raises(SessionError, match="busy"):
            await session.call("audio.capabilities.run", {"seconds": 5}, timeout=5.0)
        phone._probe_in_flight = False
        report = await session.call("audio.capabilities.run", {"seconds": 5}, timeout=5.0)
        assert isinstance(report, dict) and report.get("source") == "simulator"
        assert session.is_open, "a busy answer must not close the session"
    finally:
        await session.close()
        await phone.stop()


async def test_an_unanswered_pairing_request_expires_and_cleans_up(sim_state, pc_config) -> None:
    """A prompt nobody answered must expire, not stay answerable for ever."""
    phone = await start_phone(
        sim_state, pair_policy="wait", pairing_window_s=0.2, name="Pixel-slow"
    )
    try:
        session = make_session(phone, pc_config, request_timeout=5.0)
        with pytest.raises(PairingRejected, match="expired"):
            await session.open()
        assert phone._pending_pairings == set(), "the expired request must be forgotten"
        assert phone.pairing_requests == 1
        await session.close()
    finally:
        await phone.stop()


async def test_pending_pairing_requests_are_capped(sim_state, pc_config) -> None:
    """Pairing prompts are a finite resource: a human can only answer one."""
    phone = await start_phone(sim_state, pair_policy="wait", pairing_window_s=5.0)
    phone._pending_pairings.update({"aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb",
                                    "cccccccccccccccc", "dddddddddddddddd"})
    try:
        session = make_session(phone, pc_config, request_timeout=5.0)
        with pytest.raises((PairingRejected, SessionError), match="too many pending"):
            await session.open()
        await session.close()
    finally:
        await phone.stop()


# --------------------------------------------------------------------------- #
# 9. the Android server: the same guards, pinned by source
# --------------------------------------------------------------------------- #
def test_android_refuses_browser_origins_in_on_open() -> None:
    text = read(KOTLIN / "net" / "BridgeServer.kt")
    on_open = text[text.index("override fun onOpen("):]
    created = on_open.index("byConnection[conn] = PeerSession")
    guard = on_open[:created]
    assert 'getFieldValue("Origin")' in guard, (
        "the phone must inspect the Origin header: WebSocket bypasses CORS, so a "
        "web page can otherwise drive the bridge"
    )
    assert "1008" in guard and "return" in guard
    assert guard.index("Origin") < created, (
        "the origin check must run before a session is created"
    )


def test_android_checks_frame_size_before_copying() -> None:
    text = read(KOTLIN / "net" / "BridgeServer.kt")
    assert "message.remaining() > Framing.MAX_ENVELOPE_BODY" in text
    assert (
        text.index("message.remaining() > Framing.MAX_ENVELOPE_BODY")
        < text.index("val bytes = ByteArray(message.remaining())")
    ), "the size guard must run before the copy it protects"
    assert "1009" in text, "an oversized frame is closed with 1009 (message too big)"


def test_android_drops_pairing_prompts_for_dead_sessions() -> None:
    text = read(KOTLIN / "net" / "BridgeServer.kt")
    assert "MAX_PENDING_PAIRINGS" in text
    close_body = text[text.index("fun close(reason: String"):]
    close_body = close_body[: close_body.index("fun onClosed()")]
    assert "dropPendingPairing(this)" in close_body
    on_closed = text[text.index("fun onClosed()"):]
    on_closed = on_closed[: on_closed.index("// Java-WebSocket plumbing")]
    assert "dropPendingPairing(this)" in on_closed, (
        "a session that dies must not leave its pairing prompt on screen"
    )


def test_android_gates_the_capability_probe() -> None:
    text = read(KOTLIN / "net" / "BridgeServer.kt")
    assert "probeInFlight.compareAndSet(false, true)" in text
    assert '"busy"' in text
    assert text.index("probeInFlight.compareAndSet") < text.index(
        "runCapabilityProbeAsync(id, seconds, acoustic, peer)"
    ), "the guard must be checked before the probe is queued"


def test_android_never_puts_the_pairing_code_in_a_log_line() -> None:
    text = read(KOTLIN / "net" / "BridgeServer.kt")
    for call in _addlog_calls(text):
        assert "sasDigits" not in call, f"pairing code logged: {call[:120]}"
        assert "formatSas" not in call, f"pairing code logged: {call[:120]}"
    # ...but it is still shown to the human, which is the whole point of a SAS.
    assert "PairingRequest(deviceId, name, Crypto.formatSas(sas), token)" in text


def test_android_sanitises_peer_supplied_names() -> None:
    text = read(KOTLIN / "net" / "BridgeServer.kt")
    assert "internal fun safeLabel(" in text
    assert "isISOControl" in text and "ANSI_ESCAPE" in text
    assert "peerName = safeLabel(r.peerName)" in text
    assert "peerName = safeLabel(pcName)" in text


def _addlog_calls(text: str) -> list[str]:
    """Every ``addLog(...)`` call, sliced by balanced parentheses."""
    calls: list[str] = []
    index = 0
    while True:
        start = text.find("addLog(", index)
        if start == -1:
            return calls
        depth = 0
        for pos in range(start + len("addLog"), len(text)):
            if text[pos] == "(":
                depth += 1
            elif text[pos] == ")":
                depth -= 1
                if depth == 0:
                    calls.append(text[start : pos + 1])
                    index = pos + 1
                    break
        else:  # pragma: no cover - unbalanced source
            return calls
