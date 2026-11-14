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
import platform
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from ..audio import ERROR as AUDIO_ERROR
from ..audio import (
    AudioLink,
    AudioSnapshot,
    backend_status,
    default_sink_factory,
    default_source_factory,
)
from ..bluetooth import BluetoothLink, BluetoothSnapshot
from ..bluetooth.frames import AdapterStatus, ScanState
from ..camera import CameraStream, VideoConfig
from ..camera import preview as camera_preview
from ..camera.stream import ACTIVE as CAMERA_ACTIVE
from ..camera.stream import ERROR as CAMERA_ERROR
from ..core.device_store import DeviceStore
from ..core.session import (
    BackoffPolicy,
    BridgeSession,
    PairingRejected,
    SessionError,
    TerminalSessionError,
)
from ..files import COMPLETE as FILE_COMPLETE
from ..files import TransferManager
from ..files.frames import TransferStats
from ..sensors import SensorLink, SensorSnapshot
from ..sensors.link import ACTIVE as SENSOR_ACTIVE
from ..transport.link import DEFAULT_PORT, websocket_uri
from .adb import Adb
from .capabilities import Capability, build_registry
from .diagnostics import Diagnostics
from .endpoints import is_loopback
from .netprobe import Gateway, default_gateways, network_profile, tcp_probe
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
from .usbcheck import (
    USB_UNKNOWN,
    PnpDevice,
    UsbDiagnosis,
    UsbReport,
    build_report,
    diagnose,
    format_report,
    query_pnp,
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
    usb_diagnosis: str = ""
    usb_action: str = ""
    usb_stage: str = ""
    #: Phase 2 audio. Separate strings so one direction cannot hide the other.
    audio_playback: str = "idle"
    audio_mic: str = "idle"
    audio_detail: str = ""
    audio_volume: int = 40
    audio_muted: bool = True
    #: Phase 4 camera. `state` is the only thing the UI treats as "live".
    camera_state: str = "idle"
    camera_detail: str = ""
    camera_facing: str = "back"
    camera_width: int = 1280
    camera_height: int = 720
    camera_fps: int = 15
    camera_frames: int = 0
    #: Degrees the phone said to rotate the last frame (0/90/180/270).
    camera_rotation: int = 0
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
        usb_probe: Callable[[], list[PnpDevice]] | None = None,
        system: str | None = None,
        adb: Any | None = None,
        source_factory: Callable[[Any], Any] | None = None,
        sink_factory: Callable[[Any], Any] | None = None,
        gateways: Callable[[], list[Gateway]] | None = None,
        probe: Callable[[str, int, float], Awaitable[bool]] | None = None,
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
        # `-inf`, not 0.0: the clock is monotonic (time since boot), so on a machine
        # booted less than a minute ago `now - 0.0` looks like a fresh cache entry
        # and the very first probe is skipped. That is how a fresh boot lost the
        # Public-network hint, and why this test was flaky on a cold runner.
        self._profile_at: float = float("-inf")
        #: Windows' device tree is a subprocess; ask it at most every 30 s.
        self._usb_probe = usb_probe or query_pnp
        self._system = system or platform.system()
        #: Only the manual "Diagnose USB" path drives adb directly; discovery goes
        #: through the providers so there is still one place that talks to devices.
        self._adb = adb if adb is not None else Adb()
        self._usb_report: UsbReport | None = None
        self._pnp: list[PnpDevice] = []
        # Same sentinel rule as `_profile_at`: `-inf` so the first USB probe always
        # runs, however young the monotonic clock is.
        self._pnp_at: float = float("-inf")
        #: Network fingerprint at the moment the session came up, so a drop can
        #: say "the network changed" instead of "something went wrong".
        self._gateways = gateways or default_gateways
        self._probe = probe or tcp_probe
        self._fingerprint: str | None = None
        #: What the last failed session attempt was told about the drop.
        self._last_drop_reason: str | None = None
        #: Set once a session reached CONNECTED, so a drop after real success can
        #: restart the backoff ladder instead of leaving it at the cap.
        self._session_established = False
        self._session: BridgeSession | None = None
        #: Phase 2 audio, created when a session comes up and closed with it.
        self._audio: AudioLink | None = None
        self._camera: CameraStream | None = None
        self._camera_config = VideoConfig()
        #: Phase 5 planes: Bluetooth, sensors, files. Built with the session and
        #: closed with it, exactly like audio and the camera — none of them owns a
        #: socket, so there is still only one authenticated connection.
        self._bluetooth: BluetoothLink | None = None
        self._sensors: SensorLink | None = None
        self._transfers: TransferManager | None = None
        self._bluetooth_snapshot = BluetoothSnapshot(
            adapter=AdapterStatus(), scan=ScanState()
        )
        self._sensor_snapshot = SensorSnapshot()
        self._files_directory: Path | None = None
        self._sensor_ids: list[int] = []
        self._active_at_publish: set[str] = set()
        self._audio_snapshot = AudioSnapshot()
        #: Injected in tests; the real ones need WASAPI/sounddevice on Windows.
        self._source_factory = source_factory or default_source_factory
        self._sink_factory = sink_factory or default_sink_factory
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
        if is_loopback(cleaned):
            # Legitimate with an adb forward or the simulator; almost never what
            # someone wants when the phone is on Wi-Fi.
            self._log(
                "note: that is a loopback address - it only reaches the phone "
                "through an adb forward or a bridge running on this PC"
            )

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
                    if self._session_established:
                        # The link worked before it dropped: start the ladder over
                        # so a flaky minute cannot leave the user waiting a minute.
                        delays = self.policy.delays()
                        self._session_established = False

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
            # Any failure of one address is worth trying the next one; only a
            # terminal verdict (changed key, protocol skew) stops the ladder.
            # Catching the generic case matters: a raw OSError from the socket
            # used to escape and throw away the candidate list and the reason.
            try:
                session = await self._open(candidate)
            except _Terminal:
                raise
            except Exception as exc:  # noqa: BLE001 - one address failed, not the run
                last = exc
                self._publish(last_connection_error=str(exc))
                self._log(f"{candidate.endpoint} ({candidate.kind}): {exc}")
                continue
            try:
                await self._run_session(candidate, session)
            except _Terminal:
                raise
            except Exception as exc:  # noqa: BLE001 - see above
                last = exc
                self._publish(last_connection_error=str(exc))
                self._log(f"{candidate.endpoint} ({candidate.kind}): {exc}")
                continue
            return
        reason = str(last) if last else "every candidate failed"
        if self._last_drop_reason and self._last_drop_reason not in reason:
            reason = f"{reason} (last drop: {self._last_drop_reason})"
        raise _RetryCandidate(reason)

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

        # Several sources often see the same phone (the beacon, the gateway and a
        # saved address all name one socket). Trying it twice in one pass means
        # two sessions to one phone, and a second "found" line that is not new.
        unique: dict[tuple[str, int], Candidate] = {}
        for candidate in found:
            unique.setdefault((candidate.host, candidate.port), candidate)
        found = list(unique.values())

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
            usb_diagnosis=statuses["usb_diagnosis"].headline,
            usb_action=statuses["usb_diagnosis"].action,
            usb_stage=getattr(statuses["usb_diagnosis"], "stage", "") or "",
            audio_playback=self._audio_snapshot.playback,
            audio_mic=self._audio_snapshot.mic,
            audio_detail=self._audio_snapshot.detail or self._audio_snapshot.mic_detail,
            audio_volume=self._audio_snapshot.volume,
            audio_muted=self._audio_snapshot.muted,
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

    def _device_tree(self, *, force: bool = False) -> list[PnpDevice]:
        """Windows' USB device tree, cached; a subprocess per discovery is waste."""
        now = time.monotonic()
        if force or now - self._pnp_at > 30.0:
            try:
                self._pnp = list(self._usb_probe())
            except Exception as exc:  # noqa: BLE001 - a diagnosis must never break discovery
                self._log(f"could not read the USB device tree: {exc}")
                self._pnp = []
            self._pnp_at = now
        return self._pnp

    def _usb_diagnosis(self, devices: list[Any], adb_available: bool) -> UsbDiagnosis:
        """Cheap verdict first; ask Windows only when adb has nothing to say."""
        verdict = diagnose(
            adb_devices=devices,
            adb_available=adb_available,
            pnp=None,
            system=self._system,
        )
        if verdict.kind != USB_UNKNOWN:
            return verdict
        return diagnose(
            adb_devices=devices,
            adb_available=adb_available,
            pnp=self._device_tree(),
            system=self._system,
        )

    # ------------------------------------------------------------------ #
    # Bluetooth, sensors and files (Phase 5)
    #
    # Each plane is a thin client over the one session: the phone is the radio
    # endpoint and this PC is the remote control. Two rules hold everywhere here:
    # nothing connects to a peripheral the user did not pick, and nothing reports
    # "done" before the phone confirmed it.
    # ------------------------------------------------------------------ #
    @property
    def bluetooth(self) -> BluetoothLink | None:
        """The live Bluetooth plane, or ``None`` while there is no session."""
        return self._bluetooth

    @property
    def sensors(self) -> SensorLink | None:
        return self._sensors

    @property
    def transfers(self) -> TransferManager | None:
        return self._transfers

    def bluetooth_status(self) -> dict[str, Any]:
        """Everything the Bluetooth section draws, as plain data."""
        link = self._bluetooth
        connected = self._session is not None and self._session_established
        if link is None:
            return {
                "available": False,
                "detail": "not connected: Bluetooth needs a live session",
                **self._bluetooth_snapshot.to_dict(),
            }
        return {"available": connected, **link.snapshot().to_dict()}

    def sensor_status(self) -> dict[str, Any]:
        """The sensor registry plus the newest reading for each sensor."""
        link = self._sensors
        if link is None:
            return {
                "available": False,
                "detail": "not connected: sensors need a live session",
                **self._sensor_snapshot.to_dict(),
            }
        snapshot = link.snapshot()
        return {
            "available": self._session is not None and self._session_established,
            **snapshot.to_dict(),
            "interval_us": link.interval_us,
        }

    def files_status(self) -> dict[str, Any]:
        """Live transfers, finished history and where incoming files land."""
        manager = self._transfers
        if manager is None:
            return {
                "available": False,
                "detail": "not connected: file transfer needs a live session",
                "transfers": [],
                "history": [],
                "destination": str(self._files_directory or ""),
            }
        return {
            "available": self._session is not None and self._session_established,
            "detail": "",
            "transfers": [stats.to_dict() for stats in manager.transfers],
            "history": [stats.to_dict() for stats in manager.history()[-20:]],
            "destination": str(self._files_directory or ""),
        }

    def set_files_destination(self, directory: str | Path) -> Path:
        """Where incoming files are written. Chosen by the user, never guessed."""
        self._files_directory = Path(directory).expanduser()
        if self._transfers is not None:
            self._transfers.set_destination(self._files_directory)
        return self._files_directory

    def _bluetooth_or_explain(self, action: str) -> BluetoothLink | None:
        if self._bluetooth is None:
            self._log(f"Bluetooth: {action} needs a connected phone")
            return None
        return self._bluetooth

    async def start_scan(
        self, *, duration_ms: int = 10_000, filter_uuids: list[str] | None = None
    ) -> bool:
        """Start a BLE scan on the phone. It stops itself, or on ``stop_scan``."""
        link = self._bluetooth_or_explain("scanning")
        if link is None:
            return False
        try:
            started = await link.start_scan(duration_ms=duration_ms, filter_uuids=filter_uuids)
        except Exception as exc:  # noqa: BLE001 - the sentence is the product
            self._log(f"Bluetooth: could not start a scan: {exc}")
            return False
        self._log(
            "Bluetooth: scanning for peripherals"
            if started
            else f"Bluetooth: the scan did not start ({link.snapshot().detail})"
        )
        return started

    async def stop_scan(self) -> bool:
        link = self._bluetooth_or_explain("stopping a scan")
        if link is None:
            return False
        return await link.stop_scan()

    async def connect_device(self, address: str) -> bool:
        """Connect GATT to one peripheral the user picked from the list."""
        link = self._bluetooth_or_explain("connecting a peripheral")
        if link is None:
            return False
        try:
            connection = await link.connect(address)
        except Exception as exc:  # noqa: BLE001
            self._log(f"Bluetooth: could not connect to {address}: {exc}")
            return False
        self._log(f"Bluetooth: connected to {address} ({connection.state})")
        return connection.state == "connected"

    async def disconnect_device(self, address: str) -> bool:
        link = self._bluetooth_or_explain("disconnecting a peripheral")
        if link is None:
            return False
        ok = await link.disconnect(address)
        self._log(f"Bluetooth: {'disconnected' if ok else 'was not connected'} {address}")
        return ok

    async def gatt_services(self, address: str) -> Any:
        link = self._bluetooth_or_explain("browsing GATT services")
        return await link.services(address) if link is not None else None

    async def gatt_read(self, address: str, service: str, characteristic: str) -> bytes | None:
        link = self._bluetooth_or_explain("reading a characteristic")
        if link is None:
            return None
        return await link.read(address, service, characteristic)

    async def gatt_write(
        self,
        address: str,
        service: str,
        characteristic: str,
        data: bytes,
        *,
        with_response: bool = True,
    ) -> int | None:
        link = self._bluetooth_or_explain("writing a characteristic")
        if link is None:
            return None
        return await link.write(
            address, service, characteristic, data, with_response=with_response
        )

    async def gatt_subscribe(
        self, address: str, service: str, characteristic: str, *, enable: bool = True
    ) -> bool:
        link = self._bluetooth_or_explain("changing a subscription")
        if link is None:
            return False
        subscribed = await link.subscribe(address, service, characteristic, enable=enable)
        self._log(
            f"Bluetooth: notifications {'on' if subscribed else 'off'} for {characteristic}"
        )
        return subscribed

    async def start_sensors(self, sensor_ids: list[int] | None = None) -> bool:
        """Start the stream for the sensors the user ticked. Never all silently."""
        link = self._sensors
        if link is None:
            self._log("sensors: starting a stream needs a connected phone")
            return False
        ids = list(sensor_ids if sensor_ids is not None else self._sensor_ids)
        try:
            started = await link.start(ids or None)
        except Exception as exc:  # noqa: BLE001
            self._log(f"sensors: could not start: {exc}")
            return False
        self._sensor_ids = ids
        registry = link.snapshot().registry
        active_ids = list(registry.active) if registry is not None else []
        self._log(
            f"sensors: streaming {len(active_ids)} sensor(s)"
            if started
            else f"sensors: the phone refused ({link.snapshot().detail})"
        )
        return started

    async def stop_sensors(self) -> None:
        if self._sensors is not None:
            await self._sensors.stop("stopped by the user")

    def set_sensor_interval(self, interval_us: int) -> int:
        """Sampling interval. Takes effect on the next start, and says so."""
        if self._sensors is None:
            return max(1000, int(interval_us))
        return self._sensors.set_interval(interval_us)

    async def send_file(self, path: str | Path) -> TransferStats | None:
        manager = self._transfers
        if manager is None:
            self._log("files: sending needs a connected phone")
            return None
        return await manager.send_file(Path(path))

    async def request_file(self, name_hint: str = "") -> TransferStats | None:
        manager = self._transfers
        if manager is None:
            self._log("files: receiving needs a connected phone")
            return None
        return await manager.request_file(name_hint)

    async def pause_transfer(self, transfer_id: int) -> bool:
        return bool(self._transfers and await self._transfers.pause(transfer_id))

    async def resume_transfer(self, transfer_id: int) -> bool:
        return bool(self._transfers and await self._transfers.resume(transfer_id))

    async def cancel_transfer(self, transfer_id: int) -> None:
        if self._transfers is not None:
            await self._transfers.cancel(transfer_id)

    def _on_bluetooth(self, snapshot: BluetoothSnapshot) -> None:
        self._bluetooth_snapshot = snapshot
        self._publish_active()

    def _on_sensors(self, snapshot: SensorSnapshot) -> None:
        self._sensor_snapshot = snapshot
        if snapshot.state == "error":
            self._log(f"sensors: {snapshot.detail}")
        self._publish_active()

    def _on_transfer(self, stats: TransferStats) -> None:
        """Called for every chunk; logs only the transitions worth reading."""
        if stats.state == FILE_COMPLETE:
            verdict = "hash verified" if stats.verified else "hash NOT confirmed"
            self._log(f"files: {stats.name} ({stats.direction}) complete - {verdict}")
        elif stats.state == "failed":
            self._log(f"files: {stats.name} failed - {stats.error}")
        self._publish_active()

    def _active_plane_keys(self) -> set[str]:
        """Which Phase 5 capabilities are *running* right now.

        A radio that scanned a moment ago is not active; a scan in progress is. The
        same distinction holds for GATT (only while a link is up), sensors (only
        while frames flow) and files (only while a transfer is moving).
        """
        active = set(self._active)
        bluetooth = self._bluetooth.snapshot() if self._bluetooth is not None else None
        scanning = bool(bluetooth and bluetooth.scan.scanning)
        linking = bool(bluetooth and bluetooth.connections)
        sensors = self._sensors.snapshot() if self._sensors is not None else None
        streaming = bool(sensors and sensors.state == SENSOR_ACTIVE)
        moving = bool(
            self._transfers
            and any(
                stats.state not in {"complete", "failed", "cancelled"}
                for stats in self._transfers.transfers
            )
        )
        for key, running in (
            ("bluetooth_scan", scanning),
            ("bluetooth_gatt", linking),
            ("sensors", streaming),
            ("file_transfer", moving),
        ):
            if running:
                active.add(key)
            else:
                active.discard(key)
        if active == self._active:
            return active
        self._active = active
        return active

    def _publish_active(self) -> None:
        """Republish the capability table only when an Active flag changed.

        Transfer managers call their listener for every chunk; republishing on each
        one would rebuild the table thousands of times per file.
        """
        if self._active_plane_keys() != self._active_at_publish:
            self._active_at_publish = set(self._active)
            self._publish(capabilities=self._capabilities_with_active())

    async def _close_planes(self, reason: str) -> None:
        """Release all three Phase 5 planes, oldest first, without raising."""
        bluetooth, self._bluetooth = self._bluetooth, None
        sensors, self._sensors = self._sensors, None
        transfers, self._transfers = self._transfers, None
        for name, plane in (("bluetooth", bluetooth), ("sensors", sensors), ("files", transfers)):
            if plane is None:
                continue
            try:
                await plane.close(reason)
            except Exception as exc:  # noqa: BLE001 - teardown must not raise
                self._log(f"{name}: cleanup failed: {exc}")
        self._bluetooth_snapshot = BluetoothSnapshot(
            adapter=AdapterStatus(), scan=ScanState()
        )
        self._sensor_snapshot = SensorSnapshot()
        self._publish_active()

    # ------------------------------------------------------------------ #
    # audio (Phase 2)
    # ------------------------------------------------------------------ #
    @property
    def audio(self) -> AudioLink | None:
        """The live audio plane, or ``None`` while there is no session."""
        return self._audio

    def audio_status(self) -> dict[str, Any]:
        """Everything the Audio section shows, plus why a library is missing."""
        snap = self._audio_snapshot
        return {
            **snap.to_dict(),
            "connected": self._session is not None and self._session_established,
            "backends": backend_status(),
        }

    def _on_audio(self, snap: AudioSnapshot) -> None:
        """Called from the audio tasks; publishes, never blocks on the UI."""
        self._audio_snapshot = snap
        self._publish(
            audio_playback=snap.playback,
            audio_mic=snap.mic,
            audio_detail=snap.detail or snap.mic_detail,
            audio_volume=snap.volume,
            audio_muted=snap.muted,
        )
        self._active_audio_keys()
        if snap.playback == AUDIO_ERROR or snap.mic == AUDIO_ERROR:
            self._log(f"audio: {snap.detail or snap.mic_detail}")

    def _active_audio_keys(self) -> None:
        """The registry's ``active`` column follows the streams, not the wish."""
        snap = self._audio_snapshot
        active = set(self._active)
        for key, state in (("audio.playback", snap.playback), ("audio.capture", snap.mic)):
            if state == "active":
                active.add(key)
            else:
                active.discard(key)
        if active != self._active:
            self._active = active
            self._publish(capabilities=self._capabilities_with_active())

    def _capabilities_with_active(self) -> tuple[Capability, ...]:
        self._active = set(self._active)
        return tuple(
            capability.with_active(capability.key in self._active)
            for capability in self._snapshot.capabilities
        )

    async def _close_audio(self, reason: str) -> None:
        audio, self._audio = self._audio, None
        if audio is None:
            return
        try:
            await audio.close(reason)
        finally:
            self._audio_snapshot = audio.snapshot()
            self._audio_snapshot.playback = "idle"
            self._audio_snapshot.mic = "idle"
            self._publish(
                audio_playback="idle",
                audio_mic="idle",
                audio_detail=reason,
                audio_volume=self._audio_snapshot.volume,
                audio_muted=self._audio_snapshot.muted,
            )
            self._active_audio_keys()

    # ------------------------------------------------------------------ #
    # camera (Phase 4)
    # ------------------------------------------------------------------ #
    @property
    def camera(self) -> CameraStream | None:
        """The live camera stream, or ``None`` while there is no session."""
        return self._camera

    def camera_status(self) -> dict[str, Any]:
        """Everything the Camera section shows, plus whether a preview can draw."""
        if self._camera is None:
            return {
                "state": "idle",
                "detail": "not connected - connect first",
                "config": self._camera_config.as_dict(),
                "stats": {},
                "has_frame": False,
                "preview": camera_preview.preview_status(),
            }
        return {
            **self._camera.snapshot().to_dict(),
            "preview": camera_preview.preview_status(),
        }

    def camera_frame(self) -> Any:
        """The newest frame, for the GUI to decode. Never blocks, never raises."""
        return self._camera.latest_frame() if self._camera is not None else None

    def _on_camera(self, snap: Any) -> None:
        """Called from the stream's task; publishes state, never touches the UI."""
        config = snap.config or {}
        self._publish(
            camera_state=snap.state,
            camera_detail=snap.detail,
            camera_facing=str(config.get("facing", "back")),
            camera_width=int(config.get("width", 1280) or 1280),
            camera_height=int(config.get("height", 720) or 720),
            camera_fps=int(config.get("fps", 15) or 15),
            camera_frames=int(snap.stats.get("received", 0) or 0),
            camera_rotation=int(snap.stats.get("last_rotation", 0) or 0),
        )
        self._active_camera_keys(snap.state)
        if snap.state == CAMERA_ERROR:
            self._log(f"camera: {snap.detail}")

    def _active_camera_keys(self, state: str) -> None:
        """A lens is only ``active`` while frames are actually flowing."""
        active = set(self._active)
        for key in ("camera.back", "camera.front"):
            if state == CAMERA_ACTIVE and self._camera is not None:
                current = "camera.back" if self._camera.config.facing == "back" else "camera.front"
                if key == current:
                    active.add(key)
                else:
                    active.discard(key)
            else:
                active.discard(key)
        if active != self._active:
            self._active = active
            self._publish(capabilities=self._capabilities_with_active())

    async def _close_camera(self, reason: str) -> None:
        camera, self._camera = self._camera, None
        if camera is None:
            return
        try:
            await camera.close(reason)
        finally:
            self._publish(camera_state="idle", camera_detail=reason, camera_frames=0)
            self._active_camera_keys("idle")

    async def _camera_or_explain(self, action: str) -> CameraStream | None:
        camera = self._camera
        if camera is None:
            self._log(f"camera: {action} needs a connected session")
            self._publish(camera_detail="not connected - connect first")
        return camera

    def set_camera_config(
        self, *, width: int, height: int, fps: int, facing: str | None = None
    ) -> VideoConfig:
        """Validate a resolution/rate choice; nonsense raises instead of guessing."""
        self._camera_config = VideoConfig(
            width=width,
            height=height,
            fps=fps,
            facing=facing or self._camera_config.facing,
            quality=self._camera_config.quality,
        )
        return self._camera_config

    async def start_camera(self) -> bool:
        camera = await self._camera_or_explain("camera streaming")
        if camera is None:
            return False
        ok = await camera.start(self._camera_config)
        self._publish(camera_state=camera.snapshot().state, camera_detail=camera.snapshot().detail)
        return ok

    async def stop_camera(self, reason: str = "stopped by the user") -> None:
        if self._camera is not None:
            await self._camera.stop(reason)

    async def switch_to_other_lens(self) -> bool:
        """Flip rear <-> front. The stream restarts on the other lens."""
        current = self._camera_config.facing
        return await self.switch_camera("front" if current == "back" else "back")

    async def switch_camera(self, facing: str) -> bool:
        camera = await self._camera_or_explain("switching camera")
        if camera is None:
            return False
        ok = await camera.switch(facing)
        self._camera_config = camera.config
        return ok

    async def _audio_or_explain(self, action: str) -> AudioLink | None:
        audio = self._audio
        if audio is None:
            self._log(f"audio: {action} needs a connected session")
            self._publish(audio_detail="not connected - connect first")
        return audio

    async def start_audio_playback(self) -> bool:
        audio = await self._audio_or_explain("PC audio to the phone")
        if audio is None:
            return False
        self._log("audio: starting PC audio to the phone")
        ok = await audio.start_playback()
        self._on_audio(audio.snapshot())
        self._log("audio: PC audio is playing on the phone" if ok else "audio: PC audio did not start")
        return ok

    async def stop_audio_playback(self) -> None:
        if self._audio is not None:
            self._log("audio: stopping PC audio")
            await self._audio.stop_playback("stopped by the user")
            self._on_audio(self._audio.snapshot())

    async def start_mic(self) -> bool:
        audio = await self._audio_or_explain("the phone microphone")
        if audio is None:
            return False
        self._log("audio: asking the phone for its microphone")
        ok = await audio.start_mic()
        self._on_audio(audio.snapshot())
        self._log("audio: microphone stream is playing" if ok else "audio: microphone did not start")
        return ok

    async def stop_mic(self) -> None:
        if self._audio is not None:
            self._log("audio: stopping the phone microphone")
            await self._audio.stop_mic("stopped by the user")
            self._on_audio(self._audio.snapshot())

    async def set_audio_volume(self, percent: int) -> int:
        if self._audio is None:
            return self._audio_snapshot.volume
        volume = await self._audio.set_volume(percent)
        self._on_audio(self._audio.snapshot())
        return volume

    async def set_audio_mute(self, muted: bool) -> bool:
        if self._audio is None:
            return muted
        value = await self._audio.set_mute(muted)
        self._on_audio(self._audio.snapshot())
        return value

    async def diagnose_usb(self) -> UsbReport:
        """Run the whole USB chain on demand, and say where it stopped.

        This is the button a user presses *after* changing something (another
        cable, a driver, the USB mode on the phone). It bypasses every cache, so
        two presses separated by a replug can report different stages. Nothing
        here kills the adb server, deletes a key, or installs anything.
        """
        devices: list[Any] = []
        error = ""
        server = "not checked"
        available = False
        path = ""
        version = ""
        try:
            available = bool(self._adb.available())
            path = self._adb.path or ""
            if available:
                version = self._adb.version() or ""
                devices, error, server = self._adb.probe_devices()
        except Exception as exc:  # noqa: BLE001 - a diagnosis must not raise at the user
            error = f"adb could not be run: {exc}"
        report = build_report(
            adb_devices=devices,
            pnp=self._device_tree(force=True),
            adb_available=available,
            adb_path=path,
            adb_version=version,
            adb_server=server,
            adb_error=error,
            system=self._system,
        )
        self._usb_report = report
        for line in format_report(report).splitlines():
            self._log(line)
        self._publish(
            usb_diagnosis=report.headline,
            usb_action=report.action or "—",
            usb_stage=report.stage or "—",
        )
        return report

    @property
    def usb_report(self) -> UsbReport | None:
        """The last on-demand report, or ``None`` if nobody pressed the button."""
        return self._usb_report

    def _network_fingerprint(self) -> str:
        """The set of default gateways — i.e. which networks this PC is on."""
        try:
            gateways = self._gateways()
        except Exception:  # noqa: BLE001 - a missing route command is not a failure
            return ""
        return "|".join(sorted(g.address for g in gateways if g.address))

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
        usb_diagnosis = UsbDiagnosis(USB_UNKNOWN, "USB has not been checked yet")

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
                usb_diagnosis = self._usb_diagnosis(provider.last_devices, status.available)
                lines.append(adb_detail)
                lines.append(f"usb: {usb_diagnosis.line()}")
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
            "usb_diagnosis": usb_diagnosis,
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
        self._session_established = True
        self._fingerprint = self._network_fingerprint()
        # Phase 4. Built on the same authenticated session as audio; frames ride
        # stream 3 inside the existing envelope, so the camera has no transport of
        # its own to get wrong. Nothing is opened until the user presses start.
        self._camera = CameraStream(
            session, config=self._camera_config, on_change=self._on_camera
        )
        # Audio is only ever built on an authenticated session: the AudioLink has
        # no socket of its own, so this ordering is the security property.
        self._audio = AudioLink(
            session,
            source_factory=self._source_factory,
            sink_factory=self._sink_factory,
            on_change=self._on_audio,
        )
        # Phase 5. Same session, same envelope: the phone is the radio endpoint,
        # so nothing here opens a socket of its own. Nothing scans, streams or
        # connects until the user asks.
        self._bluetooth = BluetoothLink(session, on_change=self._on_bluetooth)
        self._sensors = SensorLink(session, on_change=self._on_sensors)
        self._transfers = TransferManager(
            session,
            destination=self._files_directory,
            on_change=self._on_transfer,
        )
        self._active_audio_keys()
        self._log(
            f"connected over {candidate.kind} at {candidate.endpoint}; "
            f"{sum(1 for c in registry if c.available)} of {len(registry)} capabilities available"
        )
        self._heartbeat = asyncio.create_task(
            self._heartbeat_loop(session), name="phonebridge-heartbeat"
        )
        reason = ""
        try:
            reason = await session.wait_closed()
            self._log(f"the session closed: {reason}")
        finally:
            await self._teardown_session()

        if self._stop.is_set():
            # Disconnecting on purpose is not a drop; reporting one would make a
            # normal exit look like a failure.
            self._log("session closed on request")
            return

        # A close reason of "1006" says the socket died; it never says why. Two
        # cheap facts turn it into something actionable: does the phone still
        # answer, and is this still the same network? A dropped session then goes
        # back through the retry ladder with backoff — it must not spin.
        explanation = await self._explain_drop(candidate)
        self._last_drop_reason = explanation
        self._publish(last_connection_error=explanation)
        self._log(f"why the link dropped: {explanation}")
        raise _RetryCandidate(explanation)

    async def _explain_drop(self, candidate: Candidate) -> str:
        """Turn "the socket closed" into "the network moved" or "the phone went".

        Deliberately two facts and no guessing, because the fixes differ: a moved
        network needs a fresh address, a silent phone needs the app restarted (and
        on HyperOS, the battery restriction lifted).
        """
        reachable = await self._probe(candidate.host, candidate.port, 1.0)
        current = self._network_fingerprint()
        was = self._fingerprint
        self._fingerprint = current
        parts: list[str] = []
        if was and current and was != current:
            parts.append(f"the network changed ({was} -> {current})")
        elif was and current and was == current:
            parts.append("the network is unchanged")
        parts.append(
            "the phone still answers on that address"
            if reachable
            else "nothing answers at that address now"
        )
        if not reachable:
            parts.append(
                "keep the bridge running in the foreground and allow PhoneBridge "
                "unrestricted battery use (HyperOS stops background apps)"
            )
        return f"{candidate.kind} at {candidate.endpoint}: " + "; ".join(parts)

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
        # Camera first: a lens left open is worse than a stuck speaker, and both
        # must be gone before the session object is dropped.
        await self._close_camera("session ended")
        await self._close_audio("session ended")
        await self._close_planes("session ended")
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
