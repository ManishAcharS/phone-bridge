"""The worker thread behind the virtual camera: decode, convert, send.

Why this exists, and how it was measured. The loop this replaces polled once per
``1/fps`` with ``await asyncio.sleep(interval)`` and did the JPEG decode (on an
executor), the Pillow resize, the numpy copy and the pyvirtualcam send around
that sleep. Three things followed from that shape:

* a frame that arrived just after a tick waited a whole interval — with a 30 fps
  source and a 33 ms tick, roughly every other frame was already stale when the
  loop looked, so the ceiling was about half the target before any work at all;
* on Windows an ``asyncio.sleep`` of 33 ms is quantised to the system timer
  (~15.6 ms), so each tick cost ~47 ms unless the process had raised the timer
  resolution — a 21 fps ceiling for a 30 fps target;
* decode, resize, the numpy copy and the device send all ran on the event loop.
  `pyvirtualcam.Camera.send()` does not pace anything — it validates the array
  and memcpys it into the backend's shared memory (0.5-1 ms at 720p, and it can
  block while the consumer collects the frame). Doing that on the loop meant
  every frame also delayed the session's audio and control traffic.

Measured on this project's CI-class CPU, one 720p q70 frame costs ~13 ms to
decode and ~7 ms when the device wants 480p (with ``Image.draft``, see
:func:`phonebridge.camera.preview.decode_into_box`). Two of those per tick is
already over budget, which is why the numbers on a real call were 5-8 fps.

This pump is deliberately the simplest thing that removes all three: one
thread, one slot, newest wins. Frames are **dropped, not queued** — a late
frame is worth less than the newest one — and the asyncio loop only ever hands a
frame over.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PumpSnapshot:
    """What the pump has been doing, for the status line and ``--stats``."""

    running: bool = False
    sent: int = 0
    received: int = 0
    dropped: int = 0
    depth: int = 0
    fps: float = 0.0
    bytes_per_frame: float = 0.0
    decode_ms: float = 0.0
    convert_ms: float = 0.0
    send_ms: float = 0.0
    decode_error: str = ""
    send_error: str = ""
    decode_failures: int = 0

    @property
    def busy_ms(self) -> float:
        """What one frame costs on the worker, end to end."""
        return self.decode_ms + self.convert_ms + self.send_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "fps": round(self.fps, 1),
            "sent": self.sent,
            "received": self.received,
            "dropped": self.dropped,
            "depth": self.depth,
            "bytes_per_frame": round(self.bytes_per_frame),
            "decode_ms": round(self.decode_ms, 2),
            "convert_ms": round(self.convert_ms, 2),
            "send_ms": round(self.send_ms, 2),
        }


class _Mean:
    """Mean of the samples taken in the last second."""

    def __init__(self, window_s: float = 1.0, limit: int = 600) -> None:
        self._window = window_s
        self._samples: deque[tuple[float, float]] = deque(maxlen=limit)

    def add(self, at: float, value: float) -> None:
        self._samples.append((at, float(value)))

    def mean(self, now: float) -> float:
        cutoff = now - self._window
        values = [value for timestamp, value in self._samples if timestamp >= cutoff]
        return sum(values) / len(values) if values else 0.0


class CameraPump:
    """Decode, convert and publish one frame at a time, on its own thread.

    ``decode`` is the frame-to-image callable the runner was given (tests inject
    their own), ``to_array`` is the numpy conversion, ``sink`` is the virtual
    camera. Nothing here touches asyncio.
    """

    def __init__(
        self,
        *,
        decode: Callable[[Any, int, int], Any],
        to_array: Callable[[Any], Any],
        sink: Any,
        width: int,
        height: int,
        fps: int,
        stall_s: float,
        delay_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        perf: Callable[[], float] = time.perf_counter,
        tick_s: float = 0.01,
    ) -> None:
        self._decode = decode
        self._to_array = to_array
        self._sink = sink
        self._width = width
        self._height = height
        self._fps_target = max(1, int(fps))
        self._stall_s = max(0.0, float(stall_s))
        self._delay_s = max(0.0, float(delay_s))
        self._clock = clock
        self._perf = perf
        self._tick_s = tick_s

        self._cond = threading.Condition()
        self._pending: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._received = 0
        self._sent = 0
        self._dropped = 0
        self._bytes = 0
        self._chunks = 0
        self._decode_failures = 0
        self._decode_error = ""
        self._send_error = ""
        self._decode_ms = _Mean()
        self._convert_ms = _Mean()
        self._send_ms = _Mean()
        self._sent_at: deque[float] = deque(maxlen=240)
        self._last_sent_at = 0.0
        self._blanked = False

    # ------------------------------------------------------------------ #
    # used by the asyncio side
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._thread is not None:
            return
        thread = threading.Thread(target=self._run, name="phonebridge-vcam-pump", daemon=True)
        self._thread = thread
        thread.start()

    def submit(self, frame: Any) -> bool:
        """Offer a frame. Returns True when it displaced one that was waiting.

        The displacement is the point: the pump is one slot deep, so a PC that is
        behind always sends the newest picture instead of working through a
        backlog of old ones.
        """
        size = len(getattr(frame, "payload", b"") or b"")
        with self._cond:
            self._received += 1
            self._bytes += size
            displaced = self._pending is not None
            if displaced:
                self._dropped += 1
            self._pending = frame
            self._cond.notify()
        return displaced

    def close(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def snapshot(self) -> PumpSnapshot:
        now = self._perf()
        with self._cond:
            received, sent, dropped = self._received, self._sent, self._dropped
            depth = 1 if self._pending is not None else 0
            decode_error, send_error = self._decode_error, self._send_error
            failures = self._decode_failures
            bytes_in, chunks = self._bytes, self._chunks
        return PumpSnapshot(
            running=self._thread is not None and self._thread.is_alive(),
            sent=sent,
            received=received,
            dropped=dropped,
            depth=depth,
            fps=self._fps(now),
            bytes_per_frame=(bytes_in / chunks) if chunks else 0.0,
            decode_ms=self._decode_ms.mean(now),
            convert_ms=self._convert_ms.mean(now),
            send_ms=self._send_ms.mean(now),
            decode_error=decode_error,
            send_error=send_error,
            decode_failures=failures,
        )

    # ------------------------------------------------------------------ #
    # the worker
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        while not self._stop.is_set():
            frame = self._take()
            if self._stop.is_set():
                break
            if frame is None:
                self._blank_if_stalled()
                continue
            if self._delay_s:
                frame = self._delay(frame)
                if frame is None or self._stop.is_set():
                    continue
            self._publish(frame)
        # A pump that stops while the device is open leaves the last face on
        # screen; the runner blanks explicitly on close, so nothing to do here.

    def _take(self) -> Any:
        with self._cond:
            if self._pending is None:
                self._cond.wait(timeout=self._tick_s)
            frame, self._pending = self._pending, None
            return frame

    def _delay(self, frame: Any) -> Any:
        """Hold a frame for the lip-sync offset, then send the newest one.

        Video is delayed, never audio, and the waiting is done here so the
        asyncio loop is not held for the offset.
        """
        due_clock = self._clock() + self._delay_s
        while not self._stop.is_set():
            remaining = due_clock - self._clock()
            if remaining <= 0:
                break
            self._stop.wait(min(0.005, remaining))
            newer = self._take_nowait()
            if newer is not None:
                frame = newer
        return frame

    def _take_nowait(self) -> Any:
        with self._cond:
            frame, self._pending = self._pending, None
            return frame

    def _blank_if_stalled(self) -> None:
        """Nothing new arrived: keep the last picture, then go black."""
        if self._stall_s <= 0 or not self._last_sent_at or self._blanked:
            return
        if self._clock() - self._last_sent_at <= self._stall_s:
            return
        if self._send_error:
            return
        started = self._perf()
        try:
            self._sink.send_black()
        except Exception as exc:  # noqa: BLE001 - the device died, the session did not
            with self._cond:
                self._send_error = f"{type(exc).__name__}: {exc}"
            return
        finished = self._perf()
        self._blanked = True
        with self._cond:
            self._sent += 1
            self._send_ms.add(finished, (finished - started) * 1000.0)

    def _publish(self, frame: Any) -> None:
        started = self._perf()
        try:
            image = self._decode(frame, self._width, self._height)
        except Exception as exc:  # noqa: BLE001 - one bad JPEG is one dropped frame
            with self._cond:
                self._decode_failures += 1
                if not self._decode_error:
                    self._decode_error = f"{type(exc).__name__}: {exc}"
            return
        decoded = self._perf()
        try:
            array = self._to_array(image)
        except Exception as exc:  # noqa: BLE001 - a conversion bug is one bad frame
            with self._cond:
                self._decode_failures += 1
                if not self._decode_error:
                    self._decode_error = f"{type(exc).__name__}: {exc}"
            return
        converted = self._perf()
        if self._send_error:
            # The device is gone: stop pushing at it, keep counting the loss.
            with self._cond:
                self._dropped += 1
            return
        try:
            self._sink.send(array)
        except Exception as exc:  # noqa: BLE001 - the device is gone, the session is not
            with self._cond:
                self._send_error = f"{type(exc).__name__}: {exc}"
            return
        sent = self._perf()
        with self._cond:
            self._sent += 1
            self._chunks += 1
            self._decode_ms.add(decoded, (decoded - started) * 1000.0)
            self._convert_ms.add(converted, (converted - decoded) * 1000.0)
            self._send_ms.add(sent, (sent - converted) * 1000.0)
            self._sent_at.append(sent)
        self._last_sent_at = self._clock()
        self._blanked = False

    def _fps(self, now_perf: float) -> float:
        """Frames per second over the last two seconds of sends."""
        with self._cond:
            stamps = [stamp for stamp in self._sent_at if now_perf - stamp <= 2.0]
        if len(stamps) < 2:
            return 0.0
        span = stamps[-1] - stamps[0]
        if span <= 0:
            return 0.0
        return (len(stamps) - 1) / span

    def __enter__(self) -> CameraPump:
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def stats_lines(pc: Any = None, phone: Any = None) -> list[str]:
    """The ``--stats`` lines: exactly the numbers asked for, nothing else.

    Takes the dictionaries the runner puts in :attr:`LiveStatus.camera_stats`, so
    the text path and the JSON path describe the same measurements.
    """
    lines: list[str] = []
    pc = pc or {}
    if pc:
        lines.append(
            f"  pc    {float(pc.get('fps', 0.0)):5.1f} fps  "
            f"rx {int(pc.get('received', 0)):4d}  "
            f"{float(pc.get('bytes_per_frame', 0)) / 1024:5.0f} KB/frame  "
            f"decode {float(pc.get('decode_ms', 0.0)):5.1f} ms  "
            f"convert {float(pc.get('convert_ms', 0.0)):4.1f} ms  "
            f"send {float(pc.get('send_ms', 0.0)):4.1f} ms  "
            f"queue {int(pc.get('depth', 0))}  dropped {int(pc.get('dropped', 0))}"
        )
    phone = phone or {}
    if phone:
        quality = phone.get("quality")
        q_text = f"q{int(quality)}  " if isinstance(quality, (int, float)) else ""
        size_text = ""
        if phone.get("width") and phone.get("height"):
            size_text = f"  {int(phone['width'])}x{int(phone['height'])}"
        # Which encoder the phone is using. `yuv` is the fast path; `rgba` means
        # the phone fell back after the fast one produced nothing, and the reason
        # is in the JSON (`path_reason`) because the line is already long.
        enc_text = ""
        encoder = phone.get("path")
        if isinstance(encoder, str) and encoder:
            enc_text = f"  {encoder}"
        # What the phone's camera actually delivered, when that is not what was
        # asked for: the difference is why frames are 500 KB instead of 40 KB.
        capture_text = ""
        captured = (phone.get("capture_width"), phone.get("capture_height"))
        requested = (phone.get("width"), phone.get("height"))
        if all(isinstance(value, int) and value > 0 for value in captured) and captured != requested:
            capture_text = f"  capture {captured[0]}x{captured[1]}"
        lines.append(
            f"  phone {float(phone.get('fps', 0.0)):5.1f} fps  "
            f"encode {float(phone.get('encode_ms', 0.0)):5.1f} ms  "
            f"{float(phone.get('bytes_per_frame', 0)) / 1024:5.0f} KB/frame  "
            f"{q_text}send {float(phone.get('send_ms', 0.0)):4.1f} ms{size_text}{enc_text}{capture_text}"
        )
        # The frame-rate control, on its own line because it is the answer to
        # "why 22 fps when I asked for 30": what was asked of auto-exposure, what
        # the device lists, and how long the picture was near-blank at start.
        fps_line = _fps_line(phone)
        if fps_line:
            lines.append(fps_line)
    return lines


def _fps_line(phone: dict[str, Any]) -> str:
    """``AE target 30-30 (device 15-30,30-30)`` plus the measured warm-up."""
    parts: list[str] = []
    target = phone.get("fps_range")
    if isinstance(target, str) and target:
        parts.append(f"AE target {target}")
    supported = phone.get("fps_ranges_supported")
    if isinstance(supported, str) and supported:
        parts.append(f"device {supported}")
    if phone.get("fps_fixed") is False:
        parts.append("variable")
    warmup = phone.get("warmup_ms")
    if isinstance(warmup, (int, float)):
        if warmup < 0:
            parts.append("warm-up not converged yet")
        elif warmup:
            first = phone.get("first_frame_bytes")
            first_text = f", first frame {int(first)} B" if isinstance(first, (int, float)) and first else ""
            parts.append(f"warm-up {int(warmup)} ms{first_text}")
    return f"  ae    {'  '.join(parts)}" if parts else ""
