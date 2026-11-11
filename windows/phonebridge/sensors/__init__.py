"""Phase 5 sensors: the phone's hardware sensors, live on the PC.

Public surface:

* :mod:`~phonebridge.sensors.frames` — the ``MSG_SENSOR`` wire format and the
  sensor metadata model (no I/O, no asyncio).
* :mod:`~phonebridge.sensors.link` — :class:`SensorLink`, which lists, starts and
  stops phone sensors and holds the newest reading of each.

Nothing here opens a socket. Sensor frames ride the session the connection
manager authenticated; if that session ends, the stream ends with it.
"""

from __future__ import annotations

from .frames import (
    HEADER_SIZE,
    KINDS,
    MSG_SENSOR,
    PREFERRED_ORDER,
    QUEUE_FRAMES,
    SENSOR_STREAM,
    SensorFrame,
    SensorFrameError,
    SensorInfo,
    SensorReading,
    SensorRegistry,
    SensorStats,
    decode_frame,
    kind_axes,
    kind_name,
    kind_unit,
)
from .link import (
    ACTIVE,
    DEFAULT_INTERVAL_US,
    ERROR,
    IDLE,
    INTERVAL_CHOICES_US,
    STARTING,
    STOPPING,
    SensorError,
    SensorLink,
    SensorSnapshot,
)

__all__ = [
    "ACTIVE",
    "DEFAULT_INTERVAL_US",
    "ERROR",
    "HEADER_SIZE",
    "IDLE",
    "INTERVAL_CHOICES_US",
    "KINDS",
    "MSG_SENSOR",
    "PREFERRED_ORDER",
    "QUEUE_FRAMES",
    "SENSOR_STREAM",
    "STARTING",
    "STOPPING",
    "SensorError",
    "SensorFrame",
    "SensorFrameError",
    "SensorInfo",
    "SensorLink",
    "SensorReading",
    "SensorRegistry",
    "SensorSnapshot",
    "SensorStats",
    "decode_frame",
    "kind_axes",
    "kind_name",
    "kind_unit",
]
