"""The five-state capability registry.

The phone's ``system.info`` answers one question per capability — "is it usable?"
— where usable means *implemented **and** hardware **and** permitted*. That single
boolean is right for the wire, but it is not enough for a product: a user looking
at "camera: unavailable" cannot tell whether to plug something in, grant a
permission, or wait for a later release.

So the PC keeps the three inputs apart, adds a fourth (is it usable now) and a
fifth that only a running relay can answer (is it active), and never collapses
them::

    present      the hardware is there            hardware.<key>
    permitted    Android has granted what it needs   permissions.<NAME>
    implemented  this app build has the feature
    available    implemented and present and permitted - usable right now
    active       a relay is currently streaming

``implemented`` is the awkward one. The phone reports the *conjunction*, so the
PC can only be certain when the conjunction is true (then all three are true) or
when the other two are true (then the conjunction being false proves the feature
is missing). In the remaining case — hardware absent — the honest answer is
"unknown", and this registry says ``None`` rather than guessing. A future app
build may publish ``implemented.<key>`` explicitly; when it does, it is used.

That is why nothing in this release can report a hardware capability as
operational: everything the app implements today is protocol plumbing, so every
relay capability is ``available = False`` even on a phone that has the hardware.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

#: Mirrors ``DeviceFacts.capabilityRows()``. Order is the order the UI shows.
KEYS: tuple[str, ...] = (
    "audio.playback",
    "audio.capture",
    "audio.bluetooth_sco",
    "camera.back",
    "camera.front",
    "sensors",
    "file_transfer",
    "bluetooth_scan",
    "bluetooth_gatt",
)

LABELS: Mapping[str, str] = {
    "audio.playback": "Audio playback",
    "audio.capture": "Microphone",
    "audio.bluetooth_sco": "Bluetooth headset route",
    "camera.back": "Rear camera",
    "camera.front": "Front camera",
    "sensors": "Sensors",
    "file_transfer": "File transfer",
    "bluetooth_scan": "Bluetooth scanning",
    "bluetooth_gatt": "Bluetooth GATT",
}

#: The roadmap phase each capability belongs to, shown as context.
PHASES: Mapping[str, int] = {
    "audio.playback": 2,
    "audio.capture": 2,
    "audio.bluetooth_sco": 2,
    "camera.back": 4,
    "camera.front": 4,
    "sensors": 5,
    "file_transfer": 5,
    "bluetooth_scan": 5,
    "bluetooth_gatt": 5,
}

#: Which Android runtime permission gates a capability.
#:
#: Must stay identical to the ``permission =`` arguments in
#: ``DeviceFacts.capabilityRows()``; ``tests/test_controller_capabilities.py``
#: parses the Kotlin source and fails if the two drift apart.
REQUIRED_PERMISSIONS: Mapping[str, str] = {
    "audio.capture": "RECORD_AUDIO",
    # Phase 4: a lens is only usable when the user has allowed the camera.
    "camera.back": "CAMERA",
    "camera.front": "CAMERA",
    # Phase 5. A radio is one capability and *talking GATT* is another: a phone
    # can scan without being able to connect, and the two need different
    # permissions on Android 12+. On Android 11 and older both are spelled
    # ACCESS_FINE_LOCATION, which the phone resolves in `Permissions.effective`.
    "bluetooth_scan": "BLUETOOTH_SCAN",
    "bluetooth_gatt": "BLUETOOTH_CONNECT",
}

#: Permission values as the phone spells them (``Permissions.Status`` lowercased).
GRANTED_STATES = frozenset({"granted"})


@dataclass(frozen=True)
class Capability:
    """One capability, with every input visible rather than folded together."""

    key: str
    label: str
    present: bool | None
    permitted: bool | None
    implemented: bool | None
    available: bool
    active: bool = False
    detail: str | None = None
    phase: int | None = None
    #: What the phone literally claimed, kept so a contradiction stays visible.
    reported_available: bool = False

    def with_active(self, active: bool) -> Capability:
        return replace(self, active=active)

    @property
    def permission(self) -> str | None:
        return REQUIRED_PERMISSIONS.get(self.key)

    @property
    def contradiction(self) -> str | None:
        """A phone claim that cannot all be true at once, if there is one."""
        if not self.reported_available:
            return None
        if self.present is False:
            return "the phone reports it usable but no hardware is present"
        if self.permitted is False:
            return "the phone reports it usable but the permission is denied"
        return None


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _permitted_for(key: str, info: Mapping[str, Any]) -> bool | None:
    """True / False / None (None = this capability needs no permission)."""
    permission = REQUIRED_PERMISSIONS.get(key)
    if permission is None:
        return None
    permissions = info.get("permissions")
    if not isinstance(permissions, Mapping):
        # The phone did not report permissions at all. We cannot claim it is
        # granted; we also must not claim it is denied.
        return None
    raw = permissions.get(permission)
    if raw is None:
        return None
    return str(raw).strip().lower() in GRANTED_STATES


def build_registry(
    info: Mapping[str, Any],
    *,
    active: Iterable[str] = (),
    keys: Iterable[str] = KEYS,
) -> tuple[Capability, ...]:
    """Turn one ``system.info`` payload into the five-state registry."""
    active_set = set(active)
    # ``system.info`` carries the capability map as a nested object
    # (``info["capabilities"]``); a flat payload is tolerated for older or
    # hand-written inputs, but the nested shape is what the phone sends.
    nested = info.get("capabilities")
    caps: Mapping[str, Any] = nested if isinstance(nested, Mapping) else info
    capabilities: list[Capability] = []
    for key in keys:
        reported = bool(_as_bool(caps.get(key)))
        present = _as_bool(caps.get(f"hardware.{key}"))
        permitted = _permitted_for(key, info)
        explicit_implemented = _as_bool(caps.get(f"implemented.{key}"))

        if explicit_implemented is not None:
            implemented: bool | None = explicit_implemented
        elif reported:
            # usable == implemented AND hardware AND permission, so this proves all three.
            implemented = True
        elif present is True and permitted is not False:
            # hardware and permission are fine, so usability must be failing here.
            implemented = False
        else:
            implemented = None

        available = (
            reported
            and present is not False
            and permitted is not False
            and implemented is not False
        )
        detail = caps.get(f"detail.{key}")
        capabilities.append(
            Capability(
                key=key,
                label=LABELS.get(key, key),
                present=present,
                permitted=permitted,
                implemented=implemented,
                available=available,
                active=key in active_set,
                detail=detail if isinstance(detail, str) else None,
                phase=PHASES.get(key),
                reported_available=reported,
            )
        )
    return tuple(capabilities)


def mark_active(
    registry: Iterable[Capability], key: str, active: bool = True
) -> tuple[Capability, ...]:
    """Set the "a relay is streaming right now" flag for one capability.

    Phase 2+ relays own this; in this release nothing calls it, which is why
    every ``active`` cell is false.
    """
    return tuple(c.with_active(active) if c.key == key else c for c in registry)


def available_keys(registry: Iterable[Capability]) -> list[str]:
    return [c.key for c in registry if c.available]


def active_keys(registry: Iterable[Capability]) -> list[str]:
    return [c.key for c in registry if c.active]


def blockers(capability: Capability) -> list[str]:
    """What stands between this capability and usable, in priority order."""
    out: list[str] = []
    if capability.implemented is False:
        out.append("not implemented in this build")
    if capability.present is False:
        out.append("no hardware")
    if capability.permitted is False:
        out.append("permission not granted")
    if capability.implemented is None:
        out.append("not reported by the phone")
    return out


#: Prefix for the per-sensor rows. A sensor is a capability of its own: the
#: aggregate ``sensors`` row answers "may I stream anything", and each row below
#: answers "may I stream *this* one, and is it streaming now".
SENSOR_PREFIX = "sensor."


def sensor_capability(row: Mapping[str, Any]) -> Capability:
    """One ``sensor.list`` entry as a five-state capability.

    The phone sends ``available`` plus, from Phase 5 onwards, the individual
    inputs. Older payloads only carried ``available``, which is still honest:
    present/permitted/implemented stay ``None`` (unreported) rather than being
    filled in with guesses.
    """
    sensor_id = row.get("id", row.get("sensor_id", 0))
    try:
        numeric_id = int(str(sensor_id))
    except (TypeError, ValueError):
        numeric_id = 0
    label = str(row.get("label") or row.get("name") or f"sensor {numeric_id}")
    reported = bool(_as_bool(row.get("available")))
    present = _as_bool(row.get("present"))
    permitted = _as_bool(row.get("permitted"))
    implemented = _as_bool(row.get("implemented"))
    if implemented is None and reported:
        implemented = True
    detail = row.get("detail")
    return Capability(
        key=f"{SENSOR_PREFIX}{numeric_id}",
        label=label,
        present=present,
        permitted=permitted,
        implemented=implemented,
        available=reported,
        active=bool(row.get("active")),
        detail=detail if isinstance(detail, str) else None,
        phase=5,
        reported_available=reported,
    )


def sensor_registry(rows: Iterable[Mapping[str, Any]]) -> tuple[Capability, ...]:
    """Every reported sensor, in the order the phone listed them."""
    return tuple(sensor_capability(row) for row in rows)
