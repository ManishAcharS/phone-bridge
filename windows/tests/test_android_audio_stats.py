"""The phone's own playback numbers, pinned in source.

There is no Android device in CI, so the Kotlin below cannot be run — but it can
be read, and the two things that matter here are both readable:

* `AudioEngine` must *ask the AudioTrack* rather than report its own opinion
  (``playState``, ``underrunCount``, ``routedDevice``, ``playbackHeadPosition``);
* `BridgeServer` must put those values on the wire, key for key, in the
  ``audio.status`` result — the protocol contract test checks the same keys from
  the docs' side, so a rename on either side fails one of the two.

A status field that the phone computes but never sends (or sends but never
computes) is worse than no field: the PC's line would say ``route none`` on a
phone that is playing perfectly.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AUDIO_ENGINE = REPO / "android/app/src/main/java/dev/phonebridge/net/AudioEngine.kt"
BRIDGE_SERVER = REPO / "android/app/src/main/java/dev/phonebridge/net/BridgeServer.kt"

#: What `audio.status` gained so the PC can tell "playing" from "accepted".
PLAYBACK_KEYS = (
    "route",
    "device_name",
    "track_state",
    "buffered_ms",
    "buffer_ms",
    "underruns",
    "played_ms",
    "frames_written",
)


def read(path: Path) -> str:
    return path.read_text("utf-8")


def code_only(path: Path) -> str:
    """The file with comments stripped: a docstring is not an implementation."""
    lines = []
    for line in read(path).splitlines():
        stripped = line.lstrip()
        if stripped.startswith("*") or stripped.startswith("//") or stripped.startswith("/*"):
            continue
        lines.append(line)
    return "\n".join(lines)


def function_body(source: str, signature: str) -> str:
    """The brace-balanced body after ``signature``."""
    start = source.index(signature)
    open_brace = source.index("{", start)
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace : index + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


# --------------------------------------------------------------------------- #
# the engine reads the device, it does not guess
# --------------------------------------------------------------------------- #
def test_the_engine_asks_the_audiotrack_for_its_state() -> None:
    source = code_only(AUDIO_ENGINE)

    assert "playState" in source and "PLAYSTATE_PLAYING" in source, (
        "PLAYSTATE_PLAYING vs PAUSED is the device's answer"
    )
    assert "track.state" in source, "STATE_INITIALIZED: the track can be built and dead"
    assert "underrunCount" in source, "how often the device ran dry"
    assert "bufferSizeInFrames" in source, "the buffer in front of the device"
    assert "playbackHeadPosition" in source, (
        "frames actually played — the only proof the device consumed anything"
    )


def test_the_play_position_is_read_as_unsigned() -> None:
    """`playbackHeadPosition` is a u32 that wraps after ~24 h at 48 kHz."""
    source = code_only(AUDIO_ENGINE)

    assert "0xFFFFFFFFL" in source


def test_the_output_route_is_translated_into_words() -> None:
    source = code_only(AUDIO_ENGINE)

    assert "AudioDeviceInfo.TYPE_BUILTIN_SPEAKER" in source
    assert "AudioDeviceInfo.TYPE_BLUETOOTH_A2DP" in source, "the headphones case"
    assert "AudioDeviceInfo.TYPE_BLUETOOTH_SCO" in source, "the call-mode case"
    assert "routedDevice" in source
    assert "productName" in source, "the device's own name, for the user's benefit"


def test_a_released_track_cannot_make_a_status_read_throw() -> None:
    source = code_only(AUDIO_ENGINE)

    assert "fun routedDevice(" in source
    assert re.search(r"catch \(e: Exception\) \{\s*null", source), (
        "routedDevice throws on a released track, and a status line must not throw"
    )


def test_every_playback_key_is_computed() -> None:
    body = function_body(code_only(AUDIO_ENGINE), "fun playbackSnapshotLocked(")

    for key in PLAYBACK_KEYS:
        assert f'"{key}" to' in body, f"AudioEngine does not report {key}"


def test_the_counters_are_reset_when_a_track_starts() -> None:
    body = function_body(code_only(AUDIO_ENGINE), "fun startPlayback(")

    assert "framesWritten.set(0)" in body, "a restart must not inherit the last run's count"


def test_the_track_reports_what_it_accepted() -> None:
    source = code_only(AUDIO_ENGINE)

    assert "framesWritten.incrementAndGet()" in source, (
        "counted only when write() returns > 0: a rejected write is not a played frame"
    )


# --------------------------------------------------------------------------- #
# the server puts them on the wire
# --------------------------------------------------------------------------- #
def test_audio_status_sends_every_playback_key() -> None:
    branch = function_body(code_only(BRIDGE_SERVER), '"audio.status" ->')

    for key in PLAYBACK_KEYS:
        assert f'"{key}" to s["{key}"]' in branch, f"BridgeServer drops {key}"


def test_the_old_keys_are_still_there() -> None:
    """Additive only: an older PC must keep working against a newer phone."""
    branch = function_body(code_only(BRIDGE_SERVER), '"audio.status" ->')

    for key in ("playback", "mic", "volume", "muted", "permitted", "format", "frames_in", "dropped"):
        assert f'"{key}"' in branch, f"the field {key} was removed"
