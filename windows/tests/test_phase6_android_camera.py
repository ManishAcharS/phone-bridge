"""Phase 6, first step: the phone's rotation metadata is deterministic.

The window can only draw the picture upright if the number it is given means
something. `ImageAnalysis`'s `rotationDegrees` is relative to the use case's
*target rotation*, and a use case that is not attached to a window defaults that
to zero — which describes the phone's natural orientation, not the way it is
being held. A phone held in landscape therefore reported a rotation that was
90 degrees out, and the picture came out sideways in the window.

This file pins the two halves that can be checked without hardware or a
compiler: the Kotlin asks for the display rotation, and the *wire* still carries
the rotation in the frame header (no protocol change, no hardcoded fudge factor).
"""

from __future__ import annotations

from pathlib import Path

from phonebridge.camera.frames import VALID_ROTATIONS

REPO = Path(__file__).resolve().parents[2]
CAMERA_KT = (
    REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
    / "net" / "CameraEngine.kt"
)


def source() -> str:
    return CAMERA_KT.read_text(encoding="utf-8")


def test_the_target_rotation_comes_from_the_display() -> None:
    text = source()
    # The use case is built by `buildAnalysis` now (one builder, used by the
    # first bind and by the encoder fallback's rebind), so the rotation is set
    # there - still from the display, still on the use case itself.
    builder = text[text.index("private fun buildAnalysis("):]
    builder = builder[: builder.index("\n    private fun ")]
    assert "body.targetRotation = displayRotation()" in builder
    helper = text[text.index("private fun displayRotation("):]
    helper = helper[: helper.index("\n    }")]
    assert "DisplayManager" in helper, "a service context has no display of its own"
    assert "Surface.ROTATION_0" in helper, "an unknown rotation must be the safe default"


def test_the_frame_header_still_carries_the_rotation() -> None:
    """No fudge factor: the phone reports, the receiver turns. That is the contract."""
    text = source()
    assert "rotation = proxy.imageInfo.rotationDegrees" in text
    assert "rotation = 90" not in text and "rotation = 270" not in text, (
        "a hardcoded offset would correct one lens and break the other"
    )


def test_the_wire_format_accepts_the_same_four_angles() -> None:
    assert VALID_ROTATIONS == (0, 90, 180, 270)
