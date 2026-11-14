"""The phone's frame path: YUV straight to JPEG, and numbers to prove it.

The measurement that started this: 5-8 fps against a 30 fps target. The phone was
asking CameraX for **RGBA_8888**, which means a native YUV->RGB conversion per
frame, a `Bitmap` allocation, a second `Bitmap` for the stride crop and
`Bitmap.compress(JPEG)` — an RGB->YCbCr conversion inside the encoder, for a
picture that was YCbCr when it arrived. It also decoded every frame it had just
encoded, 30 times a second, for the self-view thumbnail.

The YUV path that replaced it then produced **no frames at all** on one device
(Android 16 / HyperOS), so the structure now has three parts, and this module
pins all three: YUV is the default (`buildAnalysis`), the packing lives in a
pure-JVM object with its own JUnit test (`Nv21.kt`), and the old RGBA path still
exists — but only as the documented fallback (`test_android_camera_fallback.py`
pins when and how it takes over).

There is no phone or emulator in this environment, so these tests read the Kotlin
and pin the structure that produces the speed-up.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
ENGINE = SRC / "net" / "CameraEngine.kt"
PACKER = SRC / "net" / "Nv21.kt"
CARD = SRC / "CameraCard.kt"
PACKER_TEST = REPO / "android" / "app" / "src" / "test" / "java" / "dev" / "phonebridge" / "net" / "Nv21Test.kt"


def source() -> str:
    return ENGINE.read_text(encoding="utf-8")


def packer_source() -> str:
    return PACKER.read_text(encoding="utf-8")


def code_only(text: str) -> str:
    """Drop whole-line comments: they name the things the code no longer does."""
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
# the encoder
# --------------------------------------------------------------------------- #
def test_yuv_is_the_default_and_rgba_only_the_fallback() -> None:
    builder = body("private fun buildAnalysis(")

    assert 'private var activePath = Path.YUV' in source(), (
        "the fast path must be what a stream starts on"
    )
    assert "OUTPUT_IMAGE_FORMAT_YUV_420_888" in builder
    assert "Path.YUV -> ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888" in builder
    assert "Path.RGBA -> ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888" in builder, (
        "the fallback asks for RGBA explicitly instead of hoping"
    )


def test_rgba_appears_only_inside_the_fallback_encoder() -> None:
    text = code_only(source())
    start = text.index("private fun rgbaToJpeg(")
    rgba = text[start:]

    assert "Bitmap.createBitmap" in rgba
    assert "copyPixelsFromBuffer" in rgba
    assert "Bitmap.CompressFormat.JPEG" in rgba
    # …and nowhere else: the fast path never allocates a bitmap.
    without_fallback = text.replace(rgba, "")
    assert "Bitmap.createBitmap" not in without_fallback
    assert "copyPixelsFromBuffer" not in without_fallback


def test_the_fast_path_jpeg_comes_from_yuvimage_without_a_bitmap() -> None:
    fast = body("private fun yuvToJpeg(")

    assert "YuvImage(" in fast
    assert "compressToJpeg(" in fast
    assert "ImageFormat.NV21" in fast
    # Code only: the doc comment of the *next* function describes the fallback.
    assert "Bitmap" not in code_only(fast)
    assert "BitmapFactory" not in code_only(source())


def test_the_fast_path_reports_a_silent_encoder_failure() -> None:
    """`compressToJpeg` returning false used to drop the frame with no trace."""
    fast = body("private fun yuvToJpeg(")

    assert "if (!image.compressToJpeg(" in fast
    assert "throw IllegalStateException(" in fast, (
        "a refused frame must become an exception the caller can act on, "
        "not a null return that looks like a camera that never opened"
    )


# --------------------------------------------------------------------------- #
# the packing (Nv21.kt, and its JUnit test)
# --------------------------------------------------------------------------- #
def test_the_packing_is_a_pure_jvm_object_with_a_test() -> None:
    packer = packer_source()

    assert "object Nv21" in packer
    # No Android types: that is what lets `testDebugUnitTest` run it.
    assert "import android" not in packer
    assert "import java.nio.ByteBuffer" in packer
    assert PACKER_TEST.exists(), "the packing maths needs a test that runs in CI"
    junit = PACKER_TEST.read_text(encoding="utf-8")
    assert "rowStride" in junit, "row padding is one of the two real layouts"
    assert "semiPlanar" in junit, "interleaved chroma is the other"


def test_nv21_packing_handles_both_real_plane_layouts() -> None:
    """Semi-planar (stride 2) and planar (stride 1) are the only ones in the wild."""
    packer = packer_source()

    assert "u.pixelStride == 2 && v.pixelStride == 2" in packer, (
        "semi-planar devices: the V plane already holds the VU pairs"
    )
    assert "for (row in 0 until chromaHeight)" in packer, "row-by-row, honouring the stride"
    assert "pixelStride" in packer and "rowStride" in packer
    # The generic path reads each sample at its own stride, which is what makes a
    # device that reports something unusual still produce a picture.
    assert "v.buffer.get(vRow + (left + column) * v.pixelStride)" in packer
    assert "u.buffer.get(uRow + (left + column) * u.pixelStride)" in packer


def test_the_y_plane_is_copied_row_by_row() -> None:
    """A single `get()` of the whole plane would include the row padding."""
    packer = packer_source()

    assert "for (row in 0 until cropHeight)" in packer
    assert "(cropTop + row) * y.rowStride" in packer
    assert "view.get(out, write, cropWidth)" in packer


def test_the_crop_rectangle_is_honoured() -> None:
    packer = packer_source()

    assert "cropLeft" in packer and "cropTop" in packer
    assert "is outside" in packer, "a crop outside the frame is refused, not clipped silently"
    fast = body("private fun yuvToJpeg(")
    assert "proxy.cropRect" in fast
    assert "cropWidth = width" in fast and "cropTop = top" in fast


# --------------------------------------------------------------------------- #
# the size the phone was asked for
# --------------------------------------------------------------------------- #
def test_the_resolution_is_requested_with_a_resolution_strategy() -> None:
    builder = body("private fun buildAnalysis(")

    assert "ResolutionSelector" in builder
    assert "ResolutionStrategy(" in builder
    assert "FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER" in builder, (
        "a device must not answer a 640x480 request with 1920x1440"
    )
    assert "setTargetResolution(" not in code_only(source()), (
        "setTargetResolution is deprecated and treats the size as a hint"
    )


def test_the_rgba_fallback_scales_a_bigger_capture_down() -> None:
    rgba = body("private fun rgbaToJpeg(")

    assert "createScaledBitmap(" in rgba
    assert "cfg.width.toFloat() / full.width" in rgba


def test_the_frame_header_carries_the_encoded_size() -> None:
    """The header and the JPEG must agree, or the PC letterboxes against a lie."""
    analyse = body("private fun analyse(")

    assert "val frame = encode(proxy, cfg)" in analyse
    assert "width = frame.width" in analyse
    assert "height = frame.height" in analyse
    assert "width = proxy.width" not in analyse


# --------------------------------------------------------------------------- #
# the self-view
# --------------------------------------------------------------------------- #
def test_the_self_view_is_not_decoded_thirty_times_a_second() -> None:
    text = source()
    analyse = body("private fun analyse(")

    assert "PREVIEW_INTERVAL_MS" in text
    assert "now - lastPreviewAtMs >= PREVIEW_INTERVAL_MS" in analyse, (
        "the phone was decoding every frame it had just encoded, for a thumbnail"
    )
    assert "lastPreviewAtMs = now" in analyse


def test_the_self_view_still_decodes_off_the_composition_thread() -> None:
    card = CARD.read_text(encoding="utf-8")

    assert "produceState" in card, "decoding in composition would jank the screen"


# --------------------------------------------------------------------------- #
# the measurements
# --------------------------------------------------------------------------- #
def test_a_line_a_second_reports_capture_encode_and_send() -> None:
    reporter = body("private fun reportOnceASecond(")

    assert "Log.d(" in reporter, "Log.d, tag CameraEngine: adb logcat -s CameraEngine"
    for expected in (
        "capture",
        "${activePath.code}->jpeg",
        "q${cfg.quality}",
        "KB",
        "send",
        # The answer to 'why is a 640x480 frame 500 KB': what the camera delivered.
        "capture ${captureWidth}x$captureHeight",
    ):
        assert expected in reporter, f"{expected} belongs in the per-second line"


def test_every_frame_logs_its_own_size_and_encode_time() -> None:
    analyse = body("private fun analyse(")

    assert '"frame: ${frame.jpeg.size} B in' in analyse, (
        "a per-second average hides which frames were the slow ones"
    )


def test_encode_time_is_measured_around_the_jpeg_call_only() -> None:
    analyse = body("private fun analyse(")

    assert "encodeStarted = System.nanoTime()" in analyse
    assert "encodeFinished = System.nanoTime()" in analyse
    assert "encodeNanos.addAndGet(encodeFinished - encodeStarted)" in analyse


def test_send_time_is_measured_on_the_sender_thread() -> None:
    sender = body("private fun startSender(")

    assert "sendStarted = System.nanoTime()" in sender
    assert "sendNanos.addAndGet(took)" in sender


def test_the_numbers_are_on_the_wire_for_the_pcs_stats_flag() -> None:
    status = body("fun status()")

    for key in (
        '"stats" to mapOf(',
        '"fps"',
        '"encode_ms"',
        '"send_ms"',
        '"bytes_per_frame"',
        '"quality"',
        # Which encoder is live, and the size the camera really delivered.
        '"path" to activePath.code',
        '"capture_width"',
        '"capture_height"',
    ):
        assert key in status, f"{key} is what `phonebridge camera --stats` prints"


def test_the_quality_the_pc_asks_for_is_the_quality_used() -> None:
    """70-80 is the documented range: higher costs time on the phone, not detail."""
    fast = body("private fun yuvToJpeg(")
    rgba = body("private fun rgbaToJpeg(")

    assert "cfg.quality, out" in fast
    assert "cfg.quality, out" in rgba
    assert "quality = 100" not in source()
