"""Reachability checks that do not need the beacon.

Two questions the window has to answer separately:

* *Is anything listening on that address?* — a plain TCP connect, which works
  even when UDP discovery is blocked.
* *Where is the gateway?* — because on a phone hotspot the gateway **is the
  phone**. Probing one address per interface is bounded, deliberate and nothing
  like a network scan; it is the fallback that keeps the hotspot usable when the
  beacon cannot get through.

Both are read-only and both are allowed to fail quietly: "not reachable" is an
answer, not an exception.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import platform
import re
import subprocess  # noqa: S404 - fixed argv per platform, never a shell string
from collections.abc import Callable, Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)

_IPV4 = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_UNUSABLE = {"0.0.0.0", "127.0.0.1", "255.255.255.255"}

CommandRunner = Callable[[Sequence[str], float], str]


@dataclass(frozen=True)
class Gateway:
    """The default gateway of one interface — i.e. the router the PC came in on."""

    address: str
    interface: str = ""

    def describe(self) -> str:
        return f"gateway {self.address}" + (f" ({self.interface})" if self.interface else "")


# --------------------------------------------------------------------------- #
# TCP reachability
# --------------------------------------------------------------------------- #
async def tcp_probe(host: str, port: int, timeout: float = 0.6) -> bool:
    """Does something accept TCP on ``host:port`` right now?

    A connect-and-close, not a handshake: it only decides whether the address is
    worth a full session attempt, so a stale cached address costs 0.6 s instead
    of a failed handshake and a confusing error.
    """
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    del reader
    return True


# --------------------------------------------------------------------------- #
# gateways
# --------------------------------------------------------------------------- #
def _run(argv: Sequence[str], timeout: float = 3.0) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("command %s failed: %s", argv[0], exc)
        return ""
    return done.stdout or ""


def _parse_gateways(text: str, system: str) -> list[Gateway]:
    found: list[Gateway] = []
    seen: set[str] = set()

    def push(address: str, interface: str = "") -> None:
        if address in _UNUSABLE or address in seen:
            return
        seen.add(address)
        found.append(Gateway(address, interface))

    if system == "Windows":
        # `route print -4`, default routes look like:
        #   0.0.0.0  0.0.0.0  192.168.137.1  192.168.137.25  25
        for line in text.splitlines():
            parts = line.split()
            is_default = (
                len(parts) >= 4 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0"
            )
            if is_default and _IPV4.fullmatch(parts[2]):
                push(parts[2], parts[3] if _IPV4.fullmatch(parts[3]) else "")
    elif system == "Darwin":
        address = ""
        interface = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("gateway:"):
                address = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("interface:"):
                interface = stripped.split(":", 1)[1].strip()
        if _IPV4.fullmatch(address):
            push(address, interface)
    else:
        # `ip route show default` -> "default via 192.168.43.1 dev wlan0 metric 600"
        for match in re.finditer(r"default\s+via\s+(\d{1,3}(?:\.\d{1,3}){3})(?:\s+dev\s+(\S+))?", text):
            push(match.group(1), match.group(2) or "")
    return found


def default_gateways(
    *, runner: CommandRunner | None = None, system: str | None = None
) -> list[Gateway]:
    """The default gateway of every configured interface.

    On a phone hotspot this is the phone itself, which is why it is the one
    address worth trying when the beacon is blocked.
    """
    name = system or platform.system()
    run = runner or _run
    commands: list[Sequence[str]] = []
    if name == "Windows":
        commands = [
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue "
                "| Select-Object -ExpandProperty NextHop",
            ],
            ["route", "print", "-4"],
        ]
    elif name == "Darwin":
        commands = [["route", "-n", "get", "default"]]
    else:
        commands = [["ip", "route", "show", "default"], ["route", "-n"]]

    gateways: list[Gateway] = []
    for argv in commands:
        text = run(argv, 3.0)
        if not text.strip():
            continue
        if argv[0].startswith("powershell"):
            # NextHop list, one address per line
            for line in text.splitlines():
                candidate = line.strip()
                if _IPV4.fullmatch(candidate) and candidate not in _UNUSABLE:
                    gateways.append(Gateway(candidate))
        else:
            gateways.extend(_parse_gateways(text, name))
        if gateways:
            break

    unique: list[Gateway] = []
    seen: set[str] = set()
    for gateway in gateways:
        if gateway.address not in seen:
            seen.add(gateway.address)
            unique.append(gateway)
    return unique


def network_profile(system: str | None = None, *, runner: CommandRunner | None = None) -> str | None:
    """Windows' view of the current network: ``Private``, ``Public`` or unknown.

    It matters because the firewall treats them differently: a hotspot is usually
    classified *Public*, where inbound UDP (the discovery beacon) is dropped by
    default. Knowing this turns "no phones found" into an actionable sentence.
    """
    name = system or platform.system()
    if name != "Windows":
        return None
    run = runner or _run
    text = run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-NetConnectionProfile | Select-Object -ExpandProperty NetworkCategory",
        ],
        3.0,
    )
    words = [line.strip() for line in text.splitlines() if line.strip()]
    for word in words:
        if word.lower() in {"private", "public", "domainauthenticated", "domain"}:
            return word
    return words[0] if words else None
