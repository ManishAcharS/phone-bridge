"""The rules a widget draws: button state, tri-state cells, honest summaries.

No display is involved, which is the point — these decisions are the part of the
UI that can actually be wrong, so they live outside tkinter.
"""

from __future__ import annotations

import pytest

from phonebridge.controller.capabilities import build_registry
from phonebridge.controller.manager import Snapshot, State
from phonebridge.controller.view import (
    TONE_BUSY,
    TONE_ERROR,
    TONE_IDLE,
    TONE_OK,
    TONE_WARN,
    build_view,
    capability_row,
    cell,
    connected_for,
    summary,
    transport_text,
)


def snapshot(**kwargs) -> Snapshot:
    return Snapshot(**kwargs)


# --------------------------------------------------------------------------- #
# tri-state cells
# --------------------------------------------------------------------------- #
def test_cells_never_turn_unknown_into_no() -> None:
    assert cell(True) == "yes"
    assert cell(False) == "no"
    assert cell(None) == "unknown"
    assert cell(None, not_required=True) == "n/a"


# --------------------------------------------------------------------------- #
# buttons
# --------------------------------------------------------------------------- #
def test_disconnected_offers_connect() -> None:
    view = build_view(snapshot())
    assert view.state_label == "Disconnected"
    assert view.connect_enabled and not view.disconnect_enabled
    assert view.message.startswith("Not connected")
    assert view.tone == TONE_IDLE


@pytest.mark.parametrize(
    ("kind", "words"),
    [
        ("usb", "USB (adb forward)"),
        ("adb-wireless", "adb over Wi-Fi"),
        ("gateway", "hotspot gateway"),
        ("manual", "typed address"),
        ("cached", "saved address"),
        ("hotspot", "hotspot"),
    ],
)
def test_every_transport_has_a_sentence(kind: str, words: str) -> None:
    assert transport_text(Snapshot(transport=kind)) == words


def test_connected_offers_disconnect_and_reconnect() -> None:
    view = build_view(
        snapshot(
            state=State.CONNECTED,
            transport="usb",
            endpoint="127.0.0.1:50001",
            auth_ok=True,
            server_reachable=True,
        )
    )
    assert not view.connect_enabled
    assert view.disconnect_enabled and view.reconnect_enabled
    assert view.tone == TONE_OK
    assert view.transport == "USB (adb forward)"
    assert "Pinned key verified" in view.authentication
    assert view.auth_ok.startswith("yes")


def test_connected_without_a_confirmed_handshake_does_not_claim_authentication() -> None:
    """``state=CONNECTED`` alone is not proof the pinned key was verified."""
    view = build_view(snapshot(state=State.CONNECTED, transport="usb"))
    assert "Pinned key verified" not in view.authentication
    assert "not confirmed" in view.authentication
    assert view.auth_ok == "not authenticated"


def test_every_busy_state_disables_connect() -> None:
    for state in (State.DISCOVERING, State.CONNECTING, State.AUTHENTICATING, State.RECONNECTING):
        view = build_view(snapshot(state=state))
        assert not view.connect_enabled, state
        assert view.disconnect_enabled, state


def test_pairing_takes_over_the_window() -> None:
    view = build_view(snapshot(state=State.PAIRING_REQUIRED, pairing_code="123456"))
    assert view.pairing_visible
    assert view.pairing_code == "123456"
    assert not view.connect_enabled, "Connect must not start a second attempt mid-pairing"
    assert view.disconnect_enabled
    assert view.tone == TONE_WARN


def test_error_shows_the_message_and_offers_reconnect() -> None:
    view = build_view(snapshot(state=State.ERROR, message="no phone found"))
    assert view.error_text == "no phone found"
    assert view.reconnect_enabled
    assert view.tone == TONE_ERROR
    assert not view.pairing_visible


def test_reconnecting_has_its_own_tone() -> None:
    assert build_view(snapshot(state=State.RECONNECTING)).tone == TONE_WARN
    assert build_view(snapshot(state=State.CONNECTING)).tone == TONE_BUSY


# --------------------------------------------------------------------------- #
# capability table
# --------------------------------------------------------------------------- #
def test_capability_rows_keep_the_five_states_apart() -> None:
    info = {
        "capabilities": {
            "camera.back": False,
            "hardware.camera.back": True,
            "detail.camera.back": "camera streaming is Phase 4",
        },
        "permissions": {},
    }
    row = capability_row(build_registry(info)[3])  # camera.back
    assert (row.present, row.permitted, row.implemented) == ("yes", "n/a", "no")
    assert (row.available, row.active) == ("no", "no")
    assert "Phase 4" in row.note


def test_a_contradiction_is_visible_in_the_row() -> None:
    info = {
        "capabilities": {"camera.front": True, "hardware.camera.front": False},
        "permissions": {},
    }
    row = capability_row(build_registry(info)[4])
    assert row.present == "no"
    assert row.available == "no"
    assert "impossible" in row.note or "no hardware" in row.note


def test_summary_counts_but_never_flatters() -> None:
    assert summary(snapshot()) == "No capability report yet — connect to ask the phone."
    info = {
        "capabilities": {
            "camera.back": False,
            "hardware.camera.back": True,
            "camera.front": False,
            "hardware.camera.front": True,
        },
        "permissions": {},
    }
    text = summary(snapshot(capabilities=build_registry(info)))
    assert "0 of 8 capabilities usable" in text
    assert "2 with hardware" in text


# --------------------------------------------------------------------------- #
# misc fields
# --------------------------------------------------------------------------- #
def test_transport_words() -> None:
    assert transport_text(snapshot()) == "not selected"
    assert transport_text(snapshot(transport="usb")) == "USB (adb forward)"
    assert transport_text(snapshot(transport="hotspot")) == "hotspot"
    assert transport_text(snapshot(transport="something-new")) == "something-new"


def test_connected_for_formatting() -> None:
    assert connected_for(snapshot()) == "—"
    connected = snapshot(state=State.CONNECTED, connected_at=1000.0)
    assert connected_for(connected, now=1004.0) == "4s"
    assert connected_for(connected, now=1090.0) == "1m 30s"
    assert connected_for(connected, now=1000.0 + 3700) == "1h 01m"


def test_diagnostics_fallback_is_explicit() -> None:
    assert build_view(snapshot()).diagnostics == "Nothing logged yet."
    assert build_view(snapshot(diagnostics=("a", "b"))).diagnostics == "a\nb"


def test_device_line_shows_name_and_model_once() -> None:
    assert snapshot().device_line() == "no phone"
    assert snapshot(device={"name": "Pixel"}).device_line() == "Pixel"
    both = snapshot(device={"name": "Redmi Note 14 Pro+", "model": "24115RA8EI"})
    assert both.device_line() == "Redmi Note 14 Pro+ (24115RA8EI)"
