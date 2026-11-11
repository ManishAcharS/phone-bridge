"""Bluetooth models: scan results, GATT trees, characteristic values (Phase 5).

Bluetooth on this project is **not** a binary media plane. Scan results and GATT
answers are small, structured and infrequent compared with audio or camera frames,
so they travel as ordinary RPC results and control events on the authenticated
encrypted session. That has three consequences worth stating up front:

* there is no second radio, no second socket and nothing plaintext: the phone's
  radio is the endpoint, and the PC talks to it through PhoneBridge;
* a characteristic value is carried as base64 inside JSON, capped at
  :data:`MAX_VALUE_BYTES` — large enough for a 512-byte BLE attribute plus its
  encoding overhead, small enough that a notification cannot become a file
  transfer in disguise;
* nothing here pairs, bonds or bypasses security. The app reads and writes what
  the peripheral already exposes to an unpaired central; a peripheral that demands
  bonding refuses, and that refusal is reported.

This module is pure data plus validation — no I/O, no asyncio, no Android.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from typing import Any

#: 512-byte attribute value, base64 (4/3), plus room for the JSON envelope.
MAX_VALUE_BYTES = 512

#: How many devices a scan keeps. A busy office has dozens of beacons; the list is
#: for a person to read, not an inventory of the neighbourhood.
MAX_DEVICES = 200

#: How many notifications are kept for the viewer. Bounded on purpose: a chatty
#: characteristic at 50 Hz would otherwise grow without limit.
MAX_NOTIFICATIONS = 200

_UUID_RE = re.compile(
    r"^(?:[0-9a-f]{4}|[0-9a-f]{8}|[0-9a-f]{32}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

#: Characteristic properties as the GATT spec spells them.
PROPERTIES: tuple[str, ...] = (
    "read",
    "write",
    "write_no_response",
    "notify",
    "indicate",
    "broadcast",
    "signed_write",
)

#: Scan states, as the phone reports them.
SCAN_IDLE = "idle"
SCAN_SCANNING = "scanning"
SCAN_STOPPED = "stopped"
SCAN_ERROR = "error"

#: GATT connection states.
GATT_DISCONNECTED = "disconnected"
GATT_CONNECTING = "connecting"
GATT_CONNECTED = "connected"
GATT_FAILED = "failed"


class BleError(ValueError):
    """A Bluetooth request or answer that cannot be trusted."""


def normalise_address(text: str) -> str:
    """Uppercase a Bluetooth address, or return it unchanged when it is not one.

    Android reports and emits ``AA:BB:CC:DD:EE:FF``. If the PC stores the address
    in whatever spelling a user typed, every event for that device misses its
    lookup and the connection looks dead, so there is one spelling everywhere.
    """
    value = str(text or "").strip()
    if _MAC_RE.match(value):
        return value.upper()
    return value


def normalise_uuid(text: str) -> str:
    """Lowercase a UUID and expand the 16/32-bit short forms to 128-bit.

    Android reports ``0000180d-0000-1000-8000-00805f9b34fb`` and a peripheral's
    documentation says ``180d``. Treating them as different strings makes the GUI
    look broken, so both become the long form here.
    """
    value = str(text).strip().lower()
    if not value:
        raise BleError("a UUID may not be empty")
    if not _UUID_RE.match(value):
        raise BleError(f"{text!r} is not a UUID")
    if len(value) == 4:
        return f"0000{value}-0000-1000-8000-00805f9b34fb"
    if len(value) == 8:
        return f"{value}-0000-1000-8000-00805f9b34fb"
    if len(value) == 32:
        return f"{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}"
    return value


def short_uuid(uuid: str) -> str:
    """The 4-character form for the well-known base UUID, else the full string."""
    value = normalise_uuid(uuid)
    if value.endswith("-0000-1000-8000-00805f9b34fb") and value.startswith("0000"):
        return value[4:8]
    return value


def is_mac(text: str) -> bool:
    return bool(_MAC_RE.match(str(text).strip()))


def encode_value(data: bytes) -> str:
    if len(data) > MAX_VALUE_BYTES:
        raise BleError(f"a characteristic value may not exceed {MAX_VALUE_BYTES} bytes")
    return base64.b64encode(data).decode("ascii")


def decode_value(text: str) -> bytes:
    if not isinstance(text, str):
        raise BleError("a characteristic value must be a base64 string")
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise BleError(f"value is not valid base64: {exc}") from exc
    if len(raw) > MAX_VALUE_BYTES:
        raise BleError(f"a characteristic value may not exceed {MAX_VALUE_BYTES} bytes")
    return raw


def hex_of(data: bytes) -> str:
    return " ".join(f"{byte:02x}" for byte in data)


def printable_of(data: bytes) -> str:
    """A best-effort ASCII view; a peripheral's value is often a short string."""
    return "".join(chr(b) if 32 <= b < 127 else "·" for b in data)


@dataclass
class BleDevice:
    """One scan result, deduplicated by address by the phone."""

    address: str = ""
    name: str = ""
    rssi: int = 0
    service_uuids: tuple[str, ...] = ()
    connectable: bool = True
    first_seen_ms: int = 0
    last_seen_ms: int = 0
    sightings: int = 1
    address_type: str = ""

    @property
    def display_name(self) -> str:
        return self.name or "(unnamed device)"

    @property
    def services(self) -> tuple[str, ...]:
        return tuple(short_uuid(uuid) for uuid in self.service_uuids)

    def updated_with(self, other: BleDevice) -> BleDevice:
        """Merge a repeat sighting: newest RSSI, keep the name and services seen."""
        return BleDevice(
            address=self.address or other.address,
            name=other.name or self.name,
            rssi=other.rssi or self.rssi,
            service_uuids=tuple(dict.fromkeys(self.service_uuids + other.service_uuids)),
            connectable=self.connectable and other.connectable,
            first_seen_ms=self.first_seen_ms or other.first_seen_ms,
            last_seen_ms=other.last_seen_ms or self.last_seen_ms,
            sightings=self.sightings + 1,
            address_type=other.address_type or self.address_type,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "name": self.name,
            "display_name": self.display_name,
            "rssi": self.rssi,
            "service_uuids": list(self.service_uuids),
            "services": list(self.services),
            "connectable": self.connectable,
            "first_seen_ms": self.first_seen_ms,
            "last_seen_ms": self.last_seen_ms,
            "sightings": self.sightings,
            "address_type": self.address_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BleDevice:
        raw_services = data.get("service_uuids") or data.get("services") or ()
        services: list[str] = []
        if isinstance(raw_services, (list, tuple)):
            for entry in raw_services:
                try:
                    services.append(normalise_uuid(str(entry)))
                except BleError:
                    continue
        def as_int(value: Any, default: int = 0) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        return cls(
            address=str(data.get("address") or ""),
            name=str(data.get("name") or ""),
            rssi=as_int(data.get("rssi")),
            service_uuids=tuple(dict.fromkeys(services)),
            connectable=bool(data.get("connectable", True)),
            first_seen_ms=as_int(data.get("first_seen_ms")),
            last_seen_ms=as_int(data.get("last_seen_ms")),
            sightings=as_int(data.get("sightings"), 1) or 1,
            address_type=str(data.get("address_type") or ""),
        )


@dataclass
class GattCharacteristic:
    uuid: str
    properties: tuple[str, ...] = ()
    descriptors: tuple[str, ...] = ()
    value: bytes | None = None

    @property
    def can_read(self) -> bool:
        return "read" in self.properties

    @property
    def can_write(self) -> bool:
        return bool({"write", "write_no_response", "signed_write"} & set(self.properties))

    @property
    def can_notify(self) -> bool:
        return bool({"notify", "indicate"} & set(self.properties))

    def to_dict(self) -> dict[str, Any]:
        return {
            "uuid": self.uuid,
            "short": short_uuid(self.uuid),
            "properties": list(self.properties),
            "descriptors": list(self.descriptors),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GattCharacteristic:
        props = tuple(
            str(p).lower() for p in (data.get("properties") or ()) if str(p)
        )
        descriptors = tuple(str(d) for d in (data.get("descriptors") or ()))
        return cls(
            uuid=normalise_uuid(str(data.get("uuid") or "")),
            properties=tuple(p for p in props if p in PROPERTIES) or props,
            descriptors=descriptors,
        )


@dataclass
class GattService:
    uuid: str
    characteristics: tuple[GattCharacteristic, ...] = ()

    @property
    def short(self) -> str:
        return short_uuid(self.uuid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "uuid": self.uuid,
            "short": self.short,
            "characteristics": [c.to_dict() for c in self.characteristics],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GattService:
        chars = []
        for entry in data.get("characteristics") or ():
            if isinstance(entry, dict):
                try:
                    chars.append(GattCharacteristic.from_dict(entry))
                except BleError:
                    continue
        return GattService(
            uuid=normalise_uuid(str(data.get("uuid") or "")), characteristics=tuple(chars)
        )


@dataclass
class GattTree:
    """A connected peripheral's services and characteristics."""

    address: str
    services: tuple[GattService, ...] = ()
    mtu: int = 23
    connected_at_ms: int = 0

    def characteristic(self, service: str, characteristic: str) -> GattCharacteristic | None:
        want_service = normalise_uuid(service)
        want_char = normalise_uuid(characteristic)
        for entry in self.services:
            if entry.uuid == want_service:
                return next((c for c in entry.characteristics if c.uuid == want_char), None)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "mtu": self.mtu,
            "connected_at_ms": self.connected_at_ms,
            "services": [s.to_dict() for s in self.services],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GattTree:
        services = []
        for entry in data.get("services") or ():
            if not isinstance(entry, dict):
                continue
            try:
                services.append(GattService.from_dict(entry))
            except BleError:
                continue
        def as_int(value: Any, default: int = 0) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        return cls(
            address=str(data.get("address") or ""),
            services=tuple(services),
            mtu=as_int(data.get("mtu"), 23) or 23,
            connected_at_ms=as_int(data.get("connected_at_ms")),
        )


@dataclass
class Notification:
    """One notification/indication, kept for the GUI's viewer."""

    address: str
    service: str
    characteristic: str
    value: bytes
    timestamp_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "service": self.service,
            "characteristic": self.characteristic,
            "value_b64": encode_value(self.value) if len(self.value) <= MAX_VALUE_BYTES else "",
            "hex": hex_of(self.value),
            "text": printable_of(self.value),
            "bytes": len(self.value),
            "timestamp_ms": self.timestamp_ms,
        }

    @classmethod
    def from_event(cls, data: dict[str, Any]) -> Notification:
        raw = decode_value(str(data.get("value_b64") or ""))
        def as_int(value: Any, default: int = 0) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        return cls(
            address=str(data.get("address") or ""),
            service=normalise_uuid(str(data.get("service") or "0")),
            characteristic=normalise_uuid(str(data.get("characteristic") or "0")),
            value=raw,
            timestamp_ms=as_int(data.get("ts_ms")),
        )


@dataclass
class AdapterStatus:
    """What the phone's Bluetooth stack is willing to do right now."""

    supported: bool = False
    enabled: bool = False
    le_supported: bool = False
    name: str = ""
    address: str = ""
    permitted: bool | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.supported and self.enabled and self.le_supported and self.permitted is not False

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "enabled": self.enabled,
            "le_supported": self.le_supported,
            "name": self.name,
            "address": self.address,
            "permitted": self.permitted,
            "usable": self.usable,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdapterStatus:
        permitted = data.get("permitted")
        return cls(
            supported=bool(data.get("supported")),
            enabled=bool(data.get("enabled")),
            le_supported=bool(data.get("le_supported")),
            name=str(data.get("name") or ""),
            address=str(data.get("address") or ""),
            permitted=permitted if isinstance(permitted, bool) else None,
            detail=str(data.get("detail") or ""),
        )


@dataclass
class ScanState:
    """Scan lifecycle, including why a scan ended."""

    state: str = SCAN_IDLE
    devices: tuple[BleDevice, ...] = ()
    started_at_ms: int = 0
    duration_ms: int = 0
    filter_uuids: tuple[str, ...] = ()
    detail: str = ""
    error: str = ""

    @property
    def scanning(self) -> bool:
        return self.state == SCAN_SCANNING

    def sorted_devices(self) -> tuple[BleDevice, ...]:
        """Strongest signal first: that is the order a person scans a room in."""
        return tuple(sorted(self.devices, key=lambda d: (-d.rssi, d.display_name.lower())))

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "scanning": self.scanning,
            "started_at_ms": self.started_at_ms,
            "duration_ms": self.duration_ms,
            "filter_uuids": list(self.filter_uuids),
            "detail": self.detail,
            "error": self.error,
            "devices": [d.to_dict() for d in self.sorted_devices()],
        }


@dataclass
class GattConnection:
    """One connected peripheral and everything the GUI shows about it."""

    address: str
    state: str = GATT_CONNECTED
    tree: GattTree | None = None
    subscriptions: tuple[str, ...] = ()
    notifications: list[Notification] = field(default_factory=list)
    error: str = ""
    detail: str = ""

    def add_notification(self, notification: Notification) -> None:
        self.notifications.append(notification)
        if len(self.notifications) > MAX_NOTIFICATIONS:
            del self.notifications[: len(self.notifications) - MAX_NOTIFICATIONS]

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "state": self.state,
            "error": self.error,
            "detail": self.detail,
            "subscriptions": list(self.subscriptions),
            "tree": self.tree.to_dict() if self.tree else None,
            "notifications": [n.to_dict() for n in self.notifications[-50:]],
        }


def parse_devices(payload: Any) -> tuple[BleDevice, ...]:
    """Merge a device list from the phone, newest sighting wins, capped."""
    devices: dict[str, BleDevice] = {}
    raw = payload.get("devices") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return ()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        device = BleDevice.from_dict(entry)
        if not device.address:
            continue
        existing = devices.get(device.address)
        devices[device.address] = existing.updated_with(device) if existing else device
        if len(devices) >= MAX_DEVICES:
            break
    return tuple(devices.values())
