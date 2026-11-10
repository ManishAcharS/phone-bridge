"""Where the phone might be, and which of those to try first.

The project already has three ways to reach a phone: an adb forward over USB, the
UDP discovery beacon, and the last address that worked. The old CLI made the user
choose. This module asks all three and ranks the answers:

===============  =====  ==================================================
source           score  why
===============  =====  ==================================================
USB (adb)        100    no network involved; survives Wi-Fi drops
beacon, pinned    85    fresh, and the identity matches a phone we paired
beacon            80    fresh, address observed seconds ago
cached, valid     50    worked before; a TCP connect proves it still answers
===============  =====  ==================================================

Everything is a :class:`Candidate`, so the manager can try them in order without
knowing how they were found, and a test can hand it a fixed list.

Two deliberate non-features: there is no mDNS provider (the beacon is cheaper and
already shipped), and there is no subnet sweep (it would look like a port scan to
every network the user joins).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from ..core.device_store import DeviceStore
from ..transport.discovery import DiscoveredPhone, DiscoveryService
from ..transport.link import DEFAULT_PORT
from .adb import REMOTE_PORT, Adb, AdbError, ForwardLease, is_wireless_serial

log = logging.getLogger(__name__)

#: Scores are deliberately far apart so ordering is obvious in a log line.
SCORE_USB = 100
SCORE_BEACON_PINNED = 85
SCORE_BEACON = 80
SCORE_CACHED = 50

TRANSPORT_USB = "usb"
TRANSPORT_WIFI = "wifi"
TRANSPORT_HOTSPOT = "hotspot"
TRANSPORT_CACHED = "cached"


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
# USB
# --------------------------------------------------------------------------- #
class UsbCandidates:
    """Candidates from attached adb devices, with the forward set up on demand."""

    name = "usb"

    def __init__(self, adb: Adb, *, remote_port: int = REMOTE_PORT) -> None:
        self.adb = adb
        self.remote_port = remote_port
        #: Set up forwards, so ``release`` can undo exactly these.
        self.leases: list[ForwardLease] = []

    async def candidates(self, *, timeout: float) -> list[Candidate]:
        if not self.adb.available():
            log.debug("usb: adb is not on PATH")
            return []
        try:
            devices = await asyncio.to_thread(self.adb.devices)
        except AdbError as exc:
            log.warning("usb: `adb devices` failed: %s", exc)
            return []
        found: list[Candidate] = []
        for device in devices:
            if not device.usable:
                log.info("usb: skipping %s", device.explain())
                continue
            try:
                lease = await asyncio.to_thread(
                    self.adb.ensure_forward, device.serial, self.remote_port
                )
            except AdbError as exc:
                log.warning("usb: forward failed for %s: %s", device.serial, exc)
                continue
            if lease is not None:
                self.leases.append(lease)
            port = lease.local_port if lease else (
                await asyncio.to_thread(self.adb.existing_forward, device.serial, self.remote_port)
            )
            if port is None:  # pragma: no cover - ensure_forward guarantees one
                continue
            kind = "usb" if not is_wireless_serial(device.serial) else "usb-wireless"
            found.append(
                Candidate(
                    kind=kind,
                    host="127.0.0.1",
                    port=int(port),
                    score=SCORE_USB,
                    detail=(
                        f"USB via {device.serial}: forwarding tcp:{port} -> "
                        f"phone tcp:{self.remote_port}"
                        + ("" if lease else " (existing rule, left in place)")
                    ),
                    name=device.display_name,
                )
            )
        return found

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
        except OSError as exc:  # port in use, no broadcast interface, ...
            log.warning("beacon: could not listen: %s", exc)
            return []
        finally:
            if own_service:
                with contextlib.suppress(Exception):
                    await service.stop()
        self.last_seen = list(found)
        pinned = self._pinned_ids()
        out: list[Candidate] = []
        for phone in found:
            is_pinned = phone.device_id in pinned
            out.append(
                Candidate(
                    kind=_kind_from_beacon(phone),
                    host=phone.host,
                    port=int(phone.port or self.port),
                    score=SCORE_BEACON_PINNED if is_pinned else SCORE_BEACON,
                    detail=(
                        f"{'pinned phone' if is_pinned else 'beacon'} "
                        f"\"{phone.name}\" at {phone.host}:{phone.port}"
                    ),
                    device_id=phone.device_id,
                    name=phone.name,
                    discovered_at=time.time(),
                )
            )
        return out

    async def release(self) -> None:  # pragma: no cover - symmetry with USB
        return None


# --------------------------------------------------------------------------- #
# cached endpoint
# --------------------------------------------------------------------------- #
async def tcp_probe(host: str, port: int, timeout: float = 0.6) -> bool:
    """Does something accept TCP on ``host:port`` right now?

    A connect-and-close, not a handshake: it only decides whether the address is
    worth a full session attempt, so a stale cached address costs 0.6 s instead
    of a failed handshake and a confusing error.
    """
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    del reader
    return True


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
    store: DeviceStore, adb: Adb | None = None, *, usb: UsbCandidates | None = None
) -> list[CandidateProvider]:
    """USB first, then the beacon, then the cache — the order products expect."""
    usb_provider = usb or UsbCandidates(adb or Adb())
    return [usb_provider, BeaconCandidates(store=store), CachedCandidates(store)]
