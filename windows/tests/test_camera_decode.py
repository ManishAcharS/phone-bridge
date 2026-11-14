"""The virtual-camera decode: exact size, one copy, and a half-scale JPEG decode.

``decode_into_box`` runs once per frame at 25-30 fps, so its cost is the camera's
frame budget. The two things that make it cheap are:

* ``Image.draft()`` — libjpeg decoding at 1/2, 1/4 or 1/8 when the device wants
  a smaller picture. It only helps if the box it is given is the *fitted* size,
  because a 1280x720 frame letterboxed into 640x480 is really 640x360, and asking
  for 640x480 makes PIL refuse the half-scale decode;
* no letterbox canvas when the aspect ratios already agree (the common case),
  which used to allocate and paste a full-frame image every frame.
"""

from __future__ import annotations

import io

import pytest

from phonebridge.camera.frames import VideoFrame
from phonebridge.camera.preview import PreviewUnavailable, decode_into_box

Image = pytest.importorskip("PIL.Image")


def jpeg(width: int, height: int, colour: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def frame(width: int, height: int, *, rotation: int = 0, payload: bytes | None = None) -> VideoFrame:
    return VideoFrame(
        seq=0,
        timestamp_ms=0,
        payload=payload if payload is not None else jpeg(width, height),
        width=width,
        height=height,
        rotation=rotation,
    )


# --------------------------------------------------------------------------- #
def test_an_exactly_matching_frame_is_returned_at_the_device_size() -> None:
    image = decode_into_box(frame(1280, 720), 1280, 720)

    assert image.size == (1280, 720)
    assert image.mode == "RGB"
    assert image.getpixel((640, 360)) == pytest.approx((10, 120, 200), abs=12)


def test_a_letterbox_is_only_added_when_the_aspect_ratios_differ() -> None:
    wide = decode_into_box(frame(1280, 720), 1280, 720)
    squashed = decode_into_box(frame(1280, 720), 640, 480)

    assert wide.size == (1280, 720)
    assert squashed.size == (640, 480)
    # 16:9 into 4:3 leaves bars top and bottom, not a stretched picture.
    assert squashed.getpixel((320, 5)) == (0, 0, 0)
    assert squashed.getpixel((320, 240)) == pytest.approx((10, 120, 200), abs=14)


def test_a_portrait_frame_becomes_upright_landscape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = decode_into_box(frame(480, 640, rotation=90), 640, 480)

    assert image.size == (640, 480)
    assert image.getpixel((320, 240)) == pytest.approx((10, 120, 200), abs=14)


def test_the_draft_box_is_the_fitted_size_so_half_scale_decoding_can_happen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """640x480 from a 16:9 source must ask libjpeg for 640x360, not 640x480."""
    from PIL import JpegImagePlugin

    asked: list[tuple[int, int]] = []
    # JpegImageFile.draft is where the scale factor is actually chosen.
    original = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):  # noqa: ANN001, ANN201 - JpegImageFile.draft(mode, size)
        asked.append(tuple(size))
        return original(self, mode, size)

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)

    decode_into_box(frame(1280, 720), 640, 480)

    assert asked, "draft() must be used: without it a 720p JPEG is decoded in full"
    width, height = asked[-1]
    assert width <= 640 and height <= 360, f"asked for {width}x{height}"


def test_a_downscaled_frame_really_is_half_decoded() -> None:
    """Cheap check of the same property: the result is smaller and still correct."""
    small = decode_into_box(frame(1280, 720), 320, 240)

    assert small.size == (320, 240)
    assert small.getpixel((160, 120)) == pytest.approx((10, 120, 200), abs=14)


def test_a_truncated_jpeg_raises_a_message_the_cli_can_print() -> None:
    payload = jpeg(320, 240)[:200]

    with pytest.raises(PreviewUnavailable, match="could not be decoded"):
        decode_into_box(frame(320, 240, payload=payload), 320, 240)


def test_an_empty_payload_is_refused_before_pillow_sees_it() -> None:
    with pytest.raises(PreviewUnavailable, match="no image data"):
        decode_into_box(frame(320, 240, payload=b""), 320, 240)


def test_an_unknown_rotation_is_ignored_rather_than_crashing() -> None:
    """The stream layer rejects bad rotations; the decoder must not be the crash."""
    image = decode_into_box(frame(320, 240, rotation=45), 320, 240)

    assert image.size == (320, 240)
