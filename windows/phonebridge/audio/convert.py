"""Channel and rate conversion for 16-bit PCM, with no third-party dependency.

Both directions of the audio plane carry interleaved PCM16 and both have to meet
a device whose shape is not the session's:

* the phone's microphone is **mono**, while "CABLE Input" is a **stereo**
  endpoint. A mono frame written into a two-channel stream plays at half speed
  (or the stream refuses to open at all), so it has to be duplicated per channel;
* a WASAPI loopback device runs at the **system mix rate** — 44.1 kHz on plenty
  of PCs — while the protocol is 48 kHz. Capturing 44.1 kHz and labelling the
  samples 48 kHz plays ~9 % fast, which is the "chipmunk" defect, not a click.

Linear interpolation is a deliberate choice: it is a dozen lines, it keeps the
duration exact (which is what every downstream clock cares about), and at an
8-9 % ratio on speech the error it adds is far below what the phone's speaker or
a Bluetooth link contributes. It is never applied when the rates already match,
which is the normal case.
"""

from __future__ import annotations

from array import array

__all__ = ["LinearResampler", "remix", "samples"]

#: One C ``short``: the wire format is little-endian PCM16 and so is ``array('h')``
#: on every platform Python runs on.
SAMPLE_BYTES = 2


def samples(payload: bytes, channels: int = 1) -> array:
    """PCM16 bytes as an ``array('h')``, trimmed to a whole number of frames."""
    if not payload:
        return array("h")
    usable = len(payload) - (len(payload) % (SAMPLE_BYTES * max(1, channels)))
    out = array("h")
    out.frombytes(payload[:usable])
    return out


def _clamp(value: float) -> int:
    return max(-32768, min(32767, int(round(value))))


def remix(payload: bytes, source_channels: int, target_channels: int) -> bytes:
    """Re-channel interleaved PCM16, or return it unchanged.

    Supportd and tested shapes: 1 <-> 2, 2 -> 1, 1 -> n. Any other combination is
    returned untouched rather than guessed at: a wrong re-channel is a pitch and
    speed defect that is very hard to hear as the bug it is.
    """
    if not payload or source_channels == target_channels or source_channels < 1:
        return payload
    if target_channels < 1:
        return payload

    mono = samples(payload, source_channels)

    if target_channels == 1:
        if source_channels == 2:
            return array("h", ((mono[i] + mono[i + 1]) // 2 for i in range(0, len(mono) - 1, 2))).tobytes()
        width = source_channels
        return array(
            "h", (sum(mono[i : i + width]) // width for i in range(0, len(mono) - width + 1, width))
        ).tobytes()

    if source_channels == 1:
        return array("h", (sample for sample in mono for _ in range(target_channels))).tobytes()

    # 2 -> n (n > 2): duplicate each frame's channels round-robin.
    if source_channels == 2:
        out = array("h")
        for index in range(0, len(mono) - 1, 2):
            for channel in range(target_channels):
                out.append(mono[index + (channel % 2)])
        return out.tobytes()

    return payload


class LinearResampler:
    """Stateful linear-interpolation resampler for mono PCM16.

    Stateful on purpose: the phone writes one frame at a time, so a stateless
    converter would restart the interpolation at every frame boundary and add a
    click at 12.5 Hz.
    """

    def __init__(self, source_rate: int, target_rate: int) -> None:
        if source_rate <= 0 or target_rate <= 0:
            raise ValueError(f"resampling needs positive rates, got {source_rate}->{target_rate}")
        self.source_rate = int(source_rate)
        self.target_rate = int(target_rate)
        self._step = self.source_rate / self.target_rate
        self._position = 0.0
        self._tail = array("h")

    @property
    def identity(self) -> bool:
        return self.source_rate == self.target_rate

    def reset(self) -> None:
        self._position = 0.0
        self._tail = array("h")

    def process(self, payload: bytes) -> bytes:
        """Consume one chunk, return however many samples that chunk is worth."""
        if self.identity or not payload:
            return payload
        chunk = samples(payload)
        if not chunk:
            return b""
        buffered = self._tail + chunk
        if len(buffered) < 2:
            self._tail = buffered
            return b""

        limit = len(buffered) - 1
        position = self._position
        out = array("h")
        while position < limit:
            index = int(position)
            fraction = position - index
            value = buffered[index] * (1.0 - fraction) + buffered[index + 1] * fraction
            out.append(_clamp(value))
            position += self._step

        consumed = int(position)
        self._position = position - consumed
        self._tail = buffered[consumed:]
        return out.tobytes()
