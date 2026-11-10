"""Snapshot -> the exact strings and flags a widget draws.

All of the judgement lives here rather than in the window: which button is
enabled in which state, how a tri-state capability cell is spelled, what colour a
state is. That keeps ``ui/app.py`` to layout, and it makes the rules testable
without tkinter — which matters, because this environment has no display.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    "wifi": "Wi-Fi",
    "hotspot": "hotspot",
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
        adb="found" if snapshot.adb_available else "not on PATH (USB unavailable)",
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
