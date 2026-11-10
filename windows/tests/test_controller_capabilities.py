"""The five-state capability registry.

The rule this file exists to defend: hardware being present is never enough to
show a capability as usable, and a missing input is never silently turned into
"no". A few of these tests parse ``DeviceFacts.kt`` so the PC's idea of which
permission gates which capability cannot drift away from the phone's.
"""

from __future__ import annotations

import re
from pathlib import Path

from phonebridge.controller.capabilities import (
    KEYS,
    LABELS,
    REQUIRED_PERMISSIONS,
    blockers,
    build_registry,
    mark_active,
)

REPO = Path(__file__).resolve().parents[2]
DEVICE_FACTS = REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge" / "net" / "DeviceFacts.kt"


def info(**capabilities) -> dict:
    """Build a payload the way the phone does: usable flag + hardware + permission."""
    payload: dict = {"permissions": {}}
    for key, spec in capabilities.items():
        hardware, usable, permission = spec
        payload[key] = usable
        payload[f"hardware.{key}"] = hardware
        if permission is not None:
            payload["permissions"][REQUIRED_PERMISSIONS[key]] = permission
    return payload


def by_key(registry):
    return {c.key: c for c in registry}


# --------------------------------------------------------------------------- #
# the five states
# --------------------------------------------------------------------------- #
def test_everything_true_is_the_only_way_to_be_available() -> None:
    registry = by_key(build_registry(info(**{"camera.back": (True, True, None)})))
    camera = registry["camera.back"]
    assert (camera.present, camera.implemented, camera.available) == (True, True, True)
    assert camera.permitted is None, "this capability needs no permission"


def test_hardware_alone_is_not_availability() -> None:
    registry = by_key(build_registry(info(**{"camera.back": (True, False, None)})))
    camera = registry["camera.back"]
    assert camera.present is True
    assert camera.available is False
    assert camera.implemented is False, (
        "hardware is present and no permission is involved, so the phone's 'no' "
        "can only mean the feature is not implemented"
    )
    assert "not implemented" in blockers(camera)[0]


def test_missing_hardware_leaves_implementation_unknown() -> None:
    registry = by_key(build_registry(info(**{"audio.bluetooth_sco": (False, False, None)})))
    sco = registry["audio.bluetooth_sco"]
    assert sco.present is False
    assert sco.implemented is None, "the PC must not guess"
    assert sco.available is False


def test_permission_denial_is_named_as_the_blocker() -> None:
    registry = by_key(build_registry(info(**{"audio.capture": (True, False, "denied")})))
    capture = registry["audio.capture"]
    assert capture.present is True
    assert capture.permitted is False
    assert capture.available is False
    assert "permission not granted" in blockers(capture)


def test_granted_permission_with_no_feature_is_not_available() -> None:
    registry = by_key(build_registry(info(**{"audio.capture": (True, False, "granted")})))
    capture = registry["audio.capture"]
    assert capture.permitted is True
    assert capture.implemented is False


def test_contradictory_phone_claim_is_downgraded_and_flagged() -> None:
    registry = by_key(build_registry(info(**{"camera.front": (False, True, None)})))
    front = registry["camera.front"]
    assert front.available is False, "a claimed capability without hardware is not usable"
    assert front.contradiction is not None
    assert "no hardware" in front.contradiction


def test_explicit_implemented_key_is_honoured_when_a_phone_publishes_one() -> None:
    payload = info(**{"camera.back": (True, False, None)})
    payload["implemented.camera.back"] = True
    camera = by_key(build_registry(payload))["camera.back"]
    assert camera.implemented is True
    assert camera.available is False, "the phone still says it is not usable"


def test_unknown_inputs_stay_unknown() -> None:
    registry = by_key(build_registry({}))
    camera = registry["camera.back"]
    assert camera.present is None
    assert camera.implemented is None
    assert camera.permitted is None
    assert camera.available is False


def test_registry_covers_every_documented_capability() -> None:
    registry = build_registry({})
    assert [c.key for c in registry] == list(KEYS)
    assert len(registry) == 8
    assert all(c.label for c in registry)


def test_detail_from_the_phone_is_kept() -> None:
    payload = info(**{"sensors": (True, False, None)})
    payload["detail.sensors"] = "present: accelerometer, gyroscope"
    sensors = by_key(build_registry(payload))["sensors"]
    assert sensors.detail == "present: accelerometer, gyroscope"


# --------------------------------------------------------------------------- #
# active is a runtime fact, not a guess
# --------------------------------------------------------------------------- #
def test_nothing_is_active_by_default() -> None:
    registry = build_registry(info(**{"camera.back": (True, True, None)}))
    assert all(not c.active for c in registry)


def test_mark_active_only_touches_the_named_capability() -> None:
    registry = build_registry(info(**{"camera.back": (True, True, None)}))
    updated = mark_active(registry, "camera.back")
    states = {c.key: c.active for c in updated}
    assert states["camera.back"] is True
    assert sum(states.values()) == 1
    assert not any(c.active for c in registry), "the input registry is not mutated"


# --------------------------------------------------------------------------- #
# drift against the Android source
# --------------------------------------------------------------------------- #
def _kotlin_rows() -> dict[str, str | None]:
    text = DEVICE_FACTS.read_text("utf-8")
    rows: dict[str, str | None] = {}
    for block in text.split("CapabilityRow(")[1:]:
        key = re.search(r'key = "([^"]+)"', block)
        if not key:
            continue
        permission = re.search(r"permission = android\.Manifest\.permission\.(\w+)", block)
        rows[key.group(1)] = permission.group(1) if permission else None
    return rows


def test_capability_keys_match_the_android_source() -> None:
    rows = _kotlin_rows()
    assert rows, "could not read DeviceFacts.capabilityRows()"
    assert set(rows) == set(KEYS), (
        "the PC registry and DeviceFacts.capabilityRows() disagree: "
        f"only in PC {set(KEYS) - set(rows)}, only in Kotlin {set(rows) - set(KEYS)}"
    )


def test_required_permissions_match_the_android_source() -> None:
    rows = _kotlin_rows()
    from_kotlin = {key: permission for key, permission in rows.items() if permission}
    assert from_kotlin == dict(REQUIRED_PERMISSIONS), (
        "a capability's gating permission is declared differently on the two sides: "
        f"Kotlin {from_kotlin}, PC {dict(REQUIRED_PERMISSIONS)}"
    )


def test_labels_are_present_for_every_key() -> None:
    assert set(LABELS) == set(KEYS)
