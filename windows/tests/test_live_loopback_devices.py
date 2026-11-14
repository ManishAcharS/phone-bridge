"""Choosing *which* output the speaker path captures.

The user's report: the phone plays PC audio, but the PC's own speakers keep
playing too, and muting them kills the phone's audio. That is not a bug in the
capture — WASAPI loopback copies what an endpoint renders, so capturing the
default output captures the speakers, and muting an endpoint zeroes what its
loopback hands over. The fix is a *second* virtual cable: the phone gets its own
endpoint, the room stays quiet, and the microphone keeps the cable it already has.

These tests cover the part that lives on the PC, with a fake PyAudioWPatch: which
devices are listed, what the listing says about them, and that the name reaches
the capture path on both ``speaker`` and ``start``. The real device list needs
Windows, and is in docs/19-audio.md §7b.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

import phonebridge.cli.live as live_cli
from phonebridge.live.config import SpeakerPlan
from phonebridge.live.devices import (
    DeviceUnavailable,
    describe_loopback_choice,
    is_virtual_cable,
    list_loopback_devices,
)


class FakeInfo(dict):
    """A dict with the attribute access PyAudio's device info also offers."""


#: What a PC with VB-CABLE installed looks like: the default output, two cables.
DEVICES = [
    FakeInfo(index=0, name="Speakers (Realtek(R) Audio)", maxInputChannels=2,
             defaultSampleRate=48000.0),
    FakeInfo(index=7, name="CABLE Input (VB-Audio Virtual Cable)", maxInputChannels=2,
             defaultSampleRate=48000.0),
    FakeInfo(index=9, name="CABLE-A Input (VB-Audio Cable A)", maxInputChannels=2,
             defaultSampleRate=48000.0),
    # Loopback entries with no channels are not capture devices.
    FakeInfo(index=11, name="Unused", maxInputChannels=0, defaultSampleRate=44100.0),
]


class FakePyAudio:
    paWASAPI = 1

    def __init__(self, devices: list[FakeInfo] | None = None, default_index: int = 0) -> None:
        self._devices = DEVICES if devices is None else devices
        self._default = default_index
        self.terminated = False

    def get_host_api_info_by_type(self, kind: int) -> dict[str, Any]:
        return {"defaultOutputDevice": self._default}

    def get_loopback_device_info_generator(self):
        yield from self._devices

    def terminate(self) -> None:
        self.terminated = True


class FakeModule:
    """Stands in for ``pyaudiowpatch``: same names, no audio."""

    paWASAPI = 1

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs

    def PyAudio(self) -> FakePyAudio:  # noqa: N802 - the real name is camelCase
        return FakePyAudio(**self._kwargs)


# --------------------------------------------------------------------------- #
# the listing
# --------------------------------------------------------------------------- #
def test_the_listing_marks_the_default_output_and_the_cables() -> None:
    devices = list_loopback_devices(module=FakeModule())

    names = [device["name"] for device in devices]
    assert names == [
        "Speakers (Realtek(R) Audio)",
        "CABLE Input (VB-Audio Virtual Cable)",
        "CABLE-A Input (VB-Audio Cable A)",
    ], "the device with no capture channels must not be offered"
    by_name = {device["name"]: device for device in devices}
    assert by_name["Speakers (Realtek(R) Audio)"]["default"] is True
    assert by_name["CABLE-A Input (VB-Audio Cable A)"]["default"] is False
    assert by_name["CABLE-A Input (VB-Audio Cable A)"]["virtual"] is True
    assert by_name["CABLE-A Input (VB-Audio Cable A)"]["sample_rate"] == 48000


def test_no_capture_device_is_a_sentence_not_a_traceback() -> None:
    with pytest.raises(DeviceUnavailable) as caught:
        list_loopback_devices(module=FakeModule(devices=[], default_index=-1))
    assert "no WASAPI loopback device" in str(caught.value)


def test_a_missing_package_names_the_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--list-loopback-devices`` on a PC without the extra: say which one."""
    import sys

    # A ``None`` entry in sys.modules makes the import raise ImportError, which is
    # exactly what a PC without the extra does — without breaking every other
    # import in the interpreter, as patching __import__ would.
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", None)
    monkeypatch.setitem(sys.modules, "soundcard", None)

    with pytest.raises(DeviceUnavailable) as caught:
        list_loopback_devices()
    assert "PyAudioWPatch" in str(caught.value)


def test_a_virtual_cable_is_described_as_quiet_and_a_speaker_as_audible() -> None:
    assert "speakers stay quiet" in describe_loopback_choice("CABLE-A Input (VB-Audio)")
    audible = describe_loopback_choice("Speakers (Realtek(R) Audio)")
    assert "still be heard" in audible
    assert "virtual cable" in audible, "the sentence must say what to do instead"
    assert is_virtual_cable("VoiceMeeter Input (VB-Audio VoiceMeeter VAIO)")
    assert not is_virtual_cable("Headphones (2- USB Audio)")


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #
def _args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {"format": "text", "list_loopback_devices": True}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_listing_prints_every_device_and_how_to_use_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(live_cli, "list_loopback_devices", lambda: list_loopback_devices(module=FakeModule()))

    code = live_cli.run(_args())

    out = capsys.readouterr().out
    assert code == 0
    assert "CABLE-A Input (VB-Audio Cable A)" in out
    assert "[default output]" in out
    assert "[virtual cable]" in out
    assert '--loopback-device "<name here>"' in out
    assert "two virtual cables" in out, "two cables is the recommended setup"


def test_listing_with_one_cable_says_to_install_a_second(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    only_mic = FakeModule(devices=DEVICES[:2])
    monkeypatch.setattr(live_cli, "list_loopback_devices", lambda: list_loopback_devices(module=only_mic))

    assert live_cli.run(_args()) == 0

    out = capsys.readouterr().out
    assert "one virtual cable is installed" in out
    assert "vb-audio.com/Cable/" in out


def test_listing_never_dials_the_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is a question about this PC; connecting would be a way to fail for free."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the listing must not open a session")

    monkeypatch.setattr(live_cli, "open_session", refuse)
    monkeypatch.setattr(live_cli, "list_loopback_devices", lambda: list_loopback_devices(module=FakeModule()))
    assert live_cli.run(_args()) == 0


def test_listing_says_what_is_missing_when_it_cannot_ask(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing() -> None:
        raise DeviceUnavailable("listing capture devices needs PyAudioWPatch", hint="pip install PyAudioWPatch")

    monkeypatch.setattr(live_cli, "list_loopback_devices", missing)

    assert live_cli.run(_args()) == 1
    captured = capsys.readouterr()
    # ``note`` writes to stderr, so a user who redirects stdout still sees it.
    assert "pip install PyAudioWPatch" in (captured.out + captured.err)


def test_the_flag_is_named_loopback_device_and_keeps_the_old_alias() -> None:
    """``--loopback`` was in the docs and in tutorials; renaming it breaks scripts."""
    parser = argparse.ArgumentParser()
    live_cli.add_parsers(parser.add_subparsers())
    for command in ("speaker", "start"):
        for spelling in ("--loopback-device", "--loopback"):
            args = parser.parse_args([command, spelling, "CABLE-A"])
            assert args.loopback_device == "CABLE-A"
        assert parser.parse_args([command, "--list-loopback-devices"]).list_loopback_devices


def test_the_named_device_reaches_the_capture_plan() -> None:
    plan = SpeakerPlan(latency_ms=60, device="CABLE-A Input")
    assert plan.as_dict()["device"] == "CABLE-A Input"


def test_speaker_passes_the_device_into_the_plan() -> None:
    parser = argparse.ArgumentParser()
    live_cli.add_parsers(parser.add_subparsers())
    args = parser.parse_args(["speaker", "--loopback-device", "cable-a"])
    args.command = "speaker"
    plan = live_cli.plan_from_args(args)
    assert plan.speaker is not None
    assert plan.speaker.device == "cable-a"


def test_start_passes_the_device_into_the_plan() -> None:
    parser = argparse.ArgumentParser()
    live_cli.add_parsers(parser.add_subparsers())
    args = parser.parse_args(["start", "--speaker", "--loopback-device", "cable-a"])
    args.command = "start"
    plan = live_cli.plan_from_args(args)
    assert plan.speaker is not None
    assert plan.speaker.device == "cable-a"
