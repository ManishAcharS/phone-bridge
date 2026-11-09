"""``phonebridge`` command line.

Inspect and drive a phone link from the PC:

    phonebridge doctor                   inspect this PC (Phase 0 audit)
    phonebridge identity [--name NAME]   this PC's device id / config dir
    phonebridge devices                  pinned phones, saved addresses, live beacons
    phonebridge discover [--json]        listen for phone beacons
    phonebridge info                     device information (JSON by default)
    phonebridge capabilities             capability table, honest about today
    phonebridge audio status|last        headset state and the last audio probe
    phonebridge ping                     RTT histogram
    phonebridge bench                    encrypted binary throughput + latency
    phonebridge watch                    phone -> PC events, auto-reconnect
    phonebridge selftest                 every supported workflow, end to end
    phonebridge forget <id> | --all      drop pinned phones
    phonebridge compat run|show|compare|report
                                         multi-device audio capability records

Which phone: ``--host`` always wins. ``--device <id|name>`` uses a pinned phone's
saved address, ``--last`` the most recent one, and with no target at all the last
seen endpoint is used (announced on stderr). Run ``phonebridge <command> -h`` for
the options of one command; ``docs/11-pc-client.md`` has the workflows.

Exit codes: 0 ok, 1 not found / nothing to show, 2 bad arguments or the phone
could not be reached, 4 a ``--require`` capability is not available, 5 the phone
does not implement that operation.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .. import PROTOCOL_VERSION, __version__
from ..core import compat
from ..core.device_store import DeviceStore
from ..core.session import BridgeSession, ReconnectingSession, SessionError
from ..protocol.framing import MAX_ENVELOPE_BODY
from ..transport.discovery import HELP as DISCOVERY_HELP
from ..transport.discovery import DiscoveryService
from ..transport.link import DEFAULT_PORT, LinkError, websocket_uri
from . import format as fmt
from .selftest import FAIL, NOT_IMPLEMENTED, PASS, run_selftest, summarise
from .target import (
    EXIT_ERROR,
    EXIT_REQUIREMENT,
    EXIT_USAGE,
    CliError,
    Target,
    note,
    open_session,
    pairing_prompt,
    resolve_target,
    unsupported,
)

log = logging.getLogger("phonebridge.cli")

CONNECT_TIMEOUT_DEFAULT = 6.0
REQUEST_TIMEOUT_DEFAULT = 15.0
MAX_BENCH_PAYLOAD = MAX_ENVELOPE_BODY - 128


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _configure_console() -> None:
    """Make non-ASCII phone data printable on a legacy Windows code page.

    The CLI's own output is ASCII, but a phone's model or ROM string is not under
    our control; without this, one accented character aborts a command with
    UnicodeEncodeError when the output is redirected or the console uses cp437.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError, io.UnsupportedOperation):  # pragma: no cover
            log.debug("could not reconfigure %r", stream)


def _store(args: argparse.Namespace) -> DeviceStore:
    return DeviceStore(Path(args.config_dir) if args.config_dir else None)


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _as_dict(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CliError(
            f"the phone answered {what} with {type(value).__name__}, not an object",
            hint="this CLI and the phone app probably differ in protocol version.",
        )
    return value


def _rpc_failure(method: str, exc: SessionError) -> CliError:
    if str(exc).startswith("method_not_found"):
        return unsupported(method, exc)
    return CliError(f"{method} failed: {exc}", hint="check the phone's log screen.")


# --- argument validators ----------------------------------------------------- #
def _int_in(name: str, low: int, high: int):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} must be a whole number, got {text!r}") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(
                f"{name} must be between {low} and {high}, got {value}"
            )
        return value

    return parse


def _float_in(name: str, low: float, high: float):
    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} must be a number, got {text!r}") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(
                f"{name} must be between {low:g} and {high:g}, got {value:g}"
            )
        return value

    return parse


_port = _int_in("--port", 1, 65535)
_count = _int_in("count", 1, 100_000)
_count_or_zero = _int_in("--retries", 0, 10)
_interval = _float_in("interval", 0.0, 60.0)
_seconds = _float_in("seconds", 0.05, 3600.0)
_seconds_or_zero = _float_in("seconds", 0.0, 3600.0)
_frames = _int_in("frames", 1, 200_000)
_payload = _int_in("size", 1, MAX_BENCH_PAYLOAD)
_probe_seconds = _int_in("--seconds", 5, 300)


# --------------------------------------------------------------------------- #
# compat - multi-device capability records
# --------------------------------------------------------------------------- #
async def _compat_run(args: argparse.Namespace) -> int:
    """Ask the phone to measure itself, then store the report."""
    store = _store(args)
    session, _target = await open_session(args, store)
    try:
        print(f"running capability probe on {session.uri} "
              f"(duplex soak {args.seconds}s{', acoustic latency' if args.acoustic else ''}) ...")
        params = {"seconds": args.seconds, "acoustic": args.acoustic}
        try:
            report = await session.call("audio.capabilities.run", params, timeout=args.timeout)
        except SessionError as exc:
            raise _rpc_failure("audio.capabilities.run", exc) from exc
    finally:
        await session.close()

    if not isinstance(report, dict):
        print("error: the phone returned no report", file=sys.stderr)
        return EXIT_USAGE
    if args.label:
        report["label"] = args.label
    report.setdefault("captured_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    problems = compat.validate_report(report)
    if problems:
        print("error: the phone returned a malformed report:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(json.dumps(report, indent=2), file=sys.stderr)
        return EXIT_USAGE

    path = compat.save_report(store.dir, report)
    print()
    print(compat.compare_table([report]))
    print()
    for finding in compat.derive_findings(report):
        print(f"  [{finding.level:8s}] {finding.code}: {finding.text}")
    print()
    print(f"stored: {path}")
    print("regenerate the report with: phonebridge compat report")
    return 0


def _compat_show(args: argparse.Namespace) -> int:
    store = _store(args)
    reports = compat.load_reports(store.dir)
    if not reports:
        print(f"no compat records in {compat.compat_dir(store.dir)}")
        print('run: phonebridge compat run --host <phone-ip> --label "<device name>"')
        return EXIT_ERROR
    print(compat.compare_table(reports))
    print()
    for report in reports:
        print(f"{compat._label(report)}: {compat.verdict(report)} "
              f"({compat.VERDICT_LABELS[compat.verdict(report)]})")
        for finding in compat.derive_findings(report):
            if finding.level != "info":
                print(f"    [{finding.level}] {finding.code}: {finding.text}")
    return 0


def _compat_compare(args: argparse.Namespace) -> int:
    store = _store(args)
    reports = compat.load_reports(store.dir)
    if not reports:
        print(f"no compat records in {compat.compat_dir(store.dir)}")
        return EXIT_ERROR
    print(compat.compare_table(reports))
    return 0


def _compat_report(args: argparse.Namespace) -> int:
    store = _store(args)
    reports = compat.load_reports(store.dir)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(compat.render_markdown(reports), "utf-8")
    print(f"wrote {out.resolve()} ({len(reports)} record(s) found)")
    if not reports:
        print("no records yet - the report lists every planned device as unmeasured")
    return 0


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
OPTIONAL_PACKAGES: tuple[tuple[str, str], ...] = (
    ("websockets", "required for the control channel"),
    ("cryptography", "required for pairing and encryption"),
    ("soundcard", "Phase 2: WASAPI loopback capture of PC audio"),
    ("sounddevice", "Phase 2/3: low-latency audio playback"),
    ("numpy", "Phase 2+: audio/video buffers"),
    ("av", "Phase 2: Opus encode/decode (bundles libopus)"),
    ("PySide6", "Phase 3: desktop dashboard"),
    ("pyvirtualcam", "Phase 4: virtual webcam output"),
)


def _console_script() -> str | None:
    """Where the ``phonebridge`` script lives, if it is reachable at all.

    ``shutil.which`` fails when the venv is used by absolute path without being
    activated - which is exactly how a scheduled task or an IDE runs it - so the
    interpreter's own Scripts/bin directory is checked as well.
    """
    found = shutil.which("phonebridge")
    if found:
        return found
    scripts = Path(sys.executable).parent
    for name in ("phonebridge", "phonebridge.exe", "phonebridge.cmd", "phonebridge-script.py"):
        candidate = scripts / name
        if candidate.exists():
            return str(candidate)
    return None


def _package_versions() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for mod, why in OPTIONAL_PACKAGES:
        try:
            module = __import__(mod)
            version = getattr(module, "__version__", "?")
        except Exception:  # noqa: BLE001 - a broken optional package is just "missing"
            found.append({"module": mod, "version": None, "why": why})
        else:
            found.append({"module": mod, "version": str(version), "why": why})
    return found


def cmd_doctor(args: argparse.Namespace) -> int:
    store = _store(args)
    is_windows = sys.platform == "win32"
    packages = _package_versions()

    if args.json:
        _json({
            "phonebridge": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "is_windows": is_windows,
            "config_dir": str(store.dir),
            "console_script": _console_script(),
            "packages": packages,
            "pinned_phones": len(store.list_peers()),
            "known_endpoints": len(store.known_endpoints()),
        })
        return 0

    ok = "  [ok]  "
    warn = "  [warn]"
    print("PhoneBridge environment report")
    print("=" * 66)
    print(f"  version       : {__version__} (protocol v{PROTOCOL_VERSION})")
    print(f"  time          : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  platform      : {platform.platform()}")
    print(f"  machine       : {platform.machine()}")
    print(f"  python        : {sys.version.split()[0]} ({sys.executable})")
    print(f"  config dir    : {store.path_summary()}")
    script = _console_script()
    print(f"{ok if script else warn}console script: {script or 'not on PATH (use: python -m phonebridge)'}")
    print(f"  paired phones : {len(store.list_peers())}")

    print(f"{ok if is_windows else warn}windows      : {'yes' if is_windows else 'NO - this is ' + sys.platform}")
    if not is_windows:
        print("         The PC application targets Windows; audio/virtual-device")
        print("         features need Windows APIs. Protocol tests still run here.")

    # --- optional packages ------------------------------------------------ #
    for entry in packages:
        if entry["version"] is None:
            print(f"{warn}{entry['module']:<13}: missing  ({entry['why']})")
        else:
            print(f"{ok}{entry['module']:<13}: {entry['version']}")

    # --- Windows-specific bits -------------------------------------------- #
    if is_windows:
        print("-" * 66)
        print("  Windows integration")
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            script_cmd = (
                "Get-CimInstance Win32_SoundDevice | "
                "Select-Object -ExpandProperty Name"
            )
            try:
                out = subprocess.run(
                    [ps, "-NoProfile", "-Command", script_cmd],
                    capture_output=True, text=True, timeout=25,
                ).stdout.strip()
                names = [n for n in out.splitlines() if n.strip()]
                print(f"{ok}audio devices: {len(names)} found")
                for n in names:
                    print(f"            - {n}")
                cables = [n for n in names if "CABLE" in n.upper() or "VOICEMEETER" in n.upper()]
                if cables:
                    print(f"{ok}virtual cable: {cables[0]}")
                else:
                    print(f"{warn}virtual cable: none - needed only for 'phone mic "
                          "as PC mic' (Phase 4). WASAPI loopback needs no driver.")
            except Exception as exc:  # noqa: BLE001
                print(f"{warn}audio devices: could not query ({exc})")

        # virtual camera backends
        obs = Path(r"C:\Program Files\obs-studio")
        if obs.exists():
            print(f"{ok}OBS Studio   : {obs} (pyvirtualcam 'obs' backend available)")
        else:
            print(f"{warn}OBS Studio   : not installed (Phase 4 virtual webcam backend)")

        try:
            adb_lines = subprocess.run(
                ["adb", "version"], capture_output=True, text=True, timeout=10
            ).stdout.splitlines()
            print(f"{ok}adb          : {adb_lines[0] if adb_lines else 'present'}")
        except Exception:  # noqa: BLE001
            print(f"{warn}adb          : not on PATH (needed for USB dev mode forwarding)")

        # java / android sdk for building the app
        java = shutil.which("java")
        print(f"{ok if java else warn}JDK          : {java or 'not on PATH (needed to build the Android app)'}")
        sdk = None
        for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            import os

            if os.environ.get(env):
                sdk = os.environ[env]
                break
        print(f"{ok if sdk else warn}Android SDK  : {sdk or 'ANDROID_HOME not set (Android Studio installs it)'}")

    # --- network ---------------------------------------------------------- #
    print("-" * 66)
    print("  Network")
    local_ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0])
            if ip not in local_ips:
                local_ips.append(ip)
    except OSError:
        pass
    print(f"{ok}local IPv4   : {', '.join(local_ips) or 'none detected'}")
    print(f"{ok}discovery    : UDP 47823 (inbound, Private profile)")
    print(f"{ok}control port : TCP {DEFAULT_PORT} outbound to the phone")

    print("=" * 66)
    print("  If Windows Firewall prompts for python.exe, allow Private networks")
    print("  only. No internet access is required or used.")
    return 0


# --------------------------------------------------------------------------- #
# identity / devices / discovery
# --------------------------------------------------------------------------- #
def cmd_identity(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.name is not None:
        wanted = args.name.strip()
        if not wanted:
            raise CliError("--name must not be empty", hint="the name is what the phone shows.")
        store.set_pc_name(wanted)
        print(f"device name set to {wanted}")
    ident = store.load_or_create_identity()
    peers = store.list_peers()
    endpoints = store.known_endpoints()

    if args.json:
        _json({
            "device_id": ident.device_id,
            "name": ident.name,
            "config_dir": str(store.dir),
            "peers": [
                {
                    "peer_id": p.peer_id,
                    "peer_name": p.peer_name,
                    "added_at": p.added_at,
                    "last_seen": p.last_seen,
                    "endpoint": store.endpoint_for(p.peer_id),
                }
                for p in peers
            ],
            "known_endpoints": endpoints,
        })
        return 0

    print(f"device id   : {ident.device_id}")
    print(f"device name : {ident.name}")
    print(f"config dir  : {store.path_summary()}")
    if not peers:
        print("paired phones: none yet")
    else:
        print(f"paired phones ({len(peers)}):")
        for p in peers:
            endpoint = store.endpoint_for(p.peer_id)
            where = f"  last {endpoint['host']}:{endpoint['port']}" if endpoint else ""
            print(f"  - {p.peer_name}  id={p.peer_id}  since {p.added_at}{where}")
    for ep in endpoints:
        print(f"last seen   : {ep['transport']} {ep['host']}:{ep['port']} at {ep['at']}")
    return 0


async def _listen_for_phones(seconds: float, *, keep_going) -> tuple[list[dict[str, Any]], int]:
    """Collect beacons for up to `seconds`; `keep_going(found)` may stop early."""
    svc = DiscoveryService()
    try:
        await svc.start()
    except RuntimeError as exc:
        # Another PhoneBridge process (or another beacon listener) owns UDP 47823.
        raise CliError(
            str(exc),
            hint="close the other instance, or connect with --host and skip discovery.",
            code=EXIT_ERROR,
        ) from exc
    found: dict[str, dict[str, Any]] = {}
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            for phone in svc.live():
                found.setdefault(phone.device_id, phone.to_dict())
            if found and keep_going(found):
                break
            await asyncio.sleep(0.2)
    finally:
        await svc.stop()
    return list(found.values()), svc.port


async def _devices(args: argparse.Namespace) -> int:
    store = _store(args)
    ident = store.load_or_create_identity()
    peers = store.list_peers()
    live: list[dict[str, Any]] = []
    port = 0
    if not args.no_discover and args.timeout > 0:
        live, port = await _listen_for_phones(args.timeout, keep_going=lambda _found: False)

    if args.json:
        _json({
            "identity": {"device_id": ident.device_id, "name": ident.name},
            "config_dir": str(store.dir),
            "pinned": [
                {
                    "peer_id": p.peer_id,
                    "peer_name": p.peer_name,
                    "added_at": p.added_at,
                    "endpoint": store.endpoint_for(p.peer_id),
                }
                for p in peers
            ],
            "live": live,
        })
        return 0 if peers or live else EXIT_ERROR

    print(f"this PC     : {ident.name} ({ident.device_id})")
    print(f"config dir  : {store.path_summary()}")
    print()
    if peers:
        rows = []
        for p in peers:
            endpoint = store.endpoint_for(p.peer_id)
            rows.append([
                p.peer_name,
                p.peer_id,
                f"{endpoint['host']}:{endpoint['port']}" if endpoint else "-",
                str(endpoint.get("transport", "-")) if endpoint else "-",
                str(endpoint.get("at", "-")) if endpoint else "-",
            ])
        print(f"pinned phones ({len(peers)}):")
        print(fmt.table(["NAME", "DEVICE ID", "LAST ADDRESS", "TRANSPORT", "SEEN"], rows))
    else:
        print("pinned phones: none yet (pairing happens automatically on first connect)")
    print()
    if args.no_discover or args.timeout <= 0:
        print("live beacons: skipped (--no-discover)")
    elif live:
        print(f"live beacons (UDP {port}, {args.timeout:g}s):")
        for phone in live:
            print(f"  - {phone['name']} ({phone['device_id']}) at {phone['uri']} via {phone['host']}")
    else:
        print(f"live beacons (UDP {port}, {args.timeout:g}s): none heard")
        print("  " + DISCOVERY_HELP)

    if not peers and not live:
        print()
        print("nothing to connect to yet. Pair once with an address:")
        print("  phonebridge info --host <phone-ip>")
        return EXIT_ERROR
    return 0


async def _discover(args: argparse.Namespace) -> int:
    if not args.json:
        print(f"Listening for phone beacons on UDP {DiscoveryService().port} "
              f"for {args.timeout}s ...")
    else:
        note(f"listening for phone beacons for {args.timeout}s (JSON on stdout) ...")

    found, _port = await _listen_for_phones(
        args.timeout,
        keep_going=(lambda _found: True) if args.first else (lambda _found: False),
    )

    if args.json:
        _json(found)
    else:
        for phone in found:
            print(f"  found: {phone['name']} ({phone['device_id']}) at {phone['uri']} "
                  f"via {phone['host']}")
    if not found:
        if not args.json:
            print("  no phones found.")
            print("  " + DISCOVERY_HELP)
        return EXIT_ERROR
    if not args.json and len(found) == 1:
        phone = found[0]
        print()
        print("connect to it with:")
        print(f"  phonebridge info --host {phone['host']} --port {phone['port']}")
    return 0


# --------------------------------------------------------------------------- #
# info / capabilities
# --------------------------------------------------------------------------- #
async def _system_info(session: BridgeSession, timeout: float) -> dict[str, Any]:
    try:
        info = await session.call("system.info", timeout=timeout)
    except SessionError as exc:
        raise _rpc_failure("system.info", exc) from exc
    return _as_dict(info, "system.info")


async def _info(args: argparse.Namespace) -> int:
    store = _store(args)
    session, _target = await open_session(args, store)
    try:
        info = await _system_info(session, args.request_timeout)
    finally:
        await session.close()

    if args.format == "json":
        # Unchanged shape: the whole system.info object, sorted, pretty. Scripts
        # and tools/verify.py already parse exactly this.
        _json(info)
        return 0

    print(fmt.render_info(info))
    if fmt.is_simulated(info):
        note("note: this is the simulator; its device values are synthetic.")
    return 0


async def _capabilities(args: argparse.Namespace) -> int:
    store = _store(args)
    session, _target = await open_session(args, store)
    try:
        info = await _system_info(session, args.request_timeout)
    finally:
        await session.close()

    if args.format == "json":
        _json(fmt.capability_report(info))
    else:
        print(fmt.render_capabilities(info))

    required = list(args.require or [])
    if required:
        rows = {row.key: row for row in fmt.capability_rows(info)}
        missing = fmt.unmet_requirements(info, required)
        for key in required:
            if key not in rows and key not in fmt.capabilities_of(info):
                note(f"warning: {key!r} is not a capability this protocol defines")
        if missing:
            for key in missing:
                row = rows.get(key)
                reason = row.note() if row else "not reported by this phone"
                note(f"requirement not met: {key} ({reason})")
            return EXIT_REQUIREMENT
        if args.format != "json":
            print()
            print("  requirements met: " + ", ".join(required))
    return 0


# --------------------------------------------------------------------------- #
# audio
# --------------------------------------------------------------------------- #
async def _audio_status(args: argparse.Namespace) -> int:
    store = _store(args)
    session, _target = await open_session(args, store)
    try:
        try:
            status = _as_dict(
                await session.call("bluetooth.headset.status", timeout=args.request_timeout),
                "bluetooth.headset.status",
            )
        except SessionError as exc:
            raise _rpc_failure("bluetooth.headset.status", exc) from exc
        route: dict[str, Any] | None = None
        try:
            route = _as_dict(
                await session.call("audio.route.bluetooth", timeout=args.request_timeout),
                "audio.route.bluetooth",
            )
        except SessionError as exc:
            if not str(exc).startswith("method_not_found"):
                raise _rpc_failure("audio.route.bluetooth", exc) from exc
            note("this phone does not report a relay state (older app)")
    finally:
        await session.close()

    if args.format == "json":
        _json({"headset": status, "relay": route})
        return 0
    print(fmt.render_audio_status(status, route))
    return 0


async def _audio_last(args: argparse.Namespace) -> int:
    store = _store(args)
    session, _target = await open_session(args, store)
    try:
        try:
            last = _as_dict(
                await session.call("audio.capabilities.last", timeout=args.request_timeout),
                "audio.capabilities.last",
            )
        except SessionError as exc:
            raise _rpc_failure("audio.capabilities.last", exc) from exc
    finally:
        await session.close()

    if not last.get("available"):
        note(f"the phone has no stored probe report ({last.get('reason', 'no reason given')})")
        note("measure one with: phonebridge compat run --host <phone-ip> --label \"<phone name>\"")
        return EXIT_ERROR
    report = _as_dict(last.get("report"), "audio.capabilities.last.report")
    problems = compat.validate_report(report)
    if problems:
        note("the stored report is malformed:")
        for problem in problems:
            note(f"  - {problem}")
        return EXIT_USAGE
    if args.format == "json":
        _json(report)
        return 0
    print(compat.compare_table([report]))
    print()
    for finding in compat.derive_findings(report):
        print(f"  [{finding.level:8s}] {finding.code}: {finding.text}")
    print()
    print("  (read from the phone's last probe; nothing was stored locally)")
    return 0


# --------------------------------------------------------------------------- #
# ping / bench / watch
# --------------------------------------------------------------------------- #
async def _ping(args: argparse.Namespace) -> int:
    store = _store(args)
    session, _target = await open_session(args, store)
    samples: list[float] = []
    try:
        phone = session.phone
        print(f"pinging {phone.name if phone else 'the phone'} {args.count} times ...")
        for i in range(args.count):
            rtt = await session.ping()
            samples.append(rtt)
            print(f"  seq={i:<3} rtt={rtt:7.2f} ms")
            await asyncio.sleep(args.interval)
    finally:
        await session.close()
    if samples:
        samples_sorted = sorted(samples)
        p95 = samples_sorted[max(0, int(len(samples_sorted) * 0.95) - 1)]
        print(
            f"\nmin {min(samples):.2f}  median {statistics.median(samples):.2f}  "
            f"p95 {p95:.2f}  max {max(samples):.2f}  (ms, application-level RTT)"
        )
    return 0


async def _bench(args: argparse.Namespace) -> int:
    store = _store(args)
    session, _target = await open_session(args, store)
    payload = bytes((i * 7 + 13) & 0xFF for i in range(args.size))
    try:
        print(f"benchmark: {args.frames} frames x {args.size} B "
              f"({args.frames * args.size / 1e6:.1f} MB each way) over {session.uri}")
        queue = session.subscribe_blobs(1)
        warmup = 20
        total = args.frames + warmup
        start = time.perf_counter()
        received = 0
        rtts: list[float] = []
        pipeline = args.pipeline

        async def consume() -> None:
            nonlocal received, rtts
            while received < total:
                blob = await queue.get()
                if blob is None:
                    # The session ended. Without this, `bench` used to sit on a
                    # dead queue until its own 60 s timeout expired.
                    raise SessionError(
                        f"session closed after {received} of {total} frames"
                    )
                if blob != payload:
                    raise SystemExit("payload corruption detected on the echo stream")
                received += 1

        consumer = asyncio.create_task(consume())

        # Warm-up is untimed: it fills the socket buffers so the measured window
        # reflects steady state rather than connection setup.
        for _ in range(warmup):
            await session.send_blob(payload, stream_id=1)

        async def _drain_warmup() -> None:
            while received < warmup:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(_drain_warmup(), timeout=30)
        start = time.perf_counter()
        for i in range(args.frames):
            await session.send_blob(payload, stream_id=1)
            if pipeline and i and i % pipeline == 0:
                await asyncio.sleep(0)  # yield so the reader keeps up
        await asyncio.wait_for(consumer, timeout=60)
        elapsed = time.perf_counter() - start

        useful = args.frames * args.size
        print(f"  frames echoed : {args.frames} (+{warmup} warm-up)")
        print(f"  wall time     : {elapsed:.3f} s")
        print(f"  throughput    : {useful / elapsed / 1e6:.2f} MB/s "
              f"({useful * 8 / elapsed / 1e6:.1f} Mbit/s) payload, both directions")
        print(f"  stats         : {json.dumps(session.stats.to_dict())}")
        rtt = await session.ping()
        print(f"  app RTT       : {rtt:.2f} ms")
        # Every frame was compared byte for byte in `consume`, so this line is a
        # statement about what happened, not a cheerful assumption.
        print(f"  integrity     : {received} frames returned byte-identical")
        if args.expect_ms is not None:
            ok = rtt <= args.expect_ms
            print(f"  threshold     : {args.expect_ms} ms -> {'PASS' if ok else 'FAIL'}")
            return 0 if ok else EXIT_ERROR
    finally:
        await session.close()
    return 0


async def _watch(args: argparse.Namespace) -> int:
    store = _store(args)
    identity = store.load_or_create_identity()
    target: Target = resolve_target(store, args)
    if args.auto_pair:
        note("warning: --auto-pair accepts the pairing code without comparing it.")
    confirm = (lambda _sas: True) if args.auto_pair else pairing_prompt

    def emit(payload: dict[str, Any], text: str) -> None:
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(text)
        sys.stdout.flush()

    def on_state(state: str, details: dict) -> None:
        if args.json:
            emit({"at": time.strftime("%H:%M:%S"), "state": state, **details},
                 "")
        else:
            print(f"[{time.strftime('%H:%M:%S')}] {state:<12} {details if details else ''}")
            sys.stdout.flush()
        if state == "connected":
            store.remember_endpoint(
                target.transport, target.host, target.port,
                peer_id=details.get("device_id"),
            )

    def on_event(name: str, data: dict) -> None:
        emit(
            {"at": time.strftime("%H:%M:%S"), "event": name, "data": data},
            f"[{time.strftime('%H:%M:%S')}] event {name}: {json.dumps(data)}",
        )

    note(f"watching {target.describe()} (Ctrl-C to stop) ...")
    sup = ReconnectingSession(
        websocket_uri(target.host, target.port),
        identity,
        store,
        on_state=on_state,
        confirm_pairing=confirm,
        on_event=on_event,
        max_attempts=args.max_attempts,
        request_timeout=args.request_timeout,
    )
    await sup.start()
    try:
        await asyncio.wait_for(_until_interrupt(), None if args.timeout == 0 else args.timeout)
    except (asyncio.TimeoutError, KeyboardInterrupt):
        pass
    finally:
        await sup.stop()
    # In JSON mode even this line is an object, so a consumer can read every
    # line of stdout as JSON without special-casing the last one.
    emit({"at": time.strftime("%H:%M:%S"), "state": "stopped"}, "stopped")
    return 0


async def _until_interrupt() -> None:
    while True:
        await asyncio.sleep(3600)


# --------------------------------------------------------------------------- #
# selftest - every workflow the protocol actually specifies
# --------------------------------------------------------------------------- #
async def _selftest(args: argparse.Namespace) -> int:
    store = _store(args)
    session, target = await open_session(args, store)
    try:
        rows = await run_selftest(session, frames=args.frames)
        phone = session.phone
        phone_name = phone.name if phone else "?"
    finally:
        await session.close()

    counts = summarise(rows)
    if args.json:
        _json({
            "target": target.to_dict(),
            "phone": phone_name,
            "rows": [row.to_dict() for row in rows],
            "summary": counts,
            "protocol_version": PROTOCOL_VERSION,
        })
        return 0 if counts.get(FAIL, 0) == 0 else EXIT_ERROR

    print(f"end-to-end workflow self-test against {phone_name} at {target.describe()}")
    print()
    table_rows = [
        [row.workflow, row.status, fmt.clip(row.evidence, 74)] for row in rows
    ]
    print(fmt.table(["WORKFLOW", "RESULT", "EVIDENCE"], table_rows))
    print()
    supported = counts.get(PASS, 0) + counts.get(FAIL, 0)
    print(
        f"  {counts.get(PASS, 0)} of {supported} supported workflows passed"
        + (f", {counts[FAIL]} failed" if counts.get(FAIL) else "")
    )
    print(
        f"  {counts.get(NOT_IMPLEMENTED, 0)} workflows are not in protocol v1 "
        "(marked, not skipped): file transfer, camera, sensors, audio relay."
    )
    print("  matrix and evidence per row: docs/12-end-to-end-verification.md")
    return 0 if counts.get(FAIL, 0) == 0 else EXIT_ERROR


# --------------------------------------------------------------------------- #
# forget
# --------------------------------------------------------------------------- #
def cmd_forget(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.all:
        peers = store.list_peers()
        if not peers:
            print("no pinned phones to forget")
            return EXIT_ERROR
        for peer in peers:
            store.forget_peer(peer.peer_id)
        print(f"forgot {len(peers)} phone(s); they must pair again next time")
        return 0
    if not args.device_id:
        raise CliError(
            "nothing to forget: pass a device id, or --all",
            hint="`phonebridge identity` lists the pinned ids.",
        )
    if store.forget_peer(args.device_id):
        print(f"forgot {args.device_id}; it must pair again next time")
        return 0
    print(f"no pinned phone with id {args.device_id}", file=sys.stderr)
    return EXIT_ERROR


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def _add_target_args(
    parser: argparse.ArgumentParser,
    *,
    timeouts: bool = True,
    retries: bool = True,
    request_only: bool = False,
) -> None:
    group = parser.add_argument_group("which phone")
    group.add_argument("--host", help="phone IP or hostname (127.0.0.1 with adb forward)")
    group.add_argument("--device", metavar="ID|NAME",
                       help="a pinned phone: full device id, or part of its name")
    group.add_argument("--last", action="store_true",
                       help="use the most recently seen endpoint")
    group.add_argument("--port", type=_port, default=None,
                       help=f"phone port (default {DEFAULT_PORT}; a saved endpoint keeps "
                            "the port it was seen on unless this is given)")
    group.add_argument("--transport", default="auto",
                       choices=["auto", "usb", "wifi", "hotspot"],
                       help="label for the saved endpoint (default auto)")
    group.add_argument("--auto-pair", action="store_true",
                       help="accept the pairing code without comparing it (tests only)")
    if timeouts:
        group.add_argument("--connect-timeout", type=_float_in("--connect-timeout", 0.1, 120.0),
                           default=CONNECT_TIMEOUT_DEFAULT,
                           help=f"seconds to establish the link (default {CONNECT_TIMEOUT_DEFAULT:g})")
    if request_only or timeouts:
        group.add_argument("--request-timeout", type=_float_in("--request-timeout", 0.1, 3600.0),
                           default=REQUEST_TIMEOUT_DEFAULT,
                           help=f"seconds to wait for one RPC answer (default {REQUEST_TIMEOUT_DEFAULT:g})")
    if retries:
        group.add_argument("--retries", type=_count_or_zero, default=0, metavar="N",
                           help="extra attempts after a transient connection failure (default 0)")


def _add_format_args(parser: argparse.ArgumentParser, *, default: str = "json") -> None:
    parser.add_argument("--format", choices=["json", "text"], default=default,
                        help=f"output format (default {default})")
    parser.add_argument("--json", dest="format", action="store_const", const="json",
                        help="shorthand for --format json")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="phonebridge",
        description=f"PhoneBridge PC client (protocol v{PROTOCOL_VERSION})",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--version", action="version",
                   version=f"phonebridge {__version__} (protocol v{PROTOCOL_VERSION}, "
                           f"Python {platform.python_version()})")
    p.add_argument("--config-dir", help="override the config directory (tests/portable mode; "
                                        "also PHONEBRIDGE_CONFIG_DIR on non-Windows)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="inspect this PC's development environment")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")

    ident = sub.add_parser("identity", help="show (or name) this PC and its pinned phones")
    ident.add_argument("--name", metavar="NAME", default=None,
                       help="set this PC's device name (what the phone displays)")
    ident.add_argument("--json", action="store_true", help="machine-readable output")

    devices = sub.add_parser(
        "devices",
        help="pinned phones, saved addresses and live beacons in one view",
        description="Lists what this PC knows and what is announcing itself right now.",
    )
    devices.add_argument("--timeout", type=_seconds_or_zero, default=3.0,
                         help="seconds to listen for beacons; 0 skips discovery (default 3)")
    devices.add_argument("--no-discover", action="store_true",
                         help="do not listen for beacons at all")
    devices.add_argument("--json", action="store_true", help="machine-readable output")

    d = sub.add_parser("discover", help="listen for phone discovery beacons")
    d.add_argument("--timeout", type=_seconds_or_zero, default=8.0,
                   help="seconds to listen (default 8)")
    d.add_argument("--first", action="store_true",
                   help="stop as soon as the first phone is heard")
    d.add_argument("--json", action="store_true", help="emit a JSON array")

    info = sub.add_parser("info", help="connect and print the phone's device information")
    _add_target_args(info)
    _add_format_args(info, default="json")

    caps = sub.add_parser(
        "capabilities",
        help="what this phone can actually do today (hardware vs usable)",
        description=(
            "Prints the capability contract of protocol v1: hardware.<key> is what "
            "the phone measured, <key> is what it can deliver right now, detail.<key> "
            "explains the gap. --require makes the command usable as a gate."
        ),
    )
    _add_target_args(caps)
    _add_format_args(caps, default="text")
    caps.add_argument("--require", action="append", metavar="KEY",
                      help="exit 4 unless this capability is usable today (repeatable)")

    audio = sub.add_parser(
        "audio",
        help="headset state and the last audio capability probe",
        description=(
            "Reads what the phone reports. The audio relay itself is Phase 2 and is "
            "not implemented, so nothing here pretends to move audio."
        ),
    )
    asub = audio.add_subparsers(dest="audio_command", required=True)
    a_status = asub.add_parser("status", help="bluetooth headset, profile and relay state")
    _add_target_args(a_status)
    _add_format_args(a_status, default="text")
    a_last = asub.add_parser("last", help="the last capability probe stored on the phone")
    _add_target_args(a_last)
    _add_format_args(a_last, default="text")

    ping = sub.add_parser("ping", help="measure application-level round-trip time")
    _add_target_args(ping)
    ping.add_argument("--count", type=_count, default=10, help="pings to send (default 10)")
    ping.add_argument("--interval", type=_interval, default=0.1,
                      help="seconds between pings (default 0.1)")

    bench = sub.add_parser("bench", help="encrypted binary throughput test")
    _add_target_args(bench)
    bench.add_argument("--frames", type=_frames, default=2000, help="frames to send (default 2000)")
    bench.add_argument("--size", type=_payload, default=1024,
                       help=f"payload bytes per frame, 1..{MAX_BENCH_PAYLOAD} (default 1024)")
    bench.add_argument("--pipeline", type=_count_or_zero, default=16, metavar="N",
                       help="yield to the reader every N frames (default 16)")
    bench.add_argument("--expect-ms", type=_float_in("--expect-ms", 0.01, 60_000.0),
                       default=None, help="exit 1 if the final RTT exceeds this")

    watch = sub.add_parser("watch", help="stream phone events with automatic reconnect")
    _add_target_args(watch, retries=False)
    watch.add_argument("--timeout", type=_seconds_or_zero, default=0.0,
                       help="seconds before exiting; 0 = run until Ctrl-C")
    watch.add_argument("--max-attempts", type=_int_in("--max-attempts", 1, 1000), default=None,
                       help="give up after this many connection attempts")
    watch.add_argument("--json", action="store_true",
                       help="one JSON object per line (states and events)")

    st = sub.add_parser(
        "selftest",
        help="run every workflow protocol v1 specifies, end to end",
        description=(
            "Exercises each supported workflow against the phone and prints a "
            "verification matrix. Features with no protocol operation are listed "
            "as NOT IMPLEMENTED - they are never reported as passing or skipped. "
            "Exit 1 if any supported workflow fails."
        ),
    )
    _add_target_args(st)
    st.add_argument("--frames", type=_int_in("--frames", 1, 256), default=16,
                    help="binary frames to echo for the integrity check (default 16)")
    st.add_argument("--json", action="store_true", help="machine-readable matrix")

    forget = sub.add_parser("forget", help="unpin a phone (or every phone)")
    forget.add_argument("device_id", nargs="?", help="device id from `phonebridge identity`")
    forget.add_argument("--all", action="store_true", help="forget every pinned phone")

    cp = sub.add_parser(
        "compat",
        help="multi-device audio capability records",
        description=(
            "Measure what each phone can actually do with a Bluetooth headset, "
            "and render the comparison. Nothing is inferred from specs."
        ),
    )
    csub = cp.add_subparsers(dest="compat_command", required=True)

    cr = csub.add_parser("run", help="run the capability probe on a phone and store it")
    _add_target_args(cr, timeouts=False)
    cr.add_argument("--label", default=None,
                    help='human name for the report, e.g. "Redmi Note 14 Pro+ 5G"')
    cr.add_argument("--seconds", type=_probe_seconds, default=30,
                    help="duplex soak length on the phone (5-300, default 30)")
    cr.add_argument("--acoustic", action="store_true",
                    help="also measure acoustic round-trip latency (chirp)")
    cr.add_argument("--timeout", type=_float_in("--timeout", 1.0, 3600.0), default=420.0,
                    help="seconds to wait for the phone to finish probing")
    cr.add_argument("--request-timeout", type=_float_in("--request-timeout", 0.1, 3600.0),
                    default=REQUEST_TIMEOUT_DEFAULT,
                    help="seconds to wait for short RPC answers")

    csub.add_parser("show", help="print stored records with their findings")
    csub.add_parser("compare", help="print the comparison table only")

    crep = csub.add_parser("report", help="write the Markdown compatibility report")
    crep.add_argument("--out", default=str(Path("docs") / "09-compat-report.md"),
                      help="output path (default: docs/09-compat-report.md)")
    return p


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
_ASYNC_COMMANDS = {
    "discover": _discover,
    "devices": _devices,
    "info": _info,
    "capabilities": _capabilities,
    "ping": _ping,
    "bench": _bench,
    "watch": _watch,
    "selftest": _selftest,
}


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "identity":
        return cmd_identity(args)
    if args.command == "forget":
        return cmd_forget(args)
    if args.command == "compat":
        if args.compat_command == "run":
            return asyncio.run(_compat_run(args))
        if args.compat_command == "show":
            return _compat_show(args)
        if args.compat_command == "compare":
            return _compat_compare(args)
        if args.compat_command == "report":
            return _compat_report(args)
        raise CliError(f"unknown compat command {args.compat_command!r}")
    if args.command == "audio":
        if args.audio_command == "status":
            return asyncio.run(_audio_status(args))
        if args.audio_command == "last":
            return asyncio.run(_audio_last(args))
        raise CliError(f"unknown audio command {args.audio_command!r}")
    handler = _ASYNC_COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        raise CliError(f"unknown command {args.command!r}")
    return asyncio.run(handler(args))


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return _dispatch(args)
    except CliError as exc:
        note(f"error: {exc}")
        if exc.hint:
            note(f"hint: {exc.hint}")
        return exc.code
    except (SessionError, LinkError) as exc:
        # Belt and braces: a link failure during open() is a SessionError now,
        # but a raw one must still never reach the user as a traceback.
        note(f"error: {exc}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        note("interrupted")
        return 130
    except BrokenPipeError:  # e.g. `phonebridge info | head`
        try:
            devnull = Path("/dev/null")
            if devnull.exists():  # pragma: no cover - POSIX only
                sys.stdout = open(devnull, "w")  # noqa: SIM115
        except OSError:  # pragma: no cover
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
