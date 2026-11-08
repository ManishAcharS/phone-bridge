"""Regression tests for defects found during the Phase 1 audit.

Each test names the defect it guards against, so a future change that
reintroduces it fails loudly instead of silently degrading the link.
"""

from __future__ import annotations

import asyncio

import pytest
from phone_sim import SimulatedPhone, _identity_for

from phonebridge.core.device_store import DeviceStore
from phonebridge.core.session import (
    BackoffPolicy,
    BridgeSession,
    ReconnectingSession,
    SessionError,
)
from phonebridge.protocol.messages import MSG_PONG
from phonebridge.transport.link import WebSocketLink, websocket_uri


async def _phone(sim_state, **kwargs) -> SimulatedPhone:
    ident = _identity_for(sim_state / "phone_identity.pem", "Pixel-test")
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
    kwargs.setdefault("confirm_pairing", lambda _sas: True)
    return BridgeSession(
        websocket_uri("127.0.0.1", phone.port),
        store.load_or_create_identity(),
        store,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# ping correlation
# --------------------------------------------------------------------------- #
async def test_concurrent_pings_all_resolve(sim_state, pc_config):
    """Defect: every ping shared one dict slot (key -2), so concurrent pings
    overwrote each other and all but the last timed out after 5 s."""
    phone = await _phone(sim_state)
    try:
        session = await _session(phone, pc_config).open()
        rtts = await asyncio.wait_for(
            asyncio.gather(*(session.ping() for _ in range(5))), timeout=4
        )
        assert len(rtts) == 5
        assert all(isinstance(r, float) and r >= 0 for r in rtts)
        assert len(session.stats._rtt_samples) == 5
        await session.close()
    finally:
        await phone.stop()


async def test_ping_fails_fast_when_session_closes(sim_state, pc_config):
    """A ping in flight when the link dies must fail, not hang until timeout."""
    phone = await _phone(sim_state)
    try:
        session = await _session(phone, pc_config).open()
        await phone.stop()
        await asyncio.sleep(0.2)
        with pytest.raises(SessionError):
            await asyncio.wait_for(session.ping(), timeout=2)
    finally:
        await phone.stop()


async def test_ping_refused_on_closed_session(sim_state, pc_config):
    phone = await _phone(sim_state)
    try:
        session = await _session(phone, pc_config).open()
        await session.close()
        with pytest.raises(SessionError, match="not open"):
            await session.ping()
    finally:
        await phone.stop()


async def test_malformed_pong_is_ignored_and_ping_still_works(sim_state, pc_config):
    """Defect: a short PONG raised struct.error inside the read loop, killing
    the session. It must be dropped instead."""
    phone = await _phone(sim_state)
    try:
        session = await _session(phone, pc_config).open()
        session._dispatch(MSG_PONG, 0, b"tooshort")
        session._dispatch(MSG_PONG, 0, b"")
        assert await session.ping() >= 0
        assert session.is_open
        await session.close()
    finally:
        await phone.stop()


# --------------------------------------------------------------------------- #
# link liveness
# --------------------------------------------------------------------------- #
async def test_link_reports_closed_after_close(sim_state):
    phone = await _phone(sim_state)
    link = WebSocketLink(websocket_uri("127.0.0.1", phone.port))
    try:
        await link.open()
        assert link.is_open is True
        await link.close()
        assert link.is_open is False
        # LinkError is the documented contract for sending on a dead link.
        from phonebridge.transport.link import LinkError

        with pytest.raises(LinkError):
            await link.send(b"after close")
    finally:
        await phone.stop()


async def test_link_is_open_tracks_websocket_state_not_just_existence(sim_state):
    """is_open must reflect the protocol state, not "we still hold an object".

    The original implementation was `self._ws is not None and state is not
    None`, which is True for a socket that is closing or already closed as long
    as the wrapper has not nulled its reference. That is only invisible in
    practice because `close()` nulls the reference; any other path that ends the
    connection (peer close, transport failure) would have reported "open".
    """
    phone = await _phone(sim_state)
    link = WebSocketLink(websocket_uri("127.0.0.1", phone.port))
    try:
        await link.open()
        assert link.is_open is True
        # Close the underlying socket directly, bypassing WebSocketLink.close(),
        # so the wrapper still holds the object in a non-OPEN state.
        assert link._ws is not None
        await link._ws.close()
        await asyncio.sleep(0.05)
        assert link.is_open is False, "is_open reported a closed socket as open"
    finally:
        await phone.stop()


async def test_link_is_open_false_before_open():
    link = WebSocketLink("ws://127.0.0.1:1")
    assert link.is_open is False


# --------------------------------------------------------------------------- #
# backoff
# --------------------------------------------------------------------------- #
def test_backoff_grows_then_caps():
    policy = BackoffPolicy(initial_s=0.5, max_s=4.0, factor=2.0, jitter=0.0)
    delays = policy.delays()
    observed = [round(next(delays), 3) for _ in range(5)]
    assert observed[0] == 0.5
    assert observed[1] == 1.0
    assert observed[2] == 2.0
    assert observed[3] == 4.0
    assert observed[4] == 4.0  # capped


class _CountingPolicy(BackoffPolicy):
    """Counts how often the delay ladder is (re)created."""

    def __init__(self) -> None:
        super().__init__(initial_s=0.01, max_s=0.02, factor=2.0, jitter=0.0)
        self.calls = 0

    def delays(self):
        self.calls += 1
        return super().delays()


async def test_backoff_restarts_after_a_healthy_session(sim_state, pc_config):
    """Defect: the ladder was created once, so a drop after a long healthy
    session could wait the full 15 s instead of restarting at 0.5 s."""
    phone = await _phone(sim_state)
    store = DeviceStore(pc_config)
    policy = _CountingPolicy()
    sup = ReconnectingSession(
        websocket_uri("127.0.0.1", phone.port),
        store.load_or_create_identity(),
        store,
        policy=policy,
        max_attempts=4,
        confirm_pairing=lambda _s: True,
    )
    await sup.start()
    try:
        await sup.wait_connected(10)
        assert policy.calls == 2, (
            "the delay ladder was not recreated after a successful connection "
            f"(delays() called {policy.calls} times)"
        )
        await phone.stop()
        await asyncio.sleep(0.3)
        assert policy.calls >= 2
    finally:
        await sup.stop()
        await phone.stop()


async def test_transient_failures_are_not_reported_as_terminal(sim_state, pc_config):
    """`failed` must be reserved for giving up; a retry is `reconnecting`."""
    store = DeviceStore(pc_config)
    states: list[str] = []
    sup = ReconnectingSession(
        websocket_uri("127.0.0.1", 1),  # nothing listening
        store.load_or_create_identity(),
        store,
        on_state=lambda s, _d: states.append(s),
        policy=BackoffPolicy(initial_s=0.01, max_s=0.02, jitter=0.0),
        max_attempts=3,
        confirm_pairing=lambda _s: True,
    )
    await sup.start()
    for _ in range(100):
        if "closed" in states:
            break
        await asyncio.sleep(0.05)
    await sup.stop()
    assert "failed" not in states, f"terminal state emitted for a retry: {states}"
    assert "reconnecting" in states
    assert "closed" in states


async def test_state_callbacks_that_raise_do_not_kill_the_supervisor(sim_state, pc_config):
    phone = await _phone(sim_state)
    store = DeviceStore(pc_config)

    def angry(_state, _details):
        raise RuntimeError("ui exploded")

    sup = ReconnectingSession(
        websocket_uri("127.0.0.1", phone.port),
        store.load_or_create_identity(),
        store,
        on_state=angry,
        confirm_pairing=lambda _s: True,
    )
    await sup.start()
    try:
        session = await sup.wait_connected(10)
        assert session.is_open
    finally:
        await sup.stop()
        await phone.stop()
