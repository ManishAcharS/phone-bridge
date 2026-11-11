"""Phase 5 command line: Bluetooth, sensors and file transfer.

These three planes are driven exactly the way the GUI drives them, through the
small clients in :mod:`phonebridge.bluetooth`, :mod:`phonebridge.sensors` and
:mod:`phonebridge.files`. Nothing here opens a second connection: every command
uses the authenticated session from :func:`open_session`.

The commands are deliberately honest about hardware. A scan that finds nothing
says so, a sensor stream the phone refuses prints the phone's own reason, and a
file transfer reports success only after the hash verified on both sides.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from ..bluetooth import BluetoothLink, normalise_uuid
from ..bluetooth.frames import GATT_CONNECTED, hex_of, printable_of
from ..core.device_store import DeviceStore
from ..files import ACTIVE as FILE_ACTIVE
from ..files import COMPLETE as FILE_COMPLETE
from ..files import FAILED as FILE_FAILED
from ..files import TransferManager
from ..sensors import SensorLink, kind_name, kind_unit
from ..sensors.link import ACTIVE as SENSOR_ACTIVE
from . import format as fmt
from .target import EXIT_ERROR, CliError, note, open_session

#: Transfer states that will not change again.
_FINAL_FILE_STATES = {FILE_COMPLETE, FILE_FAILED, "cancelled"}

#: How often the streaming commands redraw / print.
_POLL_S = 0.5


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _bytes(count: float) -> str:
    """Human size, for progress lines the user reads at a glance."""
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024.0 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GiB"  # pragma: no cover - unreachable


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _scan_ms(text: str) -> int:
    try:
        value = int(float(text) * 1000)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected seconds, e.g. 10") from exc
    if not 1000 <= value <= 120_000:
        raise argparse.ArgumentTypeError("expected 1 .. 120 seconds")
    return value


def _seconds(limit: float = 600.0):
    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("expected seconds") from exc
        if not 0.5 <= value <= limit:
            raise argparse.ArgumentTypeError(f"expected 0.5 .. {limit:g} seconds")
        return value

    return parse


def _interval_us(text: str) -> int:
    """Accept ``20ms``, ``20000us``, ``50hz`` or a bare number of microseconds."""
    raw = text.strip().lower()
    try:
        if raw.endswith("ms"):
            value = int(float(raw[:-2]) * 1000)
        elif raw.endswith("us"):
            value = int(float(raw[:-2]))
        elif raw.endswith("hz"):
            rate = float(raw[:-2])
            if rate <= 0:
                raise ValueError("rate must be positive")
            value = int(1_000_000 / rate)
        else:
            value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected e.g. 20ms, 20000us or 50hz") from exc
    if value < 1000:
        raise argparse.ArgumentTypeError("the phone cannot sample faster than 1000us")
    return value


def _device_store(args: argparse.Namespace) -> DeviceStore:
    """The same config directory every other command uses."""
    config_dir = getattr(args, "config_dir", None)
    return DeviceStore(Path(config_dir) if config_dir else None)


def _split_path(path: str) -> tuple[str, str]:
    parts = [part for part in path.replace("/", ":").split(":") if part]
    if len(parts) != 2:
        raise CliError("characteristic must be given as SERVICE:CHARACTERISTIC")
    try:
        return normalise_uuid(parts[0]), normalise_uuid(parts[1])
    except Exception as exc:  # noqa: BLE001 - the sentence is the point
        raise CliError(f"{path}: {exc}") from exc


# --------------------------------------------------------------------------- #
# bluetooth
# --------------------------------------------------------------------------- #
async def bluetooth_scan(args: argparse.Namespace) -> int:
    """Scan for BLE peripherals and print what the radio actually saw."""
    session, _target = await open_session(args, _device_store(args))
    link = BluetoothLink(session)
    session.on_event = link.on_event
    try:
        adapter = await link.refresh_adapter()
        if args.format != "json":
            print(f"adapter: {_adapter_line(adapter)}")
            if adapter.detail:
                print(f"         {adapter.detail}")
        if not adapter.supported:
            note("this phone has no Bluetooth LE radio; nothing to scan")
            return EXIT_ERROR
        if not adapter.enabled:
            note("Bluetooth is switched off on the phone; switch it on and retry")
            return EXIT_ERROR
        if adapter.permitted is False:
            note(
                "the phone has not granted the Bluetooth scan permission; grant it "
                "in PhoneBridge on the phone and retry"
            )
            return EXIT_ERROR

        started = await link.start_scan(
            duration_ms=args.duration_ms,
            filter_uuids=args.uuid,
            name_prefix=args.name,
        )
        if not started:
            note(f"the phone refused to scan: {link.snapshot().detail}")
            return EXIT_ERROR

        deadline = time.monotonic() + args.duration_ms / 1000.0
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_S if args.format != "json" else 1.0)
            scan = await link.refresh_scan()
            if not scan.scanning:
                break
            if args.format != "json" and not args.quiet:
                print(f"\r  scanning … {len(scan.devices)} seen", end="", flush=True)
        await link.stop_scan()
        scan = await link.refresh_scan()
        devices = scan.devices
    finally:
        await link.close()
        await session.close()

    if args.format == "json":
        _print_json(
            {
                "adapter": adapter.to_dict(),
                "scan": scan.to_dict(),
                "devices": [device.to_dict() for device in devices],
            }
        )
        return 0
    if not args.quiet:
        print(f"\r  found {len(devices)} peripheral(s)          ")
    print(render_devices(devices))
    return 0


def _adapter_line(adapter: Any) -> str:
    if not adapter.supported:
        return "no Bluetooth LE radio"
    state = "on" if adapter.enabled else "off"
    name = adapter.name or "(unnamed)"
    permission = {True: "granted", False: "not granted", None: "not reported"}
    return (
        f"{name} [{adapter.address or 'address hidden'}] {state}, "
        f"permission {permission.get(adapter.permitted, adapter.permitted)}"
    )


async def bluetooth_gatt(args: argparse.Namespace) -> int:
    """Discover services, read, write or watch notifications on one peripheral."""
    session, _target = await open_session(args, _device_store(args))
    link = BluetoothLink(session)
    session.on_event = link.on_event
    try:
        if args.gatt_command == "services":
            tree = await link.connect(args.address)
            if tree.state != GATT_CONNECTED or tree.tree is None:
                note(f"could not connect to {args.address}: {tree.error or tree.state}")
                return EXIT_ERROR
            tree_ = tree.tree
            if args.format == "json":
                _print_json(tree_.to_dict())
            else:
                print(render_gatt(tree_))
            return 0

        if args.gatt_command == "read":
            service, characteristic = _split_path(args.path)
            connection = await link.connect(args.address)
            if connection.state != GATT_CONNECTED:
                note(f"could not connect: {connection.error or connection.state}")
                return EXIT_ERROR
            try:
                data = await link.read(args.address, service, characteristic)
            finally:
                await link.disconnect(args.address)
            if args.format == "json":
                _print_json(
                    {
                        "value_b64": __import__("base64").b64encode(data).decode(),
                        "hex": hex_of(data),
                        "text": printable_of(data),
                        "length": len(data),
                    }
                )
            else:
                print(f"value: {hex_of(data)}  ({len(data)} bytes)")
                if printable_of(data):
                    print(f"text : {printable_of(data)!r}")
            return 0

        if args.gatt_command == "write":
            service, characteristic = _split_path(args.path)
            payload = _payload(args.data)
            await link.connect(args.address)
            try:
                written = await link.write(
                    args.address,
                    service,
                    characteristic,
                    payload,
                    with_response=args.mode != "without",
                )
            finally:
                await link.disconnect(args.address)
            if args.format == "json":
                _print_json({"written": written, "mode": args.mode})
            else:
                print(f"wrote {written} byte(s) ({args.mode} response)")
            return 0

        if args.gatt_command == "watch":
            service, characteristic = _split_path(args.path)
            await link.connect(args.address)
            try:
                subscribed = await link.subscribe(args.address, service, characteristic)
                if not subscribed:
                    note("the phone did not subscribe; check the characteristic properties")
                    return EXIT_ERROR
                note(f"watching {args.address} for {args.seconds:g}s (Ctrl-C to stop)")
                await _watch_notifications(link, args.address, args.seconds, args.format)
            finally:
                await link.disconnect(args.address)
            return 0
        raise CliError(f"unknown gatt command {args.gatt_command!r}")  # pragma: no cover
    finally:
        await link.close()
        await session.close()


async def _watch_notifications(
    link: BluetoothLink, address: str, seconds: float, mode: str
) -> None:
    deadline = time.monotonic() + seconds
    seen = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(_POLL_S)
        connection = link.connection(address)
        if connection is None or connection.state != GATT_CONNECTED:
            note("the peripheral disconnected")
            return
        for item in connection.notifications[seen:]:
            if mode == "json":
                _print_json(item.to_dict())
            else:
                print(
                    f"{item.timestamp_ms:8d}ms  {short_of(item.characteristic)}  "
                    f"{hex_of(item.value)}  {printable_of(item.value)!r}"
                )
            seen += 1


def short_of(uuid: str) -> str:
    from ..bluetooth.frames import short_uuid

    return short_uuid(uuid)


def _payload(data: str) -> bytes:
    if data.startswith("hex:"):
        try:
            return bytes.fromhex(data[4:].strip())
        except ValueError as exc:
            raise CliError(f"not hex: {exc}") from exc
    return data.encode("utf-8")


# --------------------------------------------------------------------------- #
# sensors
# --------------------------------------------------------------------------- #
async def sensors_list(args: argparse.Namespace) -> int:
    session, _target = await open_session(args, _device_store(args))
    link = SensorLink(session)
    try:
        registry = await link.refresh()
        snapshot = link.snapshot()
    finally:
        await session.close()

    if args.format == "json":
        _print_json(snapshot.to_dict())
        return 0
    print(render_sensors(registry))
    if snapshot.state == "error":
        note(snapshot.detail)
        return EXIT_ERROR
    return 0


async def sensors_stream(args: argparse.Namespace) -> int:
    """Stream readings for a while and print the rate the PC actually measured."""
    session, _target = await open_session(args, _device_store(args))
    # The sensor plane needs no event hook: readings arrive as stream frames.
    link = SensorLink(session, interval_us=args.interval_us)
    try:
        registry = await link.refresh()
        if args.sensor:
            wanted = list(args.sensor)
        else:
            wanted = [info.id for info in registry.ordered()][: args.limit]
        if not wanted:
            note("this phone reported no sensors to stream")
            return EXIT_ERROR
        note(
            f"streaming sensors {', '.join(str(i) for i in wanted)} for "
            f"{args.seconds:g}s at {args.interval_us}us"
        )
        if not await link.start(wanted):
            note(f"the phone refused: {link.snapshot().detail}")
            return EXIT_ERROR
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_S)
            if args.format == "json":
                _print_json(link.snapshot().to_dict())
            else:
                print(_render_readings(link))
            if link.state != SENSOR_ACTIVE:
                note(f"stream ended: {link.snapshot().detail or link.state}")
                break
    finally:
        await link.close()
        await session.close()
    return 0


def _render_readings(link: SensorLink) -> str:
    snapshot = link.snapshot()
    registry = snapshot.registry
    stats = snapshot.stats or {}
    lines = [
        "",
        f"state={snapshot.state}  interval={link.interval_us}us  "
        f"active={list(registry.active) if registry else []}",
    ]
    for sensor_id, reading in sorted((registry.readings if registry else {}).items()):
        axes = " ".join(
            f"{name}={value:+.3f}" for name, value in reading.reading.items()
        )
        counter = stats.get(str(sensor_id), {})
        measured = counter.get("received", 0)
        lines.append(
            f"  [{sensor_id:2d}] {kind_name(reading.kind):<22s} "
            f"seq={reading.seq:<6d} ts={reading.timestamp_ms:>8d}ms "
            f"{axes} {kind_unit(reading.kind)}  ({measured} frames, "
            f"{reading.hz:.1f} Hz)"
        )
    if len(lines) == 2:
        lines.append("  (no readings yet)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #
async def files_send(args: argparse.Namespace) -> int:
    sources = [Path(item) for item in args.paths]
    for source in sources:
        if not source.is_file():
            raise CliError(f"{source} is not a file")
        if source.stat().st_size > 4 * 1024 * 1024 * 1024:
            raise CliError(f"{source.name} is larger than the 4 GiB protocol limit")

    session, _target = await open_session(args, _device_store(args))
    manager = TransferManager(session)
    failed = 0
    try:
        for source in sources:
            try:
                stats = await _send_with_progress(manager, source, args)
            except Exception as exc:  # noqa: BLE001 - the sentence matters
                note(f"could not send {source.name}: {exc}")
                failed += 1
                continue
            if args.format == "json":
                _print_json(stats.to_dict())
            else:
                print(render_transfer(stats))
            if not stats.verified:
                note("the phone did not confirm the hash; treating this as a failure")
                failed += 1
    finally:
        await manager.close()
        await session.close()
    return EXIT_ERROR if failed else 0


async def _send_with_progress(
    manager: TransferManager, source: Path, args: argparse.Namespace
) -> Any:
    task = asyncio.create_task(manager.send_file(source))
    shown = -1
    while not task.done():
        await asyncio.sleep(_POLL_S)
        transfers = manager.transfers
        if not transfers or args.format == "json":
            continue
        stats = transfers[-1]
        percent = int(stats.fraction * 100)
        if percent != shown:
            shown = percent
            speed = stats.speed_bps(time.monotonic())
            print(
                f"\r  {source.name}: {percent:3d}% "
                f"{_bytes(stats.bytes_done)}/{_bytes(stats.size)} "
                f"{_bytes(speed)}/s",
                end="",
                flush=True,
            )
    if shown >= 0:
        print()
    return await task


async def files_receive(args: argparse.Namespace) -> int:
    destination = Path(args.into).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    session, _target = await open_session(args, _device_store(args))
    manager = TransferManager(session, destination=destination)
    try:
        stats = await manager.request_file(args.name or "")
        note(f"receiving {stats.name} ({_bytes(stats.size)}) into {destination}")
        while True:
            current = manager.get(stats.transfer_id)
            if current is None:  # pragma: no cover - defensive
                break
            stats = current
            if stats.state in _FINAL_FILE_STATES:
                break
            await asyncio.sleep(_POLL_S)
    finally:
        await manager.close()
        await session.close()

    if args.format == "json":
        _print_json(stats.to_dict())
    else:
        print(render_transfer(stats))
    if stats.state == FILE_COMPLETE and stats.verified:
        return 0
    if stats.state == FILE_ACTIVE:  # pragma: no cover - defensive
        note("the transfer did not finish")
    return EXIT_ERROR


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def render_devices(devices: Any) -> str:
    if not devices:
        return "  (no peripherals seen — a real result, not an error)"
    lines = []
    for device in devices:
        flags = "connectable" if device.connectable else "not connectable"
        lines.append(
            f"  {device.address:<17s} {device.rssi:>4d} dBm  "
            f"{device.display_name:<28s} [{flags}, seen {device.sightings}x]"
        )
        if device.services:
            lines.append("      services: " + ", ".join(device.services))
    return "\n".join(lines)


def render_gatt(tree: Any) -> str:
    lines = [f"{tree.address}  (mtu {tree.mtu})"]
    for service in tree.services:
        lines.append(f"  service {service.short}  {service.uuid}")
        for characteristic in service.characteristics:
            properties = ",".join(characteristic.properties) or "no properties"
            lines.append(f"    char {characteristic.short:<6s} [{properties}]")
            for descriptor in characteristic.descriptors:
                lines.append(f"      desc {descriptor}")
    return "\n".join(lines)


def render_sensors(registry: Any) -> str:
    if not registry.sensors:
        return "  (the phone reported no sensors)"
    lines = []
    for info in registry.sensors:
        rate = f"{info.max_hz} Hz max" if info.max_hz else "rate not advertised"
        lines.append(
            f"  [{info.id:2d}] {info.label:<26s} {kind_name(info.kind):<24s} "
            f"{rate:<16s} {info.unit or '-'}"
        )
        if info.vendor:
            lines.append(f"       vendor: {info.vendor}")
    if registry.detail:
        lines.append(f"  note: {registry.detail}")
    if registry.permitted is False:
        lines.append("  ! the phone has not granted sensor permission")
    return "\n".join(lines)


def render_transfer(stats: Any) -> str:
    now = time.monotonic()
    lines = [
        f"{stats.direction}: {stats.name}",
        f"  state       {stats.state}",
        f"  size        {_bytes(stats.size)} ({stats.size} bytes)",
        f"  transferred {_bytes(stats.bytes_done)} at {_bytes(stats.speed_bps(now))}/s",
        f"  sha256      {stats.sha256 or '(not reported)'}",
        f"  verified    {'yes' if stats.verified else 'no'}",
    ]
    if stats.resumed_from:
        lines.append(f"  resumed at  {_bytes(stats.resumed_from)} offset")
    if stats.retries:
        lines.append(f"  retries     {stats.retries}")
    if stats.error:
        lines.append(f"  error       {stats.error}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# parser wiring and dispatch
# --------------------------------------------------------------------------- #
def add_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from .main import _add_format_args, _add_target_args

    bt = sub.add_parser(
        "bluetooth",
        help="scan for BLE peripherals and talk GATT through the phone",
        description=(
            "The phone is the radio: it scans and connects, and the PC drives it "
            "over the authenticated session. Nothing is connected to without an "
            "address you named."
        ),
    )
    bsub = bt.add_subparsers(dest="bluetooth_command", required=True)

    scan = bsub.add_parser("scan", help="scan for BLE peripherals")
    _add_target_args(scan)
    _add_format_args(scan, default="text")
    scan.add_argument("--duration", dest="duration_ms", type=_scan_ms, default=10_000,
                      metavar="SECONDS", help="scan length in seconds (default 10)")
    scan.add_argument("--uuid", action="append", metavar="UUID",
                      help="only report devices advertising this service (repeatable)")
    scan.add_argument("--name", default="", metavar="PREFIX",
                      help="only report names starting with this prefix")
    scan.add_argument("--quiet", action="store_true", help="omit the found-N line")

    gatt = bsub.add_parser(
        "gatt", help="connect, discover, read, write or watch one peripheral"
    )
    gsub = gatt.add_subparsers(dest="gatt_command", required=True)

    services = gsub.add_parser("services", help="discover services and characteristics")
    _add_target_args(services)
    _add_format_args(services, default="text")
    services.add_argument("address", help="peripheral address from `bluetooth scan`")

    read = gsub.add_parser("read", help="read one characteristic")
    _add_target_args(read)
    _add_format_args(read, default="text")
    read.add_argument("address")
    read.add_argument("path", metavar="SERVICE:CHARACTERISTIC")

    write = gsub.add_parser("write", help="write one characteristic")
    _add_target_args(write)
    _add_format_args(write, default="text")
    write.add_argument("address")
    write.add_argument("path", metavar="SERVICE:CHARACTERISTIC")
    write.add_argument("data", help="text, or hex:0a0b")
    write.add_argument("--mode", choices=["auto", "with", "without"], default="auto",
                       help="write type (default: with a response)")

    watch = gsub.add_parser("watch", help="subscribe and print notifications")
    _add_target_args(watch)
    _add_format_args(watch, default="text")
    watch.add_argument("address")
    watch.add_argument("path", metavar="SERVICE:CHARACTERISTIC")
    watch.add_argument("--seconds", type=_seconds(3600.0), default=10.0,
                       help="how long to watch (default 10)")

    sensors = sub.add_parser(
        "sensors",
        help="list the phone's sensors, or stream readings for a while",
        description=(
            "Readings are measured by the phone. The PC prints what arrived and "
            "the rate it actually measured; it never invents a sample."
        ),
    )
    ssub = sensors.add_subparsers(dest="sensors_command", required=True)

    slist = ssub.add_parser("list", help="what the phone reports, with metadata")
    _add_target_args(slist)
    _add_format_args(slist, default="text")

    stream = ssub.add_parser("stream", help="stream readings and print measured rates")
    _add_target_args(stream)
    _add_format_args(stream, default="text")
    stream.add_argument("--sensor", type=int, action="append", metavar="ID",
                        help="sensor id from `sensors list` (repeatable)")
    stream.add_argument("--limit", type=int, default=3, metavar="N",
                        help="stream the first N sensors when --sensor is not given")
    stream.add_argument("--interval", dest="interval_us", type=_interval_us,
                        default=20_000, metavar="RATE",
                        help="sampling interval: 20ms, 5000us or 50hz (default 20ms)")
    stream.add_argument("--seconds", type=_seconds(), default=5.0,
                        help="how long to stream (default 5)")

    files = sub.add_parser(
        "files",
        help="send files to the phone, or receive the one it offers",
        description=(
            "Chunked, hashed transfers over the existing session. The phone asks "
            "its owner before writing anything, and a transfer is reported "
            "complete only after the hash verified."
        ),
    )
    fsub = files.add_subparsers(dest="files_command", required=True)

    send = fsub.add_parser("send", help="send one or more files to the phone")
    _add_target_args(send)
    _add_format_args(send, default="text")
    send.add_argument("paths", nargs="+", metavar="FILE")

    receive = fsub.add_parser("receive", help="receive the file the phone offers")
    _add_target_args(receive)
    _add_format_args(receive, default="text")
    receive.add_argument("--into", default=".", metavar="DIR",
                         help="destination directory (default: current)")
    receive.add_argument("--name", default="", metavar="NAME",
                         help="the name to expect, when the phone needs a hint")


_COMMANDS: dict[tuple[str, str], Any] = {
    ("bluetooth", "scan"): bluetooth_scan,
    ("bluetooth", "gatt"): bluetooth_gatt,
    ("sensors", "list"): sensors_list,
    ("sensors", "stream"): sensors_stream,
    ("files", "send"): files_send,
    ("files", "receive"): files_receive,
}

#: Sub-selector attribute per command, so dispatch stays a two-line lookup.
_SECOND = {
    "bluetooth": "bluetooth_command",
    "sensors": "sensors_command",
    "files": "files_command",
}


def run(args: argparse.Namespace) -> int:
    """Run one Phase 5 command; ``main`` routes here by ``args.command``."""
    second = str(getattr(args, _SECOND[args.command], "") or "")
    handler = _COMMANDS.get((args.command, second))
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        raise CliError(f"unknown {args.command} command {second!r}")
    return asyncio.run(handler(args))


__all__ = [
    "add_parsers",
    "run",
    "render_devices",
    "render_gatt",
    "render_sensors",
    "render_transfer",
    "bluetooth_scan",
    "bluetooth_gatt",
    "sensors_list",
    "sensors_stream",
    "files_send",
    "files_receive",
]

# `fmt` is kept for symmetry with the other command modules; the plain-text
# renderers here are self-contained.
del fmt
