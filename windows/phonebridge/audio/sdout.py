"""Writing raw PCM16 into one Windows output device through ``sounddevice``.

Two contracts are easy to get wrong, and both were:

* ``sounddevice.OutputStream`` writes **numpy arrays of exactly its dtype**. Given
  the bytes that come off the wire it raises
  ``dtype mismatch: 'bytes15360' vs 'int16'`` — one exception per frame, and the
  microphone path is dead before the call starts. ``RawOutputStream`` takes bytes
  and does no conversion, which is what a byte-oriented pipeline needs.
* a device's **channel count is the device's**, not the stream's. "CABLE Input" is
  a stereo endpoint; the phone's microphone is mono. Opening the endpoint with the
  session's channel count either fails or plays at half speed, so the count is
  negotiated from what the device reports and the frames are re-channelled.

The sample rate is negotiated the same way: a device that will not take 48 kHz is
opened at the rate it does take, and the frames are resampled, so the *duration*
still matches what the phone captured.
"""

from __future__ import annotations

from typing import Any

from .convert import LinearResampler, remix
from .frames import AudioFormat

__all__ = ["SdOutputError", "SoundDeviceOutput"]


class SdOutputError(RuntimeError):
    """The device could not be opened or written to; the message is for a user."""


def _import_sounddevice() -> Any:
    try:
        import sounddevice  # noqa: PLC0415 - optional extra
    except Exception as exc:  # noqa: BLE001 - any import failure means "not installed"
        raise SdOutputError(
            "writing the phone microphone into a Windows device needs sounddevice"
        ) from exc
    return sounddevice


def _device_info(sd: Any, device: str | int | None) -> dict[str, Any]:
    try:
        info = sd.query_devices(device, "output")
    except Exception as exc:  # noqa: BLE001 - PortAudio raises several types
        raise SdOutputError(f"the device could not be inspected: {exc}") from exc
    return dict(info)


def negotiate(sd: Any, device: str | int | None, fmt: AudioFormat) -> tuple[int, int]:
    """Pick a ``(samplerate, channels)`` pair the device says it accepts.

    Channels come from the device: a stereo endpoint is opened as stereo and mono
    frames are duplicated, because that is what the endpoint's mixer expects.
    ``sd.check_output_settings`` is the library's own answer to "will this open?",
    so it is used instead of guessing from the device name.
    """
    info = _device_info(sd, device)
    max_out = int(info.get("max_output_channels") or 0)
    device_rate = int(info.get("default_samplerate") or 0)

    channels = min(2, max_out) if max_out >= 1 else fmt.channels
    candidates: list[tuple[int, int]] = []
    for rate in (fmt.sample_rate, device_rate):
        for count in (channels, 1):
            pair = (int(rate or fmt.sample_rate), count)
            if pair not in candidates:
                candidates.append(pair)

    for rate, count in candidates:
        try:
            sd.check_output_settings(device=device, samplerate=rate, channels=count, dtype="int16")
        except Exception:  # noqa: BLE001 - "no" is an answer, not an error
            continue
        return rate, count

    tried = ", ".join(f"{rate} Hz/{count} ch" for rate, count in candidates)
    raise SdOutputError(
        f"the output device accepts none of these formats: {tried}. "
        f"PhoneBridge sends PCM16 at {fmt.sample_rate} Hz; set the device's default "
        "format to 48 kHz in Windows sound settings (or in the VB-CABLE control "
        "panel) and try again"
    )


class SoundDeviceOutput:
    """One opened output stream, fed with the bytes that arrive from the phone."""

    def __init__(
        self,
        fmt: AudioFormat,
        device: str | int | None = None,
        *,
        sd: Any = None,
        latency: float = 0.0,
    ) -> None:
        self.fmt = fmt
        self.device = device
        self._sd = sd if sd is not None else _import_sounddevice()

        self.samplerate, self.channels = negotiate(self._sd, device, fmt)
        self._resampler = (
            LinearResampler(fmt.sample_rate, self.samplerate)
            if self.samplerate != fmt.sample_rate
            else None
        )

        # One frame worth of samples at the *device* rate; zero (let PortAudio
        # choose) when resampling, because the output length then varies by a
        # sample and a fixed block size would refuse it.
        blocksize = 0
        if self._resampler is None:
            blocksize = max(1, self.samplerate * fmt.frame_ms // 1000)

        options: dict[str, Any] = {
            "samplerate": self.samplerate,
            "channels": self.channels,
            "dtype": "int16",  # RawOutputStream: bytes in, no conversion by us
            "device": device,
            "blocksize": blocksize,
        }
        if latency:
            options["latency"] = latency
        try:
            self._stream = self._sd.RawOutputStream(**options)
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 - PortAudio raises several types
            raise SdOutputError(f"could not open the output device: {exc}") from exc

    @property
    def resampled(self) -> bool:
        return self._resampler is not None

    def write(self, payload: bytes) -> None:
        if not payload:
            return
        if self._stream is None:
            raise SdOutputError("the output device was closed")
        pcm = remix(payload, self.fmt.channels, self.channels)
        if self._resampler is not None:
            pcm = self._resampler.process(pcm)
        if not pcm:
            return
        try:
            self._stream.write(pcm)
        except Exception as exc:  # noqa: BLE001
            raise SdOutputError(str(exc)) from exc

    def close(self) -> None:
        stream, self._stream = getattr(self, "_stream", None), None
        if stream is None:
            return
        for method in ("stop", "close"):
            try:
                getattr(stream, method)()
            except Exception:  # noqa: BLE001, S110 - closing must never raise
                pass
