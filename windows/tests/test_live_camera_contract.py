"""The camera edge: what ``pyvirtualcam`` actually accepts, and nothing looser.

``pyvirtualcam`` takes a **numpy uint8 array of shape (height, width, 3)** and
does not convert anything: bytes, a PIL image, or an RGBA array are all errors —
and the old fake here accepted any object at all, which is how the microphone sink
shipped a dtype bug. This file fakes the library with its real checks, then proves
the production path produces something it accepts.

The tests need numpy (for the array) and Pillow (for the JPEG decode), which is
exactly what the real path needs; they skip with a reason rather than pretend.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from phonebridge.live.config import CameraPlan
from phonebridge.live.devices import DeviceUnavailable, VirtualCamera, open_virtual_camera

numpy = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")

WIDTH, HEIGHT = 64, 48


class FakePyvirtualcam:
    """The parts of pyvirtualcam the code uses, with the library's own checks."""

    class PixelFormat:
        RGB = "RGB"

    def __init__(self, backend: str = "obs") -> None:
        self.backend = backend
        self.frames: list[Any] = []

    def Camera(self, *, width: int, height: int, fps: int, fmt: Any, backend: str) -> FakeCamera:  # noqa: N802
        if backend == "unitycapture":
            raise RuntimeError("Unity Capture is not installed")
        return FakeCamera(self, width=width, height=height, fps=fps, fmt=fmt, backend=backend)


class FakeCamera:
    def __init__(
        self, owner: FakePyvirtualcam, *, width: int, height: int, fps: int, fmt: Any, backend: str
    ) -> None:
        self.owner = owner
        self.width = width
        self.height = height
        self.fps = fps
        self.fmt = fmt
        self.backend = backend
        self.closed = False

    def send(self, frame: Any) -> None:
        # Exactly what pyvirtualcam requires, in the order it checks it.
        array = numpy.asarray(frame)
        if array.ndim != 3 or array.shape[2] != 3:
            raise ValueError(f"array must have shape (h, w, 3), got {array.shape}")
        if array.dtype != numpy.uint8:
            raise ValueError(f"array must be uint8, got {array.dtype}")
        if (array.shape[0], array.shape[1]) != (self.height, self.width):
            raise ValueError(
                f"array is {array.shape[0]}x{array.shape[1]}, camera is {self.height}x{self.width}"
            )
        if not array.flags["C_CONTIGUOUS"]:
            raise ValueError("array must be C-contiguous")
        self.owner.frames.append(array)

    def close(self) -> None:
        self.closed = True


def jpeg(width: int = WIDTH, height: int = HEIGHT) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def frame_payload(width: int = WIDTH, height: int = HEIGHT) -> bytes:
    from phonebridge.camera.frames import VideoFrame

    return VideoFrame(seq=1, timestamp_ms=0, payload=jpeg(width, height), width=width, height=height)


# --------------------------------------------------------------------------- #
def test_the_fake_rejects_everything_pyvirtualcam_rejects() -> None:
    """Pins the fake: without this, the tests below could pass on garbage."""
    camera = FakeCamera(FakePyvirtualcam(), width=WIDTH, height=HEIGHT, fps=30, fmt="RGB", backend="obs")

    with pytest.raises(ValueError, match="shape"):
        camera.send(b"\x00" * (WIDTH * HEIGHT * 3))  # bytes: the microphone bug, in video
    with pytest.raises(ValueError, match="uint8"):
        camera.send(numpy.zeros((HEIGHT, WIDTH, 3), dtype=numpy.float32))
    with pytest.raises(ValueError, match="camera is"):
        camera.send(numpy.zeros((HEIGHT, WIDTH + 1, 3), dtype=numpy.uint8))


def test_the_production_decode_produces_an_array_the_device_accepts() -> None:
    from phonebridge.live.runner import _to_array, default_decode

    image = default_decode(frame_payload(), WIDTH, HEIGHT)
    array = _to_array(image)

    assert array.shape == (HEIGHT, WIDTH, 3)
    assert array.dtype == numpy.uint8
    assert array.flags["C_CONTIGUOUS"]

    fake = FakePyvirtualcam()
    camera = VirtualCamera(
        FakeCamera(fake, width=WIDTH, height=HEIGHT, fps=30, fmt="RGB", backend="obs"),
        width=WIDTH,
        height=HEIGHT,
        fps=30,
        backend="obs",
    )
    camera.send(array)

    assert len(fake.frames) == 1


def test_a_frame_of_a_different_aspect_ratio_is_letterboxed_not_stretched() -> None:
    """A webcam that changes aspect ratio looks broken to everything downstream."""
    from phonebridge.live.runner import _to_array, default_decode

    portrait = frame_payload(48, 64)  # phone held upright
    array = _to_array(default_decode(portrait, WIDTH, HEIGHT))

    assert array.shape == (HEIGHT, WIDTH, 3)
    # Letterbox: the top-left corner is the black bar, not the picture.
    assert array[0, 0].tolist() == [0, 0, 0]
    assert array[HEIGHT // 2, WIDTH // 2].tolist() == pytest.approx([10, 120, 200], abs=12)


def test_the_stall_blank_is_the_right_shape_and_colour() -> None:
    from phonebridge.live.runner import _to_array

    camera = VirtualCamera(
        FakeCamera(FakePyvirtualcam(), width=WIDTH, height=HEIGHT, fps=30, fmt="RGB", backend="obs"),
        width=WIDTH,
        height=HEIGHT,
        fps=30,
        backend="obs",
    )

    camera.send_black()

    sent = camera._handle.owner.frames[0]  # noqa: SLF001 - the fake's record
    assert sent.shape == (HEIGHT, WIDTH, 3)
    assert not sent.any(), "the blank has to be black, not whatever numpy leaves there"
    assert _to_array(sent).dtype == numpy.uint8


def test_the_first_backend_that_opens_wins() -> None:
    plan = CameraPlan(backend="auto")

    camera = open_virtual_camera(plan, module=FakePyvirtualcam())

    assert camera.backend == "obs"
    camera.close()


def test_no_usable_backend_names_every_attempt() -> None:
    class NoBackends(FakePyvirtualcam):
        def Camera(self, **kwargs: Any) -> FakeCamera:  # noqa: N802
            raise RuntimeError(f"backend {kwargs.get('backend')} is not installed")

    with pytest.raises(DeviceUnavailable) as caught:
        open_virtual_camera(CameraPlan(backend="auto"), module=NoBackends())

    message = str(caught.value)
    assert "obs" in message and "unitycapture" in message, (
        "both attempts belong in the message: the fix is the same for either"
    )
