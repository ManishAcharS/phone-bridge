"""The ``adb`` command line, and the rules that decide what we may undo.

USB is the most reliable transport this project has — it needs no network, and
it survives the Wi-Fi dropping mid-session — but the phone only speaks WebSocket,
so reaching it means ``adb forward``.

Two rules make that safe to automate:

1. **A rule we did not create is never removed.** Every forward this module
   creates comes back as a :class:`ForwardLease`; rules that already existed are
   returned with ``owned=False`` and the cleanup path skips them. A user's own
   ``adb forward tcp:8765 tcp:8765`` is therefore left exactly as it was.
2. **Every adb call has a timeout.** ``adb`` can block for a long time when the
   daemon is starting or the cable is flapping; a UI thread must never wait on
   that. The default is short and the failure is reported, not swallowed.

Nothing here raises because adb is missing: that is a normal state on a PC that
only uses Wi-Fi, so :meth:`Adb.available` answers instead.
"""

from __future__ import annotations

import logging
import re
import shutil
import socket
import subprocess  # noqa: S404 - adb is the interface; argv is never a shell string
from collections.abc import Callable, Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: The port the Android app listens on (``docs/03``), and the default remote side
#: of a forward rule.
REMOTE_PORT = 8765

#: How long one adb invocation may take. ``adb devices`` is normally ~30 ms;
#: anything near a second means the daemon is unhappy.
DEFAULT_TIMEOUT = 6.0

#: ``adb devices -l`` states that can carry traffic. Everything else is reported
#: to the user with its own explanation instead of being retried blindly.
USABLE_STATES = frozenset({"device"})

_STATE_HELP = {
    "unauthorized": "accept the USB debugging prompt on the phone",
    "offline": "the device is not responding; replug the cable or restart adb",
    "no permissions": "the USB device is owned by another user (Linux udev rules)",
    "connecting": "the device is still connecting",
}


@dataclass(frozen=True)
class AdbResult:
    """The parts of a finished process we care about."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], float], AdbResult]


class AdbError(RuntimeError):
    """An adb invocation failed, or adb itself is unusable."""


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str
    model: str | None = None
    product: str | None = None
    transport_id: str | None = None

    @property
    def usable(self) -> bool:
        return self.state in USABLE_STATES

    def explain(self) -> str:
        if self.usable:
            return f"{self.display_name} is ready over USB"
        hint = _STATE_HELP.get(self.state, "run `adb devices` for details")
        return f"{self.serial} is {self.state}: {hint}"

    @property
    def display_name(self) -> str:
        if self.model:
            return self.model.replace("_", " ")
        return self.serial


@dataclass(frozen=True)
class ForwardLease:
    """A forward rule created by this controller, and therefore ours to remove."""

    serial: str
    local_port: int
    remote_port: int = REMOTE_PORT

    def remove_argv(self) -> list[str]:
        return ["-s", self.serial, "forward", "--remove", f"tcp:{self.local_port}"]


def _run_adb(argv: Sequence[str], timeout: float) -> AdbResult:
    try:
        done = subprocess.run(  # noqa: S603 - argv list, no shell
            ["adb", *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - guarded by available()
        raise AdbError("adb is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"adb {' '.join(argv)} timed out after {timeout:g}s") from exc
    return AdbResult(done.returncode, done.stdout or "", done.stderr or "")


def free_local_port() -> int:
    """An unused TCP port on the loopback interface.

    There is an unavoidable race between closing this socket and adb binding the
    port; :meth:`Adb.ensure_forward` retries a few times, which is the pragmatic
    answer for a one-connection-at-a-time product.
    """
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
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._runner = runner or _run_adb
        self._executable = executable
        self.timeout = timeout

    # ------------------------------------------------------------------ #
    # availability
    # ------------------------------------------------------------------ #
    @property
    def path(self) -> str | None:
        return self._executable or shutil.which("adb")

    def available(self) -> bool:
        return self.path is not None

    def version(self) -> str | None:
        if not self.available():
            return None
        try:
            result = self._runner(["--version"], self.timeout)
        except AdbError:
            return None
        if result.returncode != 0:
            return None
        first = (result.stdout or "").strip().splitlines()
        return first[0].strip() if first else None

    # ------------------------------------------------------------------ #
    # devices
    # ------------------------------------------------------------------ #
    def devices(self) -> list[AdbDevice]:
        """Every attached device, including the ones we cannot use."""
        if not self.available():
            return []
        result = self._runner(["devices", "-l"], self.timeout)
        if result.returncode != 0:
            raise AdbError(result.stderr.strip() or "adb devices failed")
        return parse_devices(result.stdout)

    # ------------------------------------------------------------------ #
    # forwards
    # ------------------------------------------------------------------ #
    def list_forwards(self) -> list[tuple[str, str, str]]:
        """``(serial, local, remote)`` for every existing rule."""
        if not self.available():
            return []
        result = self._runner(["forward", "--list"], self.timeout)
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
            raise AdbError("adb is not on PATH; USB transport needs it")
        result = self._runner(
            ["-s", serial, "forward", f"tcp:{local_port}", f"tcp:{remote_port}"],
            self.timeout,
        )
        if result.returncode != 0:
            raise AdbError(result.stderr.strip() or f"could not forward to {serial}")
        return ForwardLease(serial, local_port, remote_port)

    def remove_forward(self, lease: ForwardLease) -> bool:
        """Remove a rule *this controller created*. False if it was already gone."""
        if not self.available():  # pragma: no cover - adb cannot vanish mid-run
            return False
        try:
            result = self._runner(lease.remove_argv(), self.timeout)
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
            except AdbError as exc:  # usually a port that was taken between bind and use
                last = exc
        raise AdbError(f"could not set up a USB forward for {serial}: {last}")


def parse_devices(stdout: str) -> list[AdbDevice]:
    """Parse ``adb devices -l``.

    Lines look like::

        R58M12345          device product:x model:Redmi_Note_14_Pro device:y transport_id:1
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
            )
        )
    return devices


_IP_SERIAL = re.compile(r"^\d{1,3}(\.\d{1,3}){3}:\d+$")


def is_wireless_serial(serial: str) -> bool:
    """True for an ``adb connect``-style serial (wireless debugging).

    Those are useful — they survive a cable being unplugged — but they are not
    the local USB link, and the UI names them differently.
    """
    return bool(_IP_SERIAL.match(serial))
