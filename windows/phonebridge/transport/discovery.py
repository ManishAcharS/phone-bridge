"""UDP discovery beacon listener.

The phone broadcasts (and the PC listens -- no reply, no handshake) a small
JSON datagram every 2 seconds::

    {"app":"phonebridge","v":1,"id":"<device id>","name":"Pixel 7",
     "port":8765,"method":"wifi","ts":1234567890}

Why a raw broadcast instead of mDNS/NSD: it is ~30 lines on each side, has no
extra dependency, works identically on a LAN, on the phone's hotspot and over
USB tethering, and cannot fail because of missing Avahi/Bonjour on Windows.
Discovery is a *convenience*: the beacon carries no keys and grants no trust,
so an unauthenticated announcement is harmless -- the handshake still decides
whether the phone is allowed in.

Note: broadcast is not routed across subnets and is unreliable on some Wi-Fi
APs with client isolation.  Manual entry of an IP always works, and the last
known endpoint is cached, so discovery is never required.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field

DISCOVERY_PORT = 47823
BEACON_MAGIC = "phonebridge"

HELP = (
    "If nothing is found: confirm the PC and phone are on the same network, "
    "check that the AP does not use client isolation, and allow UDP "
    f"{DISCOVERY_PORT} through Windows Firewall (inbound, Private profile). "
    "You can always connect by typing the phone's IP:port."
)


@dataclass
class DiscoveredPhone:
    device_id: str
    name: str
    host: str
    port: int
    method: str
    last_seen: float = field(default_factory=time.time)

    @property
    def uri(self) -> str:
        return f"ws://{self.host}:{self.port}"

    def to_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "method": self.method,
            "uri": self.uri,
            "age_s": round(time.time() - self.last_seen, 1),
        }


class _BeaconProtocol(asyncio.DatagramProtocol):
    def __init__(self, table: dict[str, DiscoveredPhone], on_new: Callable[[DiscoveredPhone], None]):
        self.table = table
        self.on_new = on_new

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        try:
            msg = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(msg, dict) or msg.get("app") != BEACON_MAGIC:
            return
        if msg.get("v") != 1:
            return
        device_id = str(msg.get("id", "")).strip()
        port = msg.get("port")
        if not device_id or not isinstance(port, int) or not 0 < port < 65536:
            return
        host = addr[0]
        phone = DiscoveredPhone(
            device_id=device_id,
            name=str(msg.get("name", "android-phone")),
            host=host,
            port=port,
            method=str(msg.get("method", "wifi")),
        )
        is_new = device_id not in self.table
        self.table[device_id] = phone
        if is_new:
            self.on_new(phone)


class DiscoveryService:
    """Listen for phone beacons; keeps the freshest entry per device id."""

    def __init__(self, port: int = DISCOVERY_PORT) -> None:
        self.port = port
        self.phones: dict[str, DiscoveredPhone] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self._new_event: asyncio.Event = asyncio.Event()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        def _on_new(_phone: DiscoveredPhone) -> None:
            self._new_event.set()

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT") and hasattr(socket, "SO_BROADCAST"):
            try:  # noqa: SIM105
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                pass
        try:
            sock.bind(("0.0.0.0", self.port))
        except OSError as exc:
            sock.close()
            raise RuntimeError(
                f"cannot bind UDP {self.port} for discovery: {exc}. "
                "Another PhoneBridge instance may be running."
            ) from exc
        sock.setblocking(False)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _BeaconProtocol(self.phones, _on_new), sock=sock
        )
        self._transport = transport  # type: ignore[assignment]

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def live(self, max_age_s: float = 8.0) -> list[DiscoveredPhone]:
        now = time.time()
        return sorted(
            (p for p in self.phones.values() if now - p.last_seen <= max_age_s),
            key=lambda p: p.last_seen,
            reverse=True,
        )

    async def wait_for_first(self, timeout: float = 5.0) -> DiscoveredPhone | None:
        self._new_event.clear()
        try:  # noqa: SIM105
            await asyncio.wait_for(self._new_event.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        live = self.live()
        return live[0] if live else None
