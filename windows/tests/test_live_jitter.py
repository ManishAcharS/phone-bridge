"""The jitter buffer: bounded, prefill, drop-oldest, and honest underruns."""

from __future__ import annotations

import threading
import time

import pytest

from phonebridge.live.jitter import JitterBuffer

FRAME_MS = 20


def test_it_holds_frames_until_the_target_depth_is_reached() -> None:
    buffer = JitterBuffer(frame_ms=FRAME_MS, target_ms=60)

    buffer.push(b"a")
    buffer.push(b"b")
    # Two frames is less than the 3-frame target: the device is not started yet.
    assert buffer.pop(timeout=0.01) is None
    assert buffer.stats.underruns == 1

    buffer.push(b"c")
    buffer.push(b"d")
    assert buffer.pop(timeout=0.01) == b"a"
    assert buffer.stats.delivered == 1


def test_prefill_is_reestablished_after_an_underrun() -> None:
    buffer = JitterBuffer(frame_ms=FRAME_MS, target_ms=40)  # target = 2 frames
    for payload in (b"1", b"2", b"3"):
        buffer.push(payload)
    assert buffer.pop(timeout=0.01) == b"1"
    assert buffer.pop(timeout=0.01) == b"2"
    assert buffer.pop(timeout=0.01) == b"3"
    # Drained: the next pop underruns and re-arms the prefill rather than
    # dribbling out single frames as they trickle in.
    assert buffer.pop(timeout=0.01) is None
    buffer.push(b"4")
    assert buffer.pop(timeout=0.01) is None
    buffer.push(b"5")
    assert buffer.pop(timeout=0.01) == b"4"


def test_an_overrun_drops_the_oldest_frame_not_the_newest() -> None:
    """Latency matters more than completeness: the newest audio is the useful one."""
    buffer = JitterBuffer(frame_ms=FRAME_MS, target_ms=40)  # capacity = 4 frames
    for index in range(6):
        buffer.push(bytes([index]))

    assert buffer.stats.received == 6
    assert buffer.stats.dropped_late == 2
    assert buffer.depth_ms == 4 * FRAME_MS
    assert buffer.pop(timeout=0.01) == bytes([2])  # 0 and 1 were dropped


def test_pop_blocks_until_a_frame_arrives() -> None:
    buffer = JitterBuffer(frame_ms=FRAME_MS, target_ms=FRAME_MS)
    threading.Timer(0.05, lambda: buffer.push(b"late")).start()

    started = time.monotonic()
    payload = buffer.pop(timeout=1.0)

    assert payload == b"late"
    assert time.monotonic() - started < 1.0


def test_underruns_are_counted_as_concealment_time() -> None:
    buffer = JitterBuffer(frame_ms=FRAME_MS, target_ms=FRAME_MS)
    for _ in range(3):
        assert buffer.pop(timeout=0.01) is None

    stats = buffer.stats.to_dict()
    assert stats["underruns"] == 3
    assert stats["concealment_ms"] == 3 * FRAME_MS
    assert stats["delivered"] == 0


def test_close_releases_a_waiting_reader_and_refuses_new_frames() -> None:
    buffer = JitterBuffer(frame_ms=FRAME_MS, target_ms=FRAME_MS)
    threading.Timer(0.05, buffer.close).start()

    assert buffer.pop(timeout=2.0) is None
    buffer.push(b"ignored")
    assert buffer.depth_ms == 0


def test_a_target_shorter_than_one_frame_is_refused() -> None:
    with pytest.raises(ValueError):
        JitterBuffer(frame_ms=FRAME_MS, target_ms=5)
    with pytest.raises(ValueError):
        JitterBuffer(frame_ms=0, target_ms=20)
