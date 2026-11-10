"""Snapshot -> the exact strings and flags a widget draws.

All of the judgement lives here rather than in the window: which button is
enabled in which state, how a tri-state capability cell is spelled, what colour a
state is. That keeps ``ui/app.py`` to layout, and it makes the rules testable
without tkinter — which matters, because this environment has no display.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..transport.link import DEFAULT_PORT
from .capabilities import Capability
from .manager import Snapshot, State

#: Tone names rather than colours: the window owns the palette.
TONE_OK = "ok"
TONE_BUSY = "busy"
TONE_WARN = "warn"
TONE_ERROR = "error"
TONE_IDLE = "idle"

_TONES: dict[State, str] = {
    State.CONNECTED: TONE_OK,
    State.DISCOVERING: TONE_BUSY,
    State.CONNECTING: TONE_BUSY,
    State.AUTHENTICATING: TONE_BUSY,
    State.RECONNECTING: TONE_WARN,
    State.PAIRING_REQUIRED: TONE_WARN,
    State.ERROR: TONE_ERROR,
    State.DISCONNECTED: TONE_IDLE,
}

_TRANSPORT_WORDS = {
    "usb": "USB (adb forward)",
    "usb-wireless": "USB debugging over Wi-Fi",
    "adb-wireless": "adb over Wi-Fi",
    "wifi": "Wi-Fi",
    "hotspot": "hotspot",
    "gateway": "hotspot gateway",
    "manual": "typed address",
    "cached": "saved address",
}


@dataclass(frozen=True)
class CapabilityRow:
    name: str
    present: str
    permitted: str
    implemented: str
    available: str
    active: str
    note: str


@dataclass(frozen=True)
class View:
    state_label: str
    tone: str
    message: str
    device: str
    transport: str
    endpoint: str
    authentication: str
    adb: str
    attempts: str
    connected_for: str
    summary: str
    connect_label: str
    connect_enabled: bool
    disconnect_enabled: bool
    reconnect_enabled: bool
    pairing_visible: bool
    pairing_code: str
    error_text: str
    capabilities: tuple[CapabilityRow, ...]
    diagnostics: str
    # Milestone 2: the window has to separate the failures that all used to
    # read "no phone found".
    adb_path: str = "—"
    usb: str = "—"
    wifi: str = "—"
    server: str = "—"
    auth_ok: str = "—"
    discovery_error: str = "—"
    connection_error: str = "—"


def tri(value: bool | None, yes: str, no: str, unknown: str = "not checked yet") -> str:
    """Spell a yes/no/unknown honestly — ``None`` is never rendered as "no"."""
    if value is None:
        return unknown
    return yes if value else no


def adb_text(snapshot: Snapshot) -> str:
    """What adb is, where it came from, and whether it can see anything."""
    if not snapshot.adb_available:
        detail = f" ({snapshot.adb_detail})" if snapshot.adb_detail else ""
        return f"not found — USB unavailable, Wi-Fi unaffected{detail}"
    return snapshot.adb_detail or "ready"


def adb_path_text(snapshot: Snapshot) -> str:
    return snapshot.adb_path or "not found in PATH, ANDROID_HOME, ANDROID_SDK_ROOT or the usual SDK folders"


def usb_text(snapshot: Snapshot) -> str:
    if not snapshot.adb_available:
        return "unavailable — adb is not installed"
    if snapshot.usb_available:
        return "yes — a cable-attached device is listed"
    return "no device on USB — plug in the cable and accept the debugging prompt"


def server_text(snapshot: Snapshot) -> str:
    return tri(
        snapshot.server_reachable,
        "yes — the phone app answered on TCP",
        "no — nothing is listening at that address",
    )


def auth_text(snapshot: Snapshot) -> str:
    return tri(
        snapshot.auth_ok,
        "yes — pinned key verified, AES-256-GCM in use",
        "not authenticated",
        "not yet",
    )


def discovery_text(snapshot: Snapshot) -> str:
    return snapshot.last_discovery_error or "none"


def connection_text(snapshot: Snapshot) -> str:
    return snapshot.last_connection_error or "none"


class EndpointError(ValueError):
    """What the user typed is not an address we can try."""


def parse_endpoint(text: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    """``"10.0.0.5"`` or ``"10.0.0.5:8765"`` -> ``(host, port)``.

    Deliberately strict about the port and permissive about the host: a hostname
    is a valid thing to type, a nonsense port is not.
    """
    cleaned = text.strip()
    if not cleaned:
        raise EndpointError("type the phone's address, for example 192.168.1.20:8765")
    host, separator, port_text = cleaned.partition(":")
    host = host.strip()
    if not host or " " in host:
        raise EndpointError(f"'{text.strip()}' is not a host address")
    if not separator or not port_text.strip():
        return host, default_port
    port_text = port_text.strip()
    if not port_text.isdigit():
        raise EndpointError(f"'{port_text}' is not a port number")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise EndpointError(f"port {port} is outside 1-65535")
    return host, port


def cell(value: bool | None, *, not_required: bool = False) -> str:
    """Spell one tri-state honestly.

    ``None`` is not "no" — it means the phone did not report the input, or the
    capability needs no permission at all. Showing "no" there would be a lie the
    user cannot detect.
    """
    if value is None:
        return "n/a" if not_required else "unknown"
    return "yes" if value else "no"


def capability_row(capability: Capability) -> CapabilityRow:
    note_parts: list[str] = []
    if capability.contradiction:
        note_parts.append(capability.contradiction)
    if capability.detail:
        note_parts.append(capability.detail)
    if capability.phase and not capability.available:
        note_parts.append(f"Phase {capability.phase}")
    return CapabilityRow(
        name=capability.label,
        present=cell(capability.present),
        permitted=cell(capability.permitted, not_required=True),
        implemented=cell(capability.implemented),
        available="yes" if capability.available else "no",
        active="yes" if capability.active else "no",
        note="; ".join(note_parts),
    )


def transport_text(snapshot: Snapshot) -> str:
    if not snapshot.transport:
        return "not selected"
    return _TRANSPORT_WORDS.get(snapshot.transport, snapshot.transport)


def connected_for(snapshot: Snapshot, now: float | None = None) -> str:
    if snapshot.connected_at is None or not snapshot.connected:
        return "—"
    import time as _time

    seconds = max(0.0, (now if now is not None else _time.time()) - snapshot.connected_at)
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def summary(snapshot: Snapshot) -> str:
    """One line that never overstates what works."""
    if not snapshot.capabilities:
        return "No capability report yet — connect to ask the phone."
    available = sum(1 for c in snapshot.capabilities if c.available)
    active = sum(1 for c in snapshot.capabilities if c.active)
    present = sum(1 for c in snapshot.capabilities if c.present)
    return (
        f"{available} of {len(snapshot.capabilities)} capabilities usable · "
        f"{active} active · {present} with hardware"
    )


def build_view(snapshot: Snapshot) -> View:
    state = snapshot.state
    connected = state is State.CONNECTED
    pairing = state is State.PAIRING_REQUIRED
    return View(
        state_label=state.label,
        tone=_TONES.get(state, TONE_IDLE),
        message=snapshot.message or _default_message(state),
        device=snapshot.device_line(),
        transport=transport_text(snapshot),
        endpoint=snapshot.endpoint or "—",
        authentication=snapshot.auth_line(),
        adb=adb_text(snapshot),
        attempts=str(snapshot.attempts) if snapshot.attempts else "—",
        connected_for=connected_for(snapshot),
        summary=summary(snapshot),
        connect_label="Connect" if not state.busy else state.label + "…",
        connect_enabled=not state.busy and not connected and not pairing,
        disconnect_enabled=connected or state.busy or pairing,
        reconnect_enabled=state in {State.ERROR, State.DISCONNECTED} or connected,
        pairing_visible=pairing,
        pairing_code=snapshot.pairing_code or "",
        error_text=snapshot.message if state is State.ERROR else "",
        capabilities=tuple(capability_row(c) for c in snapshot.capabilities),
        diagnostics="\n".join(snapshot.diagnostics) or "Nothing logged yet.",
        adb_path=adb_path_text(snapshot),
        usb=usb_text(snapshot),
        wifi=snapshot.wifi_status or "not checked yet",
        server=server_text(snapshot),
        auth_ok=auth_text(snapshot),
        discovery_error=discovery_text(snapshot),
        connection_error=connection_text(snapshot),
    )


def _default_message(state: State) -> str:
    return {
        State.DISCONNECTED: "Not connected. Press Connect to find your phone.",
        State.DISCOVERING: "Looking for your phone over USB and the local network…",
        State.CONNECTING: "Opening an encrypted channel…",
        State.AUTHENTICATING: "Verifying the phone's pinned key…",
        State.CONNECTED: "Connected and authenticated.",
        State.RECONNECTING: "The link dropped; trying again.",
        State.PAIRING_REQUIRED: "Compare the code with the one on the phone.",
        State.ERROR: "Something went wrong.",
    }[state]
