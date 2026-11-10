"""Where the phone might be, and which of those to try first.

The project has four ways to reach a phone, and the old CLI made the user choose
between them. This module asks all of them and ranks the answers:

===================  =====  =================================================
source               score  why
===================  =====  =================================================
a cable (adb)        100    no network involved; survives Wi-Fi drops
address typed by hand 95    the user said so; nothing overrides intent
adb over Wi-Fi        90    debugging bridge, still a real network path
beacon, phone pinned  85    fresh, and the identity matches a paired phone
beacon                80    fresh; the address is the datagram's source
beacon, advertised    78    an extra interface the phone listed (hotspot + LAN)
**hotspot gateway**   60    the phone *is* the gateway when it runs the hotspot
cached endpoint       50    worked before; a TCP connect proves it still answers
===================  =====  =================================================

The gateway row is the hotspot fix. A phone acting as an access point is the
default gateway of the PC that joined it, so one outbound TCP connect to that
address answers "is the server there?" even when UDP broadcast is blocked by
Windows Firewall or never left the phone. One address per interface: bounded,
readable from the log, and nothing like a network scan.

Deliberately absent: mDNS (the beacon is cheaper and already shipped), subnet
sweeps, and any guess that the hotspot subnet is 192.168.43.x.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from ..core.device_store import DeviceStore
from ..transport.discovery import DiscoveredPhone, DiscoveryService
from ..transport.link import DEFAULT_PORT
from .adb import (
    CONNECTION_USB,
    CONNECTION_WIRELESS,
    REMOTE_PORT,
    Adb,
    AdbDevice,
    AdbError,
    AdbStatus,
    ForwardLease,
)
from .netprobe import Gateway, default_gateways, tcp_probe

log = logging.getLogger(__name__)

SCORE_USB = 100
SCORE_MANUAL = 95
SCORE_ADB_WIRELESS = 90
SCORE_BEACON_PINNED = 85
SCORE_BEACON = 80
SCORE_BEACON_ADVERTISED = 78
SCORE_GATEWAY = 60
SCORE_CACHED = 50

TRANSPORT_USB = "usb"
TRANSPORT_ADB_WIRELESS = "adb-wireless"
TRANSPORT_WIFI = "wifi"
TRANSPORT_HOTSPOT = "hotspot"
TRANSPORT_GATEWAY = "gateway"
TRANSPORT_CACHED = "cached"
TRANSPORT_MANUAL = "manual"

__all__ = [
    "SCORE_ADB_WIRELESS",
    "SCORE_BEACON",
    "SCORE_BEACON_ADVERTISED",
    "SCORE_BEACON_PINNED",
    "SCORE_CACHED",
    "SCORE_GATEWAY",
    "SCORE_MANUAL",
    "SCORE_USB",
    "BeaconCandidates",
    "CachedCandidates",
    "Candidate",
    "CandidateProvider",
    "FixedCandidates",
    "GatewayCandidates",
    "UsbCandidates",
    "default_providers",
    "rank",
    "select_first",
    "tcp_probe",
]


@dataclass(frozen=True)
class Candidate:
    """One address that might accept a connection."""

    kind: str
    host: str
    port: int
    score: int
    detail: str
    device_id: str | None = None
    name: str | None = None
    lease: ForwardLease | None = None
    discovered_at: float = field(default_factory=time.time)

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def __str__(self) -> str:  # pragma: no cover - used in logs only
        return f"{self.kind} {self.endpoint} ({self.score})"


class CandidateProvider(Protocol):
    """Anything that can suggest addresses for the phone."""

    name: str

    async def candidates(self, *, timeout: float) -> list[Candidate]:
        """Addresses worth trying. Never raises: failures become an empty list."""
        ...


def rank(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Best first: score, then the freshest discovery."""
    return sorted(candidates, key=lambda c: (-c.score, -c.discovered_at))


def select_first(candidates: Sequence[Candidate]) -> Candidate | None:
    ranked = rank(candidates)
    return ranked[0] if ranked else None


def _kind_from_beacon(phone: DiscoveredPhone) -> str:
    """The phone labels its own network; keep its word, normalised."""
    method = (phone.method or TRANSPORT_WIFI).strip().lower()
    if method in {TRANSPORT_USB, TRANSPORT_WIFI, TRANSPORT_HOTSPOT}:
        return method
    return TRANSPORT_WIFI


# --------------------------------------------------------------------------- #
# USB and adb-over-Wi-Fi
# --------------------------------------------------------------------------- #
class UsbCandidates:
    """Candidates from attached adb devices, with the forward set up on demand.

    ``status()`` is what the window shows: where adb was found, whether a cable
    is present, and why not when there is none.
    """

    name = "adb"

    def __init__(
        self,
        adb: Adb,
        *,
        remote_port: int = REMOTE_PORT,
        include_wireless: bool = True,
    ) -> None:
        self.adb = adb
        self.remote_port = remote_port
        self.include_wireless = include_wireless
        #: Set up forwards, so ``release`` can undo exactly these.
        self.leases: list[ForwardLease] = []
        self.last_status: AdbStatus | None = None
        self.last_devices: list[AdbDevice] = []
        self.last_error: str | None = None

    def status(self) -> AdbStatus:
        return self.adb.status()

    async def candidates(self, *, timeout: float) -> list[Candidate]:
        self.last_error = None
        status = await asyncio.to_thread(self.adb.status)
        self.last_status = status
        if not status.available:
            log.info("adb: not found (%s)", status.error)
            return []
        try:
            devices = await asyncio.to_thread(self.adb.devices)
        except AdbError as exc:
            self.last_error = str(exc)
            self.last_devices = []
            log.warning("adb: `adb devices` failed: %s", exc)
            return []
        self.last_devices = list(devices)
        found: list[Candidate] = []
        usable = 0
        for device in devices:
            if not device.usable:
                log.info("adb: skipping %s", device.explain())
                continue
            if device.connection != CONNECTION_USB and not self.include_wireless:
                log.info("adb: ignoring wireless bridge %s", device.serial)
                continue
            usable += 1
            try:
                lease = await asyncio.to_thread(
                    self.adb.ensure_forward, device.serial, self.remote_port
                )
            except AdbError as exc:
                self.last_error = str(exc)
                log.warning("adb: forward failed for %s: %s", device.serial, exc)
                continue
            if lease is not None:
                self.leases.append(lease)
            port = lease.local_port if lease else (
                await asyncio.to_thread(
                    self.adb.existing_forward, device.serial, self.remote_port
                )
            )
            if port is None:  # pragma: no cover - ensure_forward guarantees one
                continue
            wireless = device.connection != CONNECTION_USB
            found.append(
                Candidate(
                    kind=TRANSPORT_ADB_WIRELESS if wireless else TRANSPORT_USB,
                    host="127.0.0.1",
                    port=int(port),
                    score=SCORE_ADB_WIRELESS if wireless else SCORE_USB,
                    detail=(
                        f"{'wireless debugging' if wireless else 'USB'} via {device.serial}: "
                        f"forwarding tcp:{port} -> phone tcp:{self.remote_port}"
                        + ("" if lease else " (existing rule, left in place)")
                    ),
                    name=device.display_name,
                )
            )
        if not usable:
            self.last_error = (
                "adb is installed but no device is connected — plug in the cable "
                "and accept the debugging prompt on the phone"
            )
            log.info("adb: %s", self.last_error)
        return found

    @property
    def usb_available(self) -> bool:
        """True only for a device adb reached over a cable — never for a bridge."""
        return any(d.is_usb and d.usable for d in self.last_devices)

    def summary(self) -> str:
        """One honest sentence about adb, for the diagnostics panel."""
        status = self.last_status
        if status is None:
            return "adb has not been checked yet"
        if not status.available:
            return status.error or "adb not found (USB unavailable)"
        if self.last_error and not self.last_devices:
            return self.last_error
        if not self.last_devices:
            return "adb is installed but no device is listed"
        cabled = [d for d in self.last_devices if d.is_usb and d.usable]
        bridged = [d for d in self.last_devices if d.connection == CONNECTION_WIRELESS]
        broken = [d for d in self.last_devices if not d.usable]
        parts: list[str] = []
        if cabled:
            parts.append(f"{len(cabled)} on USB ({cabled[0].display_name})")
        else:
            parts.append("no USB device")
        if bridged:
            parts.append(f"{len(bridged)} over wireless debugging")
        if broken:
            parts.append(f"{len(broken)} not ready: {broken[0].explain()}")
        return "adb: " + ", ".join(parts)

    async def release(self) -> None:
        """Remove the rules this provider created. Never touches anyone else's."""
        for lease in self.leases:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.adb.remove_forward, lease)
        self.leases.clear()


# --------------------------------------------------------------------------- #
# discovery beacon
# --------------------------------------------------------------------------- #
class BeaconCandidates:
    """Candidates from the UDP beacon the phone already broadcasts."""

    name = "beacon"

    def __init__(
        self,
        discovery: DiscoveryService | None = None,
        store: DeviceStore | None = None,
        *,
        port: int = DEFAULT_PORT,
    ) -> None:
        self._discovery = discovery
        self.store = store
        self.port = port
        self.last_seen: list[DiscoveredPhone] = []
        self.last_error_permission: str | None = None

    def _pinned_ids(self) -> set[str]:
        if self.store is None:
            return set()
        return {peer.peer_id for peer in self.store.list_peers()}

    async def candidates(self, *, timeout: float) -> list[Candidate]:
        service = self._discovery or DiscoveryService()
        own_service = self._discovery is None
        found: list[DiscoveredPhone] = []
        try:
            await service.start()
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                found = service.live(max_age_s=8.0)
                if found or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.1)
        except (OSError, RuntimeError) as exc:
            # A blocked port is not the same failure as a silent network, and the
            # window says which one it is.
            self.last_error_permission = str(exc)
            log.warning("beacon: could not listen: %s", exc)
            return []
        finally:
            if own_service:
                with contextlib.suppress(Exception):
                    await service.stop()
        self.last_seen = list(found)
        pinned = self._pinned_ids()
        out: list[Candidate] = []
        seen: set[tuple[str, int]] = set()

        def push(host: str, port: int, score: int, detail: str, phone: DiscoveredPhone,
                 is_pinned: bool) -> None:
            key = (host, port)
            if key in seen:
                return
            seen.add(key)
            out.append(
                Candidate(
                    kind=_kind_from_beacon(phone),
                    host=host,
                    port=port,
                    score=score,
                    detail=detail,
                    device_id=phone.device_id,
                    name=phone.name,
                    discovered_at=time.time(),
                )
            )
            del is_pinned

        for phone in found:
            is_pinned = phone.device_id in pinned
            base = SCORE_BEACON_PINNED if is_pinned else SCORE_BEACON
            who = f"{'pinned phone' if is_pinned else 'beacon'} \"{phone.name}\""
            push(
                phone.host,
                int(phone.port or self.port),
                base,
                f"{who} announcing from {phone.host}:{phone.port}",
                phone,
                is_pinned,
            )
            # Newer phones also list every interface they have (a hotspot address
            # and a LAN address at once). The datagram source is still the most
            # trustworthy, so the advertised ones are ranked just below it.
            for extra in getattr(phone, "hosts", ()) or ():
                if extra == phone.host:
                    continue
                push(
                    extra,
                    int(phone.port or self.port),
                    SCORE_BEACON_ADVERTISED,
                    f"{who} also advertised {extra}:{phone.port}",
                    phone,
                    is_pinned,
                )
        return out

    async def release(self) -> None:  # pragma: no cover - symmetry with USB
        return None


# --------------------------------------------------------------------------- #
# hotspot: the gateway is the phone
# --------------------------------------------------------------------------- #
class GatewayCandidates:
    """The default gateway of each interface, when it answers on the app port.

    Bounded on purpose: one address per interface, and only kept if a TCP connect
    succeeds. That is the difference between "try the router I am already using"
    and "scan the network".
    """

    name = "gateway"

    def __init__(
        self,
        *,
        port: int = REMOTE_PORT,
        gateways: Callable[[], Sequence[Gateway]] | None = None,
        probe: Callable[[str, int, float], Awaitable[bool]] | None = None,
    ) -> None:
        self.port = port
        self._gateways = gateways or default_gateways
        self._probe = probe or tcp_probe
        self.last_checked: list[Gateway] = []
        self.last_answered: list[Gateway] = []
        self.last_error: str | None = None

    async def candidates(self, *, timeout: float) -> list[Candidate]:
        self.last_error = None
        try:
            gateways = await asyncio.to_thread(self._gateways)
        except Exception as exc:  # noqa: BLE001 - a missing route command is normal
            self.last_error = str(exc)
            log.debug("gateway: could not read the routing table: %s", exc)
            return []
        self.last_checked = list(gateways)
        self.last_answered = []
        out: list[Candidate] = []
        for gateway in gateways:
            if not await self._probe(gateway.address, self.port, min(1.0, max(0.2, timeout))):
                continue
            self.last_answered.append(gateway)
            out.append(
                Candidate(
                    kind=TRANSPORT_GATEWAY,
                    host=gateway.address,
                    port=self.port,
                    score=SCORE_GATEWAY,
                    detail=(
                        f"{gateway.describe()} answers on tcp:{self.port} — likely the "
                        "phone running the hotspot"
                    ),
                )
            )
        if not out and gateways:
            self.last_error = (
                "the gateway "
                + ", ".join(g.address for g in gateways)
                + f" did not answer on tcp:{self.port}"
            )
        return out

    async def release(self) -> None:  # pragma: no cover - symmetry with USB
        return None


# --------------------------------------------------------------------------- #
# an address the user typed
# --------------------------------------------------------------------------- #
class FixedCandidates:
    """An endpoint the user entered in the window.

    Deliberately outranks every automatic source: if someone types an address,
    guessing something else instead would be infuriating. The TCP probe only
    decides what the diagnostic says, never whether the attempt is made.
    """

    name = "manual"

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        probe: Callable[[str, int, float], Awaitable[bool]] | None = None,
    ) -> None:
        self.host = host.strip()
        self.port = port
        self._probe = probe or tcp_probe
        self.reachable: bool | None = None

    async def candidates(self, *, timeout: float) -> list[Candidate]:
        if not self.host:
            return []
        reachable = await self._probe(self.host, self.port, min(1.5, max(0.3, timeout)))
        self.reachable = reachable
        note = "answers" if reachable else "does not answer"
        return [
            Candidate(
                kind=TRANSPORT_MANUAL,
                host=self.host,
                port=self.port,
                score=SCORE_MANUAL,
                detail=f"{self.host}:{self.port} (entered by hand) {note} on tcp:{self.port}",
                discovered_at=time.time(),
            )
        ]

    async def release(self) -> None:  # pragma: no cover - symmetry with USB
        return None


# --------------------------------------------------------------------------- #
# cached endpoint
# --------------------------------------------------------------------------- #
class CachedCandidates:
    """Candidates from ``known_hosts.json``, validated before they are offered."""

    name = "cached"

    def __init__(
        self,
        store: DeviceStore,
        *,
        probe=tcp_probe,
        limit: int = 3,
    ) -> None:
        self.store = store
        self.probe = probe
        self.limit = limit

    async def candidates(self, *, timeout: float) -> list[Candidate]:
        out: list[Candidate] = []
        for entry in self.store.known_endpoints()[: self.limit]:
            host = str(entry.get("host") or "")
            if not host:
                continue
            port = int(entry.get("port") or DEFAULT_PORT)
            if not await self.probe(host, port, min(1.0, max(0.2, timeout))):
                log.info("cached: %s:%s does not answer, skipping", host, port)
                continue
            out.append(
                Candidate(
                    kind=TRANSPORT_CACHED,
                    host=host,
                    port=port,
                    score=SCORE_CACHED,
                    detail=f"last known address {host}:{port} still answers",
                    device_id=entry.get("device_id"),
                    discovered_at=0.0,
                )
            )
        return out

    async def release(self) -> None:  # pragma: no cover - symmetry with USB
        return None


# --------------------------------------------------------------------------- #
# defaults
# --------------------------------------------------------------------------- #
def default_providers(
    store: DeviceStore,
    adb: Adb | None = None,
    *,
    usb: UsbCandidates | None = None,
    manual: FixedCandidates | None = None,
) -> list[CandidateProvider]:
    """adb first, then the beacon, then the hotspot gateway, then the cache."""
    usb_provider = usb or UsbCandidates(adb or Adb())
    providers: list[CandidateProvider] = [usb_provider, BeaconCandidates(store=store)]
    providers.append(GatewayCandidates())
    if manual is not None:
        providers.append(manual)
    providers.append(CachedCandidates(store))
    return providers
