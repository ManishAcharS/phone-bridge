"""The virtual-camera pump: newest wins, nothing blocks the loop, numbers measured.

These tests use a *slow* decoder on purpose, because that is the case the old
design failed: with a poll-every-33 ms loop and a 13 ms decode plus a 2 ms device
send, the loop could not start a frame per tick, and every frame it did start
delayed the session's audio. Here the same workload has to keep the source's
frame rate, drop what it cannot use, and say what it cost.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from phonebridge.live.pump import CameraPump, PumpSnapshot, stats_lines


class Recorder:
    def __init__(self) -> None:
        self.frames: list[Any] = []
        self.blanks = 0
        self.fail_on_send: Exception | None = None
        self.delay_s = 0.0

    def send(self, frame: Any) -> None:
        if self.fail_on_send is not None:
            raise self.fail_on_send
        if self.delay_s:
            time.sleep(self.delay_s)
        self.frames.append(frame)

    def send_black(self) -> None:
        self.blanks += 1


def pump(
    *,
    sink: Any = None,
    decode: Any = None,
    stall_s: float = 5.0,
    delay_s: float = 0.0,
    fps: int = 30,
) -> CameraPump:
    return CameraPump(
        decode=decode or (lambda frame, width, height: f"rgb-{width}x{height}"),
        to_array=lambda image: image,
        sink=sink or Recorder(),
        width=640,
        height=480,
        fps=fps,
        stall_s=stall_s,
        delay_s=delay_s,
    )


def feed(source: CameraPump, *, rate: float, seconds: float) -> None:
    """Offer frames at ``rate`` for ``seconds``, like a phone that keeps up."""
    deadline = time.perf_counter() + seconds
    period = 1.0 / rate
    while time.perf_counter() < deadline:
        source.submit(object())
        time.sleep(period)


# --------------------------------------------------------------------------- #
def test_a_30fps_source_keeps_its_frame_rate() -> None:
    """The old polling loop could not: it started one frame per tick."""
    sink = Recorder()
    source = pump(sink=sink)
    source.start()
    try:
        feed(source, rate=30, seconds=1.0)
        time.sleep(0.1)
        snapshot = source.snapshot()
    finally:
        source.close()

    assert snapshot.fps >= 24, f"30 fps in should give ~30 out, got {snapshot.fps:.1f}"
    assert snapshot.sent >= 24
    assert snapshot.depth == 0, "nothing may be left waiting"


def test_a_slow_decoder_drops_frames_instead_of_queueing_them() -> None:
    """A backlog of old pictures is worse than a missing one."""

    def slow(frame: Any, width: int, height: int) -> Any:
        time.sleep(0.05)  # 50 ms per frame: 20 fps at best
        return "rgb"

    source = pump(decode=slow)
    source.start()
    try:
        feed(source, rate=30, seconds=1.0)
        time.sleep(0.1)
        snapshot = source.snapshot()
    finally:
        source.close()

    assert snapshot.received >= 25, "the source really did send 30 fps"
    assert snapshot.dropped > 0, "a source faster than the decoder must lose frames"
    assert snapshot.depth == 0, "the slot is one deep, never a queue"
    assert snapshot.sent < snapshot.received


def test_only_the_newest_pending_frame_is_decoded() -> None:
    """After the worker frees up it must take the last frame, not the first."""
    decoded: list[Any] = []
    release = threading.Event()

    def blocked(frame: Any, width: int, height: int) -> Any:
        release.wait(timeout=2.0)
        decoded.append(frame)
        return "rgb"

    source = pump(decode=blocked, stall_s=0)
    source.start()
    try:
        first = object()
        source.submit(first)
        time.sleep(0.05)  # the worker is now inside `blocked` holding `first`
        newest = object()
        for _ in range(5):
            source.submit(object())
        source.submit(newest)
        release.set()
        time.sleep(0.2)
    finally:
        source.close()

    assert decoded[0] is first
    assert decoded[-1] is newest, "the frame that was waiting must be the newest one"
    assert len(decoded) <= 3, f"one slot, so at most two more frames: {len(decoded)}"


def test_a_stalled_source_is_blanked_once_not_every_tick() -> None:
    sink = Recorder()
    source = pump(sink=sink, stall_s=0.05)
    source.start()
    try:
        source.submit(object())
        time.sleep(0.4)
    finally:
        source.close()

    assert sink.frames, "the one frame that arrived must be sent"
    assert sink.blanks == 1, "a dead camera goes black once, not 20 times a second"


def test_a_dead_device_stops_the_sends_and_keeps_the_reason() -> None:
    sink = Recorder()
    sink.fail_on_send = OSError("the device is no longer available")
    source = pump(sink=sink)
    source.start()
    try:
        feed(source, rate=30, seconds=0.2)
        time.sleep(0.1)
        snapshot = source.snapshot()
        source.submit(object())
        time.sleep(0.1)
    finally:
        source.close()

    assert "the device is no longer available" in snapshot.send_error
    assert sink.frames == [], "nothing may be delivered to a dead device"


def test_a_bad_frame_is_counted_and_the_next_one_still_goes() -> None:
    calls = {"n": 0}

    def flaky(frame: Any, width: int, height: int) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("truncated JPEG")
        return "rgb"

    sink = Recorder()
    source = pump(sink=sink, decode=flaky)
    source.start()
    try:
        source.submit(object())
        time.sleep(0.1)
        source.submit(object())
        time.sleep(0.1)
        snapshot = source.snapshot()
    finally:
        source.close()

    assert snapshot.decode_failures == 1
    assert "truncated JPEG" in snapshot.decode_error
    assert sink.frames == ["rgb"], "the good frame must still be published"


def test_the_lip_sync_delay_holds_video_and_still_sends_the_newest() -> None:
    sink = Recorder()
    # decode returns the frame itself, so the assertion can tell them apart
    source = pump(sink=sink, decode=lambda frame, width, height: frame, delay_s=0.2)
    source.start()
    try:
        source.submit("first")
        time.sleep(0.08)
        source.submit("newest")
        time.sleep(0.35)
    finally:
        source.close()

    assert sink.frames == ["newest"], "a delayed frame gives way to a fresher one"


def test_the_stage_timings_are_measured_separately() -> None:
    def slow_decode(frame: Any, width: int, height: int) -> Any:
        time.sleep(0.02)
        return "rgb"

    sink = Recorder()
    sink.delay_s = 0.01
    source = pump(sink=sink, decode=slow_decode)
    source.start()
    try:
        for _ in range(6):
            source.submit(object())
            time.sleep(0.03)
        time.sleep(0.1)
        snapshot = source.snapshot()
    finally:
        source.close()

    assert snapshot.decode_ms >= 15, f"a 20 ms decode must show up: {snapshot.decode_ms:.1f}"
    assert snapshot.send_ms >= 8, f"a 10 ms send must show up: {snapshot.send_ms:.1f}"
    assert snapshot.convert_ms >= 0
    assert snapshot.bytes_per_frame == 0, "no payload on these fake frames"


def test_the_snapshot_is_json_shaped() -> None:
    snapshot = PumpSnapshot(sent=10, received=12, dropped=2, decode_ms=6.25, bytes_per_frame=98304)

    payload = snapshot.to_dict()
    assert payload["decode_ms"] == 6.25
    assert payload["bytes_per_frame"] == 98304
    assert set(payload) == {
        "fps",
        "sent",
        "received",
        "dropped",
        "depth",
        "bytes_per_frame",
        "decode_ms",
        "convert_ms",
        "send_ms",
        "decode_peak_ms",
        "convert_peak_ms",
        "send_peak_ms",
        "stale",
    }


def test_a_peak_is_reported_apart_from_the_mean() -> None:
    """45 ms on one frame of fifty is a 3.8 ms mean and one lost frame."""
    calls = {"n": 0}

    def spiky(frame: Any, width: int, height: int) -> Any:
        calls["n"] += 1
        if calls["n"] == 3:
            time.sleep(0.045)
        return "rgb"

    source = pump(decode=spiky)
    source.start()
    try:
        for _ in range(8):
            source.submit(object())
            time.sleep(0.03)
        time.sleep(0.2)
        snapshot = source.snapshot()
    finally:
        source.close()

    assert snapshot.decode_peak_ms >= 40, snapshot.decode_peak_ms
    assert snapshot.decode_ms < 25, "the mean must not be dragged to the spike"
    assert snapshot.stale >= 0


def test_a_blocked_send_does_not_stop_the_decoder() -> None:
    """The consumer being slow costs the frames in flight, not the incoming ones.

    Before the sender had its own thread, a 40 ms send held the decoder for 40 ms,
    so the phone's 20 fps stream filled the single slot and was dropped there —
    which read as "the PC cannot keep up" when the truth was "the device is slow".
    """
    sink = Recorder()
    sink.delay_s = 0.04  # harsher than any real virtual camera
    source = pump(sink=sink)
    source.start()
    try:
        feed(source, rate=20, seconds=1.0)
        time.sleep(0.3)
        snapshot = source.snapshot()
    finally:
        source.close()

    assert snapshot.received >= 15, snapshot.received
    assert snapshot.sent >= 10, f"a 40 ms send still manages ~20 fps: {snapshot.sent}"
    assert snapshot.dropped <= 2, (
        f"a slow device must not make the incoming stream look slow: "
        f"dropped {snapshot.dropped} of {snapshot.received}"
    )


def test_the_peak_line_names_the_stage_that_spiked() -> None:
    lines = stats_lines(
        {
            "fps": 19.4,
            "received": 20,
            "bytes_per_frame": 58000,
            "decode_ms": 3.1,
            "decode_peak_ms": 45.2,
            "convert_ms": 1.2,
            "convert_peak_ms": 4.0,
            "send_ms": 0.9,
            "send_peak_ms": 2.0,
            "stale": 4,
            "depth": 0,
            "dropped": 0,
        },
        None,
    )

    peak = lines[1]
    assert "peak decode 45.2 ms" in peak, peak
    assert "stale 4" in peak, peak
    assert "convert" not in peak, "a 4 ms peak is not a spike"


def test_no_peak_line_without_a_spike() -> None:
    lines = stats_lines({"fps": 20.0, "decode_ms": 3.0, "decode_peak_ms": 6.0}, None)
    assert len(lines) == 1, lines


# --------------------------------------------------------------------------- #
# the --stats text
# --------------------------------------------------------------------------- #
def test_the_stats_line_names_every_measurement() -> None:
    lines = stats_lines(
        {
            "fps": 29.6,
            "received": 30,
            "bytes_per_frame": 98304,
            "decode_ms": 6.2,
            "convert_ms": 2.1,
            "send_ms": 1.4,
            "depth": 0,
            "dropped": 3,
        },
        {
            "fps": 29.8,
            "encode_ms": 12.4,
            "bytes_per_frame": 96000,
            "quality": 75,
            "send_ms": 1.9,
            "width": 1280,
            "height": 720,
        },
    )

    assert len(lines) == 2
    pc, phone = lines
    for expected in ("29.6 fps", "96 KB/frame", "decode   6.2 ms", "convert  2.1 ms", "dropped 3"):
        assert expected in pc, pc
    for expected in ("29.8 fps", "encode  12.4 ms", "q75", "1280x720"):
        assert expected in phone, phone


def test_the_phone_line_says_which_encoder_and_what_was_captured() -> None:
    """A 500 KB frame at "640x480" is answered by `capture`, not by guessing."""
    lines = stats_lines(
        None,
        {
            "fps": 10.2,
            "encode_ms": 61.5,
            "bytes_per_frame": 520192,
            "quality": 70,
            "width": 640,
            "height": 480,
            "path": "rgba",
            "capture_width": 1920,
            "capture_height": 1440,
        },
    )

    assert len(lines) == 1
    phone = lines[0]
    assert "508 KB/frame" in phone, phone
    assert "rgba" in phone, phone
    assert "capture 1920x1440" in phone, phone


def test_the_phone_line_stays_short_when_nothing_fell_back() -> None:
    lines = stats_lines(
        None,
        {
            "fps": 29.8,
            "encode_ms": 8.1,
            "bytes_per_frame": 40960,
            "quality": 70,
            "width": 640,
            "height": 480,
            "path": "yuv",
            "capture_width": 640,
            "capture_height": 480,
        },
    )

    phone = lines[0]
    assert "yuv" in phone, phone
    # The capture size matched the request, so it is not repeated.
    assert "capture" not in phone, phone


def test_stats_lines_survive_a_phone_that_has_not_answered() -> None:
    lines = stats_lines({"fps": 30.0}, None)

    assert len(lines) == 1
    assert "phone" not in lines[0]
