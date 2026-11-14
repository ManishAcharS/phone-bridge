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


def test_live_plan_reports_which_paths_are_active() -> None:
    plan = LivePlan(camera=CameraPlan(), mic=MicPlan(), speaker=SpeakerPlan())

    assert plan.names == ("camera", "mic", "speaker")
    assert plan.empty is False
    assert LivePlan().empty is True
    assert LivePlan(camera=CameraPlan()).names == ("camera",)
    assert LivePlan(mic=MicPlan()).as_dict()["camera"] is None
