"""BLE scan and GATT operations, driven from the PC, executed on the phone.

Design notes that matter more than the code:

* **The phone is the radio.** Windows talks to PhoneBridge, PhoneBridge talks to
  the phone, the phone talks to the peripheral. There is no second transport and
  no pairing bypass: what a peripheral allows an ordinary central is what you get,
  and a bonded-only peripheral refuses with the reason reported verbatim.
* **Nothing connects silently.** :meth:`BluetoothLink.connect` is only ever called
  because a person pressed Connect on a device they saw in a scan.
* **Scan state is state, not events alone.** The phone pushes
  ``bluetooth.device`` events during a scan *and* answers ``bluetooth.scan.results``
  with the full list, so a dropped event cannot leave the GUI permanently behind.
* **Bounded.** At most :data:`~.frames.MAX_DEVICES` devices and
  :data:`~.frames.MAX_NOTIFICATIONS` notifications are kept; an advertising beacon
  or a chatty characteristic cannot grow memory without limit.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .frames import (
    GATT_CONNECTED,
    GATT_DISCONNECTED,
    GATT_FAILED,
    SCAN_ERROR,
    SCAN_IDLE,
    SCAN_SCANNING,
    SCAN_STOPPED,
    AdapterStatus,
    BleDevice,
    BleError,
    GattConnection,
    GattTree,
    Notification,
    ScanState,
    decode_value,
    encode_value,
    hex_of,
    is_mac,
    normalise_uuid,
    parse_devices,
    printable_of,
)

log = logging.getLogger(__name__)

#: Scan durations the GUI offers, in milliseconds.
SCAN_DURATIONS_MS: tuple[tuple[str, int], ...] = (
    ("5 seconds", 5000),
    ("10 seconds", 10000),
    ("30 seconds", 30000),
    ("Until stopped", 0),
)

DEFAULT_SCAN_MS = 10000

#: How long a GATT operation may take before the PC gives up waiting.
GATT_TIMEOUT_S = 20.0
SCAN_CALL_TIMEOUT_S = 15.0


class BluetoothError(RuntimeError):
    """A Bluetooth operation the user needs to hear about, in words."""


@dataclass
class BluetoothSnapshot:
    """What the GUI draws, as one immutable value."""

    adapter: AdapterStatus
    scan: ScanState
    connections: tuple[GattConnection, ...] = ()
    detail: str = ""

    def connected(self, address: str | None = None) -> GattConnection | None:
        if address is None:
            return self.connections[0] if self.connections else None
        return next((c for c in self.connections if c.address == address), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter.to_dict(),
            "scan": self.scan.to_dict(),
            "connections": [c.to_dict() for c in self.connections],
            "detail": self.detail,
        }


class BluetoothLink:
    """Scan for BLE peripherals and talk GATT, through the phone."""

    def __init__(
        self,
        session: Any,
        *,
        on_change: Callable[[BluetoothSnapshot], None] | None = None,
    ) -> None:
        self._session = session
        self._on_change = on_change
        self._adapter = AdapterStatus()
        self._scan = ScanState()
        self._connections: dict[str, GattConnection] = {}
        self._detail = ""
        self._pending_scan_ms = DEFAULT_SCAN_MS

    # ------------------------------------------------------------------ #
    # observation
    # ------------------------------------------------------------------ #
    def snapshot(self) -> BluetoothSnapshot:
        return BluetoothSnapshot(
            adapter=self._adapter,
            scan=self._scan,
            connections=tuple(self._connections.values()),
            detail=self._detail,
        )

    def connection(self, address: str) -> GattConnection | None:
        return self._connections.get(address)

    def devices(self) -> tuple[BleDevice, ...]:
        return self._scan.sorted_devices()

    # ------------------------------------------------------------------ #
    # adapter and scanning
    # ------------------------------------------------------------------ #
    async def refresh_adapter(self) -> AdapterStatus:
        """Ask what this phone can actually do. Never guesses from a radio alone."""
        if not getattr(self._session, "is_open", False):
            self._detail = "not connected: Bluetooth needs a live session"
            self._publish()
            return self._adapter
        try:
            reply = await self._session.call("bluetooth.adapter.status", {})
        except Exception as exc:  # noqa: BLE001
            self._detail = _explain("could not read the phone's Bluetooth state", exc)
            self._publish()
            return self._adapter
        self._adapter = AdapterStatus.from_dict(reply if isinstance(reply, dict) else {})
        self._detail = self._adapter.detail
        self._publish()
        return self._adapter

    async def start_scan(
        self,
        *,
        duration_ms: int = DEFAULT_SCAN_MS,
        filter_uuids: list[str] | None = None,
        name_prefix: str = "",
    ) -> bool:
        """Start a BLE scan. Refuses, with a reason, instead of failing silently."""
        if not getattr(self._session, "is_open", False):
            self._detail = "not connected: a scan needs a live session"
            self._publish()
            return False
        if self._scan.scanning:
            return True
        if not self._adapter.supported or not self._adapter.enabled:
            await self.refresh_adapter()

        params: dict[str, Any] = {"duration_ms": max(0, int(duration_ms))}
        if filter_uuids:
            params["filter_uuids"] = [normalise_uuid(u) for u in filter_uuids]
        if name_prefix:
            params["name_prefix"] = name_prefix
        try:
            reply = await self._session.call(
                "bluetooth.scan.start", params, timeout=SCAN_CALL_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001
            self._scan = ScanState(state=SCAN_ERROR, detail=str(exc), error=str(exc))
            self._detail = _explain("the phone would not start a scan", exc)
            self._publish()
            return False

        self._pending_scan_ms = max(0, int(duration_ms))
        self._scan = ScanState(
            state=SCAN_SCANNING,
            devices=self._scan.devices,
            started_at_ms=_int(reply, "started_at_ms"),
            duration_ms=self._pending_scan_ms,
            filter_uuids=tuple(normalise_uuid(u) for u in (filter_uuids or ())),
            detail="",
        )
        self._detail = ""
        self._publish()
        return True

    async def stop_scan(self) -> bool:
        if not getattr(self._session, "is_open", False):
            self._detail = "not connected"
            self._publish()
            return False
        try:
            reply = await self._session.call(
                "bluetooth.scan.stop", {}, timeout=SCAN_CALL_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001
            self._detail = _explain("the phone would not stop the scan", exc)
            self._publish()
            return False
        devices = parse_devices(reply) if isinstance(reply, dict) else ()
        self._scan = ScanState(
            state=SCAN_STOPPED,
            devices=devices or self._scan.devices,
            started_at_ms=self._scan.started_at_ms,
            duration_ms=self._scan.duration_ms,
            filter_uuids=self._scan.filter_uuids,
            detail="",
        )
        self._publish()
        return True

    async def refresh_scan(self) -> ScanState:
        """Pull the full device list, so a missed event cannot lose a device."""
        if not getattr(self._session, "is_open", False):
            self._detail = "not connected"
            self._publish()
            return self._scan
        try:
            reply = await self._session.call("bluetooth.scan.results", {}, timeout=SCAN_CALL_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            self._detail = _explain("could not read the scan results", exc)
            self._publish()
            return self._scan
        if not isinstance(reply, dict):
            return self._scan
        devices = parse_devices(reply)
        state = self._scan.state
        if isinstance(reply.get("scanning"), bool):
            state = SCAN_SCANNING if reply["scanning"] else (
                SCAN_STOPPED if devices else SCAN_IDLE
            )
        self._scan = ScanState(
            state=state,
            devices=devices or self._scan.devices,
            started_at_ms=_int(reply, "started_at_ms", self._scan.started_at_ms),
            duration_ms=_int(reply, "duration_ms", self._scan.duration_ms),
            filter_uuids=self._scan.filter_uuids,
            detail=str(reply.get("detail") or ""),
        )
        self._publish()
        return self._scan

    # ------------------------------------------------------------------ #
    # GATT
    # ------------------------------------------------------------------ #
    async def connect(self, address: str) -> GattConnection:
        """Connect to a device the user picked. Never called by discovery."""
        if not is_mac(address):
            raise BluetoothError(f"{address!r} is not a Bluetooth address")
        if not getattr(self._session, "is_open", False):
            raise BluetoothError("not connected: GATT needs a live session")
        existing = self._connections.get(address)
        if existing is not None and existing.state == GATT_CONNECTED:
            return existing

        connection = GattConnection(address=address, state="connecting")
        self._connections[address] = connection
        self._publish()
        try:
            await self._session.call(
                "bluetooth.gatt.connect", {"address": address}, timeout=GATT_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001
            connection.state = GATT_FAILED
            connection.error = _explain("could not connect", exc)
            self._detail = connection.error
            self._publish()
            return connection

        try:
            tree = await self.services(address)
        except BluetoothError as exc:
            connection.state = GATT_FAILED
            connection.error = str(exc)
            self._detail = str(exc)
            self._publish()
            return connection
        connection.state = GATT_CONNECTED
        connection.tree = tree
        connection.error = ""
        self._detail = ""
        self._publish()
        return connection

    async def services(self, address: str) -> GattTree:
        """Discover services and characteristics for a connected peripheral."""
        reply = await self._call(
            "bluetooth.gatt.services", {"address": address}, "the phone could not read GATT services"
        )
        return GattTree.from_dict(reply if isinstance(reply, dict) else {})

    async def disconnect(self, address: str) -> bool:
        connection = self._connections.pop(address, None)
        ok = True
        if getattr(self._session, "is_open", False):
            try:
                await self._session.call(
                    "bluetooth.gatt.disconnect", {"address": address}, timeout=GATT_TIMEOUT_S
                )
            except Exception as exc:  # noqa: BLE001 - the local side is already closed
                self._detail = _explain("the phone did not confirm the disconnect", exc)
                ok = False
        if connection is not None:
            connection.state = GATT_DISCONNECTED
            for notification in connection.notifications:
                del notification
            self._publish()
        return ok

    async def read(self, address: str, service: str, characteristic: str) -> bytes:
        reply = await self._call(
            "bluetooth.gatt.read",
            {
                "address": address,
                "service": normalise_uuid(service),
                "characteristic": normalise_uuid(characteristic),
            },
            "the phone could not read that characteristic",
        )
        connection = self._connections.get(address)
        if connection is not None and isinstance(reply, dict):
            entry = connection.tree.characteristic(service, characteristic) if connection.tree else None
            if entry is not None:
                try:
                    entry.value = decode_value(str(reply.get("value_b64") or ""))
                except BleError:
                    pass
        raw = str(reply.get("value_b64") or "") if isinstance(reply, dict) else ""
        return decode_value(raw)

    async def write(
        self,
        address: str,
        service: str,
        characteristic: str,
        data: bytes,
        *,
        with_response: bool = True,
    ) -> int:
        payload = encode_value(data)
        reply = await self._call(
            "bluetooth.gatt.write",
            {
                "address": address,
                "service": normalise_uuid(service),
                "characteristic": normalise_uuid(characteristic),
                "value_b64": payload,
                "with_response": with_response,
            },
            "the phone could not write that characteristic",
        )
        return _int(reply, "bytes", len(data))

    async def subscribe(
        self, address: str, service: str, characteristic: str, *, enable: bool = True
    ) -> bool:
        """Subscribe (or unsubscribe) and report whether it is subscribed *now*."""
        reply = await self._call(
            "bluetooth.gatt.subscribe",
            {
                "address": address,
                "service": normalise_uuid(service),
                "characteristic": normalise_uuid(characteristic),
                "enable": enable,
            },
            "the phone could not change that subscription",
        )
        connection = self._connections.get(address)
        if connection is not None:
            key = f"{normalise_uuid(service)}/{normalise_uuid(characteristic)}"
            subscriptions = list(connection.subscriptions)
            if enable and key not in subscriptions:
                subscriptions.append(key)
            if not enable:
                subscriptions = [s for s in subscriptions if s != key]
            connection.subscriptions = tuple(subscriptions)
            self._publish()
        return bool(reply.get("subscribed")) if isinstance(reply, dict) else enable

    async def refresh_status(self) -> None:
        """Reconcile local state with the phone: disconnects and scan endings."""
        if not getattr(self._session, "is_open", False):
            return
        try:
            reply = await self._session.call("bluetooth.gatt.status", {}, timeout=SCAN_CALL_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - reconciliation is best effort
            return
        if not isinstance(reply, dict):
            return
        live = {
            str(entry.get("address")): str(entry.get("state") or "")
            for entry in reply.get("connections") or ()
            if isinstance(entry, dict)
        }
        changed = False
        for address, connection in list(self._connections.items()):
            state = live.get(address)
            if state is None:
                connection.state = GATT_DISCONNECTED
                connection.error = connection.error or "the phone dropped the connection"
                changed = True
            elif state and state != connection.state and state != "connecting":
                connection.state = state
                changed = True
        if (
            "scanning" in reply
            and isinstance(reply["scanning"], bool)
            and self._scan.scanning
            and not reply["scanning"]
        ):
            self._scan = ScanState(
                state=SCAN_STOPPED,
                devices=self._scan.devices,
                started_at_ms=self._scan.started_at_ms,
                duration_ms=self._scan.duration_ms,
                filter_uuids=self._scan.filter_uuids,
            )
            changed = True
        if changed:
            self._publish()

    # ------------------------------------------------------------------ #
    # events
    # ------------------------------------------------------------------ #
    def on_event(self, name: str, data: dict[str, Any]) -> None:
        """Handle the phone's ``bluetooth.*`` events. Never raises."""
        try:
            if name == "bluetooth.device":
                device = BleDevice.from_dict(data)
                if not device.address:
                    return
                devices = {d.address: d for d in self._scan.devices}
                existing = devices.get(device.address)
                devices[device.address] = (
                    existing.updated_with(device) if existing else device
                )
                self._scan = ScanState(
                    state=SCAN_SCANNING,
                    devices=tuple(devices.values()),
                    started_at_ms=self._scan.started_at_ms,
                    duration_ms=self._scan.duration_ms,
                    filter_uuids=self._scan.filter_uuids,
                )
                self._publish()
            elif name == "bluetooth.scan":
                state = str(data.get("state") or "").strip().lower()
                if state in {SCAN_IDLE, SCAN_SCANNING, SCAN_STOPPED, SCAN_ERROR}:
                    self._scan = ScanState(
                        state=state,
                        devices=self._scan.devices,
                        started_at_ms=self._scan.started_at_ms,
                        duration_ms=self._scan.duration_ms,
                        filter_uuids=self._scan.filter_uuids,
                        detail=str(data.get("detail") or ""),
                        error=str(data.get("error") or ""),
                    )
                    self._publish()
            elif name == "bluetooth.gatt":
                address = str(data.get("address") or "")
                state = str(data.get("state") or "")
                connection = self._connections.get(address)
                if connection is not None and state:
                    connection.state = state
                    if state == GATT_FAILED:
                        connection.error = str(data.get("error") or "the connection failed")
                    self._publish()
            elif name == "bluetooth.notification":
                notification = Notification.from_event(data)
                connection = self._connections.get(notification.address)
                if connection is not None:
                    connection.add_notification(notification)
                    self._publish()
        except Exception:  # noqa: BLE001 - an event must never break the session
            log.exception("ignoring a malformed %s event", name)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    async def _call(self, method: str, params: dict[str, Any], prefix: str) -> Any:
        if not getattr(self._session, "is_open", False):
            raise BluetoothError("not connected: this needs a live session")
        try:
            return await self._session.call(method, params, timeout=GATT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            raise BluetoothError(_explain(prefix, exc)) from exc

    def _publish(self) -> None:
        if self._on_change is None:
            return
        with contextlib.suppress(Exception):
            self._on_change(self.snapshot())

    async def close(self, reason: str = "session closed") -> None:
        """Drop local state. The phone releases its connections with the session."""
        for connection in self._connections.values():
            connection.state = GATT_DISCONNECTED
            connection.detail = reason
        self._scan = ScanState(
            state=SCAN_STOPPED if self._scan.devices else SCAN_IDLE,
            devices=self._scan.devices,
            started_at_ms=self._scan.started_at_ms,
            duration_ms=self._scan.duration_ms,
            filter_uuids=self._scan.filter_uuids,
            detail=reason,
        )
        self._detail = reason
        self._publish()


def describe_value(data: bytes) -> str:
    """``"03 0f 41 · 41"`` — hex and printable, the way a GATT tool shows it."""
    if not data:
        return "(empty)"
    return f"{hex_of(data)} · {printable_of(data)}"


def _int(payload: Any, key: str, default: int = 0) -> int:
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return default


def _explain(prefix: str, exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return f"{prefix}: {text}"


__all__ = [
    "BluetoothError",
    "BluetoothLink",
    "BluetoothSnapshot",
    "DEFAULT_SCAN_MS",
    "GATT_TIMEOUT_S",
    "SCAN_DURATIONS_MS",
    "describe_value",
]
