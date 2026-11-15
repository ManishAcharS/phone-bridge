"""One-click daily launch: find the phone, start the bridge, prove it answers.

This is the transport half of ``phonebridge start --daily``. It deliberately does
not open OBS, VB-CABLE or any media stream: :class:`~phonebridge.live.runner.LiveRunner`
already owns those and reports a missing device with the exact install hint.

Rules that matter for daily use:

* USB is addressed by serial (``adb -s``), never by "the first device" when two
  phones are attached. The forward is re-created before every reconnect attempt.
* ``am start`` is only a request. The launcher waits for an authenticated PING
  before it calls the bridge ready or remembers the address.
* Wi-Fi discovery is a convenience, not a trust boundary. A saved address is tried
  first, then the beacon source, then the beacon's advertised addresses. The pinned
  handshake still decides whether the phone is allowed in.
* A locked phone can keep an *already running* foreground service alive, but Android
  14+/HyperOS may refuse a cold camera/microphone foreground-service start behind the
  keyguard. The app defers that start until the Activity is resumed; this launcher
  reports the timeout and says "unlock once" instead of pretending ``am start`` proved
  the service started.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..cli.target import CliError, Target, note, open_session
from ..controller.adb import Adb, AdbDevice, AdbError
from ..core.device_store import DeviceStore
from ..core.session import SessionError, TerminalSessionError
from ..transport.discovery import HELP as DISCOVERY_HELP
from ..transport.discovery import DiscoveredPhone, DiscoveryService
from ..transport.link import DEFAULT_PORT, LinkError
from .usb import is_loopback

APP_COMPONENT = "dev.phonebridge/.MainActivity"
DEFAULT_DISCOVERY_TIMEOUT_S = 6.0
DEFAULT_STARTUP_TIMEOUT_S = 45.0
RECONNECT_TIMEOUT_S = 10.0
LAUNCH_RETRY_INTERVAL_S = 15.0

USB_FIX = (
    "USB fix: plug in the phone, unlock it, allow USB debugging, then run `adb devices`. "
    "If PhoneBridge is not listening, open it once and tap Start Bridge; grant Camera "
    "and Microphone. On HyperOS enable Autostart and set battery to No restrictions. "
    "For the phone's reason: `adb -s <serial> logcat -s PhoneBridgeAutostart BridgeService`."
)
WIFI_FIX = (
    "Wi-Fi/hotspot fix: open PhoneBridge on the phone and tap Start Bridge; keep the PC "
    "and phone on the same network. Check `Test-NetConnection <phone-ip> -Port 8765`, "
    "allow UDP 47823 and TCP 8765 through Windows Firewall, or pass -HostName <phone-ip>."
)


class LaunchError(RuntimeError):
    """A missing launch prerequisite, with a user-facing fix."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def _terminal(exc: BaseException) -> bool:
    return isinstance(exc, TerminalSessionError) or isinstance(exc.__cause__, TerminalSessionError)


def _valid_endpoint(entry: dict[str, Any]) -> tuple[str, int] | None:
    host = str(entry.get("host") or "").strip()
    raw_port = entry.get("port", DEFAULT_PORT)
    if not host or is_loopback(host) or str(entry.get("transport") or "").lower() == "usb":
        return None
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        return None
    if not 0 < port < 65536:
        return None
    return host, port


class DailyLaunch:
    """Resolve, start and ping the phone; reusable as the runner's reconnect hook."""

    def __init__(
        self,
        args: argparse.Namespace,
        store: DeviceStore,
        *,
        adb: Any = None,
        discovery_factory: Callable[[], Any] = DiscoveryService,
        session_opener: Callable[..., Awaitable[Any]] = open_session,
        status: Callable[[str], None] = note,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.args = args
        self.store = store
        self.adb = adb if adb is not None else Adb()
        self.discovery_factory = discovery_factory
        self.session_opener = session_opener
        self.status = status
        self.clock = clock
        self.sleep = sleep
        self.route = str(getattr(args, "route", "auto") or "auto").lower()
        self.host = str(getattr(args, "host", "") or "").strip()
        self.port = getattr(args, "port", None)
        self.serial = str(getattr(args, "adb_serial", "") or "").strip()
        self.device = str(getattr(args, "device", "") or "").strip()
        self.autostart = bool(getattr(args, "autostart", True))
        self.discovery_timeout = float(
            getattr(args, "discovery_timeout", DEFAULT_DISCOVERY_TIMEOUT_S)
        )
        self.startup_timeout = float(getattr(args, "startup_timeout", DEFAULT_STARTUP_TIMEOUT_S))
        self.target: Target | None = None
        self._usb_serial = ""
        self._usb_problem = ""
        self._next_launch = 0.0

    async def open(self) -> tuple[Any, Target]:
        return await self._open(self.startup_timeout)

    async def reconnect(self) -> tuple[Any, Target]:
        """One bounded reconnect episode; LiveRunner supplies the outer backoff."""
        return await self._open(min(self.startup_timeout, RECONNECT_TIMEOUT_S))

    async def _open(self, timeout: float) -> tuple[Any, Target]:
        deadline = self.clock() + max(0.1, timeout)
        targets = await self._initial_targets()
        if not targets:
            raise LaunchError(
                "no phone found for the daily launcher",
                hint=(self._usb_problem + " " if self._usb_problem else "") + WIFI_FIX + " " + DISCOVERY_HELP,
            )
        last_error = ""
        while True:
            for target in targets:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    break
                try:
                    session = await self._connect_and_ping(target, remaining)
                except CliError as exc:
                    if _terminal(exc):
                        raise
                    last_error = str(exc)
                except (SessionError, LinkError, OSError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                else:
                    self.target = target
                    return session, target
            if self._usb_serial and self.autostart and self.clock() >= self._next_launch:
                self._launch_app()
                self._next_launch = self.clock() + LAUNCH_RETRY_INTERVAL_S
            remaining = deadline - self.clock()
            if remaining <= 0:
                where = ", ".join(t.describe() for t in targets)
                fix = USB_FIX if self._usb_serial else WIFI_FIX
                raise LaunchError(
                    f"bridge not running or not answering at {where}"
                    + (f" (last error: {last_error})" if last_error else ""),
                    hint=fix,
                )
            await self.sleep(min(1.0, remaining))

    async def _initial_targets(self) -> list[Target]:
        if self.host:
            if is_loopback(self.host):
                return [self._usb_target()]
            if self.route == "usb":
                raise LaunchError(
                    "-HostName/--host is not a USB forward address",
                    hint="use --host 127.0.0.1 for USB, or -Transport wifi for a phone IP.",
                )
            return [Target(self.host, int(self.port or DEFAULT_PORT), self._wifi_label(), "--host")]
        if self.route == "usb":
            return [self._usb_target()]
        if self.route == "auto":
            usb = self._optional_usb_target()
            if usb is not None:
                return [usb]
        return await self._wifi_targets()

    def _wifi_label(self) -> str:
        label = str(getattr(self.args, "transport", "auto") or "auto")
        return label if label in {"wifi", "hotspot"} else "wifi"

    def _optional_usb_target(self) -> Target | None:
        if not self.adb.available():
            self._usb_problem = "adb is not installed or not found; USB was skipped."
            return None
        try:
            devices = [d for d in self.adb.devices() if d.is_usb]
        except AdbError as exc:
            self._usb_problem = f"adb could not list devices: {exc}."
            return None
        if not devices:
            self._usb_problem = "no USB phone is listed by adb."
            return None
        return self._usb_target(devices)

    def _usb_target(self, devices: list[AdbDevice] | None = None) -> Target:
        if not self.adb.available():
            raise LaunchError(
                "adb is not installed or not found",
                hint="install Android Platform Tools (`winget install Google.PlatformTools`) "
                "or put adb on PATH; Wi-Fi does not need adb.",
            )
        try:
            devices = devices if devices is not None else [d for d in self.adb.devices() if d.is_usb]
        except AdbError as exc:
            raise LaunchError(f"adb could not list devices: {exc}", hint=USB_FIX) from exc
        if self.serial:
            matches = [d for d in devices if d.serial == self.serial]
            if not matches:
                raise LaunchError(
                    f"USB phone {self.serial!r} is not listed by adb",
                    hint="run `adb devices -l` and pass one USB serial with -AdbSerial/--adb-serial.",
                )
            chosen = matches[0]
            if not chosen.usable:
                raise LaunchError(chosen.explain(), hint=USB_FIX)
        else:
            usable = [d for d in devices if d.usable]
            if not usable:
                detail = devices[0].explain() if devices else "no USB phone is listed by adb"
                raise LaunchError(detail, hint=USB_FIX)
            if len(usable) > 1:
                options = ", ".join(d.serial for d in usable)
                raise LaunchError(
                    f"more than one USB phone is attached: {options}",
                    hint="pass -AdbSerial/--adb-serial with the serial from `adb devices -l`.",
                )
            chosen = usable[0]
        port = int(self.port or DEFAULT_PORT)
        try:
            self.adb.add_forward(chosen.serial, port, port)
        except AdbError as exc:
            raise LaunchError(
                f"adb forward tcp:{port} tcp:{port} failed on {chosen.serial}: {exc}",
                hint=USB_FIX,
            ) from exc
        self._usb_serial = chosen.serial
        return Target("127.0.0.1", port, "usb", f"USB {chosen.serial}")

    def _launch_app(self) -> None:
        token = uuid.uuid4().hex
        argv = [
            "am", "start", "-W", "-n", APP_COMPONENT,
            "--ez", "autostart", "true", "--es", "autostart_token", token,
        ]
        try:
            result = self.adb.shell(self._usb_serial, argv)
        except AdbError as exc:
            raise LaunchError(
                f"could not launch PhoneBridge on {self._usb_serial}: {exc}",
                hint="install the new APK with `adb install -r android\\app\\build\\outputs\\apk\\debug\\app-debug.apk`. "
                + USB_FIX,
            ) from exc
        text = f"{result.stdout}\n{result.stderr}".strip()
        if int(result.returncode) != 0 or re.search(r"\b(?:Error|Exception|SecurityException)\b", text):
            raise LaunchError(
                f"PhoneBridge did not accept the autostart intent on {self._usb_serial}: {text or 'no output'}",
                hint="install the APK that handles `--ez autostart true`, unlock the phone once, "
                "and open PhoneBridge manually if HyperOS blocks the launch. " + USB_FIX,
            )
        self.status(
            f"requested PhoneBridge autostart on {self._usb_serial}; "
            "waiting for an authenticated ping (unlock the phone once if it is locked)"
        )

    async def _connect_and_ping(self, target: Target, remaining: float) -> Any:
        per_try = max(0.1, min(3.0, remaining))
        probe_args = argparse.Namespace(
            **{
                **vars(self.args),
                "connect_timeout": per_try,
                "request_timeout": per_try,
                "retries": 0,
            }
        )
        session, _ = await self.session_opener(
            probe_args, self.store, quiet=True, target=target, remember=False
        )
        try:
            rtt = await session.ping(timeout=per_try)
        except Exception:
            with contextlib.suppress(Exception):
                await session.close("ping failed during daily launch")
            raise
        phone = getattr(session, "phone", None)
        if phone is None:
            await session.close("no authenticated phone identity")
            raise SessionError("phone answered the socket but not the authenticated handshake")
        self.store.remember_endpoint(target.transport, target.host, target.port, peer_id=phone.device_id)
        build = getattr(phone, "build", "") or "build not reported (older APK)"
        self.status(
            f"bridge ready: {phone.name} via {target.describe()}, ping {rtt:.1f} ms, app {build}"
        )
        return session

    async def _wifi_targets(self) -> list[Target]:
        phones = await self._listen()
        peers = self.store.list_peers()
        wanted_peer = self._wanted_peer(peers)
        label = self._wifi_label()
        candidates: list[Target] = []
        seen: set[tuple[str, int]] = set()

        def add(host: str, port: int, origin: str) -> None:
            host = host.strip()
            if not host or is_loopback(host) or not 0 < port < 65536:
                return
            key = (host.lower(), port)
            if key in seen:
                return
            seen.add(key)
            candidates.append(Target(host, port, label, origin))

        def add_saved(peer_id: str | None) -> None:
            entries = self.store.known_endpoints()
            for entry in entries:
                if peer_id is not None and entry.get("device_id") != peer_id:
                    continue
                endpoint = _valid_endpoint(entry)
                if endpoint is None:
                    continue
                host, saved_port = endpoint
                add(host, int(self.port or saved_port), "last working address")

        def add_beacon(phone: DiscoveredPhone) -> None:
            port = int(self.port or phone.port)
            add(phone.host, port, "discovery beacon")
            for host in phone.hosts:
                add(host, port, "discovery beacon address")

        if wanted_peer is not None:
            add_saved(wanted_peer.peer_id)
            matching = [p for p in phones if p.device_id == wanted_peer.peer_id]
            for phone in matching:
                add_beacon(phone)
            if not candidates:
                raise LaunchError(
                    f"no reachable address found for pinned phone {wanted_peer.peer_name}",
                    hint=WIFI_FIX + " " + DISCOVERY_HELP,
                )
            return candidates

        pinned = {p.peer_id for p in peers}
        matching = [p for p in phones if p.device_id in pinned] if pinned else list(phones)
        if pinned:
            ids = {p.device_id for p in matching}
            if len(ids) > 1:
                raise LaunchError(
                    "more than one pinned phone was discovered",
                    hint="pass --device <id|name> (or -Device in the launcher) to choose one.",
                )
            if len(ids) == 1:
                peer_id = next(iter(ids))
                add_saved(peer_id)
                for phone in matching:
                    add_beacon(phone)
                return candidates
            # No pinned beacon: fall back only to saved addresses. If several
            # pinned phones have saved addresses, choosing one silently is wrong.
            saved_ids = {
                str(e.get("device_id"))
                for e in self.store.known_endpoints()
                if e.get("device_id") in pinned and _valid_endpoint(e) is not None
            }
            if len(saved_ids) > 1:
                raise LaunchError(
                    "several pinned phones have saved addresses but none was discovered",
                    hint="pass --device <id|name>, or --host <ip> for the phone you want.",
                )
            add_saved(next(iter(saved_ids)) if saved_ids else None)
        else:
            ids = {p.device_id for p in matching}
            if len(ids) > 1:
                raise LaunchError(
                    "more than one unpaired PhoneBridge beacon was heard",
                    hint="pass --host <ip> to choose and pair one phone; discovery never auto-pairs.",
                )
            for phone in matching:
                add_beacon(phone)
            if not candidates:
                add_saved(None)
        if not candidates:
            detail = "no phone beacon or saved Wi-Fi address was found"
            if pinned and phones:
                detail += "; heard beacons only from phones this PC has not pinned"
            raise LaunchError(detail, hint=WIFI_FIX + " " + DISCOVERY_HELP)
        return candidates

    def _wanted_peer(self, peers: list[Any]) -> Any:
        if not self.device:
            return None
        exact = [p for p in peers if p.peer_id == self.device]
        if exact:
            return exact[0]
        needle = self.device.lower()
        named = [p for p in peers if needle in p.peer_name.lower()]
        if len(named) == 1:
            return named[0]
        options = ", ".join(f"{p.peer_name} ({p.peer_id})" for p in (named or peers)) or "none"
        raise LaunchError(
            f"--device/-Device {self.device!r} does not identify one pinned phone",
            hint=f"pinned phones: {options}. Use the full device id, or --host for a new phone.",
        )

    async def _listen(self) -> list[DiscoveredPhone]:
        if self.discovery_timeout <= 0:
            return []
        service = self.discovery_factory()
        try:
            await service.start()
            await self.sleep(self.discovery_timeout)
            return list(service.live())
        except Exception as exc:  # noqa: BLE001 - discovery is optional; saved/manual still work
            self.status(f"discovery listener unavailable: {exc}; trying saved/manual addresses")
            return []
        finally:
            try:
                await service.stop()
            except Exception:  # noqa: BLE001
                pass
