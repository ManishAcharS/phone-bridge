"""``phonebridge camera|mic|speaker|start`` — the Windows delivery paths.

These commands are the shortest honest path from "the phone has a camera" to
"Zoom lists a camera":

    phonebridge camera --virtual      phone camera -> OBS Virtual Camera
    phonebridge mic --to-cable        phone mic    -> CABLE Input (VB-CABLE)
    phonebridge speaker               PC audio     -> phone -> Bluetooth
    phonebridge start --camera --mic --speaker
                                      all three at once, one session, one status line

Everything here rides the existing authenticated session from
:func:`phonebridge.cli.target.open_session`: pairing, encryption, framing and
reconnection are untouched. A missing driver is reported as an instruction
(OBS, VB-CABLE, PyAudioWPatch) rather than as an exception.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from typing import Any

from ..live.config import (
    VIRTUAL_CAMERA_AUTO,
    VIRTUAL_CAMERA_BACKENDS,
    CameraPlan,
    ConfigError,
    LivePlan,
    MicPlan,
    SpeakerPlan,
)
from ..live.devices import OBS_DOWNLOAD_URL, VB_CABLE_URL, DeviceUnavailable
from ..live.runner import STATUS_INTERVAL_S, LiveRunner
from .target import EXIT_ERROR, EXIT_USAGE, CliError, note, open_session


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def add_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from .main import _add_format_args, _add_target_args

    camera = sub.add_parser(
        "camera",
        help="deliver the phone camera to a Windows webcam device",
        description=(
            "Streams the phone's camera over the authenticated session and feeds it "
            "to a virtual camera device, so Zoom, Meet, OBS and browsers select it as "
            "an ordinary webcam. Requires OBS Studio (for its virtual camera) or "
            "Unity Capture."
        ),
    )
    _add_target_args(camera)
    _add_format_args(camera, default="text")
    camera.add_argument(
        "--virtual", action="store_true", help="publish to the Windows virtual camera (required)"
    )
    camera.add_argument("--width", type=int, default=1280)
    camera.add_argument("--height", type=int, default=720)
    camera.add_argument("--fps", type=int, default=30)
    camera.add_argument("--quality", type=int, default=70, metavar="JPEG")
    camera.add_argument("--facing", choices=("back", "front"), default="back")
    camera.add_argument(
        "--backend",
        choices=(*VIRTUAL_CAMERA_BACKENDS, VIRTUAL_CAMERA_AUTO),
        default=VIRTUAL_CAMERA_AUTO,
        help="virtual camera backend",
    )
    camera.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help="stop after this long (default: until Ctrl+C)",
    )
    camera.add_argument("--no-status", action="store_true", help="do not print the status line")

    mic = sub.add_parser(
        "mic",
        help="deliver the phone microphone into VB-CABLE",
        description=(
            "Writes the phone's microphone into a VB-CABLE playback device, so other "
            'applications select "CABLE Output" as their microphone. Nothing is '
            "installed for you: VB-CABLE is a signed driver and must be installed by hand."
        ),
    )
    _add_target_args(mic)
    _add_format_args(mic, default="text")
    mic.add_argument(
        "--to-cable", action="store_true", help="write into the VB-CABLE device (required)"
    )
    mic.add_argument(
        "--out-device",
        dest="out_device",
        default="CABLE Input",
        metavar="NAME",
        help="playback device name or substring (default: CABLE Input)",
    )
    mic.add_argument(
        "--latency",
        dest="latency_ms",
        type=int,
        default=120,
        metavar="MS",
        help="jitter buffer depth in ms (default 120)",
    )
    mic.add_argument("--gain", type=float, default=1.0, help="input gain, 0-4 (default 1)")
    mic.add_argument("--duration", type=float, default=None, metavar="SECONDS")
    mic.add_argument("--no-status", action="store_true")

    speaker = sub.add_parser(
        "speaker",
        help="play PC audio on the phone (and its Bluetooth headphones)",
        description=(
            "Captures what this PC is playing with WASAPI loopback and sends it to the "
            "phone, which plays it with AudioTrack in its foreground service. Android "
            "routes that to connected Bluetooth headphones itself; PhoneBridge does not "
            "pair anything."
        ),
    )
    _add_target_args(speaker)
    _add_format_args(speaker, default="text")
    speaker.add_argument(
        "--latency",
        dest="latency_ms",
        type=int,
        default=200,
        metavar="MS",
        help="buffer sent to the phone in ms (default 200)",
    )
    speaker.add_argument(
        "--lipsync",
        dest="lipsync_ms",
        type=int,
        default=0,
        metavar="MS",
        help="delay the camera by this much so picture matches audio",
    )
    speaker.add_argument(
        "--loopback",
        dest="loopback_device",
        default="",
        metavar="NAME",
        help="loopback device name substring (default: the default output)",
    )
    speaker.add_argument("--duration", type=float, default=None, metavar="SECONDS")
    speaker.add_argument("--no-status", action="store_true")

    start = sub.add_parser(
        "start",
        help="run camera, microphone and speaker together on one session",
        description="The whole bridge, one command, one status line.",
    )
    _add_target_args(start)
    _add_format_args(start, default="text")
    start.add_argument("--camera", action="store_true")
    start.add_argument("--mic", action="store_true")
    start.add_argument("--speaker", action="store_true")
    start.add_argument("--width", type=int, default=1280)
    start.add_argument("--height", type=int, default=720)
    start.add_argument("--fps", type=int, default=30)
    start.add_argument("--quality", type=int, default=70, metavar="JPEG")
    start.add_argument("--facing", choices=("back", "front"), default="back")
    start.add_argument(
        "--backend",
        choices=(*VIRTUAL_CAMERA_BACKENDS, VIRTUAL_CAMERA_AUTO),
        default=VIRTUAL_CAMERA_AUTO,
    )
    start.add_argument("--cable", default="CABLE Input", metavar="NAME")
    start.add_argument("--mic-latency", dest="mic_latency_ms", type=int, default=120, metavar="MS")
    start.add_argument(
        "--speaker-latency", dest="speaker_latency_ms", type=int, default=200, metavar="MS"
    )
    start.add_argument("--lipsync", dest="lipsync_ms", type=int, default=0, metavar="MS")
    start.add_argument("--duration", type=float, default=None, metavar="SECONDS")
    start.add_argument("--no-status", action="store_true")


def plan_from_args(args: argparse.Namespace) -> LivePlan:
    """Turn one parsed command line into a validated plan, or refuse it."""
    command = args.command
    try:
        if command == "camera":
            if not args.virtual:
                raise CliError(
                    "camera needs --virtual: the only delivery path implemented today is "
                    "the Windows virtual camera",
                    hint="phonebridge camera --virtual   (add --facing front to use the "
                    "selfie camera; Ctrl+C to switch back? see docs/26)",
                    code=EXIT_USAGE,
                )
            return LivePlan(
                camera=CameraPlan(
                    width=args.width,
                    height=args.height,
                    fps=args.fps,
                    quality=args.quality,
                    facing=args.facing,
                    backend=args.backend,
                )
            )
        if command == "mic":
            if not args.to_cable:
                raise CliError(
                    "mic needs --to-cable: writing the phone microphone anywhere else is "
                    "the in-app monitor, which no other application can select",
                    hint="phonebridge mic --to-cable",
                    code=EXIT_USAGE,
                )
            return LivePlan(
                mic=MicPlan(device=args.out_device, latency_ms=args.latency_ms, gain=args.gain)
            )
        if command == "speaker":
            return LivePlan(
                speaker=SpeakerPlan(
                    latency_ms=args.latency_ms,
                    lipsync_ms=args.lipsync_ms,
                    device=args.loopback_device,
                )
            )
        if command == "start":
            if not (args.camera or args.mic or args.speaker):
                raise CliError(
                    "start needs at least one of --camera, --mic or --speaker",
                    hint="phonebridge start --camera --mic --speaker",
                    code=EXIT_USAGE,
                )
            return LivePlan(
                camera=CameraPlan(
                    width=args.width,
                    height=args.height,
                    fps=args.fps,
                    quality=args.quality,
                    facing=args.facing,
                    backend=args.backend,
                )
                if args.camera
                else None,
                mic=MicPlan(device=args.cable, latency_ms=args.mic_latency_ms)
                if args.mic
                else None,
                speaker=SpeakerPlan(latency_ms=args.speaker_latency_ms, lipsync_ms=args.lipsync_ms)
                if args.speaker
                else None,
            )
    except ConfigError as exc:
        raise CliError(str(exc), code=EXIT_USAGE) from exc
    raise CliError(f"unknown live command {command!r}", code=EXIT_USAGE)


async def _status_loop(runner: LiveRunner, stop: asyncio.Event, *, quiet: bool) -> None:
    while not stop.is_set():
        status = runner.status()
        if not quiet:
            print(f"  {status.line()}", flush=True)
        if status.mic_state == "error" and status.mic_detail:
            print(f"  microphone stopped: {status.mic_detail}", flush=True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=STATUS_INTERVAL_S)


async def _run(args: argparse.Namespace) -> int:
    plan = plan_from_args(args)
    store = _store(args)
    session, target = await open_session(args, store)
    runner = LiveRunner(session, plan)
    stop = asyncio.Event()
    printer: asyncio.Task[None] | None = None
    try:
        note(f"delivering: {', '.join(plan.names)} from {target.describe()}")
        if plan.camera is not None:
            note(f"camera: {plan.camera.label}")
            note(f"select the virtual camera in your app; OBS: {OBS_DOWNLOAD_URL}")
        if plan.mic is not None:
            note(
                f'microphone: writing into {plan.mic.device!r}; select "CABLE Output" '
                f"as the microphone in the other app; VB-CABLE: {VB_CABLE_URL}"
            )
        if plan.speaker is not None:
            note(
                f"speaker: buffer {plan.speaker.latency_ms} ms, lip-sync "
                f"{plan.speaker.lipsync_ms} ms; the phone plays it, Android routes it to "
                f"Bluetooth headphones when they are connected"
            )
        await runner.start()
        printer = asyncio.get_running_loop().create_task(
            _status_loop(runner, stop, quiet=bool(getattr(args, "no_status", False)))
        )
        status = await runner.run(stop, duration=getattr(args, "duration", None))
    finally:
        stop.set()
        if printer is not None:
            printer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await printer
        with contextlib.suppress(Exception):
            await runner.close()

    if getattr(args, "format", "text") == "json":
        _print_json(status.to_dict())
    else:
        note(status.line())
    # A microphone that died mid-run is a failure, not a clean stop.
    return EXIT_ERROR if status.mic_state == "error" else 0


def _store(args: argparse.Namespace) -> Any:
    from .main import _store as store  # noqa: PLC0415 - avoid a circular import at module load

    return store(args)


def run(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_run(args))
    except DeviceUnavailable as exc:
        raise CliError(str(exc), hint=exc.hint, code=EXIT_ERROR) from exc
