"""Sensor frames: the wire format for live readings from the phone (Phase 5).

One message is one reading from one sensor. That keeps the design as boring as
the audio and camera planes it sits beside:

* **One message, one sample.** A lost message loses a sample; nothing has to be
  reassembled, and a reconnect costs nothing.
* **The phone's clock is the timestamp.** ``ts_ms`` is milliseconds since the
  stream started, not a wall-clock time, so a PC with a wrong clock still shows
  correct *intervals*. The PC never invents a timestamp of its own.
* **Sequence numbers are per sensor**, because sensors are independent: the
  accelerometer at 50 Hz and the light sensor at 1 Hz share a stream but not a
  counter. A gap in one sensor says nothing about another.
* **Readings are float32 big-endian**, the widest type Android's
  ``SensorEvent.values`` uses. Integers are sent as float32 too, exactly as
  Android reports them (a step counter at 8192 steps is lossless in float32 up to
  16.7 million).

Unknown sensor kinds are *carried*, not guessed: the header says which Android
sensor type produced a frame, and the PC maps the id to a name from
``sensor.list``. A PC that meets a sensor it has no name for still shows the
numbers under the vendor name the phone reported.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Any

MSG_SENSOR = 0x60

#: Sensor frames from the phone (docs/03): 4 = sensors, fixed.
SENSOR_STREAM = 4

#: ``u16 sensor_id | u16 seq | u32 ts_ms | u8 kind | u8 count | u16 reserved``.
HEADER = struct.Struct(">HHIBBH")
HEADER_SIZE = HEADER.size  # 12

#: Android ``Sensor.TYPE_*`` values. Kept as numbers on the wire so a new phone
#: sensor does not need a protocol change to be reported usefully.
KINDS: dict[int, tuple[str, tuple[str, ...], str]] = {
    1: ("accelerometer", ("x", "y", "z"), "m/s²"),
    2: ("magnetometer", ("x", "y", "z"), "µT"),
    3: ("orientation", ("azimuth", "pitch", "roll"), "°"),
    4: ("gyroscope", ("x", "y", "z"), "rad/s"),
    5: ("light", ("lux",), "lx"),
    6: ("pressure", ("hPa",), "hPa"),
    8: ("proximity", ("distance",), "cm"),
    9: ("gravity", ("x", "y", "z"), "m/s²"),
    10: ("linear_acceleration", ("x", "y", "z"), "m/s²"),
    11: ("rotation_vector", ("x", "y", "z", "w"), ""),
    12: ("relative_humidity", ("percent",), "%"),
    13: ("ambient_temperature", ("°C",), "°C"),
    14: ("magnetic_field_uncalibrated", ("x", "y", "z"), "µT"),
    15: ("game_rotation_vector", ("x", "y", "z", "w"), ""),
    16: ("gyroscope_uncalibrated", ("x", "y", "z"), "rad/s"),
    18: ("step_detector", ("event",), ""),
    19: ("step_counter", ("steps",), "steps"),
    20: ("geomagnetic_rotation_vector", ("x", "y", "z", "w"), ""),
    21: ("heart_rate", ("bpm",), "bpm"),
    22: ("pose_6dof", ("x", "y", "z", "w"), ""),
    28: ("pose_6dof", ("x", "y", "z", "w"), ""),
    29: ("hinge_angle", ("degrees",), "°"),
}

#: The kinds the phone is expected to offer first, in the order the GUI lists them.
PREFERRED_ORDER: tuple[int, ...] = (
    1, 4, 2, 9, 10, 11, 15, 3, 8, 5, 19, 18,
)

#: Half a second of 100 Hz data: enough to ride out a hiccup, small enough that a
#: slow consumer cannot build a backlog behind the numbers on screen.
QUEUE_FRAMES = 64

MAX_SENSORS_PER_FRAME = 16


class SensorFrameError(ValueError):
    """A sensor frame that cannot be trusted."""


def kind_name(kind: int) -> str:
    entry = KINDS.get(kind)
    return entry[0] if entry else f"sensor_{kind}"


def kind_axes(kind: int) -> tuple[str, ...]:
    entry = KINDS.get(kind)
    return entry[1] if entry else ("value",)


def kind_unit(kind: int) -> str:
    entry = KINDS.get(kind)
    return entry[2] if entry else ""


@dataclass
class SensorInfo:
    """One sensor the phone offers, as ``sensor.list`` reported it."""

    id: int
    kind: int
    name: str = ""
    vendor: str = ""
    min_delay_us: int = 0
    max_delay_us: int = 0
    max_range: float = 0.0
    resolution: float = 0.0
    power_ma: float = 0.0

    @property
    def label(self) -> str:
        return self.name or kind_name(self.kind)

    @property
    def axes(self) -> tuple[str, ...]:
        return kind_axes(self.kind)

    @property
    def unit(self) -> str:
        return kind_unit(self.kind)

    @property
    def max_hz(self) -> int:
        """The fastest rate this sensor advertises, or 0 when it does not say."""
        return int(1_000_000 / self.min_delay_us) if self.min_delay_us > 0 else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "label": self.label,
            "axes": list(self.axes),
            "unit": self.unit,
            "vendor": self.vendor,
            "min_delay_us": self.min_delay_us,
            "max_delay_us": self.max_delay_us,
            "max_range": self.max_range,
            "resolution": self.resolution,
            "power_ma": self.power_ma,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SensorInfo:
        def as_int(value: Any, default: int = 0) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def as_float(value: Any, default: float = 0.0) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        return cls(
            id=as_int(data.get("id")),
            kind=as_int(data.get("kind")),
            name=str(data.get("name") or ""),
            vendor=str(data.get("vendor") or ""),
            min_delay_us=as_int(data.get("min_delay_us")),
            max_delay_us=as_int(data.get("max_delay_us")),
            max_range=as_float(data.get("max_range")),
            resolution=as_float(data.get("resolution")),
            power_ma=as_float(data.get("power_ma")),
        )


@dataclass
class SensorFrame:
    """One reading: which sensor, which sample, when, and the values."""

    sensor_id: int
    seq: int
    timestamp_ms: int
    kind: int
    values: tuple[float, ...]
    flags: int = 0

    def encode(self) -> bytes:
        if not 0 <= self.sensor_id <= 0xFFFF:
            raise SensorFrameError(f"sensor id out of range: {self.sensor_id}")
        if not 0 <= self.seq <= 0xFFFF:
            raise SensorFrameError(f"sequence out of range: {self.seq}")
        if not 0 <= self.timestamp_ms <= 0xFFFFFFFF:
            raise SensorFrameError(f"timestamp out of range: {self.timestamp_ms}")
        if not 0 <= self.kind <= 0xFF:
            raise SensorFrameError(f"sensor kind does not fit the header: {self.kind}")
        if not 1 <= len(self.values) <= MAX_SENSORS_PER_FRAME:
            raise SensorFrameError(f"a frame carries 1-{MAX_SENSORS_PER_FRAME} values")
        for value in self.values:
            if math.isnan(value) or math.isinf(value):
                raise SensorFrameError(f"refusing to send {value!r}")
        payload = struct.pack(f">{len(self.values)}f", *self.values)
        header = HEADER.pack(
            self.sensor_id, self.seq, self.timestamp_ms, self.kind, len(self.values), 0
        )
        return header + payload

    @property
    def reading(self) -> dict[str, float]:
        """Axis name -> value, the shape a GUI wants."""
        axes = kind_axes(self.kind)
        if len(axes) == len(self.values):
            return dict(zip(axes, self.values, strict=False))
        return {f"v{index}": value for index, value in enumerate(self.values)}


def decode_frame(body: bytes) -> SensorFrame:
    """Parse one ``MSG_SENSOR`` payload, strictly but without ever raising fatally."""
    if len(body) < HEADER_SIZE:
        raise SensorFrameError(f"sensor frame shorter than its header: {len(body)} bytes")
    sensor_id, seq, ts_ms, kind, count, _reserved = HEADER.unpack_from(body, 0)
    if not 1 <= count <= MAX_SENSORS_PER_FRAME:
        raise SensorFrameError(f"impossible value count: {count}")
    expected = HEADER_SIZE + 4 * count
    if len(body) != expected:
        raise SensorFrameError(f"sensor frame is {len(body)} bytes, header says {expected}")
    values = struct.unpack_from(f">{count}f", body, HEADER_SIZE)
    for value in values:
        if math.isnan(value) or math.isinf(value):
            raise SensorFrameError(f"non-finite reading: {value!r}")
    return SensorFrame(
        sensor_id=sensor_id,
        seq=seq,
        timestamp_ms=ts_ms,
        kind=kind,
        values=tuple(float(value) for value in values),
    )


@dataclass
class SensorStats:
    """Counters for one sensor's stream, and the last problem seen."""

    received: int = 0
    dropped: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    invalid: int = 0
    last_seq: int = -1
    last_timestamp_ms: int = 0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "dropped": self.dropped,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "invalid": self.invalid,
            "last_seq": self.last_seq,
            "last_timestamp_ms": self.last_timestamp_ms,
            "last_error": self.last_error,
        }


@dataclass
class SensorReading:
    """The newest sample from one sensor, plus how fast it is arriving."""

    sensor_id: int
    kind: int
    seq: int
    timestamp_ms: int
    values: tuple[float, ...] = ()
    hz: float = 0.0

    @property
    def reading(self) -> dict[str, float]:
        return SensorFrame(
            sensor_id=self.sensor_id,
            seq=self.seq,
            timestamp_ms=self.timestamp_ms,
            kind=self.kind,
            values=self.values,
        ).reading

    def to_dict(self) -> dict[str, Any]:
        return {
            "sensor_id": self.sensor_id,
            "kind": self.kind,
            "name": kind_name(self.kind),
            "seq": self.seq,
            "timestamp_ms": self.timestamp_ms,
            "values": list(self.values),
            "reading": self.reading,
            "unit": kind_unit(self.kind),
            "hz": round(self.hz, 2),
        }


@dataclass
class SensorRegistry:
    """Everything the PC knows about the phone's sensors in this session."""

    sensors: tuple[SensorInfo, ...] = ()
    active: tuple[int, ...] = ()
    interval_us: int = 20000
    permitted: bool | None = None
    stats: dict[int, SensorStats] = field(default_factory=dict)
    readings: dict[int, SensorReading] = field(default_factory=dict)
    detail: str = ""

    def by_id(self, sensor_id: int) -> SensorInfo | None:
        return next((s for s in self.sensors if s.id == sensor_id), None)

    def name_for(self, sensor_id: int) -> str:
        info = self.by_id(sensor_id)
        return info.label if info else f"sensor {sensor_id}"

    def ordered(self) -> tuple[SensorInfo, ...]:
        """Preferred kinds first (what people actually want to see), then the rest."""
        def rank(info: SensorInfo) -> tuple[int, int, str]:
            try:
                return (0, PREFERRED_ORDER.index(info.kind), info.label)
            except ValueError:
                return (1, info.kind, info.label)

        return tuple(sorted(self.sensors, key=rank))

    def to_dict(self) -> dict[str, Any]:
        return {
            "sensors": [info.to_dict() for info in self.sensors],
            "active": list(self.active),
            "interval_us": self.interval_us,
            "permitted": self.permitted,
            "detail": self.detail,
            "readings": {str(k): v.to_dict() for k, v in self.readings.items()},
            "stats": {str(k): v.to_dict() for k, v in self.stats.items()},
        }

    @classmethod
    def from_list_reply(cls, reply: Any, *, interval_us: int | None = None) -> SensorRegistry:
        """Build from a ``sensor.list`` result, tolerating a partial answer."""
        payload = reply if isinstance(reply, dict) else {}
        raw = payload.get("sensors")
        sensors: list[SensorInfo] = []
        if isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, dict):
                    try:
                        sensors.append(SensorInfo.from_dict(entry))
                    except Exception:  # noqa: BLE001 - one bad row must not hide the rest
                        continue
        permitted = payload.get("permitted")
        registry = cls(
            sensors=tuple(sensors),
            interval_us=interval_us or int(payload.get("interval_us") or 20000),
            permitted=permitted if isinstance(permitted, bool) else None,
            detail=str(payload.get("detail") or ""),
        )
        registry.stats = {info.id: SensorStats() for info in registry.sensors}
        return registry
