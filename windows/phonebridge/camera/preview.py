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

from .frames import VideoFrame

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
        """The image as it should be shown (the phone tells us the rotation)."""
        if not self.rotation:
            return self.image
        # Pillow rotates counter-clockwise; the header says how far the receiver
        # must turn the picture, so the sign is inverted here once, in one place.
        return self.image.rotate(-self.rotation, expand=True)


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
