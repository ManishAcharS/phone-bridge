"""Rendering for the command line: tables, capability views, device summaries.

Everything here is pure formatting over data the *phone* reported.  It never
invents a value: a capability the phone did not mention is printed as ``?``
rather than quietly rendered as "no" or, worse, "yes".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The capability keys of the contract described in docs/03-protocol.md §6.  A
# phone may report more (an older app reports fewer); this is the order the
# table is printed in, not a claim about what exists.
KNOWN_CAPABILITIES: tuple[str, ...] = (
    "audio.playback",
    "audio.capture",
    "audio.bluetooth_sco",
    "camera.back",
    "camera.front",
    "sensors",
    "file_transfer",
    "bluetooth_scan",
)

# Methods protocol v1 actually defines (docs/03-protocol.md §6, and the
# dispatch table in net/BridgeServer.kt).  Used to explain, in one line, why the
# CLI has no `files`/`camera`/`sensors` verbs: there is nothing on the phone to
# call, and printing a fake success would be worse than printing nothing.
PROTOCOL_V1_METHODS: tuple[str, ...] = (
    "system.info",
    "system.echo",
    "session.stats",
    "bluetooth.headset.status",
    "audio.route.bluetooth",
    "audio.capabilities.run",
    "audio.capabilities.last",
    "audio.playback.start",
    "audio.playback.stop",
    "audio.playback.mute",
    "audio.playback.volume",
    "audio.mic.start",
    "audio.mic.stop",
    "audio.status",
)

NO_VERB_NOTE = (
    "file transfer, camera streaming and sensor streams have no RPC in protocol v1, "
    "so this CLI offers no command that pretends to do them (see docs/04-roadmap.md)."
)

_FLAG = {True: "yes", False: "no", None: "?"}

# Keys that describe the reporter rather than a feature.
_MARKERS = {"simulated"}


@dataclass(frozen=True)
class Capability:
    key: str
    hardware: bool | None
    usable: bool | None
    detail: str | None

    def hardware_text(self) -> str:
        return _FLAG[self.hardware]

    def usable_text(self) -> str:
        return _FLAG[self.usable]

    def note(self) -> str:
        if self.detail:
            return self.detail
        if self.hardware is False:
            return "not present on this phone"
        if self.usable is True:
            return "usable"
        if self.hardware is None and self.usable is None:
            return "not reported by this phone"
        return "-"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "hardware": self.hardware,
            "usable": self.usable,
            "detail": self.detail,
        }


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    # Some phones have been seen to send 1/0; accept them, do not guess further.
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return None


def capabilities_of(info: dict[str, Any]) -> dict[str, Any]:
    caps = info.get("capabilities")
    return caps if isinstance(caps, dict) else {}


def capability_rows(info: dict[str, Any]) -> list[Capability]:
    caps = capabilities_of(info)
    rows: list[Capability] = []
    for key in KNOWN_CAPABILITIES:
        rows.append(
            Capability(
                key=key,
                hardware=_as_bool(caps.get(f"hardware.{key}")),
                usable=_as_bool(caps.get(key)),
                detail=_optional_str(caps.get(f"detail.{key}")),
            )
        )
    # Anything the phone reports that the contract does not list (a newer app)
    # is shown too instead of being dropped. Markers such as the simulator's own
    # `simulated` flag describe the reporter, not a feature, so they are skipped.
    for key, value in sorted(caps.items()):
        if key.startswith(("hardware.", "detail.")) or key in KNOWN_CAPABILITIES:
            continue
        if key in _MARKERS:
            continue
        rows.append(
            Capability(key=key, hardware=None, usable=_as_bool(value), detail=None)
        )
    return rows


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_simulated(info: dict[str, Any]) -> bool:
    return bool(capabilities_of(info).get("simulated"))


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def clip(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= width:
        return text
    return text[: max(0, width - 3)].rstrip() + "..."


def table(headers: list[str], rows: list[list[str]], *, indent: str = "  ") -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [indent + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append(indent + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append(indent + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# capabilities
# --------------------------------------------------------------------------- #
def render_capabilities(info: dict[str, Any]) -> str:
    caps = capabilities_of(info)
    legacy = not any(k.startswith("hardware.") for k in caps)
    out: list[str] = []
    if is_simulated(info):
        out.append(
            "NOTE: simulated phone - these capability values are synthetic and say "
            "nothing about any real handset."
        )
        out.append("")
    out.append(
        f"capabilities of {info.get('name', '?')} "
        f"({info.get('model', 'unknown model')}, android {info.get('android_version', '?')}, "
        f"protocol v{info.get('protocol_version', '?')})"
    )
    out.append("")
    rows = [
        [row.key, row.hardware_text(), row.usable_text(), clip(row.note(), 62)]
        for row in capability_rows(info)
    ]
    out.append(table(["CAPABILITY", "HARDWARE", "USABLE", "NOTE"], rows))
    out.append("")
    out.append("  hardware.* is measured by the phone; usable is what it can actually")
    out.append("  deliver today (hardware AND implemented AND permitted).")
    if legacy:
        out.append("")
        out.append(
            "  This phone reports the pre-0.2 capability set: no hardware.* or detail.* "
            "keys, so USABLE is all it can say about itself."
        )
    out.append("")
    for line in _wrap(NO_VERB_NOTE, 72):
        out.append("  " + line)
    return "\n".join(out)


def capability_report(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "device_id": info.get("device_id"),
        "name": info.get("name"),
        "model": info.get("model"),
        "android_version": info.get("android_version"),
        "sdk_int": info.get("sdk_int"),
        "app_version": info.get("app_version"),
        "protocol_version": info.get("protocol_version"),
        "simulated": is_simulated(info),
        "capabilities": [row.to_dict() for row in capability_rows(info)],
        "permissions": info.get("permissions") or {},
    }


def unmet_requirements(info: dict[str, Any], required: list[str]) -> list[str]:
    """Return the required capability keys this phone cannot deliver today."""
    caps = capabilities_of(info)
    missing: list[str] = []
    for key in required:
        value = _as_bool(caps.get(key))
        if value is not True:
            missing.append(key)
    return missing


# --------------------------------------------------------------------------- #
# device info
# --------------------------------------------------------------------------- #
INFO_FIELDS: tuple[tuple[str, str], ...] = (
    ("device_id", "device id"),
    ("name", "device name"),
    ("model", "model"),
    ("manufacturer", "manufacturer"),
    ("brand", "brand"),
    ("device", "codename"),
    ("android_version", "android"),
    ("sdk_int", "sdk"),
    ("target_sdk", "target sdk"),
    ("app_version", "app version"),
    ("protocol_version", "protocol"),
    ("rom", "rom"),
    ("rom_version", "rom build"),
    ("is_emulator", "emulator"),
)


def render_info(info: dict[str, Any]) -> str:
    out = [f"device information from {info.get('name', '?')}:"]
    for key, label in INFO_FIELDS:
        value = info.get(key)
        if value is None or value == "":
            continue
        out.append(f"  {label:<12}: {value}")
    permissions = info.get("permissions")
    if isinstance(permissions, dict) and permissions:
        out.append("")
        out.append("  runtime permissions:")
        for name, state in sorted(permissions.items()):
            out.append(f"    {name:<22}: {state}")
    caps = capabilities_of(info)
    if caps:
        rows = capability_rows(info)
        usable = sum(1 for row in rows if row.usable is True)
        known = sum(1 for row in rows if row.key in KNOWN_CAPABILITIES)
        out.append("")
        out.append(
            f"  capabilities: {usable} of {known} usable today "
            "(run `phonebridge capabilities` for the table)"
        )
    return "\n".join(out)


def render_audio_status(status: dict[str, Any], route: dict[str, Any] | None) -> str:
    out = ["bluetooth headset (as reported by the phone):"]
    connected = status.get("connected")
    out.append(f"  headset connected : {_FLAG.get(_as_bool(connected), str(connected))}")
    profile = status.get("profile") or status.get("profile_name")
    if profile:
        out.append(f"  active profile    : {profile}")
    devices = status.get("devices")
    if isinstance(devices, list) and devices:
        out.append("  communication devices:")
        for device in devices:
            out.append(f"    - {device}")
    detail = status.get("detail")
    if detail:
        out.append(f"  note              : {detail}")
    out.append("")
    if route is None:
        out.append("audio relay (Phase 2): this phone does not report a relay state.")
    else:
        supported = _as_bool(route.get("supported"))
        out.append(f"audio relay (Phase 2): supported={_FLAG[supported]}")
        route_detail = route.get("detail")
        if route_detail:
            out.append(f"  {route_detail}")
    return "\n".join(out)
