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
    CAMERA_PROFILES,
    DEFAULT_CAMERA_PROFILE,
    VIRTUAL_CAMERA_AUTO,
    VIRTUAL_CAMERA_BACKENDS,
    CameraPlan,
    ConfigError,
    LivePlan,
    MicPlan,
    SpeakerPlan,
    profile_size,
)
from ..live.devices import OBS_DOWNLOAD_URL, VB_CABLE_URL, DeviceUnavailable
from ..live.pump import stats_lines
from ..live.runner import STATUS_INTERVAL_S, LiveRunner
from .target import EXIT_ERROR, EXIT_USAGE, CliError, note, open_session


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _add_camera_args(parser: argparse.ArgumentParser) -> None:
    """The camera flags both ``camera`` and ``start`` accept.

    ``--profile`` is the one-flag answer for a machine that cannot sustain 720p:
    ``--profile low`` is 640x480 at 30 fps, a quarter of the pixels and a quarter
    of the JPEG work on both ends. The three explicit flags still win over the
    profile, so ``--profile low --fps 15`` means exactly that.
    """
    parser.add_argument(
        "--profile",
        choices=tuple(CAMERA_PROFILES),
        default=DEFAULT_CAMERA_PROFILE,
        help=(
            "camera size preset: "
            + ", ".join(f"{name} = {w}x{h}@{f}" for name, (w, h, f) in CAMERA_PROFILES.items())
            + f" (default: {DEFAULT_CAMERA_PROFILE})"
        ),
    )
    parser.add_argument("--width", type=int, default=None, help="override the profile's width")
    parser.add_argument("--height", type=int, default=None, help="override the profile's height")
    parser.add_argument("--fps", type=int, default=None, help="override the profile's frame rate")
    parser.add_argument(
        "--quality",
        type=int,
        default=70,
        metavar="JPEG",
        help="JPEG quality the phone encodes at (default 70; 20-95)",
    )
    parser.add_argument("--facing", choices=("back", "front"), default="back")
    parser.add_argument(
        "--stats",
        action="store_true",
        help=(
            "print per-second measurements: frames received, KB per frame, decode/"
            "convert/send milliseconds, queue depth and drops on the PC, plus the "
            "phone's own capture fps, encode time and JPEG size"
        ),
    )


def _camera_geometry(args: argparse.Namespace) -> tuple[int, int, int]:
    """Profile defaults, with whatever the user named explicitly on top."""
    try:
        width, height, fps = profile_size(getattr(args, "profile", DEFAULT_CAMERA_PROFILE))
    except ConfigError as exc:
        raise CliError(str(exc), code=EXIT_USAGE) from exc
    return (
        args.width if getattr(args, "width", None) is not None else width,
        args.height if getattr(args, "height", None) is not None else height,
        args.fps if getattr(args, "fps", None) is not None else fps,
    )


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
    _add_camera_args(camera)
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
    speaker.add_argument(
        "--test-tone",
        dest="test_tone",
        action="store_true",
        help=(
            "send a 440 Hz sine for 3 s instead of capturing this PC's audio, then "
            "stop: tests the phone's own output without a sound card, PyAudioWPatch "
            "or anything playing on the PC"
        ),
    )
    speaker.add_argument(
        "--muted",
        action="store_true",
        help=(
            "start muted (sends silence with the MUTED flag); the default is to send "
            "audio, because that is what this command is for"
        ),
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
    _add_camera_args(start)
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
            width, height, fps = _camera_geometry(args)
            return LivePlan(
                camera=CameraPlan(
                    width=width,
                    height=height,
                    fps=fps,
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
                    test_tone=bool(getattr(args, "test_tone", False)),
                    muted=bool(getattr(args, "muted", False)),
                )
            )
        if command == "start":
            if not (args.camera or args.mic or args.speaker):
                raise CliError(
                    "start needs at least one of --camera, --mic or --speaker",
                    hint="phonebridge start --camera --mic --speaker",
                    code=EXIT_USAGE,
                )
            width, height, fps = _camera_geometry(args)
            return LivePlan(
                camera=CameraPlan(
                    width=width,
                    height=height,
                    fps=fps,
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


#: What to do about each path, once it has failed. The reason itself always comes
#: from the component that failed, verbatim; this is only the advice.
_ADVICE = {
    "camera": (
        "the session is still up; microphone and speaker are unaffected. On the phone, "
        "allow Camera for PhoneBridge, close any app that holds the lens, then Ctrl+C "
        "and restart this command. For the phone's own reason, run on the phone: "
        "adb logcat -s CameraEngine"
    ),
    "mic": (
        "the session is still up; camera and speaker are unaffected. "
        "Fix the device, then Ctrl+C and restart this command."
    ),
    "speaker": (
        "the session is still up; camera and microphone are unaffected. Check the "
        "speaker line for the phone's own route and playback state, prove the phone "
        "alone with `phonebridge speaker --test-tone`, and for the phone's reason run "
        "on the phone: adb logcat -s BridgeServer"
    ),
}


def _report_failures(status: Any, reported: dict[str, str]) -> None:
    """Print each failure once, with its own reason, and never repeat it.

    The old loop only did this for the microphone, so a camera that never
    delivered a frame printed "camera error 0.0 fps" every second and no reason
    at all — the phone's `camera_busy`/`camera_denied` text was in the JSON and
    nowhere the user would look. Every path now gets the same treatment, and the
    camera's detail is printed whenever it changes even if it is not fatal
    (a single undecodable frame still deserves one line).
    """
    failures = (
        ("camera", status.camera_state, status.camera_detail, "camera"),
        ("mic", status.mic_state, status.mic_detail, "microphone"),
        ("speaker", status.speaker_state, status.speaker_detail, "speaker"),
    )
    for path, state, detail, label in failures:
        if not detail or reported.get(path) == detail:
            continue
        reported[path] = detail
        if state == "error":
            print(f"  {label} stopped: {detail}", flush=True)
            print(f"  {_ADVICE[path]}", flush=True)
        else:
            # A notice, not a dead path: the picture is still being delivered.
            print(f"  {label}: {detail}", flush=True)


async def _status_loop(
    runner: LiveRunner, stop: asyncio.Event, *, quiet: bool, stats: bool = False
) -> None:
    reported: dict[str, str] = {}
    while not stop.is_set():
        status = runner.status()
        if not quiet:
            print(f"  {status.line()}", flush=True)
        _report_failures(status, reported)
        if stats:
            for line in stats_lines(
                status.camera_stats.get("pc"), status.camera_stats.get("phone")
            ):
                print(line, flush=True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=STATUS_INTERVAL_S)


async def _run(args: argparse.Namespace) -> int:
    plan = plan_from_args(args)
    store = _store(args)
    session, target = await open_session(args, store)
    runner = LiveRunner(session, plan, stats=bool(getattr(args, "stats", False)))
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
        if plan.speaker is not None and plan.speaker.test_tone:
            note(
                f"test tone: {plan.speaker.tone_hz:.0f} Hz for "
                f"{plan.speaker.tone_seconds:.1f} s, sent to the phone; nothing is "
                f"captured from this PC, so a silent phone is a phone problem. Watch "
                f"the speaker line: route, f/s and played all come from the phone"
            )
        elif plan.speaker is not None:
            note(
                f"speaker: buffer {plan.speaker.latency_ms} ms, lip-sync "
                f"{plan.speaker.lipsync_ms} ms; the phone plays it, Android routes it to "
                f"Bluetooth headphones when they are connected"
            )
        await runner.start()
        early = runner.status()
        if plan.speaker is not None and plan.speaker.test_tone and early.speaker_state == "error":
            # A test tone exists to prove one thing. If the phone refused playback
            # there is nothing to listen for, so say why and exit instead of idling
            # on silence with a dead path.
            _report_failures(early, {})
            return EXIT_ERROR
        printer = asyncio.get_running_loop().create_task(
            _status_loop(
                runner,
                stop,
                quiet=bool(getattr(args, "no_status", False)),
                stats=bool(getattr(args, "stats", False)),
            )
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
    # A path that died mid-run is a failure, not a clean stop — including the
    # camera, which used to exit 0 after delivering no picture at all, and the
    # speaker, which used to exit 0 after the phone refused to play anything.
    if (
        status.mic_state == "error"
        or status.camera_state == "error"
        or status.speaker_state == "error"
    ):
        return EXIT_ERROR
    return 0


def _store(args: argparse.Namespace) -> Any:
    from .main import _store as store  # noqa: PLC0415 - avoid a circular import at module load

    return store(args)


def run(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_run(args))
    except DeviceUnavailable as exc:
        raise CliError(str(exc), hint=exc.hint, code=EXIT_ERROR) from exc
