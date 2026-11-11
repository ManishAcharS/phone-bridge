"""Sensor frames and the sensor stream, with no phone and no screen.

What is covered here: the wire format (including the ways a frame can be wrong),
the per-sensor sequence accounting that decides whether a reading is a duplicate,
late or a gap, the registry's ordering, the sampling configuration, and the whole
start/stop lifecycle including a refused permission and an interrupted session.

Nothing here is a hardware test: every reading is constructed by this file.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from phonebridge.protocol.messages import RpcError
from phonebridge.sensors import (
    ACTIVE,
    ERROR,
    HEADER_SIZE,
    IDLE,
    INTERVAL_CHOICES_US,
    SENSOR_STREAM,
    SensorFrame,
    SensorFrameError,
    SensorInfo,
    SensorLink,
    SensorRegistry,
    decode_frame,
    kind_axes,
    kind_name,
    kind_unit,
)
from phonebridge.sensors.frames import MAX_SENSORS_PER_FRAME


def run(coro):
    return asyncio.run(coro)


async def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


def body(sensor_id: int = 1, seq: int = 0, kind: int = 1, values=(0.0, 0.0, 9.81), ts: int = 20):
    return SensorFrame(
        sensor_id=sensor_id, seq=seq, timestamp_ms=ts, kind=kind, values=values
    ).encode()


def seq_is(link: SensorLink, sensor_id: int, seq: int) -> bool:
    reading = link.reading(sensor_id)
    return reading is not None and reading.seq == seq


SENSORS = (
    SensorInfo(id=1, kind=1, name="Accelerometer", min_delay_us=5000),
    SensorInfo(id=2, kind=4, name="Gyroscope", min_delay_us=5000),
    SensorInfo(id=3, kind=5, name="Light", min_delay_us=200000),
)


class FakeSession:
    def __init__(self, *, sensors: tuple[SensorInfo, ...] = SENSORS, permitted: bool = True):
        self.is_open = True
        self.calls: list[tuple[str, dict | None]] = []
        self.queues: dict[int, asyncio.Queue] = {}
        self.sensors = sensors
        self.permitted = permitted
        self.refuse: dict[str, Exception] = {}
        self.interval = 20000

    async def call(self, method, params=None, timeout: float = 10.0):
        self.calls.append((method, params))
        refusal = self.refuse.get(method)
        if refusal is not None:
            raise refusal
        if method == "sensor.list":
            return {
                "sensors": [s.to_dict() for s in self.sensors],
                "interval_us": (params or {}).get("interval_us", 20000),
                "permitted": self.permitted,
            }
        if method == "sensor.start":
            ids = list((params or {}).get("ids") or [])
            if not ids:
                raise RpcError("bad_params", "no sensors requested")
            self.interval = int((params or {}).get("interval_us") or 20000)
            return {"ok": True, "active": ids, "interval_us": self.interval}
        if method == "sensor.stop":
            return {"ok": True, "active": []}
        return {"ok": True}

    def subscribe_blobs(self, stream_id: int, maxsize: int = 256):
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.queues[stream_id] = queue
        return queue

    def methods(self):
        return [name for name, _ in self.calls]


# --------------------------------------------------------------------------- #
# wire format
# --------------------------------------------------------------------------- #
def test_the_header_is_twelve_bytes_and_a_reading_round_trips() -> None:
    assert HEADER_SIZE == 12
    frame = SensorFrame(sensor_id=7, seq=65535, timestamp_ms=4_000_000_000, kind=11,
                        values=(0.1, -0.2, 0.3, 0.9))
    encoded = frame.encode()
    assert len(encoded) == HEADER_SIZE + 16
    decoded = decode_frame(encoded)
    assert decoded.sensor_id == 7
    assert decoded.seq == 65535
    assert decoded.timestamp_ms == 4_000_000_000
    assert decoded.kind == 11
    assert decoded.values == pytest.approx((0.1, -0.2, 0.3, 0.9))
    assert decoded.reading == pytest.approx({"x": 0.1, "y": -0.2, "z": 0.3, "w": 0.9})


def test_an_unknown_sensor_kind_still_carries_its_numbers() -> None:
    frame = decode_frame(body(kind=200, values=(1.0, 2.0)))
    assert kind_name(200) == "sensor_200"
    assert frame.reading == {"v0": 1.0, "v1": 2.0}
    assert kind_unit(200) == ""


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x00" * 4,
        b"\x00" * (HEADER_SIZE - 1),
    ],
)
def test_a_frame_shorter_than_its_header_is_refused(payload: bytes) -> None:
    with pytest.raises(SensorFrameError):
        decode_frame(payload)


def test_a_frame_whose_length_contradicts_its_header_is_refused() -> None:
    encoded = bytearray(body(values=(1.0, 2.0, 3.0)))
    with pytest.raises(SensorFrameError):
        decode_frame(bytes(encoded[:-4]))
    with pytest.raises(SensorFrameError):
        decode_frame(bytes(encoded) + b"\x00\x00\x00\x00")


def test_an_impossible_value_count_is_refused() -> None:
    header = struct.Struct(">HHIBBH").pack(1, 0, 0, 1, 0, 0)
    with pytest.raises(SensorFrameError):
        decode_frame(header)
    too_many = struct.Struct(">HHIBBH").pack(1, 0, 0, 1, MAX_SENSORS_PER_FRAME + 1, 0)
    with pytest.raises(SensorFrameError):
        decode_frame(too_many)


def test_nan_and_infinity_are_refused_both_ways() -> None:
    with pytest.raises(SensorFrameError):
        SensorFrame(sensor_id=1, seq=0, timestamp_ms=0, kind=1, values=(float("nan"),)).encode()
    header = struct.Struct(">HHIBBH").pack(1, 0, 0, 1, 1, 0)
    with pytest.raises(SensorFrameError):
        decode_frame(header + struct.pack(">f", float("inf")))


def test_the_metadata_model_names_the_axes_and_the_unit() -> None:
    assert kind_axes(1) == ("x", "y", "z")
    assert kind_axes(5) == ("lux",)
    assert kind_name(19) == "step_counter"
    info = SensorInfo(id=1, kind=1, name="Accelerometer", min_delay_us=5000)
    assert info.max_hz == 200
    assert info.unit == "m/s²"
    assert info.to_dict()["axes"] == ["x", "y", "z"]


def test_the_registry_puts_the_useful_sensors_first() -> None:
    registry = SensorRegistry(
        sensors=(
            SensorInfo(id=9, kind=19, name="Step Counter"),
            SensorInfo(id=3, kind=5, name="Light"),
            SensorInfo(id=1, kind=1, name="Accelerometer"),
            SensorInfo(id=2, kind=4, name="Gyroscope"),
            SensorInfo(id=8, kind=99, name="Mystery"),
        )
    )
    order = [info.id for info in registry.ordered()]
    assert order[:3] == [1, 2, 3], "accelerometer, gyroscope, light come first"
    assert registry.name_for(1) == "Accelerometer"
    assert registry.name_for(42) == "sensor 42"


def test_a_partial_sensor_list_is_tolerated() -> None:
    registry = SensorRegistry.from_list_reply(
        {"sensors": [{"id": 1, "kind": 1, "name": "Accelerometer"}, "garbage", {"strange": 1}]}
    )
    assert len(registry.sensors) == 2
    assert registry.stats[1].received == 0


# --------------------------------------------------------------------------- #
# link: configuration and lifecycle
# --------------------------------------------------------------------------- #
def test_the_interval_choices_are_sane() -> None:
    for label, interval_us in INTERVAL_CHOICES_US:
        assert 1000 <= interval_us <= 10_000_000, label
    assert dict(INTERVAL_CHOICES_US)["50 Hz (20 ms)"] == 20000


def test_refresh_lists_the_sensors_and_does_not_start_anything() -> None:
    async def scenario():
        session = FakeSession()
        link = SensorLink(session)
        registry = await link.refresh()
        assert len(registry.sensors) == 3
        assert session.methods() == ["sensor.list"]
        assert link.snapshot().state == IDLE

    run(scenario())


def test_starting_uses_the_chosen_interval_and_reaches_active() -> None:
    async def scenario():
        session = FakeSession()
        link = SensorLink(session)
        await link.refresh()
        link.set_interval(10000)
        assert await link.start([1]) is True
        assert link.snapshot().state == ACTIVE
        method, params = session.calls[-1]
        assert method == "sensor.start"
        assert params == {"ids": [1], "interval_us": 10000}
        await link.close()

    run(scenario())


def test_starting_without_a_session_says_so_and_calls_nothing() -> None:
    async def scenario():
        session = FakeSession()
        session.is_open = False
        link = SensorLink(session)
        assert await link.start([1]) is False
        assert "not connected" in link.snapshot().detail
        assert session.calls == []

    run(scenario())


def test_a_refused_permission_is_reported_and_compensated() -> None:
    async def scenario():
        session = FakeSession(permitted=False)
        session.refuse["sensor.start"] = RpcError("sensor_denied", "sensor permission is not granted")
        link = SensorLink(session)
        assert await link.start([1]) is False
        snapshot = link.snapshot()
        assert snapshot.state == ERROR
        assert "sensor permission is not granted" in snapshot.detail
        assert "sensor.stop" in session.methods()

    run(scenario())


def test_an_unknown_sensor_id_is_refused_before_any_rpc() -> None:
    async def scenario():
        session = FakeSession()
        link = SensorLink(session)
        await link.refresh()
        assert await link.start([1, 99]) is False
        assert "does not have sensor" in link.snapshot().detail
        assert "sensor.start" not in session.methods()

    run(scenario())


def test_a_phone_with_no_sensors_says_so_instead_of_streaming() -> None:
    async def scenario():
        session = FakeSession(sensors=())
        link = SensorLink(session)
        assert await link.start() is False
        assert link.snapshot().state == ERROR
        assert "no sensors" in link.snapshot().detail

    run(scenario())


def test_stop_tells_the_phone_and_drops_the_readings() -> None:
    async def scenario():
        session = FakeSession()
        link = SensorLink(session)
        await link.start([1])
        link.push(body(1, 0))
        await wait_for(lambda: link.reading(1) is not None)
        await link.stop("user pressed stop")
        assert "sensor.stop" in session.methods()
        assert link.snapshot().state == IDLE
        assert link.registry.active == ()

    run(scenario())


# --------------------------------------------------------------------------- #
# link: readings, sequence, duplicates, gaps, backpressure
# --------------------------------------------------------------------------- #
def test_a_reading_becomes_the_latest_value_with_a_rate() -> None:
    async def scenario():
        session = FakeSession()
        link = SensorLink(session)
        await link.refresh()
        await link.start([1])
        link.push(body(1, 0, values=(0.1, 0.2, 9.8)))
        assert await wait_for(lambda: link.reading(1) is not None)
        reading = link.reading(1)
        assert reading is not None
        assert reading.reading["z"] == pytest.approx(9.8, rel=1e-6)
        assert reading.seq == 0
        assert link.snapshot().stats["1"]["received"] == 1
        await link.close()

    run(scenario())


def test_a_duplicate_sequence_is_counted_and_does_not_replace_the_reading() -> None:
    async def scenario():
        session = FakeSession()
        link = SensorLink(session)
        await link.refresh()
        await link.start([1])
        link.push(body(1, 5, values=(1.0, 0.0, 0.0)))
        assert await wait_for(lambda: link.reading(1) is not None)
        link.push(body(1, 5, values=(9.0, 0.0, 0.0)))
        await asyncio.sleep(0.01)
        assert link.reading(1).values[0] == pytest.approx(1.0)
        assert link.snapshot().stats["1"]["duplicates"] == 1
        await link.close()

    run(scenario())


def test_a_gap_in_one_sensor_is_counted_as_dropped_samples() -> None:
    async def scenario():
        session = FakeSession()
        link = SensorLink(session)
        await link.refresh()
        await link.start([1])
        link.push(body(1, 0))
        link.push(body(1, 4))  # three samples never arrived
        assert await wait_for(lambda: seq_is(link, 1, 4))
        assert link.snapshot().stats["1"]["dropped"] == 3
        await link.close()

    run(scenario())


def test_an_out_of_order_sample_is_counted_and_ignored() -> None:
    async def scenario():
        session = FakeSession()
        link = SensorLink(session)
        await link.refresh()
        await link.start([1])
        link.push(body(1, 10))
        link.push(body(1, 8))
        await asyncio.sleep(0.01)
        assert link.snapshot().stats["1"]["out_of_order"] == 1
        assert link.reading(1).seq == 10
        await link.close()

    run(scenario())


def test_the_sequence_wraps_without_looking_like_a_rewind() -> None:
    async def scenario():
        session = FakeSession()
        link = SensorLink(session)
        await link.refresh()
        await link.start([1])
        link.push(body(1, 65535))
        link.push(body(1, 0))
        assert await wait_for(lambda: seq_is(link, 1, 0))
        stats = link.snapshot().stats["1"]
        assert stats["out_of_order"] == 0
        assert stats["duplicates"] == 0
        await link.close()

    run(scenario())


def test_two_sensors_keep_separate_counters_and_readings() -> None:
    async def scenario():
        session = FakeSession()
        link = SensorLink(session)
        await link.refresh()
        await link.start([1, 2])
        link.push(body(1, 0))
        link.push(body(2, 0, kind=4, values=(0.01, 0.02, 0.03)))
        assert await wait_for(lambda: link.reading(2) is not None)
        link.push(body(1, 1))
        await asyncio.sleep(0.01)
        assert link.reading(1).seq == 1
        assert link.reading(2).seq == 0, "one sensor's frame must not advance another"
        await link.close()

    run(scenario())


def test_an_invalid_frame_is_counted_and_the_stream_survives() -> None:
    async def scenario():
        session = FakeSession()
        link = SensorLink(session)
        await link.refresh()
        await link.start([1])
        link.push(b"\x00" * 4)
        assert await wait_for(lambda: link.snapshot().stats.get("0", {}).get("invalid") == 1)
        link.push(body(1, 0))
        assert await wait_for(lambda: link.reading(1) is not None)
        assert link.snapshot().state == ACTIVE
        await link.close()

    run(scenario())


def test_a_full_queue_drops_the_oldest_frame() -> None:
    async def scenario():
        session = FakeSession()
        link = SensorLink(session, queue_frames=4)
        await link.refresh()
        await link.start([1])
        drops = [link.push(body(1, index)) for index in range(10)]
        assert any(drops), "a burst larger than the queue must report a drop"
        assert await wait_for(lambda: link.reading(1) is not None)
        await link.close()

    run(scenario())


def test_the_end_of_the_stream_ends_the_stream() -> None:
    async def scenario():
        session = FakeSession()
        link = SensorLink(session)
        await link.refresh()
        await link.start([1])
        link.push(None)
        assert link.snapshot().state == IDLE
        assert "closed the sensor stream" in link.snapshot().detail

    run(scenario())


def test_a_stopped_stream_ignores_late_frames() -> None:
    async def scenario():
        session = FakeSession()
        link = SensorLink(session)
        await link.refresh()
        await link.start([1])
        await link.stop("done")
        before = link.snapshot().stats
        link.push(body(1, 0))
        after = link.snapshot().stats
        assert after == before, "a frame after stop must not resurrect the stream"
        assert link.snapshot().state == IDLE

    run(scenario())


def test_a_broken_callback_does_not_break_the_stream() -> None:
    def exploding(_snapshot) -> None:
        raise RuntimeError("the UI thread is gone")

    async def scenario():
        session = FakeSession()
        link = SensorLink(session, on_change=exploding)
        await link.refresh()
        await link.start([1])
        link.push(body(1, 0))
        assert await wait_for(lambda: link.reading(1) is not None)
        await link.close()

    run(scenario())


def test_the_stream_subscribes_to_its_own_stream_id() -> None:
    async def scenario():
        session = FakeSession()
        link = SensorLink(session)
        await link.refresh()
        await link.start([1])
        assert SENSOR_STREAM in session.queues
        assert SENSOR_STREAM == 4, "sensor frames own stream 4 (docs/03)"
        await link.close()

    run(scenario())
