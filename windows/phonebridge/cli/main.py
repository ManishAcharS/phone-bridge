"""``phonebridge`` command line.

Phase 1 exposes exactly what is needed to prove the link works:

    phonebridge doctor                   inspect this PC (Phase 0 audit)
    phonebridge identity                 show this PC's device id / config dir
    phonebridge discover                 listen for phone beacons
    phonebridge info --host <ip>         connect, print phone capabilities
    phonebridge ping --host <ip>         RTT histogram
    phonebridge bench --host <ip>        encrypted binary throughput + latency
    phonebridge watch --host <ip>        print phone -> PC events
    phonebridge forget <device-id>       drop a pinned phone
"""

from __future__ import annotations

import argparse
import asyncio
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

from ..core import compat
from ..core.device_store import DeviceStore
from ..core.session import BridgeSession, ReconnectingSession, SessionError
from ..transport.discovery import HELP as DISCOVERY_HELP
from ..transport.discovery import DiscoveryService
from ..transport.link import DEFAULT_PORT, websocket_uri

log = logging.getLogger("phonebridge.cli")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _store(args: argparse.Namespace) -> DeviceStore:
    return DeviceStore(Path(args.config_dir) if args.config_dir else None)


def _pairing_prompt(sas: str) -> bool:
    print()
    print("  ┌─────────────────────────────────────────────────────────┐")
    print(f"  │  PAIRING CODE:  {sas:^39}│")
    print("  └─────────────────────────────────────────────────────────┘")
    print("  The phone shows the same six digits if the link is genuine.")
    print("  If the digits differ, someone is intercepting the connection:")
    print("  answer 'n' and stop.")
    try:
        answer = input("  Codes match? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


async def _connect(args: argparse.Namespace, store: DeviceStore) -> BridgeSession:
    identity = store.load_or_create_identity()
    uri = websocket_uri(args.host, args.port)
    confirm = (lambda _sas: True) if getattr(args, "auto_pair", False) else _pairing_prompt
    session = BridgeSession(uri, identity, store, confirm_pairing=confirm)
    try:
        await session.open()
    except SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    return session



# --------------------------------------------------------------------------- #
# compat - multi-device capability records
# --------------------------------------------------------------------------- #
async def _compat_run(args: argparse.Namespace) -> int:
    """Ask the phone to measure itself, then store the report."""
    store = _store(args)
    session = await _connect(args, store)
    try:
        print(f"running capability probe on {session.uri} "
              f"(duplex soak {args.seconds}s{', acoustic latency' if args.acoustic else ''}) ...")
        params = {"seconds": args.seconds, "acoustic": args.acoustic}
        report = await session.call("audio.capabilities.run", params, timeout=args.timeout)
    finally:
        await session.close()

    if not isinstance(report, dict):
        print("error: the phone returned no report", file=sys.stderr)
        return 2
    if args.label:
        report["label"] = args.label
    report.setdefault("captured_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    problems = compat.validate_report(report)
    if problems:
        print("error: the phone returned a malformed report:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(json.dumps(report, indent=2), file=sys.stderr)
        return 2

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
        return 1
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
        return 1
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
def cmd_doctor(args: argparse.Namespace) -> int:
    ok = "  [ok]  "
    warn = "  [warn]"
    print("PhoneBridge environment report")
    print("=" * 66)
    print(f"  time          : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  platform      : {platform.platform()}")
    print(f"  machine       : {platform.machine()}")
    print(f"  python        : {sys.version.split()[0]} ({sys.executable})")

    is_windows = sys.platform == "win32"
    print(f"{ok if is_windows else warn}windows      : {'yes' if is_windows else 'NO - this is ' + sys.platform}")
    if not is_windows:
        print("         The PC application targets Windows; audio/virtual-device")
        print("         features need Windows APIs. Protocol tests still run here.")

    # --- optional packages ------------------------------------------------ #
    for mod, why in [
        ("websockets", "required for the control channel"),
        ("cryptography", "required for pairing and encryption"),
        ("soundcard", "Phase 2: WASAPI loopback capture of PC audio"),
        ("sounddevice", "Phase 2/3: low-latency audio playback"),
        ("numpy", "Phase 2+: audio/video buffers"),
        ("av", "Phase 2: Opus encode/decode (bundles libopus)"),
        ("PySide6", "Phase 3: desktop dashboard"),
        ("pyvirtualcam", "Phase 4: virtual webcam output"),
    ]:
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "?")
            print(f"{ok}{mod:<13}: {ver}")
        except Exception:  # noqa: BLE001
            print(f"{warn}{mod:<13}: missing  ({why})")

    # --- Windows-specific bits -------------------------------------------- #
    if is_windows:
        print("-" * 66)
        print("  Windows integration")
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            script = (
                "Get-CimInstance Win32_SoundDevice | "
                "Select-Object -ExpandProperty Name"
            )
            try:
                out = subprocess.run(
                    [ps, "-NoProfile", "-Command", script],
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
            out = subprocess.run(
                ["adb", "version"], capture_output=True, text=True, timeout=10
            ).stdout.splitlines()
            print(f"{ok}adb          : {out[0] if out else 'present'}")
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
    local_ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
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
# other commands
# --------------------------------------------------------------------------- #
def cmd_identity(args: argparse.Namespace) -> int:
    store = _store(args)
    ident = store.load_or_create_identity()
    print(f"device id   : {ident.device_id}")
    print(f"device name : {ident.name}")
    print(f"config dir  : {store.path_summary()}")
    peers = store.list_peers()
    if not peers:
        print("paired phones: none yet")
    else:
        print(f"paired phones ({len(peers)}):")
        for p in peers:
            print(f"  - {p.peer_name}  id={p.peer_id}  since {p.added_at}")
    for ep in store.known_endpoints():
        print(f"last seen   : {ep['transport']} {ep['host']}:{ep['port']} at {ep['at']}")
    return 0


async def _discover(args: argparse.Namespace) -> int:
    svc = DiscoveryService()
    await svc.start()
    print(f"Listening for phone beacons on UDP {svc.port} for {args.timeout}s ...")
    deadline = time.time() + args.timeout
    seen: set[str] = set()
    try:
        while time.time() < deadline:
            for phone in svc.live():
                if phone.device_id not in seen:
                    seen.add(phone.device_id)
                    print(f"  found: {phone.name} ({phone.device_id}) at {phone.uri} "
                          f"via {phone.host}")
            await asyncio.sleep(0.25)
    finally:
        await svc.stop()
    if not seen:
        print("  no phones found.")
        print("  " + DISCOVERY_HELP)
        return 1
    return 0


async def _info(args: argparse.Namespace) -> int:
    store = _store(args)
    session = await _connect(args, store)
    try:
        info = await session.call("system.info")
        print(json.dumps(info, indent=2, sort_keys=True))
        store.remember_endpoint(args.transport, args.host, args.port)
    finally:
        await session.close()
    return 0


async def _ping(args: argparse.Namespace) -> int:
    store = _store(args)
    session = await _connect(args, store)
    samples: list[float] = []
    try:
        print(f"pinging {session.phone.name} {args.count} times ...")
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
    session = await _connect(args, store)
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
        if args.expect_ms is not None:
            ok = rtt <= args.expect_ms
            print(f"  threshold     : {args.expect_ms} ms -> {'PASS' if ok else 'FAIL'}")
            return 0 if ok else 1
    finally:
        await session.close()
    return 0


async def _watch(args: argparse.Namespace) -> int:
    store = _store(args)
    identity = store.load_or_create_identity()
    confirm = (lambda _sas: True) if args.auto_pair else _pairing_prompt

    def on_state(state: str, details: dict) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {state:<12} {details if details else ''}")

    def on_event(name: str, data: dict) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] event {name}: {json.dumps(data)}")

    sup = ReconnectingSession(
        websocket_uri(args.host, args.port),
        identity,
        store,
        on_state=on_state,
        confirm_pairing=confirm,
        on_event=on_event,
    )
    await sup.start()
    try:
        await asyncio.wait_for(_until_interrupt(), None if args.timeout == 0 else args.timeout)
    except (asyncio.TimeoutError, KeyboardInterrupt):
        pass
    finally:
        await sup.stop()
    print("stopped")
    return 0


async def _until_interrupt() -> None:
    while True:
        await asyncio.sleep(3600)


def cmd_forget(args: argparse.Namespace) -> int:
    store = _store(args)
    if store.forget_peer(args.device_id):
        print(f"forgot {args.device_id}; it must pair again next time")
        return 0
    print(f"no pinned phone with id {args.device_id}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="phonebridge",
        description="PhoneBridge PC client (protocol v1)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config-dir", help="override the config directory (tests/portable mode)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="inspect this PC's development environment")
    sub.add_parser("identity", help="show this PC's identity and pinned phones")

    d = sub.add_parser("discover", help="listen for phone discovery beacons")
    d.add_argument("--timeout", type=float, default=8.0)

    for name, _handler, helptext in [
        ("info", _info, "connect and print the phone's capabilities"),
        ("ping", _ping, "measure application-level round-trip time"),
        ("bench", _bench, "encrypted binary throughput test"),
        ("watch", _watch, "stream phone events with automatic reconnect"),
    ]:
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--host", required=True, help="phone IP, or 127.0.0.1 with adb forward")
        sp.add_argument("--port", type=int, default=DEFAULT_PORT)
        sp.add_argument("--transport", default="auto", choices=["auto", "usb", "wifi", "hotspot"])
        sp.add_argument("--auto-pair", action="store_true",
                        help="accept pairing without the code comparison (tests only)")
        if name == "ping":
            sp.add_argument("--count", type=int, default=10)
            sp.add_argument("--interval", type=float, default=0.1)
        if name == "bench":
            sp.add_argument("--frames", type=int, default=2000)
            sp.add_argument("--size", type=int, default=1024)
            sp.add_argument("--pipeline", type=int, default=16)
            sp.add_argument("--expect-ms", type=float, default=None,
                            help="fail if final RTT exceeds this")
        if name == "watch":
            sp.add_argument("--timeout", type=float, default=0.0,
                            help="seconds before exiting; 0 = run until Ctrl-C")

    f = sub.add_parser("forget", help="unpin a phone")
    f.add_argument("device_id")

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
    cr.add_argument("--host", required=True, help="phone IP, or 127.0.0.1 with adb forward")
    cr.add_argument("--port", type=int, default=DEFAULT_PORT)
    cr.add_argument("--label", default=None,
                    help='human name for the report, e.g. "Redmi Note 14 Pro+ 5G"')
    cr.add_argument("--seconds", type=int, default=30,
                    help="duplex soak length on the phone (5-300, default 30)")
    cr.add_argument("--acoustic", action="store_true",
                    help="also measure acoustic round-trip latency (chirp)")
    cr.add_argument("--timeout", type=float, default=420.0,
                    help="seconds to wait for the phone to finish probing")
    cr.add_argument("--auto-pair", action="store_true")

    csub.add_parser("show", help="print stored records with their findings")
    csub.add_parser("compare", help="print the comparison table only")

    crep = csub.add_parser("report", help="write the Markdown compatibility report")
    crep.add_argument("--out", default=str(Path("docs") / "09-compat-report.md"),
                      help="output path (default: docs/09-compat-report.md)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
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
        raise SystemExit(f"unknown compat command {args.compat_command!r}")
    try:
        if args.command == "discover":
            return asyncio.run(_discover(args))
        if args.command == "info":
            return asyncio.run(_info(args))
        if args.command == "ping":
            return asyncio.run(_ping(args))
        if args.command == "bench":
            return asyncio.run(_bench(args))
        if args.command == "watch":
            return asyncio.run(_watch(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
