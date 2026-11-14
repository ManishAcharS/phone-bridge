"""Camera frame rate: ask for 30, report what the device allows, measure the warm-up.

The Xiaomi delivered 20-24 fps at 640x480 for a 30 fps request, and a yuv->jpeg
encode of 7 ms proved the encoder was not the limit. It was auto-exposure: a
device in ordinary indoor light is allowed to stretch the exposure past one frame
period, and it will, because a brighter picture beats a faster one by default.

Three things fix that, and this module pins each of them:

* ``CONTROL_AE_TARGET_FPS_RANGE`` goes on the analysis use case through
  **Camera2Interop** — CameraX has no public switch for it;
* the ranges the device actually lists are read and **reported**, so a refusal is
  a number in ``--stats`` instead of a mystery;
* ``fixed_fps`` is a *choice* (``--fps-mode fixed|variable``) because pinning the
  rate is exactly what makes a dim room darker.

It also pins the warm-up measurement: the first ~6 s of frames after
``camera.start`` were 5 KB JPEGs (near-blank), which is auto-exposure and
auto-white-balance converging. The phone now times that and logs it, so the delay
has a number instead of a reputation.
"""

from __future__ import annotations

import re
from pathlib import Path

from phonebridge.camera.frames import VideoConfig
from phonebridge.live.config import CameraPlan
from phonebridge.live.pump import stats_lines

REPO = Path(__file__).resolve().parents[2]
ENGINE = (
    REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
    / "net" / "CameraEngine.kt"
)
FRAMES = (
    REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
    / "net" / "VideoFrames.kt"
)


def source() -> str:
    return ENGINE.read_text(encoding="utf-8")


def code_only(text: str) -> str:
    """Drop whole-line comments: a comment must never satisfy a source guard."""
    return "\n".join(
        line
        for line in text.splitlines()
        if not line.lstrip().startswith(("*", "/**", "//"))
    )


def function(name: str) -> str:
    """The body of ``name`` up to the next function declaration."""
    text = source()
    start = text.index(name)
    rest = text[start + len(name):]
    ends = [
        rest.index(marker)
        for marker in ("\n    fun ", "\n    private fun ", "\n    internal fun ")
        if marker in rest
    ]
    return rest[: min(ends)] if ends else rest


# --------------------------------------------------------------------------- #
# the phone sets the range through Camera2Interop
# --------------------------------------------------------------------------- #
def test_the_analysis_use_case_sets_an_ae_target_fps_range() -> None:
    body = code_only(function("private fun applyFpsRange"))
    assert "Camera2Interop.Extender(" in body
    assert "CONTROL_AE_TARGET_FPS_RANGE" in body
    assert "setCaptureRequestOption" in body


def test_the_interop_marker_is_opted_into_where_it_is_used() -> None:
    """Without the opt-in this is a Kotlin compile error, not a runtime one."""
    text = source()
    assert "import androidx.camera.camera2.interop.Camera2Interop" in text
    assert "import androidx.camera.camera2.interop.ExperimentalCamera2Interop" in text
    assert text.count("@OptIn(ExperimentalCamera2Interop::class)") >= 2


def test_the_use_case_is_built_after_the_range_is_chosen() -> None:
    """The range is a builder option: choosing it afterwards would be too late."""
    text = code_only(source())
    choose = text.index("chooseFpsRange(camProvider, requested)")
    build = text.index("val analysisUseCase = buildAnalysis(requested)")
    assert choose < build


def test_a_fixed_rate_picks_an_exact_range_and_a_variable_one_a_wide_range() -> None:
    body = code_only(function("private fun chooseFpsRange"))
    # exact (fps, fps) first, then the narrowest range ending at fps ...
    assert "it.lower == target && it.upper == target" in body
    assert "candidates.minByOrNull { it.upper - it.lower }" in body
    # ... and the widest one when the user asked for a brighter picture instead.
    assert "candidates.maxByOrNull { it.upper - it.lower }" in body


def test_the_supported_ranges_come_from_the_camera_characteristics() -> None:
    body = code_only(function("private fun availableFpsRanges"))
    assert "Camera2CameraInfo.from(" in body
    assert "CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES" in body
    # The lens facing decides which camera's list is read, not whichever is first.
    assert "LENS_FACING_BACK" in body and "LENS_FACING_FRONT" in body


def test_a_refused_option_does_not_cost_the_picture() -> None:
    body = code_only(function("private fun applyFpsRange"))
    assert "runCatching" in body
    assert "onFailure" in body
    # The camera keeps streaming: the range is dropped, not the stream.
    assert "fpsRange = null" in body


# --------------------------------------------------------------------------- #
# what the phone reports
# --------------------------------------------------------------------------- #
def test_the_stats_report_the_range_asked_for_and_the_ranges_supported() -> None:
    body = code_only(function("fun status()"))
    for key in (
        '"fps_range"',
        '"fps_ranges_supported"',
        '"fps_fixed"',
        '"warmup_ms"',
        '"first_frame_bytes"',
        '"peak_frame_bytes"',
    ):
        assert key in body, f"{key} is missing from audio/camera status"


def test_the_warm_up_is_measured_not_guessed() -> None:
    body = code_only(function("private fun trackWarmup"))
    assert "WARMUP_BYTES" in body
    assert "peakFrameBytes * 3 / 5" in body, "a frame is 'normal' relative to the peak"
    assert "warmupMs = now - analysisStartedAtMs" in body


def test_every_encoded_frame_is_timed_even_when_it_is_dropped_later() -> None:
    """A near-blank frame still counts: it is the *picture* being measured."""
    body = code_only(function("private fun analyse"))
    assert "trackWarmup(frame.jpeg.size)" in body


def test_the_warm_up_constants_survive_a_restart() -> None:
    body = code_only(function("private fun bindOnMainThread"))
    for reset in ("warmupMs = -1", "firstFrameBytes = 0", "peakFrameBytes = 0"):
        assert reset in body


def test_the_bind_log_says_the_range_and_what_the_device_supports() -> None:
    body = code_only(function("private fun bindOnMainThread"))
    assert "AE target fps" in body
    assert "fpsRangesSupported" in body


# --------------------------------------------------------------------------- #
# the request on the wire
# --------------------------------------------------------------------------- #
def test_the_config_carries_the_choice_over_the_wire() -> None:
    config = VideoConfig(fps=30, fixed_fps=True)
    assert config.as_dict()["fixed_fps"] is True
    assert VideoConfig.from_dict(config.as_dict()) == config
    assert VideoConfig.from_dict({"fps": 30}).fixed_fps is True, "the default pinned the rate"


def test_a_phone_that_sends_no_flag_keeps_the_pc_choice() -> None:
    """`from_dict` falls back to the *caller's* value, not to a hard-coded default."""
    fallback = VideoConfig(fps=30, fixed_fps=False)
    assert VideoConfig.from_dict({"fps": 30}, fallback=fallback).fixed_fps is False


def test_switching_lenses_keeps_the_frame_rate_choice() -> None:
    stream = (
        REPO / "windows" / "phonebridge" / "camera" / "stream.py"
    ).read_text(encoding="utf-8")
    start = stream.index("async def switch(")
    body = stream[start : start + 900]
    assert "fixed_fps=self._config.fixed_fps" in body


def test_the_plan_default_is_fixed_and_the_flag_can_turn_it_off() -> None:
    assert CameraPlan().fixed_fps is True
    assert CameraPlan(fixed_fps=False).as_dict()["fixed_fps"] is False


# --------------------------------------------------------------------------- #
# the --stats line
# --------------------------------------------------------------------------- #
def test_the_stats_line_shows_the_ae_target_and_the_device_ranges() -> None:
    lines = stats_lines(
        None,
        {
            "fps": 29.6,
            "fps_range": "30-30",
            "fps_ranges_supported": "30-30,15-30,7-30",
            "fps_fixed": True,
            "warmup_ms": 2400,
            "first_frame_bytes": 5200,
        },
    )
    text = "\n".join(lines)
    assert "AE target 30-30" in text
    assert "device 30-30,15-30,7-30" in text
    assert "warm-up 2400 ms" in text
    assert "first frame 5200 B" in text


def test_a_stream_that_has_not_converged_says_so() -> None:
    lines = stats_lines(None, {"fps": 20.0, "warmup_ms": -1})
    assert "warm-up not converged yet" in "\n".join(lines)


def test_a_phone_without_these_keys_prints_no_extra_line() -> None:
    assert len(stats_lines(None, {"fps": 20.0, "encode_ms": 7.0})) == 1


def test_a_variable_rate_is_labelled_as_such() -> None:
    lines = stats_lines(None, {"fps": 22.0, "fps_range": "15-30", "fps_fixed": False})
    text = "\n".join(lines)
    assert "AE target 15-30" in text
    assert "variable" in text


# --------------------------------------------------------------------------- #
# the Kotlin source stays parseable
# --------------------------------------------------------------------------- #
def test_the_kotlin_braces_balance() -> None:
    from kotlin_scan import mask_source

    for path in (ENGINE, FRAMES):
        masked = mask_source(path.read_text(encoding="utf-8"))
        assert masked.count("{") == masked.count("}"), path.name
        assert masked.count("(") == masked.count(")"), path.name


def test_no_cyrillic_or_smart_quotes_sneaked_into_the_kotlin() -> None:
    """A stray homoglyph compiles on no compiler and looks identical in a diff."""
    for path in (ENGINE, FRAMES):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"[\u0400-\u04FF\u2018\u2019\u201C\u201D]", text), path.name
