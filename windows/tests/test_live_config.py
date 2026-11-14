"""The live plans refuse nonsense before a device is opened."""

from __future__ import annotations

import pytest

from phonebridge.live.config import (
    CameraPlan,
    ConfigError,
    LivePlan,
    MicPlan,
    SpeakerPlan,
)


def test_camera_defaults_are_the_call_defaults() -> None:
    plan = CameraPlan()

    assert (plan.width, plan.height, plan.fps) == (1280, 720, 30)
    assert plan.backend == "auto"
    assert plan.label == "rear 1280x720 @ 30 fps -> auto"


def test_auto_backend_is_an_order_not_a_device() -> None:
    """'auto' must expand to the real backends, or pyvirtualcam gets a bad name."""
    assert CameraPlan(backend="auto").backends == ("obs", "unitycapture")
    assert CameraPlan(backend="obs").backends == ("obs",)
    assert CameraPlan(backend="unitycapture").backends == ("unitycapture",)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"width": 100},
        {"height": 100},
        {"fps": 0},
        {"fps": 120},
        {"quality": 5},
        {"facing": "sideways"},
        {"backend": "magic"},
    ],
)
def test_camera_refuses_impossible_settings(kwargs: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        CameraPlan(**kwargs)  # type: ignore[arg-type]


def test_front_camera_is_the_only_alternate_facing() -> None:
    assert CameraPlan(facing="front").label.startswith("front ")
    with pytest.raises(ConfigError):
        CameraPlan(facing="Front")


def test_microphone_plan_validates_buffer_and_gain() -> None:
    plan = MicPlan()
    assert plan.device == "CABLE Input"
    assert plan.latency_ms == 120

    for kwargs in (
        {"latency_ms": 1},
        {"latency_ms": 5000},
        {"gain": 0.0},
        {"gain": 9.0},
        {"device": ""},
        {"device": "   "},
    ):
        with pytest.raises(ConfigError):
            MicPlan(**kwargs)  # type: ignore[arg-type]


def test_speaker_plan_defaults_to_a_200_ms_buffer() -> None:
    plan = SpeakerPlan()
    assert plan.latency_ms == 200
    assert plan.lipsync_ms == 0
    assert plan.as_dict()["device"] == "default output (loopback)"


def test_speaker_lipsync_accepts_negative_offsets_and_rejects_nonsense() -> None:
    assert SpeakerPlan(lipsync_ms=-200).lipsync_ms == -200
    with pytest.raises(ConfigError):
        SpeakerPlan(lipsync_ms=5000)
    with pytest.raises(ConfigError):
        SpeakerPlan(latency_ms=0)


def test_a_speaker_plan_sends_audio_unless_it_is_told_to_be_muted() -> None:
    """The link defaults to muted; the command that exists to play audio must not."""
    assert SpeakerPlan().muted is False
    assert SpeakerPlan(muted=True).muted is True


def test_a_test_tone_plan_names_the_note_it_will_send() -> None:
    plan = SpeakerPlan(test_tone=True)

    assert plan.tone_hz == 440.0
    assert plan.tone_seconds == 3.0
    assert plan.as_dict()["test_tone"] == "440 Hz for 3.0 s"


def test_a_plain_speaker_plan_says_nothing_about_a_tone() -> None:
    assert "test_tone" not in SpeakerPlan().as_dict()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tone_hz": 5.0},
        {"tone_hz": 30_000.0},
        {"tone_seconds": 0.0},
        {"tone_seconds": 120.0},
    ],
)
def test_an_impossible_tone_is_refused_before_anything_is_opened(kwargs) -> None:
    with pytest.raises(ConfigError):
        SpeakerPlan(**kwargs)


def test_live_plan_reports_which_paths_are_active() -> None:
    plan = LivePlan(camera=CameraPlan(), mic=MicPlan(), speaker=SpeakerPlan())

    assert plan.names == ("camera", "mic", "speaker")
    assert plan.empty is False
    assert LivePlan().empty is True
    assert LivePlan(camera=CameraPlan()).names == ("camera",)
    assert LivePlan(mic=MicPlan()).as_dict()["camera"] is None
