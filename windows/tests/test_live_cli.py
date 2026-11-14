"""Command line surface for the three delivery paths.

These tests never connect to a phone and never open a device: they pin the flags
a user is told to type, the refusal messages, and that a missing driver reaches
them as an instruction instead of a traceback.
"""

from __future__ import annotations

import pytest

from phonebridge.cli import live
from phonebridge.cli.main import build_parser
from phonebridge.cli.target import CliError
from phonebridge.live.devices import DeviceUnavailable


def _parse(*argv: str):
    return build_parser().parse_args(list(argv))


def test_the_four_commands_exist_and_parse() -> None:
    assert _parse("camera", "--virtual").virtual is True
    assert _parse("mic", "--to-cable").to_cable is True
    assert _parse("speaker").latency_ms == 200
    assert _parse("start", "--camera", "--mic", "--speaker").speaker is True


def test_camera_defaults_match_the_call_defaults() -> None:
    args = _parse("camera", "--virtual")

    assert (args.width, args.height, args.fps) == (1280, 720, 30)
    assert args.facing == "back"
    assert args.backend == "auto"


def test_camera_plan_is_built_from_the_flags() -> None:
    plan = live.plan_from_args(_parse("camera", "--virtual", "--facing", "front", "--fps", "24"))

    assert plan.names == ("camera",)
    assert plan.camera is not None
    assert plan.camera.facing == "front"
    assert plan.camera.fps == 24


def test_camera_without_virtual_is_refused_with_a_hint() -> None:
    with pytest.raises(CliError) as caught:
        live.plan_from_args(_parse("camera"))

    assert "--virtual" in str(caught.value)
    assert caught.value.hint


def test_mic_without_to_cable_is_refused_with_a_hint() -> None:
    with pytest.raises(CliError) as caught:
        live.plan_from_args(_parse("mic"))

    assert "--to-cable" in str(caught.value)
    assert caught.value.hint


def test_start_needs_at_least_one_path() -> None:
    with pytest.raises(CliError, match="at least one"):
        live.plan_from_args(_parse("start"))


def test_start_builds_all_three_paths() -> None:
    plan = live.plan_from_args(
        _parse(
            "start",
            "--camera",
            "--mic",
            "--speaker",
            "--lipsync",
            "150",
            "--mic-latency",
            "80",
            "--speaker-latency",
            "300",
        )
    )

    assert plan.names == ("camera", "mic", "speaker")
    assert plan.mic is not None and plan.mic.latency_ms == 80
    assert plan.speaker is not None and plan.speaker.latency_ms == 300
    assert plan.speaker.lipsync_ms == 150


def test_a_bad_number_is_a_usage_error_not_a_crash() -> None:
    with pytest.raises(CliError):
        live.plan_from_args(_parse("mic", "--to-cable", "--latency", "5"))


def test_a_missing_driver_becomes_a_cli_error_with_its_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(args):  # noqa: ANN001, ARG001
        raise DeviceUnavailable("OBS is not installed", hint="install OBS Studio")

    monkeypatch.setattr(live, "_run", boom)

    with pytest.raises(CliError) as caught:
        live.run(_parse("camera", "--virtual"))

    assert "OBS is not installed" in str(caught.value)
    assert caught.value.hint == "install OBS Studio"


def test_the_mic_device_flag_does_not_collide_with_the_phone_selector() -> None:
    """--device already means "which phone"; the cable device needed its own flag."""
    args = _parse("mic", "--to-cable", "--out-device", "CABLE Input (VB-Audio)")

    assert args.out_device == "CABLE Input (VB-Audio)"
    assert args.device is None  # the phone selector, untouched


def test_speaker_loopback_flag_is_separate_from_the_phone_selector() -> None:
    args = _parse("speaker", "--loopback", "Speakers")

    assert args.loopback_device == "Speakers"
    assert args.device is None
