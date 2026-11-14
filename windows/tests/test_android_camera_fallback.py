"""The camera must never go quiet: two encoder paths, a watchdog, and a log trail.

Reported on a Xiaomi 24115RA8EI (Android 16 / HyperOS): ``camera.start`` was
accepted, the phone showed "waiting for the first frame…" and the PC received
**nothing** for as long as anyone waited. The change that introduced it replaced
the RGBA encoder with a YUV one, and a fast path that produces nothing has no
second chance unless something is watching it.

So there are now three guarantees, and this module pins each:

* a **2 s watchdog** from the bind, which switches to the RGBA path if a single
  frame has not been encoded;
* a **fallback** on any encoder failure, rebinding the same camera on the other
  format instead of dropping frames into a log;
* a **trail of five log lines** under one tag, so
  ``adb logcat -s CameraEngine`` says exactly where a stream stops.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENGINE = (
    REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
    / "net" / "CameraEngine.kt"
)


def source() -> str:
    return ENGINE.read_text(encoding="utf-8")


def code_only(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(("*", "/**", "//"))
    )


def body(function: str) -> str:
    text = source()
    start = text.index(function)
    rest = text[start + len(function):]
    ends = [
        rest.index(marker)
        for marker in ("\n    fun ", "\n    private fun ", "\n    internal fun ")
        if marker in rest
    ]
    return rest[: min(ends)] if ends else rest


# --------------------------------------------------------------------------- #
# the log trail
# --------------------------------------------------------------------------- #
def test_one_logcat_tag_shows_every_stage_of_a_stream() -> None:
    text = source()

    # In order: the request, the bind, a frame at the analyzer, a JPEG, a socket
    # write. A stream that reaches the PC passes all five.
    trail = [
        "camera.start received:",
        "camera bound:",
        "first analyzer frame:",
        "first JPEG encoded:",
        "first frame sent:",
    ]
    for marker in trail:
        assert f'"{marker}' in text, f"{marker} must be a Log.i line under the tag"

    # …and they are all Log.i under one tag, which is what `-s CameraEngine` needs.
    assert 'private const val TAG = "CameraEngine"' in text
    for marker in trail:
        window = text[max(0, text.index(f'"{marker}') - 120): text.index(f'"{marker}')]
        assert "Log.i(" in window, f"{marker} must be Log.i, not a quiet Log.d"


def test_every_encoder_failure_keeps_its_stack_in_logcat() -> None:
    analyse = body("private fun analyse(")

    assert 'Log.e(TAG, "camera encode failed on the ${activePath.code} path' in analyse


# --------------------------------------------------------------------------- #
# the proxy
# --------------------------------------------------------------------------- #
def test_the_image_proxy_is_closed_on_every_path() -> None:
    analyse = body("private fun analyse(")

    assert analyse.count("proxy.close()") == 1, "one close, in the finally block"
    assert "} finally {" in analyse
    # The close is in the `finally`, not in the body: a `return` on the throttle
    # or an exception must still hand the frame back or KEEP_ONLY_LATEST stalls.
    head, _, tail = analyse.partition("} finally {")
    assert "proxy.close()" not in head
    assert "proxy.close()" in tail


# --------------------------------------------------------------------------- #
# the watchdog
# --------------------------------------------------------------------------- #
def test_two_seconds_without_a_frame_switches_encoder() -> None:
    text = source()

    assert "private const val FALLBACK_AFTER_MS = 2_000L" in text
    bind = body("private fun bindOnMainThread(")
    assert "mainHandler.postDelayed(fallbackWatchdog, FALLBACK_AFTER_MS)" in bind, (
        "the watchdog has to be armed by the bind, not by a frame"
    )
    watchdog = body("private val fallbackWatchdog = Runnable {")
    assert "if (encoded.get() > encodedAtStart) return@Runnable" in watchdog, (
        "a stream that produced one frame is not stuck"
    )
    assert "fallBack(" in watchdog


def test_the_watchdog_is_cancelled_when_the_camera_closes() -> None:
    stop = body("private fun stopLocked(")

    assert "mainHandler.removeCallbacks(fallbackWatchdog)" in stop, (
        "a pending switch must not rebind a camera the user just closed"
    )


def test_a_watchdog_that_also_fails_says_so_once() -> None:
    watchdog = body("private val fallbackWatchdog = Runnable {")

    assert 'if (activePath == Path.RGBA)' in watchdog
    assert "logFailure(" in watchdog, (
        "both paths silent is a sentence the user can act on, not more silence"
    )


# --------------------------------------------------------------------------- #
# the fallback
# --------------------------------------------------------------------------- #
def test_a_fallback_flips_the_path_and_rebinds_on_the_main_thread() -> None:
    fall = body("private fun fallBack(")

    assert "yuvBroken = true" in fall
    assert "activePath = Path.RGBA" in fall
    assert 'Log.i(TAG, "camera falling back to the RGBA encoder: $reason")' in fall
    # The bind is a main-thread operation, so the task is posted (or run inline
    # when the watchdog already is on the main thread).
    assert "mainHandler.post(task)" in fall
    assert "Looper.myLooper() === Looper.getMainLooper()" in fall
    assert "bindToLifecycle(" in fall, "the camera has to be rebound on the new format"
    assert "buildAnalysis(config)" in fall, "…using the builder, which reads activePath"


def test_the_yuv_path_is_switched_off_after_it_fails() -> None:
    """Without this, every frame after the first failure throws again, 30/s."""
    encode = body("private fun encode(")

    assert "if (yuvBroken)" in encode
    assert "return null" in encode
    assert 'notePath(Path.RGBA, "the device delivered RGBA_8888")' in encode, (
        "the device has the last word on the output format"
    )


def test_every_way_the_fast_path_can_fail_reaches_the_fallback() -> None:
    analyse = body("private fun analyse(")
    first_frame = body("private fun logFirstFrame(")

    assert 'fallBack("the ${activePath.code} encoder failed:' in analyse, (
        "an exception in the encoder"
    )
    assert 'fallBack(\n                "CameraX delivered' in first_frame, (
        "a capture much larger than the request (the 400-590 KB frames)"
    )


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #
def test_the_active_path_is_in_camera_status() -> None:
    status = body("fun status()")

    assert '"path" to activePath.code' in status
    assert '"path_reason" to pathReason' in status
    assert '"capture_width" to captureWidth' in status
    assert '"capture_layout" to lastCaptureLayout' in status


def test_the_first_frame_log_names_the_plane_layout() -> None:
    first_frame = body("private fun logFirstFrame(")

    # The numbers that answer "why no frames": size, format, strides per plane,
    # crop, rotation, and what was requested.
    for expected in (
        "proxy.width",
        "formatName(proxy.format)",
        "plane$index(row=",
        "proxy.cropRect",
        "rotation=",
        "requested ${cfg.width}x${cfg.height}",
    ):
        assert expected in first_frame, f"{expected} belongs in the first-frame line"


def test_the_path_is_sticky_so_a_restart_does_not_rediscover_the_bug() -> None:
    text = code_only(source())

    # `activePath` and `yuvBroken` are fields that `bindOnMainThread` does not
    # reset, so a reconnect starts on the encoder that is known to work.
    bind = body("private fun bindOnMainThread(")
    assert "activePath =" not in bind
    assert "yuvBroken =" not in bind
    assert "activePath" in text
