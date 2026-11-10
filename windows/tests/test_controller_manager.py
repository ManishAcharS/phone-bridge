"""The connection state machine.

Everything here runs against a scripted fake session, so the tests can assert the
sequences a real phone rarely produces on demand: a drop after ten seconds, a
terminal key change, a pairing prompt answered late, a heartbeat that goes quiet.

The real protocol is exercised separately in ``test_controller_e2e.py``; this file
is about the decisions.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import threading
import time

import pytest

from phonebridge.controller.manager import ConnectionManager, Snapshot, State
from phonebridge.controller.transports import Candidate
from phonebridge.core.device_store import DeviceStore
from phonebridge.core.session import (
    BackoffPolicy,
    PairingRejected,
    SessionError,
    TerminalSessionError,
)

INFO = {
    "device_id": "abc123def4567890",
    "name": "Redmi Note 14 Pro+",
    "model": "24115RA8EI",
    "manufacturer": "Xiaomi",
    "android_release": "16",
    "sdk_int": 36,
    "app_version": "1.0.0",
    "capabilities": {
        "camera.back": False,
        "hardware.camera.back": True,
        "detail.camera.back": "camera streaming is Phase 4",
        "audio.capture": False,
        "hardware.audio.capture": True,
        "detail.audio.capture": "the microphone relay lands with Phase 2",
    },
    "permissions": {"RECORD_AUDIO": "denied"},
}

FAST = BackoffPolicy(initial_s=0.01, max_s=0.02, jitter=0.0)


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
class FakeSession:
    """Just enough of BridgeSession for the manager, plus a script to act out."""

    def __init__(self, uri: str, behaviour, **kwargs) -> None:
        self.uri = uri
        self.behaviour = behaviour
        self.confirm_pairing = kwargs.get("confirm_pairing")
        self.on_phase = kwargs.get("on_phase")
        self.closed = asyncio.Event()
        self._open = False
        self.ping_error: Exception | None = None
        self.open_attempts = 0
        self.close_reason = ""

    async def open(self):
        self.open_attempts += 1
        if self.on_phase is not None:
            self.on_phase("handshake")
        outcome = self.behaviour(self)  # raises to model a failure
        if inspect.isawaitable(outcome):
            await outcome
        if self.on_phase is not None:
            self.on_phase("ready")
        self._open = True
        return self

    async def call(self, method, params=None):  # noqa: ANN001
        del params
        assert method == "system.info"
        return INFO

    async def ping(self, timeout: float = 5.0) -> float:
        del timeout
        if self.ping_error is not None:
            raise self.ping_error
        return 1.0

    async def wait_closed(self) -> str:
        await self.closed.wait()
        return self.close_reason or "closed"

    async def close(self, reason: str = "closed by PC") -> None:
        if self.closed.is_set():
            return
        self.close_reason = reason
        self._open = False
        self.closed.set()

    @property
    def is_open(self) -> bool:
        return self._open and not self.closed.is_set()

    def drop(self, reason: str = "network gone") -> None:
        self.close_reason = reason
        self._open = False
        self.closed.set()


class Script:
    """A queue of behaviours; each new session consumes the next one."""

    def __init__(self, *behaviours) -> None:
        self.behaviours = list(behaviours) or [succeed]
        self.sessions: list[FakeSession] = []

    def factory(self):
        def make(uri, identity, store, **kwargs):  # noqa: ANN001
            del identity, store
            behaviour = self.behaviours[min(len(self.sessions), len(self.behaviours) - 1)]
            session = FakeSession(uri, behaviour, **kwargs)
            self.sessions.append(session)
            return session

        return make


def succeed(session: FakeSession) -> None:
    del session


def fail(message: str = "connection refused"):
    def behaviour(session: FakeSession) -> None:
        del session
        raise SessionError(message)

    return behaviour


def fail_terminal(message: str = "the phone's key changed"):
    def behaviour(session: FakeSession) -> None:
        del session
        raise TerminalSessionError(message)

    return behaviour


def ask_to_pair(sas: str = "123 456", then=None):
    async def behaviour(session: FakeSession) -> None:
        assert session.confirm_pairing is not None
        approved = session.confirm_pairing(sas)
        if inspect.isawaitable(approved):
            approved = await approved
        if not approved:
            raise PairingRejected("pairing declined on the PC side; nothing was stored")
        if then is not None:
            then(session)
        else:
            succeed(session)

    return behaviour


def reject_pairing(session: FakeSession) -> None:
    del session
    raise PairingRejected("pairing rejected on the phone")


class StaticProvider:
    name = "static"

    def __init__(self, candidates: list[Candidate], *, hold: float = 0.0) -> None:
        self._candidates = candidates
        self.released = 0
        self.hold = hold

    async def candidates(self, *, timeout: float) -> list[Candidate]:
        del timeout
        if self.hold:
            await asyncio.sleep(self.hold)
        return list(self._candidates)

    async def release(self) -> None:
        self.released += 1


async def wait_for(predicate, timeout: float = 4.0) -> bool:
    """Poll a condition that is not a manager state (session count, a flag)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def usb(port: int = 50001) -> Candidate:
    return Candidate("usb", "127.0.0.1", port, 100, "USB via R58M12345")


def wifi(host: str = "192.168.1.50") -> Candidate:
    return Candidate("wifi", host, 8765, 80, "beacon")


def make_manager(pc_config, script: Script, *providers, **kwargs):
    store = DeviceStore(pc_config)
    kwargs.setdefault("discovery_timeout", 0.2)
    kwargs.setdefault("connect_timeout", 1.0)
    kwargs.setdefault("policy", FAST)
    kwargs.setdefault("sleeper", lambda _delay: asyncio.sleep(0))
    manager = ConnectionManager(
        store,
        providers=list(providers) or [StaticProvider([usb()])],
        session_factory=script.factory(),
        **kwargs,
    )
    return manager, store


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #
async def test_connect_reports_every_state_in_order(pc_config) -> None:
    script = Script(succeed)
    manager, _ = make_manager(pc_config, script)
    published: list[State] = []
    manager.subscribe(lambda snapshot: published.append(snapshot.state))

    await manager.connect()
    assert await manager.wait_for_state(State.CONNECTED, timeout=2)
    # states are republished on every log line; the transitions are what matter
    seen = [
        state
        for index, state in enumerate(published)
        if index == 0 or published[index - 1] != state
    ]
    assert seen[:5] == [
        State.DISCONNECTED,  # the state the listener is first given
        State.DISCOVERING,
        State.CONNECTING,
        State.AUTHENTICATING,
        State.CONNECTED,
    ]
    await manager.aclose()


async def test_snapshot_carries_the_phone_and_its_capabilities(pc_config) -> None:
    manager, _ = make_manager(pc_config, Script(succeed))
    await manager.connect()
    assert await manager.wait_for_state(State.CONNECTED, timeout=2)
    snapshot = manager.snapshot
    assert snapshot.device["name"] == "Redmi Note 14 Pro+"
    assert snapshot.transport == "usb"
    assert snapshot.endpoint.endswith("50001")
    assert snapshot.connected_at is not None
    capability = {c.key: c for c in snapshot.capabilities}
    assert capability["camera.back"].present is True
    assert capability["camera.back"].available is False
    assert capability["audio.capture"].permitted is False
    assert "Pinned key verified" in snapshot.auth_line()
    await manager.aclose()


async def test_the_address_is_remembered_for_the_next_launch(pc_config) -> None:
    manager, store = make_manager(pc_config, Script(succeed))
    await manager.connect()
    assert await manager.wait_for_state(State.CONNECTED, timeout=2)
    endpoints = store.known_endpoints()
    assert endpoints and endpoints[0]["host"] == "127.0.0.1"
    assert endpoints[0]["device_id"] == "abc123def4567890"
    assert endpoints[0]["transport"] == "usb"
    await manager.aclose()


async def test_snapshots_are_immutable(pc_config) -> None:
    manager, _ = make_manager(pc_config, Script(succeed))
    snapshot = manager.snapshot
    assert isinstance(snapshot, Snapshot)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.state = State.ERROR  # type: ignore[misc]
    await manager.aclose()


# --------------------------------------------------------------------------- #
# duplicate prevention
# --------------------------------------------------------------------------- #
async def test_a_second_connect_does_not_open_a_second_session(pc_config) -> None:
    script = Script(succeed)
    provider = StaticProvider([usb()], hold=0.05)
    manager, _ = make_manager(pc_config, script, provider)

    await manager.connect()
    await manager.connect()  # a double click
    await manager.connect()
    assert await manager.wait_for_state(State.CONNECTED, timeout=3)
    assert len(script.sessions) == 1, "only one session may exist at a time"
    await manager.aclose()


async def test_connect_while_connected_is_ignored(pc_config) -> None:
    script = Script(succeed)
    manager, _ = make_manager(pc_config, script)
    await manager.connect()
    assert await manager.wait_for_state(State.CONNECTED, timeout=2)
    await manager.connect()
    await asyncio.sleep(0.05)
    assert len(script.sessions) == 1
    await manager.aclose()


# --------------------------------------------------------------------------- #
# failure and fallback
# --------------------------------------------------------------------------- #
async def test_the_next_candidate_is_tried_after_a_transient_failure(pc_config) -> None:
    script = Script(fail("connection refused"), succeed)
    manager, _ = make_manager(
        pc_config, script, StaticProvider([usb(), wifi()])
    )
    await manager.connect()
    assert await manager.wait_for_state(State.CONNECTED, timeout=3)
    assert len(script.sessions) == 2
    assert manager.snapshot.transport == "wifi", "the USB attempt failed, so Wi-Fi won"
    await manager.aclose()


async def test_a_terminal_error_stops_immediately(pc_config) -> None:
    script = Script(fail_terminal("the phone's key changed; re-pair to continue"))
    manager, _ = make_manager(pc_config, script)
    await manager.connect()
    assert await manager.wait_for_state(State.ERROR, timeout=3)
    assert "key changed" in manager.snapshot.message
    await asyncio.sleep(0.1)
    assert len(script.sessions) == 1, "a changed key must not be retried"
    await manager.aclose()


async def test_no_candidates_produces_a_message_a_user_can_act_on(pc_config) -> None:
    manager, _ = make_manager(pc_config, Script(succeed), StaticProvider([]))
    await manager.connect()
    assert await manager.wait_for_state(State.ERROR, timeout=3)
    message = manager.snapshot.message.lower()
    assert "no phone found" in message
    assert "usb" in message and "wi-fi" in message
    await manager.aclose()


async def test_a_broken_provider_does_not_break_discovery(pc_config) -> None:
    class Broken:
        name = "broken"

        async def candidates(self, *, timeout: float):
            del timeout
            raise RuntimeError("port already in use")

    manager, _ = make_manager(pc_config, Script(succeed), Broken(), StaticProvider([usb()]))
    await manager.connect()
    assert await manager.wait_for_state(State.CONNECTED, timeout=3)
    assert any("broken" in line for line in manager.snapshot.diagnostics)
    await manager.aclose()


# --------------------------------------------------------------------------- #
# pairing
# --------------------------------------------------------------------------- #
async def test_pairing_waits_for_the_user_and_then_connects(pc_config) -> None:
    script = Script(ask_to_pair("123456"))
    manager, _ = make_manager(pc_config, script)

    await manager.connect()
    assert await manager.wait_for_state(State.PAIRING_REQUIRED, timeout=2)
    assert manager.snapshot.pairing_code == "123456"

    manager.resolve_pairing(True)
    assert await manager.wait_for_state(State.CONNECTED, timeout=3)
    assert manager.snapshot.pairing_code is None
    assert len(script.sessions) == 1, "the prompt is answered inside the same handshake"
    await manager.aclose()


async def test_pairing_rejected_by_the_user_ends_in_error(pc_config) -> None:
    manager, _ = make_manager(pc_config, Script(ask_to_pair()))
    await manager.connect()
    assert await manager.wait_for_state(State.PAIRING_REQUIRED, timeout=2)
    manager.resolve_pairing(False)
    assert await manager.wait_for_state(State.ERROR, timeout=3)
    assert "declined" in manager.snapshot.message.lower()
    await manager.aclose()


async def test_the_phone_refusing_the_pairing_is_terminal(pc_config) -> None:
    manager, _ = make_manager(pc_config, Script(reject_pairing))
    await manager.connect()
    assert await manager.wait_for_state(State.ERROR, timeout=3)
    assert "refused" in manager.snapshot.message.lower()
    await manager.aclose()


async def test_an_approval_cannot_answer_a_later_prompt(pc_config) -> None:
    """Each prompt gets its own answer; a stale click must not approve anything."""
    manager, _ = make_manager(pc_config, Script(succeed))
    manager.resolve_pairing(True)  # nobody asked yet
    assert await manager.wait_for_state(State.DISCONNECTED, timeout=1)

    pending = asyncio.create_task(manager._confirm_pairing("111 222"))  # noqa: SLF001
    await asyncio.sleep(0.05)
    assert not pending.done(), "the earlier approval must not answer this prompt"
    manager.resolve_pairing(False)
    assert await asyncio.wait_for(pending, timeout=2) is False
    await manager.aclose()


async def test_an_unanswered_prompt_expires(pc_config) -> None:
    manager, _ = make_manager(pc_config, Script(ask_to_pair()), pairing_timeout=0.1)
    await manager.connect()
    assert await manager.wait_for_state(State.ERROR, timeout=3)
    assert "pairing" in manager.snapshot.message.lower()
    await manager.aclose()


async def test_pairing_can_be_answered_from_another_thread(pc_config) -> None:
    manager, _ = make_manager(pc_config, Script(ask_to_pair()))
    await manager.connect()
    assert await manager.wait_for_state(State.PAIRING_REQUIRED, timeout=2)
    thread = threading.Thread(target=manager.resolve_pairing, args=(True,))
    thread.start()
    thread.join()
    assert await manager.wait_for_state(State.CONNECTED, timeout=3)
    await manager.aclose()


# --------------------------------------------------------------------------- #
# reconnection
# --------------------------------------------------------------------------- #
async def test_a_drop_reconnects_without_asking_the_user(pc_config) -> None:
    script = Script(succeed, succeed)
    manager, _ = make_manager(pc_config, script)
    states: list[State] = []
    manager.subscribe(lambda snapshot: states.append(snapshot.state))

    await manager.connect()
    assert await manager.wait_for_state(State.CONNECTED, timeout=2)
    script.sessions[0].drop("the Wi-Fi went away")

    assert await wait_for(lambda: len(script.sessions) == 2), "the supervisor did not retry"
    assert await manager.wait_for_state(State.CONNECTED, timeout=2)
    assert State.RECONNECTING in states, "the user is told the link dropped"
    assert State.ERROR not in states
    await manager.aclose()


async def test_a_missed_heartbeat_is_treated_as_a_drop(pc_config) -> None:
    script = Script(succeed, succeed)
    manager, _ = make_manager(
        pc_config,
        script,
        heartbeat_interval=0.01,
        heartbeat_timeout=0.01,
        heartbeat_failures=1,
    )
    states: list[State] = []
    manager.subscribe(lambda snapshot: states.append(snapshot.state))
    await manager.connect()
    assert await manager.wait_for_state(State.CONNECTED, timeout=2)
    script.sessions[0].ping_error = TimeoutError("no PONG")

    assert await wait_for(lambda: len(script.sessions) == 2), "the heartbeat did not trigger"
    assert await manager.wait_for_state(State.CONNECTED, timeout=2)
    assert State.RECONNECTING in states
    assert script.sessions[0].closed.is_set(), "the silent session is closed, not left open"
    await manager.aclose()


async def test_reconnect_forces_a_fresh_attempt(pc_config) -> None:
    script = Script(succeed, succeed)
    manager, _ = make_manager(pc_config, script)
    await manager.connect()
    assert await manager.wait_for_state(State.CONNECTED, timeout=2)
    await manager.reconnect()
    assert await wait_for(lambda: len(script.sessions) == 2)
    assert await manager.wait_for_state(State.CONNECTED, timeout=2)
    await manager.aclose()


async def test_max_attempts_bounds_the_ladder(pc_config) -> None:
    script = Script(fail("nope"))
    manager, _ = make_manager(pc_config, script, max_attempts=2)
    await manager.connect()
    assert await manager.wait_for_state(State.ERROR, timeout=3)
    assert "gave up" in manager.snapshot.message
    assert len(script.sessions) == 2
    await manager.aclose()


# --------------------------------------------------------------------------- #
# disconnect and cleanup
# --------------------------------------------------------------------------- #
async def test_disconnect_closes_the_session_and_releases_forwards(pc_config) -> None:
    script = Script(succeed)
    provider = StaticProvider([usb()])
    manager, _ = make_manager(pc_config, script, provider)
    await manager.connect()
    assert await manager.wait_for_state(State.CONNECTED, timeout=2)

    await manager.disconnect("user pressed Disconnect")
    assert manager.state is State.DISCONNECTED
    assert script.sessions[0].closed.is_set()
    assert provider.released >= 1, "the USB forward it created must be removed"
    assert manager.snapshot.transport is None


async def test_a_new_connect_after_disconnect_works(pc_config) -> None:
    script = Script(succeed, succeed)
    manager, _ = make_manager(pc_config, script)
    await manager.connect()
    assert await manager.wait_for_state(State.CONNECTED, timeout=2)
    await manager.disconnect()
    assert await manager.wait_for_state(State.DISCONNECTED, timeout=2)
    await manager.connect()
    assert await manager.wait_for_state(State.CONNECTED, timeout=3)
    assert len(script.sessions) == 2
    await manager.aclose()


async def test_diagnostics_are_bounded_and_useful(pc_config) -> None:
    manager, _ = make_manager(pc_config, Script(fail("boom")), diagnostics_limit=5)
    await manager.connect()
    assert await manager.wait_for_state(State.ERROR, timeout=3)
    diagnostics = list(manager.snapshot.diagnostics)
    assert diagnostics, "a failure must leave a trace"
    assert len(diagnostics) <= 5
    assert any("boom" in line for line in diagnostics)
    await manager.aclose()


async def test_a_raising_listener_cannot_kill_the_manager(pc_config) -> None:
    manager, _ = make_manager(pc_config, Script(succeed))

    def explode(_snapshot):
        raise RuntimeError("the view is broken")

    manager.subscribe(explode)
    await manager.connect()
    assert await manager.wait_for_state(State.CONNECTED, timeout=3)
    await manager.aclose()
