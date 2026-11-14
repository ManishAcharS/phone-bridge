"""Playback latency on the phone: a shallow queue, the fast path, and a probe.

The delay to Bluetooth headphones was about half a second, and the queue alone was
160 ms of it: eight frames of 20 ms, fixed, whether or not the device needed them.
This module pins the replacement design:

* the queue target comes from the PC (``target_ms``), is clamped to 1..8 frames,
  and is **adaptive** — it grows only on evidence (the device's underrun counter
  went up, or the writer found the queue empty while audio was flowing) and
  shrinks only in the absence of it;
* the writer still drops the **oldest** frame, which is the only way a full queue
  costs a click instead of permanent delay;
* ``AudioTrack`` is asked for ``PERFORMANCE_MODE_LOW_LATENCY`` with a buffer of a
  couple of frames instead of four, guarded because vendor ROMs refuse it;
* a **probe** times one frame from arrival to the mixer, and reports ``-1`` until
  it has a measurement rather than pretending zero;
* the A2DP codec is reported as empty **with the reason**, because Android does
  not expose it to an app — an empty string alone would read as "no Bluetooth".

The Kotlin app cannot be compiled here (no JDK or Android SDK in this sandbox), so
these are source guards over the real file plus the compiler run in CI. They are
deliberately behavioural: every assertion names something that would make the
delay come back.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENGINE = (
    REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
    / "net" / "AudioEngine.kt"
)


def source() -> str:
    return ENGINE.read_text(encoding="utf-8")


def code_only(text: str) -> str:
    return "\n".join(
        line
        for line in text.splitlines()
        if not line.lstrip().startswith(("*", "/**", "//"))
    )


def function(name: str) -> str:
    text = code_only(source())
    start = text.index(name)
    rest = text[start + len(name):]
    ends = [
        rest.index(marker)
        for marker in ("\n    fun ", "\n    private fun ", "\n    internal fun ")
        if marker in rest
    ]
    return rest[: min(ends)] if ends else rest


# --------------------------------------------------------------------------- #
# the queue is shallow, bounded and adaptive
# --------------------------------------------------------------------------- #
def test_the_queue_target_comes_from_the_pc_and_is_clamped() -> None:
    body = function("fun startPlayback")
    assert 'intParam(params, "target_ms"' in body
    assert ".coerceIn(minFrames, maxFrames)" in body
    assert "minFrames = MIN_QUEUE_FRAMES" in body
    assert "maxFrames = QUEUE_FRAMES" in body


def test_the_default_target_is_three_frames_not_eight() -> None:
    text = source()
    assert "const val DEFAULT_TARGET_MS = 60" in text
    assert "const val QUEUE_FRAMES = 8" in text, "the ceiling is unchanged"
    assert "const val MIN_QUEUE_FRAMES = 1" in text


def test_the_track_asks_for_the_low_latency_path() -> None:
    body = function("fun startPlayback")
    assert "PERFORMANCE_MODE_LOW_LATENCY" in body
    # Guarded: a vendor ROM that refuses must not cost the audio.
    assert "runCatching { builder.setPerformanceMode" in body


def test_the_track_buffer_is_a_couple_of_frames_and_never_below_the_minimum() -> None:
    body = function("fun startPlayback")
    assert "maxOf(minBuffer, bytesPerFrame *" in body
    assert "bytesPerFrame * 4" not in body, "four frames of track buffer is the old delay"


def test_an_adaptive_target_grows_on_underruns_and_shrinks_only_on_quiet() -> None:
    body = function("private fun adapt")
    assert "track.underrunCount" in body
    assert "targetFrames++" in body and "targetFrames--" in body
    assert "fresh > 0 || wasEmpty" in body
    # Silence is not pressure: nothing written and nothing underrun teaches nothing.
    assert "if (writes == 0 && fresh == 0) return" in body
    assert "if (!adaptive) return" in body


def test_the_writer_still_drops_the_oldest_frame() -> None:
    body = function("fun onFrame")
    assert "while (writeQueue.size >= targetFrames)" in body
    assert "writeQueue.removeFirst()" in body
    assert "droppedFrames++" in body


def test_the_writer_sleeps_for_less_than_a_frame_when_idle() -> None:
    text = source()
    assert "IDLE_SLEEP_MS = 2L" in text
    body = function("private fun writeLoop")
    assert "Thread.sleep(IDLE_SLEEP_MS)" in body
    assert "Thread.sleep(5)" not in body


# --------------------------------------------------------------------------- #
# the probe
# --------------------------------------------------------------------------- #
def test_the_probe_times_arrival_to_the_play_head() -> None:
    body = function("private fun updateProbeLocked")
    assert "playbackHeadPosition" in body
    assert "probeHeadTarget" in body
    assert "probeLatencyMs" in body


def test_the_probe_is_armed_by_the_pc_and_cleared_by_a_stop() -> None:
    start = function("fun startPlayback")
    assert 'boolParam(params, "probe"' in start
    assert 'probeState = if (wantProbe) "armed" else "idle"' in start
    stop = function("fun stopPlayback")
    assert "probeArmed = false" in stop
    assert 'probeState = "idle"' in stop


def test_the_probe_takes_the_first_frame_it_sees() -> None:
    body = function("fun onFrame")
    assert "if (probeArmed && probeArrivedAtMs == 0L)" in body


def test_an_unmeasured_probe_reports_minus_one() -> None:
    """Zero would look like the best possible result; -1 says "unknown"."""
    start = function("fun startPlayback")
    assert "probeLatencyMs = -1" in start


# --------------------------------------------------------------------------- #
# what the phone reports back
# --------------------------------------------------------------------------- #
def test_the_status_reports_every_latency_stage() -> None:
    body = function("private fun playbackSnapshotLocked")
    for key in (
        '"queue_target_frames"',
        '"queue_target_ms"',
        '"queue_depth"',
        '"adaptive"',
        '"adapt_grow"',
        '"adapt_shrink"',
        '"low_latency"',
        '"track_buffer_ms"',
        '"probe_state"',
        '"probe_latency_ms"',
        '"a2dp_codec"',
        '"a2dp_codec_note"',
    ):
        assert key in body, f"{key} is missing from the playback status"


def test_the_bluetooth_codec_is_never_invented() -> None:
    body = function("private fun a2dpCodec")
    # It may come back empty, but only with a reason next to it.
    assert "codec not exposed to apps on this Android version" in body
    assert "no bluetooth a2dp route" in body
    assert "runCatching" in body, "a refusal must not crash audio"


def test_the_start_line_states_the_budget_it_settled_on() -> None:
    body = function("fun startPlayback")
    assert "queue target" in body
    assert "track buffer" in body
    assert "low latency" in body


# --------------------------------------------------------------------------- #
# the Kotlin source stays parseable and honest
# --------------------------------------------------------------------------- #
def test_the_braces_and_parens_balance() -> None:
    from kotlin_scan import mask_source

    masked = mask_source(source())
    assert masked.count("{") == masked.count("}")
    assert masked.count("(") == masked.count(")")


def test_no_homoglyphs_or_smart_quotes_sneaked_in() -> None:
    text = source()
    assert not re.search(r"[\u0400-\u04FF\u2018\u2019\u201C\u201D]", text)
