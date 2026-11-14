"""The phone's frame path: YUV straight to JPEG, and numbers to prove it.

The measurement that started this: 5-8 fps against a 30 fps target. The phone was
asking CameraX for **RGBA_8888**, which means a native YUV->RGB conversion per
frame, a `Bitmap` allocation, a second `Bitmap` for the stride crop and
`Bitmap.compress(JPEG)` — an RGB->YCbCr conversion inside the encoder, for a
picture that was YCbCr when it arrived. It also decoded every frame it had just
encoded, 30 times a second, for the self-view thumbnail.

There is no phone or emulator in this environment, so — like
`test_android_camera_threading.py` — these tests read the Kotlin and pin the
structure that produces the speed-up.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENGINE = (
    REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
    / "net" / "CameraEngine.kt"
)
CARD = REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge" / "CameraCard.kt"


def source() -> str:
    return ENGINE.read_text(encoding="utf-8")


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
def test_the_analysis_use_case_asks_for_yuv_not_rgba() -> None:
    text = source()

    assert "OUTPUT_IMAGE_FORMAT_YUV_420_888" in text
    assert "OUTPUT_IMAGE_FORMAT_RGBA_8888" not in text, (
        "RGBA costs a native conversion plus two Bitmap copies per frame"
    )


def test_the_jpeg_comes_from_yuvimage_without_a_bitmap() -> None:
    text = source()
    # Code only: the comments explain what was removed and name it.
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("*", "//")))

    assert "YuvImage(" in code
    assert "compressToJpeg(" in code
    assert "ImageFormat.NV21" in code
    assert "Bitmap.createBitmap" not in code, "no bitmap round trip in the frame path"
    assert "Bitmap.compress" not in code
    assert "copyPixelsFromBuffer" not in code
    assert "BitmapFactory" not in code


def test_nv21_packing_handles_both_real_plane_layouts() -> None:
    """Semi-planar (stride 2) and planar (stride 1) are the only ones in the wild."""
    packer = body("private fun ImageProxy.toNv21()")

    assert "pixelStride == 2" in packer, "semi-planar devices: copy the VU rows whole"
    assert "pixelStride == 1" in packer, "planar devices: interleave V and U"
    assert "rowStride" in packer, "row padding must be honoured or the picture skews"
    assert "unsupported camera plane layout" in packer, "an unknown stride says so"
    # Bulk copies, not a per-pixel loop over the Y plane.
    assert "yBuffer.get(nv21, row * width, width)" in packer


def test_the_y_plane_is_copied_row_by_row() -> None:
    """A single `get()` of the whole plane would include the row padding."""
    packer = body("private fun ImageProxy.toNv21()")

    assert "for (row in 0 until height)" in packer
    assert "row * y.rowStride" in packer


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
    for expected in ("capture", "yuv->jpeg", "q${cfg.quality}", "KB", "send"):
        assert expected in reporter, f"{expected} belongs in the per-second line"


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
    ):
        assert key in status, f"{key} is what `phonebridge camera --stats` prints"


def test_the_quality_the_pc_asks_for_is_the_quality_used() -> None:
    """70-80 is the documented range: higher costs time on the phone, not detail."""
    analyse = body("private fun analyse(")

    assert "proxy.toJpeg(cfg.quality)" in analyse
    assert "quality = 100" not in source()
