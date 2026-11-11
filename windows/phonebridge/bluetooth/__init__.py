"""Phase 5 Bluetooth: BLE discovery and GATT, with the phone as the radio.

Public surface:

* :mod:`~phonebridge.bluetooth.frames` — device, service, characteristic and
  notification models plus value encoding; no I/O, no asyncio.
* :mod:`~phonebridge.bluetooth.link` — :class:`BluetoothLink`, which scans,
  connects, reads, writes, subscribes and keeps the GUI's view.

Scan results and GATT answers travel as RPC results and ``bluetooth.*`` control
events on the session the connection manager authenticated. There is no second
radio, no second socket and no pairing bypass: a bonded-only peripheral refuses,
and the refusal is reported instead of worked around.
"""

from __future__ import annotations

from .frames import (
    GATT_CONNECTED,
    GATT_CONNECTING,
    GATT_DISCONNECTED,
    GATT_FAILED,
    MAX_DEVICES,
    MAX_NOTIFICATIONS,
    MAX_VALUE_BYTES,
    PROPERTIES,
    SCAN_ERROR,
    SCAN_IDLE,
    SCAN_SCANNING,
    SCAN_STOPPED,
    AdapterStatus,
    BleDevice,
    BleError,
    GattCharacteristic,
    GattConnection,
    GattService,
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
    short_uuid,
)
from .link import (
    DEFAULT_SCAN_MS,
    GATT_TIMEOUT_S,
    SCAN_DURATIONS_MS,
    BluetoothError,
    BluetoothLink,
    BluetoothSnapshot,
    describe_value,
)

__all__ = [
    "AdapterStatus",
    "BleDevice",
    "BleError",
    "BluetoothError",
    "BluetoothLink",
    "BluetoothSnapshot",
    "DEFAULT_SCAN_MS",
    "GATT_CONNECTED",
    "GATT_CONNECTING",
    "GATT_DISCONNECTED",
    "GATT_FAILED",
    "GATT_TIMEOUT_S",
    "GattCharacteristic",
    "GattConnection",
    "GattService",
    "GattTree",
    "MAX_DEVICES",
    "MAX_NOTIFICATIONS",
    "MAX_VALUE_BYTES",
    "Notification",
    "PROPERTIES",
    "SCAN_DURATIONS_MS",
    "SCAN_ERROR",
    "SCAN_IDLE",
    "SCAN_SCANNING",
    "SCAN_STOPPED",
    "ScanState",
    "decode_value",
    "describe_value",
    "encode_value",
    "hex_of",
    "is_mac",
    "normalise_uuid",
    "parse_devices",
    "printable_of",
    "short_uuid",
]
