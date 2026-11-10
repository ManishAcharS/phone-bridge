"""One connection, one owner, one state machine.

Everything the CLI did per command — find the phone, connect, pair, fetch
``system.info``, notice the drop, retry — happens here once, for as long as the
window is open.

The states are the product's, not the protocol's::

    DISCONNECTED -> DISCOVERING -> CONNECTING -> AUTHENTICATING -> CONNECTED
                          |            |              |
                          |            |              +-> PAIRING_REQUIRED (user compares a code)
                          |            +-> RECONNECTING (a drop, with backoff)
                          +-> ERROR (nothing to try, or a terminal refusal)

Three rules are load-bearing:

* **One session at a time.** ``connect()`` is single-flight, so a second click, a
  timer or an impatient user cannot open a second socket (the phone allows 8, and
  a leaked one is invisible).
* **Reconnect re-discovers.** The phone may have moved from Wi-Fi to USB while we
  were disconnected, so every attempt asks the providers again instead of reusing
  the address that just failed.
* **Terminal errors stay terminal.** A changed key, a version skew or a refused
  pairing ends in ERROR; retrying those would train the user to click past the one
  message that matters.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from ..core.device_store import DeviceStore
from ..core.session import (
    BackoffPolicy,
    BridgeSession,
    PairingRejected,
    SessionError,
    TerminalSessionError,
)
from ..transport.link import DEFAULT_PORT, websocket_uri
from .capabilities import Capability, build_registry
from .diagnostics import Diagnostics
from .netprobe import network_profile, tcp_probe
from .transports import (
    BeaconCandidates,
    Candidate,
    CandidateProvider,
    FixedCandidates,
    GatewayCandidates,
    UsbCandidates,
    default_providers,
    rank,
)

log = logging.getLogger(__name__)


class State(str, Enum):
    DISCONNECTED = "disconnected"
    DISCOVERING = "discovering"
    PAIRING_REQUIRED = "pairing_required"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"

    @property
    def label(self) -> str:
        return {
            State.DISCONNECTED: "Disconnected",
            State.DISCOVERING: "Discovering",
            State.PAIRING_REQUIRED: "Pairing required",
            State.CONNECTING: "Connecting",
            State.AUTHENTICATING: "Authenticating",
            State.CONNECTED: "Connected",
            State.RECONNECTING: "Reconnecting",
            State.ERROR: "Error",
        }[self]

    @property
    def busy(self) -> bool:
        return self in {
            State.DISCOVERING,
            State.CONNECTING,
            State.AUTHENTICATING,
            State.RECONNECTING,
        }


@dataclass(frozen=True)
class Snapshot:
    """Everything a view needs, in one immutable value.

    Immutable on purpose: the GUI reads it from a queue on another thread, and a
    mutable object would be a race waiting to happen.
    """

    state: State = State.DISCONNECTED
    message: str = ""
    transport: str | None = None
    endpoint: str | None = None
    device: dict[str, Any] = field(default_factory=dict)
    capabilities: tuple[Capability, ...] = ()
    diagnostics: tuple[str, ...] = ()
    pairing_code: str | None = None
    connected_at: float | None = None
    attempts: int = 0
    last_error: str | None = None
    adb_available: bool = False
    adb_path: str | None = None
    adb_detail: str = ""
    usb_available: bool = False
    wifi_status: str = "not checked yet"
    server_reachable: bool | None = None
    auth_ok: bool = False
    last_discovery_error: str | None = None
    last_connection_error: str | None = None
    next_retry_in: float | None = None

    @property
    def connected(self) -> bool:
        return self.state is State.CONNECTED

    def device_line(self) -> str:
        name = self.device.get("name") or self.device.get("model")
        model = self.device.get("model")
        if name and model and name != model:
            return f"{name} ({model})"
        return str(name or model or "no phone")

    def auth_line(self) -> str:
        if self.state is State.CONNECTED and self.auth_ok:
            return "Pinned key verified · AES-256-GCM"
        if self.state is State.CONNECTED:
            return "Connected (authentication not confirmed)"
        if self.state is State.PAIRING_REQUIRED:
            return "Waiting for you to compare the code"
        if self.state is State.AUTHENTICATING:
            return "Verifying the phone's key"
        if self.auth_ok:
            return "Was authenticated; not connected now"
        return "Not authenticated"


class _RetryCandidate(Exception):
    """This address failed in a way that is worth trying somewhere else."""


class _Terminal(Exception):
    """Stop the supervisor: retrying would only hide the reason."""


SessionFactory = Callable[..., BridgeSession]
Emitter = Callable[[Snapshot], None]


class ConnectionManager:
    """Owns the session, the transport choice and the retry ladder."""

    def __init__(
        self,
        store: DeviceStore,
        *,
        emit: Emitter | None = None,
        providers: Sequence[CandidateProvider] | None = None,
        session_factory: SessionFactory | None = None,
        discovery_timeout: float = 3.0,
        connect_timeout: float = 8.0,
        request_timeout: float = 15.0,
        heartbeat_interval: float = 10.0,
        heartbeat_timeout: float = 4.0,
        heartbeat_failures: int = 2,
        pairing_timeout: float = 90.0,
        policy: BackoffPolicy | None = None,
        max_attempts: int | None = None,
        remember_endpoints: bool = True,
        diagnostics_limit: int = 200,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._listeners: list[Emitter] = [emit] if emit is not None else []
        self._providers: list[CandidateProvider] = list(
            providers if providers is not None else default_providers(store)
        )
        self._session_factory: SessionFactory = session_factory or BridgeSession
        self.discovery_timeout = discovery_timeout
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.heartbeat_failures = heartbeat_failures
        self.pairing_timeout = pairing_timeout
        self.policy = policy or BackoffPolicy()
        self.max_attempts = max_attempts
        self.remember_endpoints = remember_endpoints
        self.diagnostics_limit = diagnostics_limit
        self._sleep = sleeper or asyncio.sleep

        self._snapshot = Snapshot()
        self._diag = Diagnostics(limit=diagnostics_limit)
        self._manual: FixedCandidates | None = None
        self._last_found: list[Candidate] = []
        self._profile: str | None = None
        self._profile_at: float = 0.0
        self._session: BridgeSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._identity: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = asyncio.Event()
        self._pairing_event: asyncio.Event | None = None
        self._pairing_decision: bool | None = None
        self._pairing_declined = False
        self._ever_connected = False
        self._attempt = 0
        self._active: set[str] = set()

    # ------------------------------------------------------------------ #
    # observable state
    # ------------------------------------------------------------------ #
    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    @property
    def state(self) -> State:
        return self._snapshot.state

    @property
    def session(self) -> BridgeSession | None:
        return self._session

    def _publish(self, **changes: Any) -> None:
        snapshot = replace(self._snapshot, **changes)
        self._snapshot = snapshot
        for listener in tuple(self._listeners):
            try:
                listener(snapshot)
            except Exception:  # noqa: BLE001 - a broken view must not kill the manager
                log.exception("a state listener raised")

    def _set(self, state: State, message: str = "", **changes: Any) -> None:
        self._publish(state=state, message=message, **changes)
        log.info("state=%s %s", state.value, message)

    def _log(self, text: str) -> None:
        log.info(text)
        self._diag.add(text)
        self._publish(diagnostics=self._diag.render())

    def subscribe(self, listener: Emitter) -> None:
        """Call ``listener(snapshot)`` on every change. Additive and idempotent."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unsubscribe(self, listener: Emitter) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(listener)

    # ------------------------------------------------------------------ #
    # commands (all safe to call from the GUI thread via runtime.py)
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        """Start connecting. Returns immediately; watch the snapshot."""
        if self._task is not None and not self._task.done():
            self._log("another connection attempt is already running")
            return
        if self._snapshot.state is State.CONNECTED:
            self._log("already connected")
            return
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._attempt = 0
        self._task = asyncio.create_task(self._supervise(), name="phonebridge-connect")

    async def disconnect(self, reason: str = "disconnected") -> None:
        """Stop everything and release what we own."""
        self._stop.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._teardown_session()
        await self._release_leases()
        self._ever_connected = False
        self._pairing_decision = None
        self._pairing_declined = False
        self._set(
            State.DISCONNECTED,
            reason,
            transport=None,
            endpoint=None,
            pairing_code=None,
            next_retry_in=None,
            auth_ok=False,
            server_reachable=None,
        )

    async def reconnect(self) -> None:
        """Force a new attempt now, even mid-backoff."""
        await self.disconnect("reconnecting on request")
        await self.connect()

    @property
    def provider_names(self) -> tuple[str, ...]:
        """The discovery sources in the order they are asked."""
        return tuple(getattr(provider, "name", "?") for provider in self._providers)

    def set_manual_endpoint(self, host: str, port: int | None = None) -> None:
        """Try one address the user typed, ahead of anything discovered.

        Passing an empty host clears it. The provider stays in the list until
        cleared, so a reconnect after a drop still tries it.
        """
        cleaned = host.strip()
        self._providers = [p for p in self._providers if not isinstance(p, FixedCandidates)]
        if not cleaned:
            self._manual = None
            self._log("manual endpoint cleared")
            return
        self._manual = FixedCandidates(cleaned, port or DEFAULT_PORT)
        self._providers.append(self._manual)
        self._log(f"manual endpoint set to {cleaned}:{port or DEFAULT_PORT}")

    def resolve_pairing(self, approve: bool) -> None:
        """Answer the pairing prompt (called from any thread)."""
        def _apply() -> None:
            self._pairing_decision = approve
            if self._pairing_event is not None:
                self._pairing_event.set()

        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(_apply)
        else:  # pragma: no cover - only before connect() is called
            _apply()

    async def wait_for_state(
        self, states: Iterable[State | str] | State | str, timeout: float = 10.0
    ) -> bool:
        # State subclasses str, but Enum supplies the hash, so a set of strings
        # would not match a State member. Normalise instead of trusting it.
        if isinstance(states, (State, str)):
            wanted = {State(states)}
        else:
            wanted = {State(state) for state in states}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._snapshot.state in wanted:
                return True
            await asyncio.sleep(0.02)
        return self._snapshot.state in wanted

    async def aclose(self) -> None:
        await self.disconnect("closing")

    # ------------------------------------------------------------------ #
    # supervisor
    # ------------------------------------------------------------------ #
    async def _supervise(self) -> None:
        delays = self.policy.delays()
        try:
            while not self._stop.is_set():
                self._attempt += 1
                self._publish(attempts=self._attempt)
                try:
                    await self._attempt_cycle()
                    delays = self.policy.delays()  # a real session resets the ladder
                except _Terminal as exc:
                    self._fail(str(exc))
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - a failed attempt is data
                    self._log(f"attempt {self._attempt} failed: {exc or type(exc).__name__}")
                    self._publish(last_error=str(exc) or type(exc).__name__)

                if self._stop.is_set():
                    break
                if self.max_attempts is not None and self._attempt >= self.max_attempts:
                    self._fail(
                        f"gave up after {self._attempt} attempt(s): "
                        f"{self._snapshot.last_error or 'no phone accepted the connection'}"
                    )
                    return
                if not self._ever_connected and self.max_attempts is None:
                    # The first connect is user-initiated: after one full pass over
                    # every candidate, say what went wrong rather than spinning.
                    self._fail(self._snapshot.last_error or "no phone found")
                    return

                delay = next(delays)
                self._set(
                    State.RECONNECTING,
                    f"reconnecting in {delay:.0f}s",
                    next_retry_in=delay,
                )
                await self._wait_out(delay)
        finally:
            await self._teardown_session()
            await self._release_leases()

    def _fail(self, message: str) -> None:
        self._log(f"error: {message}")
        self._set(
            State.ERROR,
            message,
            last_error=message,
            last_connection_error=message,
            next_retry_in=None,
            auth_ok=False,
        )

    async def _wait_out(self, delay: float) -> None:
        # Either the wait elapses (normal: try again) or disconnect() sets the
        # event and the supervisor loop exits. The flag is never cleared here.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=delay)

    # ------------------------------------------------------------------ #
    # one attempt
    # ------------------------------------------------------------------ #
    async def _attempt_cycle(self) -> None:
        candidates = await self._discover()
        if not candidates:
            raise _RetryCandidate(
                "no phone found over USB, Wi-Fi or a saved address. "
                "Check that the bridge is Start-ed on the phone and that both "
                "devices are on the same network (or connect the USB cable)."
            )
        last: Exception | None = None
        for candidate in rank(candidates):
            reachable = await tcp_probe(candidate.host, candidate.port, timeout=0.8)
            self._publish(server_reachable=reachable)
            if not reachable:
                self._log(
                    f"no TCP answer from {candidate.endpoint} ({candidate.kind}) - "
                    "the phone app may be stopped, or a firewall/access point is dropping it"
                )
            self._set(
                State.CONNECTING,
                f"connecting over {candidate.kind}",
                transport=candidate.kind,
                endpoint=candidate.endpoint,
                server_reachable=reachable,
            )
            try:
                session = await self._open(candidate)
            except _RetryCandidate as exc:
                last = exc
                self._publish(last_connection_error=str(exc))
                self._log(f"{candidate.endpoint} ({candidate.kind}): {exc}")
                continue
            try:
                await self._run_session(candidate, session)
            except _RetryCandidate as exc:
                last = exc
                self._publish(last_connection_error=str(exc))
                self._log(f"{candidate.endpoint} ({candidate.kind}): {exc}")
                continue
            return
        raise _RetryCandidate(str(last) if last else "every candidate failed")

    async def _discover(self) -> list[Candidate]:
        """Ask every source, then record *why* each one did or did not answer.

        The window must be able to separate four different failures that all used
        to read "no phone found": adb missing, adb present with no cable, beacons
        heard but nothing reachable, and beacons not heard at all.
        """
        self._set(State.DISCOVERING, "looking for your phone")
        results = await asyncio.gather(
            *(self._provider_candidates(provider) for provider in self._providers),
            return_exceptions=True,
        )
        found: list[Candidate] = []
        discovery_errors: list[str] = []
        for provider, result in zip(self._providers, results, strict=False):
            if isinstance(result, BaseException):
                discovery_errors.append(f"{provider.name}: {result}")
                self._log(f"{provider.name}: discovery failed: {result}")
                continue
            found.extend(result)

        statuses = self._provider_status()
        for line in statuses["lines"]:
            self._log(line)
        self._last_found = list(found)
        for candidate in rank(found):
            self._log(f"found {candidate.kind} at {candidate.endpoint} - {candidate.detail}")
        if not found:
            self._log("no candidates: adb, the beacon, the gateway and the cache all said no")

        if statuses["server_reachable"] is not None:
            self._publish(server_reachable=statuses["server_reachable"])
        self._publish(
            adb_available=statuses["adb_available"],
            adb_path=statuses["adb_path"],
            adb_detail=statuses["adb_detail"],
            usb_available=statuses["usb_available"],
            wifi_status=statuses["wifi_status"],
            last_discovery_error=(
                statuses["discovery_error"] or (discovery_errors[0] if discovery_errors else None)
            ),
        )
        return found

    def _firewall_hint(self) -> str:
        """Explain a silent beacon on Windows, where Public networks drop UDP.

        Cached for a minute: it costs a PowerShell call, and the answer does not
        change between two retries half a second apart.
        """
        now = time.monotonic()
        if now - self._profile_at > 60.0:
            self._profile = network_profile()
            self._profile_at = now
        if (self._profile or "").lower() != "public":
            return ""
        return (
            " (Windows treats this network as Public, which blocks the discovery "
            "datagram - allow PhoneBridge through the firewall, or type the phone's "
            "address below)"
        )

    def _provider_status(self) -> dict[str, Any]:
        """Summarise what each provider just observed, in one place."""
        lines: list[str] = []
        adb_available = False
        adb_path: str | None = None
        adb_detail = ""
        usb_available = False
        wifi_status = "not checked"
        discovery_error: str | None = None
        server_reachable: bool | None = None

        for provider in self._providers:
            if isinstance(provider, UsbCandidates):
                status = provider.last_status
                if status is None:  # the provider never ran; asking is a subprocess
                    with contextlib.suppress(Exception):
                        status = provider.status()
                if status is None:  # pragma: no cover - adb could not even be asked
                    lines.append("adb has not been checked yet")
                    continue
                adb_available = status.available
                adb_path = status.path
                adb_detail = provider.summary()
                usb_available = provider.usb_available
                lines.append(adb_detail)
                if not status.available:
                    discovery_error = discovery_error or (
                        status.error or "adb not found - USB unavailable, Wi-Fi unaffected"
                    )
                elif not usb_available and provider.last_error:
                    discovery_error = discovery_error or provider.last_error
            elif isinstance(provider, BeaconCandidates):
                heard = len(provider.last_seen)
                if provider.last_error_permission:
                    wifi_status = f"listener blocked: {provider.last_error_permission}"
                    discovery_error = discovery_error or provider.last_error_permission
                elif heard:
                    wifi_status = f"{heard} phone(s) announcing"
                else:
                    wifi_status = "no beacons heard" + self._firewall_hint()
                lines.append(f"wifi discovery: {wifi_status}")
            elif isinstance(provider, GatewayCandidates):
                if provider.last_checked:
                    # "the gateway did not answer" is a reachability fact, not a
                    # discovery failure: the window shows them in different rows.
                    server_reachable = bool(provider.last_answered)
                if provider.last_error:
                    lines.append(f"hotspot fallback: {provider.last_error}")
                    discovery_error = discovery_error or provider.last_error
            elif isinstance(provider, FixedCandidates) and provider.reachable is not None:
                lines.append(
                    f"manual endpoint {provider.host}:{provider.port} "
                    + ("answers" if provider.reachable else "does not answer")
                )

        if not self._last_found:
            discovery_error = discovery_error or "nothing answered on USB, beacon, gateway or cache"
        return {
            "lines": lines,
            "adb_available": adb_available,
            "adb_path": adb_path,
            "adb_detail": adb_detail,
            "usb_available": usb_available,
            "wifi_status": wifi_status,
            "discovery_error": discovery_error,
            "server_reachable": server_reachable,
        }

    async def _provider_candidates(self, provider: CandidateProvider) -> list[Candidate]:
        return await asyncio.wait_for(
            provider.candidates(timeout=self.discovery_timeout),
            timeout=self.discovery_timeout + 5.0,
        )

    async def _open(self, candidate: Candidate) -> BridgeSession:
        """Open one session, including the pairing prompt if the phone is new.

        The code is confirmed *inside* the handshake: ``BridgeSession`` awaits the
        callback, so the answer applies to that one key exchange. That matters —
        the six digits are derived from the fresh ECDH material, so they change on
        every attempt and cannot be cached across one.
        """
        self._pairing_declined = False
        session = self._make_session(candidate)
        try:
            await asyncio.wait_for(
                session.open(),
                # a backstop only: the socket has its own timeout and the prompt
                # has pairing_timeout, so this must never be the first to fire
                timeout=self.connect_timeout + self.pairing_timeout + 5.0,
            )
            return session
        except PairingRejected as exc:
            if self._pairing_declined:
                raise _Terminal(
                    "you declined the pairing on this PC; press Reconnect to try again"
                ) from exc
            raise _Terminal(f"the phone refused the pairing: {exc}") from exc
        except TerminalSessionError as exc:
            # Changed key, version skew, refusal: retrying is dishonest.
            raise _Terminal(str(exc)) from exc
        except asyncio.TimeoutError as exc:
            raise _RetryCandidate(f"timed out connecting to {candidate.endpoint}") from exc
        except (SessionError, OSError) as exc:
            raise _RetryCandidate(str(exc) or type(exc).__name__) from exc

    def _make_session(self, candidate: Candidate) -> BridgeSession:
        if self._identity is None:
            self._identity = self._store.load_or_create_identity()
        return self._session_factory(
            websocket_uri(candidate.host, candidate.port),
            self._identity,
            self._store,
            confirm_pairing=self._confirm_pairing,
            on_phase=self._on_phase,
            request_timeout=self.request_timeout,
        )

    async def _confirm_pairing(self, sas: str) -> bool:
        """Ask the user, inside the handshake, and return their answer.

        A fresh event per prompt means an earlier approval can never answer a
        later one — the six digits would not even match.
        """
        self._pairing_event = asyncio.Event()
        self._pairing_decision = None
        self._set(
            State.PAIRING_REQUIRED,
            "compare this code with the one on the phone",
            pairing_code=sas,
        )
        try:
            await asyncio.wait_for(self._pairing_event.wait(), timeout=self.pairing_timeout)
        except asyncio.TimeoutError:
            self._log("the pairing prompt expired without an answer")
            self._set(State.PAIRING_REQUIRED, "the code expired; press Reconnect to try again")
            return False
        finally:
            self._pairing_event = None
        approved = bool(self._pairing_decision)
        self._pairing_declined = not approved
        self._log("pairing approved" if approved else "pairing declined by the user")
        return approved

    def _on_phase(self, phase: str) -> None:
        if phase == "handshake":
            self._set(State.AUTHENTICATING, "verifying the phone's pinned key")

    async def _run_session(self, candidate: Candidate, session: BridgeSession) -> None:
        self._ever_connected = True
        self._session = session
        try:
            info = await session.call("system.info")
            if not isinstance(info, dict):
                raise _RetryCandidate("the phone answered system.info with something unexpected")
        except _RetryCandidate:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken session is a failed attempt
            await self._teardown_session()
            raise _RetryCandidate(f"could not read the phone's information: {exc}") from exc

        registry = build_registry(info, active=self._active)
        device = {
            key: info.get(key)
            for key in (
                "name",
                "model",
                "manufacturer",
                "android_release",
                "sdk_int",
                "app_version",
                "device_id",
            )
        }
        self._remember(candidate, info)
        self._set(
            State.CONNECTED,
            f"connected to {device.get('name') or device.get('model') or 'phone'}",
            transport=candidate.kind,
            endpoint=candidate.endpoint,
            device=device,
            capabilities=registry,
            connected_at=time.time(),
            pairing_code=None,
            last_error=None,
            next_retry_in=None,
            auth_ok=True,
            server_reachable=True,
        )
        self._log(
            f"connected over {candidate.kind} at {candidate.endpoint}; "
            f"{sum(1 for c in registry if c.available)} of {len(registry)} capabilities available"
        )
        self._heartbeat = asyncio.create_task(
            self._heartbeat_loop(session), name="phonebridge-heartbeat"
        )
        try:
            reason = await session.wait_closed()
            self._log(f"the session closed: {reason}")
        finally:
            await self._teardown_session()

    async def _heartbeat_loop(self, session: BridgeSession) -> None:
        failures = 0
        while not self._stop.is_set() and session.is_open:
            await self._sleep(self.heartbeat_interval)
            if self._stop.is_set() or not session.is_open:
                return
            try:
                await session.ping(timeout=self.heartbeat_timeout)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a missed beat is data, not a crash
                failures += 1
                self._log(f"heartbeat missed ({failures}/{self.heartbeat_failures}): {exc}")
                if failures >= self.heartbeat_failures:
                    self._log("the phone stopped answering; reconnecting")
                    await session.close("heartbeat timeout")
                    return

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _remember(self, candidate: Candidate, info: dict[str, Any]) -> None:
        if not self.remember_endpoints:
            return
        transport = "usb" if candidate.kind.startswith("usb") else candidate.kind
        try:
            self._store.remember_endpoint(
                transport, candidate.host, candidate.port, info.get("device_id")
            )
        except Exception as exc:  # noqa: BLE001 - a full disk must not drop the link
            self._log(f"could not save the address: {exc}")

    async def _teardown_session(self) -> None:
        heartbeat, self._heartbeat = self._heartbeat, None
        if heartbeat is not None and not heartbeat.done():
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat
        session, self._session = self._session, None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close("controller disconnecting")

    async def _release_leases(self) -> None:
        for provider in self._providers:
            release = getattr(provider, "release", None)
            if release is None:
                continue
            try:
                await release()
            except Exception as exc:  # noqa: BLE001 - cleanup must not raise
                self._log(f"cleanup failed for {getattr(provider, 'name', 'provider')}: {exc}")
