"""Finding ``adb``, and telling a cable from a network.

Three things this module refuses to do:

1. **Assume adb is on ``PATH``.** Android Studio does not add it there on
   Windows, so a PC with a perfectly good SDK reports "USB unavailable" while
   ``%LOCALAPPDATA%\\Android\\Sdk\\platform-tools\\adb.exe`` sits on disk. Every
   location below is searched, and the one that answered is reported so the user
   can see which binary the app is driving.
2. **Call a network device "USB".** ``adb devices`` lists a wireless-debugging
   bridge as ``192.168.1.9:5555`` or ``adb-XXXX._adb-tls-connect._tcp``; both are
   Wi-Fi paths. Labelling them USB would make the transport row a lie and would
   make the USB preference meaningless.
3. **Remove a rule it did not create.** Every forward this module creates comes
   back as a :class:`ForwardLease`; rules that already existed are returned as
   "reused" and never touched.

Nothing here raises because adb is missing: that is a normal state on a PC that
only uses Wi-Fi, so :meth:`Adb.status` answers instead.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess  # noqa: S404 - adb is the interface; argv is never a shell string
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: The port the Android app listens on (``docs/03``), and the default remote side
#: of a forward rule.
REMOTE_PORT = 8765

#: How long one adb invocation may take. ``adb devices`` is normally ~30 ms;
#: anything near a second means the daemon is unhappy.
DEFAULT_TIMEOUT = 6.0

#: Classification of one row of ``adb devices -l``.
CONNECTION_USB = "usb"
CONNECTION_WIRELESS = "wireless"
CONNECTION_EMULATOR = "emulator"

_STATE_HELP = {
    "unauthorized": "accept the USB debugging prompt on the phone, then replug",
    "offline": "the device is not responding; replug the cable or run `adb kill-server`",
    "no permissions": "the USB device belongs to another user (Linux udev rules)",
    "connecting": "the device is still connecting; try again in a moment",
    "authorizing": "confirm the RSA prompt on the phone",
}

#: Serial shapes that are a network path, never a cable.
_IP_SERIAL = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d+$")
_MDNS_SERIAL = re.compile(r"^adb-[^.]*\._adb-tls-(?:connect|pairing)\._tcp$", re.IGNORECASE)
_EMULATOR_SERIAL = re.compile(r"^(?:emulator-\d+|localhost:\d+)$")


def state_help(state: str) -> str:
    """What the user should do about one ``adb devices`` state."""
    return _STATE_HELP.get(state, "run `adb devices` for details")


@dataclass(frozen=True)
class AdbResult:
    """The parts of a finished process we care about."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], float], AdbResult]


class AdbError(RuntimeError):
    """An adb invocation failed, or adb itself is unusable."""


# --------------------------------------------------------------------------- #
# where adb might be
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AdbLocation:
    """An adb binary that exists, and how it was found."""

    path: str
    source: str

    @property
    def directory(self) -> str:
        return os.path.dirname(self.path)


def _is_windows(name: str) -> bool:
    """``os.name`` is ``"nt"`` on Windows, not ``"win32"`` — accept every spelling."""
    lowered = name.lower()
    return lowered.startswith("win") or lowered == "nt" or lowered == "windows"


def _windows_candidates(env: Mapping[str, str]) -> list[tuple[str, str]]:
    """(path, source) in the order a Windows PC is most likely to have it."""
    out: list[tuple[str, str]] = []
    local_appdata = env.get("LOCALAPPDATA") or ""
    user_profile = env.get("USERPROFILE") or ""

    def add(base: str, source: str) -> None:
        if not base:
            return
        out.append((os.path.join(base, "platform-tools", "adb.exe"), source))

    add(env.get("ANDROID_HOME", ""), "ANDROID_HOME")
    add(env.get("ANDROID_SDK_ROOT", ""), "ANDROID_SDK_ROOT")
    add(os.path.join(local_appdata, "Android", "Sdk") if local_appdata else "", "LOCALAPPDATA")
    add(
        os.path.join(user_profile, "AppData", "Local", "Android", "Sdk") if user_profile else "",
        "user profile",
    )
    add(os.path.join(local_appdata, "Android", "Sdk.1") if local_appdata else "", "LOCALAPPDATA (SDK.1)")
    add(r"C:\Android\Sdk", "C:\\Android\\Sdk")
    add(os.path.join(env.get("ProgramFiles", r"C:\Program Files"), "Android", "Sdk"), "Program Files")
    return out


def _posix_candidates(env: Mapping[str, str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def add(base: str, source: str) -> None:
        if base:
            out.append((os.path.join(base, "platform-tools", "adb"), source))

    add(env.get("ANDROID_HOME", ""), "ANDROID_HOME")
    add(env.get("ANDROID_SDK_ROOT", ""), "ANDROID_SDK_ROOT")
    add(os.path.expanduser("~/Android/Sdk"), "~/Android/Sdk")
    add("/usr/lib/android-sdk", "/usr/lib/android-sdk")
    add("/opt/android-sdk", "/opt/android-sdk")
    add("/usr/local/share/android-sdk", "/usr/local/share/android-sdk")
    return out


def candidate_locations(
    env: Mapping[str, str] | None = None, *, platform: str | None = None
) -> list[tuple[str, str]]:
    """Every place worth checking, in priority order, with its provenance."""
    environment = dict(os.environ if env is None else env)
    name = (platform or os.name).lower()
    out: list[tuple[str, str]] = []
    override = environment.get("PHONEBRIDGE_ADB")
    if override:
        out.append((override, "PHONEBRIDGE_ADB"))
    if _is_windows(name):
        out.extend(_windows_candidates(environment))
    else:
        out.extend(_posix_candidates(environment))

    # LOCALAPPDATA and USERPROFILE name the same folder on a default Windows
    # install; report each path once, with the first (most specific) source.
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path, source in out:
        key = os.path.normcase(os.path.normpath(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append((path, source))
    return unique


def find_adb(
    env: Mapping[str, str] | None = None,
    *,
    which: Callable[[str], str | None] | None = None,
    exists: Callable[[str], bool] | None = None,
    platform: str | None = None,
) -> AdbLocation | None:
    """The first usable adb: explicit override, then ``PATH``, then the SDKs."""
    environment = dict(os.environ if env is None else env)
    lookup = which or shutil.which
    is_file = exists or os.path.isfile
    name = (platform or os.name).lower()
    executable = "adb.exe" if _is_windows(name) else "adb"

    # An explicit override is an instruction, not a hint: it is honoured even if
    # a stale adb sits earlier on PATH.
    override = environment.get("PHONEBRIDGE_ADB")
    if override and is_file(override):
        return AdbLocation(override, "PHONEBRIDGE_ADB")

    on_path = lookup(executable) or lookup("adb")
    if on_path and is_file(on_path):
        return AdbLocation(on_path, "PATH")

    for path, source in candidate_locations(environment, platform=platform):
        if source == "PHONEBRIDGE_ADB":
            continue
        if is_file(path):
            return AdbLocation(path, source)
    return None


# --------------------------------------------------------------------------- #
# devices
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str
    model: str | None = None
    product: str | None = None
    transport_id: str | None = None
    usb: str | None = None

    @property
    def connection(self) -> str:
        """``usb``, ``wireless`` or ``emulator`` — never guessed from the state."""
        if _EMULATOR_SERIAL.match(self.serial):
            return CONNECTION_EMULATOR
        if _IP_SERIAL.match(self.serial) or _MDNS_SERIAL.match(self.serial):
            return CONNECTION_WIRELESS
        return CONNECTION_USB

    @property
    def is_usb(self) -> bool:
        return self.connection == CONNECTION_USB

    @property
    def usable(self) -> bool:
        return self.state == "device"

    @property
    def display_name(self) -> str:
        if self.model:
            return self.model.replace("_", " ")
        return self.serial

    def explain(self) -> str:
        kind = {
            CONNECTION_USB: "USB",
            CONNECTION_WIRELESS: "wireless debugging",
            CONNECTION_EMULATOR: "emulator",
        }[self.connection]
        if self.usable:
            return f"{self.display_name} is ready over {kind}"
        hint = _STATE_HELP.get(self.state, "run `adb devices` for details")
        return f"{self.serial} ({kind}) is {self.state}: {hint}"


@dataclass(frozen=True)
class AdbStatus:
    """Everything the UI needs to say about adb, in one value."""

    available: bool
    path: str | None = None
    source: str | None = None
    version: str | None = None
    error: str | None = None

    @property
    def summary(self) -> str:
        if not self.available:
            return self.error or "adb not found (USB unavailable)"
        where = f" ({self.source})" if self.source else ""
        return f"{self.path}{where}"

    @property
    def detail(self) -> str:
        if not self.available:
            return self.error or "adb not found"
        version = self.version or "unknown version"
        return f"{self.summary} — {version}"


def parse_devices(stdout: str) -> list[AdbDevice]:
    """Parse ``adb devices -l``.

    Lines look like::

        R58M12345          device usb:1-2 product:x model:Redmi_Note_14_Pro transport_id:3
        192.168.1.9:5555   device product:x model:Pixel transport_id:4
        ABCDEF             unauthorized

    The header line and blank lines are skipped rather than treated as devices.
    """
    devices: list[AdbDevice] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        props: dict[str, str] = {}
        for token in parts[2:]:
            if ":" in token:
                key, _, value = token.partition(":")
                props[key] = value
        devices.append(
            AdbDevice(
                serial=serial,
                state=state,
                model=props.get("model"),
                product=props.get("product"),
                transport_id=props.get("transport_id"),
                usb=props.get("usb"),
            )
        )
    return devices


def is_wireless_serial(serial: str) -> bool:
    """True for anything ``adb`` reached over the network."""
    return bool(_IP_SERIAL.match(serial) or _MDNS_SERIAL.match(serial))


# --------------------------------------------------------------------------- #
# forwards
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ForwardLease:
    """A forward rule created by this controller, and therefore ours to remove."""

    serial: str
    local_port: int
    remote_port: int = REMOTE_PORT

    def remove_argv(self) -> list[str]:
        return ["-s", self.serial, "forward", "--remove", f"tcp:{self.local_port}"]


def _run_adb(argv: Sequence[str], timeout: float, executable: str = "adb") -> AdbResult:
    """Run ``adb`` — by default the *resolved* path, never a bare name.

    Running ``"adb"`` here would undo the whole point of :func:`find_adb`: on the
    PC that reported "adb not on PATH" the search finds
    ``%LOCALAPPDATA%\\Android\\Sdk\\platform-tools\\adb.exe``, and the first
    command would then fail with "adb is not on PATH" anyway.
    """
    try:
        done = subprocess.run(  # noqa: S603 - argv list, no shell
            [executable, *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - guarded by status()
        raise AdbError(f"adb could not be run from {executable}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"adb {' '.join(argv)} timed out after {timeout:g}s") from exc
    return AdbResult(done.returncode, done.stdout or "", done.stderr or "")


def free_local_port() -> int:
    """An unused TCP port on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Adb:
    """A thin, testable wrapper over the ``adb`` executable."""

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        executable: str | None = None,
        source: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        locate: Callable[[], AdbLocation | None] | None = None,
    ) -> None:
        #: ``None`` means "run whatever :func:`find_adb` located", which is the
        #: production path; a test supplies its own callable instead.
        self._runner = runner
        self._explicit = executable
        self._source = source
        self._timeout = timeout
        self._locate = locate or (lambda: find_adb())
        self._resolved = False
        self._location: AdbLocation | None = None

    # ------------------------------------------------------------------ #
    # availability
    # ------------------------------------------------------------------ #
    def locate(self, *, refresh: bool = False) -> AdbLocation | None:
        """Where adb is, cached. ``refresh`` re-runs the search."""
        if refresh or not self._resolved:
            if self._explicit:
                self._location = AdbLocation(self._explicit, self._source or "explicit")
            else:
                self._location = self._locate()
            self._resolved = True
        return self._location

    @property
    def path(self) -> str | None:
        location = self.locate()
        return location.path if location else None

    @property
    def source(self) -> str | None:
        location = self.locate()
        return location.source if location else None

    def available(self) -> bool:
        return self.path is not None

    def _exec(self, argv: Sequence[str]) -> AdbResult:
        """Run one adb command: through the injected runner, or the found binary.

        Raises :class:`AdbError` rather than falling back to a bare ``adb`` when
        nothing was found — a silent PATH fallback is how "USB unavailable" turns
        into a confusing failure at the first command instead of at discovery.
        """
        if self._runner is not None:
            return self._runner(argv, self._timeout)
        path = self.path
        if path is None:
            raise AdbError(
                "adb is not installed: USB is unavailable, and Wi-Fi does not need it"
            )
        return _run_adb(argv, self._timeout, executable=path)

    def version(self) -> str | None:
        if not self.available():
            return None
        try:
            result = self._exec(["--version"])
        except AdbError:
            return None
        if result.returncode != 0:
            return None
        lines = (result.stdout or "").strip().splitlines()
        return lines[0].strip() if lines else None

    def status(self) -> AdbStatus:
        """One value the UI can show without re-running anything expensive."""
        location = self.locate()
        if location is None:
            return AdbStatus(
                available=False,
                error=(
                    "adb not found — USB transport needs platform-tools. Install them "
                    "(Android Studio > SDK Manager > SDK Tools > Android SDK "
                    "Platform-Tools) or put adb on PATH; Wi-Fi keeps working without it."
                ),
            )
        return AdbStatus(
            available=True,
            path=location.path,
            source=location.source,
            version=self.version(),
        )

    # ------------------------------------------------------------------ #
    # devices
    # ------------------------------------------------------------------ #
    def devices(self) -> list[AdbDevice]:
        """Every attached device, including the ones we cannot use."""
        if not self.available():
            return []
        result = self._exec(["devices", "-l"])
        if result.returncode != 0:
            raise AdbError(result.stderr.strip() or "adb devices failed")
        return parse_devices(result.stdout)

    def probe_devices(self) -> tuple[list[AdbDevice], str, str]:
        """Everything the USB report needs from one adb call, and never a raise.

        Returns ``(devices, error, server)`` where ``server`` says whether the
        daemon was already running. A broken daemon is a *state* to report, not an
        exception to escape: the user's next action depends on the difference
        between "server dead" and "no phone attached".
        """
        if not self.available():
            return [], "adb is not installed", "not running (adb missing)"
        try:
            result = self._exec(["devices", "-l"])
        except AdbError as exc:
            return [], str(exc), "unreachable"
        text = f"{result.stdout}\n{result.stderr}".lower()
        if "cannot connect to daemon" in text or "failed to start daemon" in text:
            return [], (result.stderr or result.stdout).strip(), "cannot connect to daemon"
        if "daemon not running" in text or "starting it now" in text:
            server = "was not running (started by this check)"
        else:
            server = "running"
        if result.returncode != 0:
            return [], (result.stderr or "adb devices failed").strip(), server
        return parse_devices(result.stdout), "", server

    def usb_devices(self) -> list[AdbDevice]:
        """Only the devices on a cable (or emulator-free USB state).

        Wireless debugging bridges are excluded on purpose: they are a useful
        transport, but they are not USB, and the transport row must not lie.
        """
        return [d for d in self.devices() if d.is_usb and d.connection != CONNECTION_EMULATOR]

    # ------------------------------------------------------------------ #
    # forwards
    # ------------------------------------------------------------------ #
    def list_forwards(self) -> list[tuple[str, str, str]]:
        """``(serial, local, remote)`` for every existing rule."""
        if not self.available():
            return []
        result = self._exec(["forward", "--list"])
        if result.returncode != 0:
            raise AdbError(result.stderr.strip() or "adb forward --list failed")
        out: list[tuple[str, str, str]] = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                out.append((parts[0], parts[1], parts[2]))
        return out

    def existing_forward(self, serial: str, remote_port: int = REMOTE_PORT) -> int | None:
        """The local port already forwarded to ``remote_port`` for ``serial``.

        Used to *reuse* a rule the user set up themselves. The caller must treat
        such a rule as not owned, so it is never removed.
        """
        target = f"tcp:{remote_port}"
        for rule_serial, local, remote in self.list_forwards():
            if rule_serial == serial and remote == target and local.startswith("tcp:"):
                try:
                    return int(local.split(":", 1)[1])
                except ValueError:  # pragma: no cover - adb always prints tcp:<port>
                    continue
        return None

    def add_forward(
        self, serial: str, local_port: int, remote_port: int = REMOTE_PORT
    ) -> ForwardLease:
        if not self.available():
            raise AdbError("adb is not available; USB transport needs it")
        result = self._exec(
            ["-s", serial, "forward", f"tcp:{local_port}", f"tcp:{remote_port}"]
        )
        if result.returncode != 0:
            raise AdbError(result.stderr.strip() or f"could not forward to {serial}")
        return ForwardLease(serial, local_port, remote_port)

    def remove_forward(self, lease: ForwardLease) -> bool:
        """Remove a rule *this controller created*. False if it was already gone."""
        if not self.available():  # pragma: no cover - adb cannot vanish mid-run
            return False
        try:
            result = self._exec(lease.remove_argv())
        except AdbError as exc:  # the daemon died with the cable: nothing to undo
            log.warning("could not remove forward %s: %s", lease, exc)
            return False
        if result.returncode != 0:
            log.warning("adb refused to remove %s: %s", lease, result.stderr.strip())
            return False
        return True

    def ensure_forward(
        self, serial: str, remote_port: int = REMOTE_PORT, *, attempts: int = 3
    ) -> ForwardLease | None:
        """Forward to one device, creating a rule we own.

        Returns ``None`` when a rule the user already had is being reused — the
        caller must not remove it.
        """
        existing = self.existing_forward(serial, remote_port)
        if existing is not None:
            log.info("reusing the existing forward %s -> %d on %s", existing, remote_port, serial)
            return None
        last: Exception | None = None
        for _ in range(max(1, attempts)):
            port = free_local_port()
            try:
                return self.add_forward(serial, port, remote_port)
            except AdbError as exc:  # usually a port taken between bind and use
                last = exc
        raise AdbError(f"could not set up a USB forward for {serial}: {last}")
