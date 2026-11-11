"""What the Bluetooth, sensor and file panels show, decided without a widget.

The window is a thin renderer: everything that can be turned into a string is
turned into one here, so the Phase 5 panels are testable on a machine with no
display (this repository's CI has none) and so the wording lives in one place.

Every builder takes the plain dictionary the manager produces — never a live
object — and returns rows ready for a ``ttk.Treeview``. Three properties matter:

* **Nothing is invented.** A sensor with no frame says "no frame yet"; a phone
  that never reported a permission says "not reported"; a transfer that has not
  verified says "pending", not "ok".
* **Order is meaningful.** Devices are strongest-signal-first, sensors follow the
  phone's own preferred order, and transfers are oldest-first.
* **Ids are stable.** Rows are keyed by address / sensor id / transfer id, so the
  Treeview can update in place instead of being rebuilt and losing the selection.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from ..bluetooth.frames import hex_of, printable_of
from ..files.frames import TransferStats
from ..sensors import kind_unit
from ..sensors.frames import SensorInfo, SensorRegistry

#: Sampling intervals offered in the Sensors panel, as (label, microseconds).
#: Short on purpose: these are rates a person can tell apart.
SENSOR_RATES: tuple[tuple[str, int], ...] = (
    ("200 Hz", 5_000),
    ("100 Hz", 10_000),
    ("50 Hz", 20_000),
    ("15 Hz", 66_667),
    ("5 Hz", 200_000),
    ("1 Hz", 1_000_000),
)

#: Scan durations offered in the Bluetooth panel, as (label, milliseconds).
SCAN_DURATIONS: tuple[tuple[str, int], ...] = (
    ("5 s", 5_000),
    ("10 s", 10_000),
    ("30 s", 30_000),
    ("until stopped", 0),
)

#: How each transfer state reads to a person.
FILE_STATE_WORDS: dict[str, str] = {
    "pending": "preparing",
    "active": "transferring",
    "paused": "paused",
    "complete": "complete",
    "failed": "failed",
    "cancelled": "cancelled",
}

#: States in which a transfer is still moving (or could be resumed).
FILE_LIVE_STATES = frozenset({"pending", "active", "paused"})


# --------------------------------------------------------------------------- #
# small formatters
# --------------------------------------------------------------------------- #
def format_bytes(count: float) -> str:
    """Human size, with the unit the number is actually in."""
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024.0 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GiB"  # pragma: no cover - unreachable


def short_uuid(value: str) -> str:
    """`0000180f-0000-1000-8000-00805f9b34fb` reads as `180f`."""
    text = (value or "").lower()
    if text.endswith("-0000-1000-8000-00805f9b34fb") and text.startswith("0000"):
        return text[4:8]
    return text


def permission_word(value: Any) -> str:
    if value is True:
        return "granted"
    if value is False:
        return "denied"
    return "not reported"


def interval_label(interval_us: int) -> str:
    """`50 Hz (20.000 ms)` — rate first, period second, because rate is the choice."""
    micros = max(1, int(interval_us))
    hz = 1_000_000.0 / micros
    return f"{hz:.0f} Hz ({micros / 1000:.3f} ms)"


def reading_text(reading: dict[str, Any], info: dict[str, Any]) -> str:
    """`+1.020, -0.440, +9.810 m/s²` — or an explicit "no frame yet"."""
    values = reading.get("values")
    if not values:
        return "no frame yet"
    try:
        numbers = ", ".join(f"{float(value):+.3f}" for value in values)
    except (TypeError, ValueError):
        return "unreadable frame"
    unit = str(info.get("unit") or "")
    if not unit and info.get("kind") is not None:
        unit = kind_unit(int(info["kind"]))
    return f"{numbers} {unit}".strip()


# --------------------------------------------------------------------------- #
# Bluetooth
# --------------------------------------------------------------------------- #
def bluetooth_headline(status: dict[str, Any]) -> str:
    """One line: what the radio can do, and what it is doing.

    Written so that "supported but switched off" cannot be mistaken for "no
    Bluetooth", which is the failure people actually hit.
    """
    if not status.get("available", True):
        return str(status.get("detail") or "not connected: Bluetooth needs a live session")
    adapter = dict(status.get("adapter") or {})
    scan = dict(status.get("scan") or {})
    parts = [
        "adapter " + ("supported" if adapter.get("supported") else "not supported"),
        "on" if adapter.get("enabled") else "off",
        "BLE " + ("yes" if adapter.get("le_supported") else "no"),
        "permission " + permission_word(adapter.get("permitted")),
        "scan " + ("running" if scan.get("scanning") else str(scan.get("state") or "idle")),
    ]
    text = " · ".join(parts)
    for extra in (adapter.get("detail"), scan.get("detail"), scan.get("error")):
        if extra:
            text = f"{text} — {extra}"
            break
    return text


def device_rows(status: dict[str, Any]) -> list[tuple[str, tuple[Any, ...]]]:
    """Scan results, strongest signal first, deduplicated by the phone already."""
    scan = dict(status.get("scan") or {})
    rows: list[tuple[str, tuple[Any, ...]]] = []
    for device in scan.get("devices") or ():
        address = str(device.get("address") or "")
        if not address:
            continue
        rows.append(
            (
                address,
                (
                    device.get("name") or "(unnamed device)",
                    address,
                    int(device.get("rssi") or 0),
                    " ".join(short_uuid(u) for u in device.get("service_uuids") or ()) or "—",
                    "yes" if device.get("connectable", True) else "no",
                    int(device.get("sightings") or 1),
                ),
            )
        )
    return rows


def gatt_rows(
    connection: dict[str, Any] | None,
) -> list[tuple[str, str, str, str | None]]:
    """(iid, text, properties, parent) for one connection's service tree."""
    if not connection:
        return []
    tree = dict(connection.get("tree") or {})
    subscriptions = set(connection.get("subscriptions") or ())
    rows: list[tuple[str, str, str, str | None]] = []
    for service in tree.get("services") or ():
        service_uuid = str(service.get("uuid") or "")
        parent = f"srv:{service_uuid}"
        rows.append((parent, short_uuid(service_uuid), "", None))
        for characteristic in service.get("characteristics") or ():
            char_uuid = str(characteristic.get("uuid") or "")
            subscribed = f"{service_uuid}/{char_uuid}" in subscriptions
            label = f"    {short_uuid(char_uuid)}" + (" [notifying]" if subscribed else "")
            props = ", ".join(characteristic.get("properties") or ())
            rows.append((f"char:{service_uuid}:{char_uuid}", label, props, parent))
    return rows


def gatt_headline(connection: dict[str, Any] | None, *, fallback: str) -> str:
    if not connection:
        return fallback
    tree = dict(connection.get("tree") or {})
    text = (
        f"{connection.get('address')} · {connection.get('state')} · "
        f"MTU {tree.get('mtu') or 0} · "
        f"{len(connection.get('subscriptions') or ())} subscription(s)"
    )
    if connection.get("error"):
        text = f"{text} — {connection['error']}"
    return text


def value_words(raw_b64: str) -> str:
    """A characteristic value as printable text plus hex, or why it is neither."""
    try:
        data = base64.b64decode(raw_b64 or "", validate=True)
    except (binascii.Error, ValueError):
        return "the phone sent a value this PC could not decode"
    if not data:
        return "empty value"
    return f"{printable_of(data)}  [{hex_of(data)}]"


def notification_line(note: dict[str, Any]) -> str:
    """One notification, timestamped by the phone, for the viewer."""
    stamp = note.get("timestamp_ms")
    when = f"{int(stamp)} ms" if stamp is not None else "?"
    characteristic = short_uuid(str(note.get("characteristic") or ""))
    return f"{when} {characteristic} #{note.get('seq', '?')}: {value_words(str(note.get('value_b64') or ''))}"


# --------------------------------------------------------------------------- #
# sensors
# --------------------------------------------------------------------------- #
def sensor_headline(status: dict[str, Any]) -> str:
    if not status.get("available", True):
        return str(status.get("detail") or "not connected: sensors need a live session")
    registry = dict(status.get("registry") or {})
    interval = int(status.get("interval_us") or registry.get("interval_us") or 20_000)
    state = str(status.get("state") or "idle")
    text = f"{state} · every {interval_label(interval)}"
    if registry.get("permitted") is False:
        text = f"{text} · the phone has not granted sensor access"
    detail = status.get("detail") or registry.get("detail")
    if detail:
        text = f"{text} — {detail}"
    return text


def sensor_rows(status: dict[str, Any]) -> list[tuple[str, tuple[Any, ...]]]:
    """One row per sensor the phone listed, in the phone's preferred order."""
    registry = dict(status.get("registry") or {})
    active = {int(value) for value in registry.get("active") or ()}
    stats = status.get("stats") or {}
    readings = dict(registry.get("readings") or {})
    rows: list[tuple[str, tuple[Any, ...]]] = []
    for sensor in registry.get("sensors") or ():
        sensor_id = int(sensor.get("id") or 0)
        entry = dict(stats.get(str(sensor_id)) or stats.get(sensor_id) or {})
        reading = dict(readings.get(str(sensor_id)) or readings.get(sensor_id) or {})
        rows.append(
            (
                f"sensor:{sensor_id}",
                (
                    str(sensor.get("name") or f"sensor {sensor_id}"),
                    "streaming" if sensor_id in active else "idle",
                    f"{float(entry.get('hz') or 0):.1f} Hz" if entry.get("hz") else "—",
                    int(entry.get("frames") or 0),
                    int(entry.get("lost") or 0),
                    reading_text(reading, sensor),
                    f"{float(entry.get('age_ms') or 0):.0f} ms" if entry.get("age_ms") else "—",
                ),
            )
        )
    return rows


def sensor_tooltip(registry: SensorRegistry | None, sensor_id: int) -> str:
    """The metadata the phone reported for one sensor, for a hover-like label."""
    if registry is None:
        return ""
    info: SensorInfo | None = registry.by_id(sensor_id)
    if info is None:
        return f"sensor {sensor_id} (not in the phone's list)"
    parts = [
        f"{info.label} ({info.kind})",
        f"range ±{info.max_range:g} {info.unit}".strip(),
        f"resolution {info.resolution:g}",
        f"power {info.power_ma:g} mA",
        f"fastest {info.max_hz} Hz" if info.max_hz else "no minimum delay reported",
    ]
    return " · ".join(parts)


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #
def files_headline(status: dict[str, Any]) -> str:
    if not status.get("available", True):
        return str(status.get("detail") or "not connected: file transfer needs a live session")
    destination = str(status.get("destination") or "")
    live = [
        stats
        for stats in list(status.get("transfers") or ())
        if str(stats.get("state")) in FILE_LIVE_STATES
    ]
    if live:
        return (
            f"{len(live)} transfer(s) in flight · "
            f"saving to {destination or 'the folder you choose'}"
        )
    return f"no transfers in flight · saving to {destination or 'the folder you choose'}"


def file_rows(status: dict[str, Any]) -> list[tuple[str, tuple[Any, ...]]]:
    """Live transfers then history, oldest first, with an honest integrity column."""
    rows: list[tuple[str, tuple[Any, ...]]] = []
    for stats in list(status.get("transfers") or ()) + list(status.get("history") or ()):
        rows.append((f"file:{stats.get('transfer_id', '?')}", file_values(stats)))
    return rows


def file_values(stats: dict[str, Any]) -> tuple[Any, ...]:
    state = str(stats.get("state") or "pending")
    verified = stats.get("verified")
    if state == "complete":
        integrity = "hash verified" if verified else "hash NOT confirmed"
    elif state == "failed":
        integrity = str(stats.get("error") or "failed")
    elif state == "cancelled":
        integrity = "cancelled"
    else:
        integrity = "pending"
    fraction = stats.get("fraction")
    if fraction is None:
        fraction = TransferStats(
            transfer_id=int(stats.get("transfer_id") or 0),
            size=int(stats.get("size") or 0),
            bytes_done=int(stats.get("bytes_done") or 0),
            state=state,
        ).fraction
    speed = stats.get("speed_bps")
    return (
        str(stats.get("name") or f"transfer {stats.get('transfer_id', '?')}"),
        "to phone" if stats.get("direction") == "to_phone" else "from phone",
        f"{float(fraction) * 100:.0f}%",
        f"{format_bytes(stats.get('bytes_done', 0))} / {format_bytes(stats.get('size', 0))}",
        f"{format_bytes(speed)}/s" if speed else "—",
        FILE_STATE_WORDS.get(state, state),
        integrity,
    )
