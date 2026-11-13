"""The preview's geometry: rotation, mirroring and fitting — without a toolkit.

These are the rules that decide whether a picture is upright, which way round it
faces, and how big it is drawn. They live in `camera/preview.py` as plain
functions precisely so they can be tested here: `tkinter` needs a display, and
this environment has none, so a rule buried inside a widget would be a rule
nobody can check.

What is *not* proven here: that the phone's CameraX reports the rotation these
tests assume. `tests/test_phase51_android_contract.py` pins the Kotlin side
(`setTargetRotation` from the display rotation), and `docs/20` §9 is the
handset checklist. The arithmetic between "the phone said 90" and "the pixel at
the top-left is the one from the top-right" is what is proven.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL", reason="the preview needs Pillow; these rules are Pillow rules")

from PIL import Image  # noqa: E402

from phonebridge.camera.frames import VideoFrame  # noqa: E402
from phonebridge.camera.preview import (  # noqa: E402
    Preview,
    PreviewUnavailable,
    decode,
    fit_size,
    matching_scale,
    mirrored_for_display,
    render,
)

RED = (255, 0, 0)
GREEN = (0, 255, 0)


def corner_image(width: int = 4, height: int = 2) -> Image.Image:
    """An image whose four quadrants are distinct, so a flip is visible."""
    image = Image.new("RGB", (width, height), (30, 30, 30))
    image.putpixel((0, 0), RED)  # only the top-left corner is red
    image.putpixel((width - 1, 0), GREEN)  # only the top-right is green
    return image


# --------------------------------------------------------------------------- #
# rotation
# --------------------------------------------------------------------------- #
def test_the_phone_says_clockwise_and_the_preview_turns_clockwise() -> None:
    """90 must move the top-left corner to the top-right, not the bottom-left.

    CameraX's `rotationDegrees` is "rotate the picture this far clockwise to make
    it upright". Pillow turns counter-clockwise for a positive angle, so this is
    the one place the sign is inverted; inverting it twice is what made the rear
    lens look rotated left and the front look rotated right.
    """
    image = corner_image(4, 2)
    turned = Preview(image=image, width=4, height=2, rotation=90).rotated()
    assert turned.size == (2, 4), "a 90 degree turn must swap width and height"
    assert turned.getpixel((1, 0)) == RED, "the top-left corner belongs top-right now"
    assert turned.getpixel((0, 0)) != RED


def test_180_puts_the_corner_opposite_and_keeps_the_shape() -> None:
    turned = Preview(image=corner_image(4, 2), width=4, height=2, rotation=180).rotated()
    assert turned.size == (4, 2)
    assert turned.getpixel((3, 1)) == RED


def test_270_is_the_other_way_and_swaps_the_shape() -> None:
    turned = Preview(image=corner_image(4, 2), width=4, height=2, rotation=270).rotated()
    assert turned.size == (2, 4)
    assert turned.getpixel((0, 3)) == RED


def test_zero_rotation_does_not_copy_the_frame() -> None:
    """The common case must not allocate: a frame is decoded once and drawn once."""
    image = corner_image(4, 2)
    assert Preview(image=image, width=4, height=2, rotation=0).rotated() is image


def test_a_rotation_the_protocol_never_allows_is_refused() -> None:
    """The wire format only accepts 0/90/180/270; anything else is a bug here."""
    with pytest.raises(PreviewUnavailable):
        Preview(image=corner_image(), width=4, height=2, rotation=45).rotated()


# --------------------------------------------------------------------------- #
# mirroring
# --------------------------------------------------------------------------- #
def test_the_front_lens_is_mirrored_for_a_person_and_the_rear_is_not() -> None:
    assert mirrored_for_display("front") is True
    assert mirrored_for_display(" Front ") is True, "the phone's spelling is not trusted"
    assert mirrored_for_display("back") is False
    assert mirrored_for_display("") is False, "an unknown lens is not silently mirrored"


def test_mirroring_swaps_left_and_right_without_resizing() -> None:
    image = corner_image(4, 2)
    mirrored, width, height = render(
        Preview(image=image, width=4, height=2, rotation=0), facing="front"
    )
    assert (width, height) == (4, 2)
    assert mirrored.getpixel((3, 0)) == RED, "the red corner moved to the right"
    assert mirrored.getpixel((0, 0)) == GREEN


def test_a_virtual_camera_consumer_can_ask_for_the_unmirrored_frame() -> None:
    """Anything that ships frames to a *device* must not be pre-mirrored."""
    image = corner_image(4, 2)
    plain, _w, _h = render(
        Preview(image=image, width=4, height=2, rotation=0), facing="front", mirror=False
    )
    assert plain.getpixel((0, 0)) == RED


# --------------------------------------------------------------------------- #
# fitting
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("frame", "box", "expected"),
    [
        ((1280, 720), (640, 360), (640, 360)),  # exact halves
        ((1280, 720), (800, 300), (533, 300)),  # height-limited, aspect kept
        ((1280, 720), (300, 800), (300, 168)),  # width-limited
        ((1920, 1080), (1000, 1000), (1000, 562)),
        ((640, 480), (1920, 1080), (640, 480)),  # never upscale by default
        ((640, 480), (1920, 1080), (1440, 1080)),  # ... unless asked
    ],
)
def test_fitting_uses_the_box_and_keeps_the_aspect_ratio(frame, box, expected) -> None:
    width, height = frame
    expected_width, expected_height = expected
    result = fit_size(
        width,
        height,
        box[0],
        box[1],
        allow_upscale=(expected_width, expected_height) == (1440, 1080),
    )
    assert result == (expected_width, expected_height)
    aspect_in = width / height
    aspect_out = result[0] / result[1]
    assert abs(aspect_in - aspect_out) < 0.01, "the picture must not be distorted"


def test_one_dimension_always_uses_the_whole_box() -> None:
    """A strip-shaped result means the layout wasted the space it was given."""
    width, height = fit_size(1280, 720, 640, 480)
    assert max(width / 640, height / 480) == pytest.approx(1.0)


def test_a_degenerate_box_or_frame_is_not_a_crash() -> None:
    assert fit_size(0, 0, 640, 360) == (0, 0)
    assert fit_size(1280, 720, 0, 0) == (1280, 720)
    assert matching_scale(1280, 720, 0, 0) == 1.0


def test_a_portrait_frame_in_a_landscape_box_stays_portrait() -> None:
    """The phone held upright must not be stretched into the wide box."""
    assert fit_size(720, 1280, 800, 400) == (225, 400)


# --------------------------------------------------------------------------- #
# the whole path: JPEG bytes -> something drawable
# --------------------------------------------------------------------------- #
def jpeg_frame(width: int = 64, height: int = 32, *, rotation: int = 0) -> VideoFrame:
    buffer = io.BytesIO()
    corner_image(width, height).save(buffer, format="JPEG", quality=90)
    return VideoFrame(
        seq=1,
        timestamp_ms=0,
        payload=buffer.getvalue(),
        width=width,
        height=height,
        rotation=rotation,
    )


def test_a_rotated_jpeg_is_decoded_turned_and_fitted_once() -> None:
    preview = decode(jpeg_frame(640, 480, rotation=90))
    assert preview.rotation == 90
    image, width, height = render(preview, facing="back", box=(100, 300))
    # Upright the frame is 480x640. The box is narrower than it is tall, so the
    # width is the limit: 100 wide and 133 tall, aspect intact.
    assert (width, height) == (100, 133)
    assert image.size == (100, 133)


def test_an_unrotated_jpeg_in_a_big_box_is_never_blown_up() -> None:
    preview = decode(jpeg_frame(64, 32))
    _image, width, height = render(preview, box=(1920, 1080))
    assert (width, height) == (64, 32)


# --------------------------------------------------------------------------- #
# the window's wiring, as a source contract (there is no toolkit here)
# --------------------------------------------------------------------------- #
WINDOW = (
    Path(__file__).resolve().parents[1] / "phonebridge" / "controller" / "ui" / "app.py"
)


def test_the_window_renders_through_the_shared_rules_and_owns_its_photo() -> None:
    """The defect this replaces: a `Preview` object handed straight to `PhotoImage`.

    That cannot work — it is not a Pillow image — and even if it had, it bypassed
    rotation and sizing. The window must go through `preview.render`, keep a
    reference to the `PhotoImage` (Tk drops the picture otherwise), and paste into
    an existing one when the geometry has not changed.
    """
    text = WINDOW.read_text(encoding="utf-8")
    assert "camera_preview.render(" in text
    assert "facing=facing" in text, "the lens decides whether to mirror"
    assert "self._camera_photo = ImageTk.PhotoImage(image)" in text
    assert "photo.paste(image)" in text, "a new Tk image per frame is waste"
    assert "_camera_box_resized" in text and "pack_propagate(False)" in text, (
        "the preview needs a real box, or it collapses to a strip"
    )
    assert "ImageTk.PhotoImage(preview" not in text
