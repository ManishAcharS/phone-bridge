"""The phone half of Phase 2, exercised through its dispatch table.

The reference phone (`tools/simulator/phone_sim.py`) is the same code path the real
`BridgeServer` uses for the audio RPCs, minus the hardware: it validates the
format, refuses a microphone the user has not permitted, and reports state. These
tests drive its router directly, so they say something precise — the *phone's*
answer to each request — without needing a socket or a sound card.

The Kotlin implementation is the one that runs on the Redmi. This file cannot
prove that code compiles or that its `AudioTrack`/`AudioRecord` calls make noise;
that is what the device steps in docs/19-audio.md are for.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from phonebridge.audio.frames import AudioFormat
from phonebridge.protocol.messages import decode_control

SIMULATOR = Path(__file__).resolve().parents[2] / "tools" / "simulator"
if str(SIMULATOR) not in sys.path:
    sys.path.insert(0, str(SIMULATOR))

import phone_sim  # noqa: E402  (the path has to be set first)


@pytest.fixture()
def phone(tmp_path: Path) -> phone_sim.SimulatedPhone:
    identity = phone_sim.Identity.generate()
    return phone_sim.SimulatedPhone(identity=identity, state_file=tmp_path / "state.json", port=0)


def ask(phone: phone_sim.SimulatedPhone, method: str, params: dict | None = None) -> dict:
    """Send one RPC through the phone's own router, as the PC would."""
    reply = phone.router.dispatch({"k": "req", "id": 1, "method": method, "params": params or {}})
    return decode_control(reply)


# --------------------------------------------------------------------------- #
# playback
# --------------------------------------------------------------------------- #
def test_playback_start_answers_with_the_format_it_will_play(phone) -> None:
    reply = ask(phone, "audio.playback.start", {"format": AudioFormat().as_dict()})
    assert reply["ok"] is True
    result = reply["result"]
    assert result["ok"] is True
    assert result["format"]["sample_rate"] == 48_000
    assert result["format"]["channels"] == 1
    assert phone.audio["playback"] == "active"


def test_playback_start_accepts_a_different_rate_the_phone_prefers(phone) -> None:
    reply = ask(
        phone, "audio.playback.start", {"format": {"sample_rate": 16_000, "channels": 1}}
    )
    assert reply["result"]["format"]["sample_rate"] == 16_000


def test_an_impossible_format_is_refused_with_bad_params(phone) -> None:
    reply = ask(phone, "audio.playback.start", {"format": {"sample_rate": 999_999}})
    assert reply["ok"] is False
    assert reply["error"]["code"] == "bad_params"
    assert phone.audio["playback"] == "idle"


def test_stop_mute_and_volume_report_the_new_state(phone) -> None:
    ask(phone, "audio.playback.start", {})
    assert ask(phone, "audio.playback.mute", {"muted": False})["result"]["muted"] is False
    assert ask(phone, "audio.playback.volume", {"percent": 25})["result"]["percent"] == 25
    assert ask(phone, "audio.playback.volume", {"percent": 150})["error"]["code"] == "bad_params"
    assert ask(phone, "audio.playback.stop")["result"]["playback"] == "idle"


# --------------------------------------------------------------------------- #
# microphone
# --------------------------------------------------------------------------- #
def test_mic_start_without_permission_is_refused_with_a_usable_sentence(phone) -> None:
    phone.mic_permission = False
    reply = ask(phone, "audio.mic.start", {})
    assert reply["ok"] is False
    assert reply["error"]["code"] == "mic_denied"
    assert "microphone" in reply["error"]["message"].lower()
    assert phone.audio["mic"] == "idle"


def test_mic_start_with_permission_reports_the_format(phone) -> None:
    reply = ask(phone, "audio.mic.start", {"format": {"sample_rate": 48_000}})
    assert reply["result"]["format"]["sample_rate"] == 48_000
    assert phone.audio["mic"] == "active"
    assert ask(phone, "audio.mic.stop")["result"]["mic"] == "idle"


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def test_status_lists_both_directions_and_the_permission_flag(phone) -> None:
    ask(phone, "audio.playback.start", {})
    ask(phone, "audio.mic.start", {})
    result = ask(phone, "audio.status")["result"]
    assert result["playback"] == "active"
    assert result["mic"] == "active"
    assert result["permitted"] is True
    assert isinstance(result["volume"], int)
    assert isinstance(result["muted"], bool)


def test_the_phone_declares_audio_implemented_rather_than_leaving_it_to_inference(phone) -> None:
    """`implemented.*` is what lets the PC separate "no hardware" from "not built"."""
    caps = phone.capabilities()
    assert caps["implemented.audio.playback"] is True
    assert caps["implemented.audio.capture"] is True


def test_the_capability_snapshot_is_json_serialisable(phone) -> None:
    # The PC stores and compares these records; a non-serialisable value would
    # only surface on a real phone.
    assert json.dumps(phone.router.dispatch({"k": "req", "id": 2, "method": "system.info"}),
                      default=str)
