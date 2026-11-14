"""A sine wave where PC audio would normally come from.

The speaker path is usually verified by playing something on the PC and asking
the user whether the phone made a noise. That test depends on WASAPI loopback, on
the PC having an output device, on something actually being played, and on the
loopback device being the one the sound came out of — four ways to fail that all
look identical from the phone's side (silence).

:class:`ToneSource` removes all four. It is an :class:`~phonebridge.audio.backends.AudioSource`
that produces 440 Hz PCM16 for a fixed number of seconds and nothing afterwards,
so ``phonebridge speaker --test-tone`` exercises the *phone* — its AudioTrack, its
route, its volume — with no capture device on the PC at all.

It is a real source, not a bypass: frames travel through
:class:`~phonebridge.audio.link.AudioLink`'s queue, the encrypted session and the
same protocol frames PC audio uses. A tone that arrives proves the whole path;
a tone that does not arrive proves the phone or the link, and nothing else.

The end of the tone is *reported*, not signalled by an exception: ``read()``
returns ``b""`` (the link's documented "less than a frame" case) after pacing one
frame period, so the capture loop neither spins nor treats a finished test as a
device failure.
"""

from __future__ import annotations

import math
import sys
import time
from array import array
from collections.abc import Callable

from .frames import AudioFormat

#: The A above middle C. Audible on every phone speaker and every headset, and far
#: from both the DC thump and the ultrasonic filtering some routes apply.
DEFAULT_TONE_HZ = 440.0

#: Long enough to hear a continuous note and to watch the status line update a few
#: times; short enough that nobody has to wait for it.
DEFAULT_TONE_SECONDS = 3.0

#: -12 dBFS. Loud enough to be unmistakable, quiet enough not to clip after the
#: phone's own volume scaling or a Bluetooth codec's AGC.
DEFAULT_TONE_AMPLITUDE = 0.25

_TAU = 2.0 * math.pi


class ToneSource:
    """Exactly ``seconds`` of a sine wave, one frame per :meth:`read`."""

    def __init__(
        self,
        fmt: AudioFormat,
        *,
        frequency: float = DEFAULT_TONE_HZ,
        seconds: float = DEFAULT_TONE_SECONDS,
        amplitude: float = DEFAULT_TONE_AMPLITUDE,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 20.0 <= frequency <= 20_000.0:
            raise ValueError(f"tone frequency out of range: {frequency}")
        if not 0.0 < seconds <= 60.0:
            raise ValueError(f"tone length out of range: {seconds} s")
        if not 0.0 < amplitude <= 1.0:
            raise ValueError(f"tone amplitude out of range: {amplitude}")
        self.fmt = fmt
        self.frequency = float(frequency)
        self.seconds = float(seconds)
        self.amplitude = float(amplitude)
        #: Frames the tone will produce before it goes quiet.
        self.frames_total = max(1, int(round(self.seconds * 1000.0 / fmt.frame_ms)))
        self._clock = clock
        self._sleep = sleep
        self._next_at: float | None = None
        self._frames = 0
        self._phase = 0.0
        self._done = False
        self._closed = False

    # ------------------------------------------------------------------ #
    # AudioSource
    # ------------------------------------------------------------------ #
    def read(self) -> bytes:
        """One frame of the tone, or ``b""`` once it has all been produced.

        Paced to one frame per frame period, like a capture device. That is not
        politeness: the phone buffers four 20 ms frames and drops the *oldest*
        when its queue is full, so a source that handed over three seconds of
        audio in thirty milliseconds would be heard as a click at the end of the
        note, not as a tone. The empty read is paced for the other reason — the
        link's capture loop treats ``b""`` as "the device had less than a frame,
        read again", and an unpaced one would spin a core for as long as the
        session stayed up.
        """
        self._pace()
        if self._closed or self._frames >= self.frames_total:
            self._done = True
            return b""
        pcm = self._frame()
        self._frames += 1
        if self._frames >= self.frames_total:
            self._done = True
        return pcm

    def _pace(self) -> None:
        """Wait until the next frame is due, on an absolute schedule.

        Absolute rather than "sleep a frame each time" because Windows rounds a
        sleep up to its 15.6 ms timer: 150 of those would turn a 3 s tone into
        4.7 s. A slow sleep moves the schedule forward instead of accumulating.
        """
        period = self.fmt.frame_ms / 1000.0
        now = self._clock()
        if self._next_at is None:
            self._next_at = now + period
            return
        delay = self._next_at - now
        if delay > 0:
            self._sleep(delay)
            now += delay
        self._next_at = max(self._next_at + period, now)

    def close(self) -> None:
        self._closed = True

    # ------------------------------------------------------------------ #
    # observation
    # ------------------------------------------------------------------ #
    @property
    def finished(self) -> bool:
        """True once every frame of the tone has been handed out."""
        return self._done

    @property
    def frames_sent(self) -> int:
        return self._frames

    @property
    def duration_ms(self) -> int:
        return self.frames_total * self.fmt.frame_ms

    # ------------------------------------------------------------------ #
    # the wave
    # ------------------------------------------------------------------ #
    def _frame(self) -> bytes:
        samples = self.fmt.samples_per_frame
        step = _TAU * self.frequency / self.fmt.sample_rate
        peak = self.amplitude * 32767.0
        mono = array("h")
        append = mono.append
        phase = self._phase
        for _ in range(samples):
            append(int(round(peak * math.sin(phase))))
            phase += step
            if phase >= _TAU:  # keep the phase bounded without a modulo per sample
                phase -= _TAU
        self._phase = phase
        out = mono
        if self.fmt.channels == 2:
            # Duplicate each sample into left and right rather than generating
            # two phases: a stereo tone must not be a stereo *image*.
            stereo = array("h")
            extend = stereo.extend
            for sample in mono:
                extend((sample, sample))
            out = stereo
        if sys.byteorder == "big":  # the wire is PCM16**LE**
            out.byteswap()
        return out.tobytes()
