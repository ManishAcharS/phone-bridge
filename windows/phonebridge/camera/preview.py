"""Turning a JPEG frame into something Tk can draw.

Tkinter itself only understands GIF/PNG, so a JPEG preview needs an image
library. Pillow is imported on demand and reported honestly by
:func:`preview_status`: without it the Camera section still shows the stream
state, resolution and counters — it just has no picture, and says why.

Decoding happens on the caller's thread in the tests and on the controller
thread in the app, which is why this module returns a plain Pillow image and
never touches Tk. The GUI converts it with ``ImageTk`` on the UI thread.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any

from .frames import VALID_ROTATIONS, VideoFrame

log = logging.getLogger(__name__)


class PreviewUnavailable(RuntimeError):
    """The preview cannot be drawn, with a reason the user can act on."""


def _pillow() -> Any:
    try:
        from PIL import Image  # noqa: PLC0415 - optional extra
    except Exception as exc:  # noqa: BLE001
        raise PreviewUnavailable(
            "the camera preview needs Pillow: run `pip install pillow` in the "
            "PhoneBridge virtualenv (the stream itself works without it)"
        ) from exc
    return Image


def preview_status() -> dict[str, object]:
    """Is a preview possible on this PC, and why not."""
    try:
        _pillow()
    except PreviewUnavailable as exc:
        return {
            "library": "Pillow",
            "available": False,
            "detail": str(exc),
            "verified_here": False,
        }
    return {
        "library": "Pillow",
        "available": True,
        "detail": "importable",
        "verified_here": False,
    }


@dataclass
class Preview:
    """A decoded frame, ready for the UI thread."""

    image: Any
    width: int
    height: int
    rotation: int

    def rotated(self) -> Any:
        """The image as it should be shown (the phone tells us the rotation).

        This is the **only** place the sign of `rotation` is interpreted. The
        phone sends CameraX's `imageInfo.rotationDegrees`, which is "how far to
        turn the picture clockwise to make it upright". Pillow's `rotate()` turns
        counter-clockwise, hence the negation. Rotating a 90/270 frame swaps its
        width and height, which is why `expand=True` is not optional: without it
        Pillow crops the picture to the original canvas and the result is a
        letterboxed sliver of the scene.
        """
        if self.rotation not in VALID_ROTATIONS:
            raise PreviewUnavailable(f"the frame claims a rotation of {self.rotation} degrees")
        if not self.rotation:
            return self.image
        return self.image.rotate(-self.rotation, expand=True)


def mirrored_for_display(facing: str) -> bool:
    """Whether a frame from this lens should be mirrored for a human to look at.

    A selfie camera is mirrored on the phone's own screen, so showing it
    un-mirrored looks wrong (text in the scene reads backwards). The *fix* is to
    mirror the front lens and never the rear one — while anything that consumes
    the frame as a *device* (a virtual webcam, a recording) must get the
    un-mirrored image, because every application mirrors its own preview.
    """
    return facing.strip().lower() == "front"


def matching_scale(width: int, height: int, box_width: int, box_height: int) -> float:
    """How much to scale to fill the box while keeping the aspect ratio."""
    if width <= 0 or height <= 0 or box_width <= 0 or box_height <= 0:
        return 1.0
    return min(box_width / width, box_height / height)


def fit_size(
    width: int,
    height: int,
    box_width: int,
    box_height: int,
    *,
    allow_upscale: bool = False,
) -> tuple[int, int]:
    """The largest size that fits `width`x`height` inside a box, aspect intact.

    Pure arithmetic, no Pillow, so the layout rule can be tested without a
    toolkit. One dimension of the result always equals the box (unless upscaling
    is allowed and the frame is smaller): that is what "use the space you have"
    means for a preview, and it is what stops a 1280x720 frame from being shown
    as a strip eight text lines tall.
    """
    if width <= 0 or height <= 0 or box_width <= 0 or box_height <= 0:
        return (max(0, width), max(0, height))
    scale = matching_scale(width, height, box_width, box_height)
    if scale > 1.0 and not allow_upscale:
        return (width, height)
    return (max(1, int(width * scale)), max(1, int(height * scale)))


def render(
    preview: Preview,
    *,
    facing: str = "",
    box: tuple[int, int] | None = None,
    mirror: bool | None = None,
) -> tuple[Any, int, int]:
    """Orient, optionally mirror, and scale one frame for a sized surface.

    Returns ``(image, width, height)``. `image` may be `preview.image` itself
    when nothing had to change — a frame is decoded once and drawn once, so
    copying it "for safety" would be a full-frame allocation per frame at
    15-30 fps.

    The three steps are in this order on purpose: rotate first (so the box is
    measured against the upright picture), then mirror (a flip, no size change),
    then scale once (one resample, not two).
    """
    image = preview.rotated()
    should_mirror = mirrored_for_display(facing) if mirror is None else bool(mirror)
    if should_mirror:
        from PIL import ImageOps  # noqa: PLC0415 - optional extra, imported on demand

        image = ImageOps.mirror(image)
    if box is not None:
        target = fit_size(image.width, image.height, box[0], box[1])
        if target != (image.width, image.height):
            from PIL import Image as _Image  # noqa: PLC0415

            image = image.resize(target, _Image.Resampling.BILINEAR)
    return image, image.width, image.height


def decode(frame: VideoFrame) -> Preview:
    """Decode one frame's JPEG payload. Raises :class:`PreviewUnavailable`."""
    Image = _pillow()
    if not frame.payload:
        raise PreviewUnavailable("the frame carried no image data")
    try:
        image = Image.open(io.BytesIO(frame.payload))
        image.load()
    except Exception as exc:  # noqa: BLE001 - a truncated JPEG is a dropped frame
        raise PreviewUnavailable(f"the frame could not be decoded: {exc}") from exc
    return Preview(
        image=image,
        width=frame.width or getattr(image, "width", 0),
        height=frame.height or getattr(image, "height", 0),
        rotation=frame.rotation,
    )
