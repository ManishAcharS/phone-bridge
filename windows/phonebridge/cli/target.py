"""Which phone to talk to, and getting a session open.

Every command that needs a live link used to require ``--host``.  That is still
supported and still wins.  A phone that has been seen before can now be reached
with ``--device <id|name>`` or ``--last``, and with no target at all the most
recently seen endpoint is used -- announced on stderr every time, because
silently connecting to a phone the user did not name is exactly how people end
up testing the wrong one.

Human-readable chatter (the target line, the pairing box, retry notices) always
goes to **stderr**.  Data goes to stdout, so `phonebridge info --format json`
stays pipeable and `tools/verify.py` can keep parsing it.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core.device_store import DeviceStore, Peer
from ..core.session import (
    BridgeSession,
    PairingRejected,
    SessionError,
    TerminalSessionError,
)
from ..live.usb import ensure_forward, is_loopback
from ..transport.link import DEFAULT_PORT, LinkError, WebSocketLink, websocket_uri

# Exit codes, part of the CLI contract (documented in docs/11-pc-client.md).
EXIT_OK = 0
EXIT_ERROR = 1          # nothing found / record missing
EXIT_USAGE = 2          # bad arguments, or the phone could not be reached
                        # (connection failure reused 2 in Phase 1: kept)
EXIT_REQUIREMENT = 4    # --require capability not met
EXIT_UNSUPPORTED = 5    # the phone speaks protocol v1 but has no such method


class CliError(Exception):
    """A failure with a message for the user and an exit code."""

    def __init__(self, message: str, *, hint: str | None = None, code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.hint = hint
        self.code = code


def note(message: str) -> None:
    """Human chatter on stderr, so stdout stays machine-readable."""
    print(message, file=sys.stderr)


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    transport: str
    origin: str

    def describe(self) -> str:
        return f"{self.host}:{self.port} ({self.origin})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "transport": self.transport,
            "origin": self.origin,
        }


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def _match_peer(peers: list[Peer], wanted: str) -> Peer:
    exact = [p for p in peers if p.peer_id == wanted]
    if exact:
        return exact[0]
    needle = wanted.lower()
    named = [p for p in peers if needle in p.peer_name.lower()]
    if len(named) == 1:
        return named[0]
    if not named:
        known = ", ".join(f"{p.peer_name} ({p.peer_id})" for p in peers) or "none"
        raise CliError(
            f"no pinned phone matches --device {wanted!r}",
            hint=(
                f"paired phones: {known}. "
                "Run `phonebridge identity` to list them, or connect with --host once."
            ),
        )
    options = ", ".join(f"{p.peer_name} ({p.peer_id})" for p in named)
    raise CliError(
        f"--device {wanted!r} is ambiguous: {options}",
        hint="use the full device id, which is unique.",
    )


def _target_from_endpoint(
    entry: dict[str, Any], origin: str, *, port: int | None = None
) -> Target:
    return Target(
        host=str(entry.get("host", "")),
        # An explicit --port wins over the remembered one; silently ignoring a
        # port the user typed would connect to a different socket than asked.
        port=int(port if port is not None else entry.get("port", DEFAULT_PORT)),
        transport=str(entry.get("transport", "auto")),
        origin=origin,
    )


def resolve_target(store: DeviceStore, args: Any) -> Target:
    """Turn ``--host`` / ``--device`` / ``--last`` into one address."""
    host = (getattr(args, "host", None) or "").strip()
    port: int | None = getattr(args, "port", None)  # None = "not given"
    transport = str(getattr(args, "transport", "auto") or "auto")

    if host:
        if "://" in host:
            raise CliError(
                f"--host takes a hostname or IP, not a URL: {host!r}",
                hint="for example: --host 192.168.42.129 --port 8765",
            )
        looks_like_host_port = (
            "/" in host or (":" in host and host.count(":") == 1 and not host.startswith("["))
        ) and host.rsplit(":", 1)[-1].isdigit()
        if looks_like_host_port:
            # A bare IPv6 literal is allowed (websocket_uri brackets it); a
            # "host:port" pair is a common mistake worth naming.
            name, _, port_text = host.rpartition(":")
            raise CliError(
                f"--host must not include a port: {host!r}",
                hint=f"use --host {name} --port {port_text}",
            )
        return Target(
            host=host, port=port or DEFAULT_PORT, transport=transport, origin="--host"
        )

    wanted = getattr(args, "device", None)
    if wanted:
        peer = _match_peer(store.list_peers(), str(wanted))
        entry = store.endpoint_for(peer.peer_id)
        if entry is None:
            raise CliError(
                f"no saved address for {peer.peer_name} ({peer.peer_id})",
                hint=(
                    "connect to it once with --host; the address is remembered, "
                    "then --device works without it."
                ),
            )
        target = _target_from_endpoint(entry, f"pinned {peer.peer_name}", port=port)
        note(f"using saved address for {peer.peer_name}: {target.host}:{target.port}")
        return target

    endpoints = store.known_endpoints()
    if getattr(args, "last", False):
        if not endpoints:
            raise CliError(
                "no phone has been seen yet, so there is nothing for --last to reuse",
                hint="run `phonebridge discover`, then connect with --host once.",
            )
        return _target_from_endpoint(endpoints[0], "last seen", port=port)

    if not endpoints:
        raise CliError(
            "no phone chosen: pass --host <ip> (or --device/--last once a phone is known)",
            hint=(
                "USB with adb forward:  adb forward tcp:8765 tcp:8765 && "
                "phonebridge info --host 127.0.0.1"
            ),
        )
    target = _target_from_endpoint(endpoints[0], "last seen", port=port)
    note(
        f"no --host given; using the last seen endpoint {target.host}:{target.port}. "
        "Pass --host or --device to choose a different phone."
    )
    return target


# --------------------------------------------------------------------------- #
# pairing prompt
# --------------------------------------------------------------------------- #
def pairing_prompt(sas: str) -> bool:
    """Ask the human to compare the six-digit code.

    Deliberately ASCII: a Windows PowerShell console with a legacy code page
    cannot print box-drawing characters, and a pairing prompt that raises
    UnicodeEncodeError is a pairing prompt nobody can answer.
    """
    if sys.stdin is None or not sys.stdin.isatty():
        raise CliError(
            "pairing needs an interactive terminal: the six-digit code has to be "
            "compared by a human on both screens",
            hint="re-run the command in a terminal window (or use --auto-pair in tests only)",
        )
    note("")
    note("  +---------------------------------------------------------+")
    note(f"  |  PAIRING CODE:  {sas:^39}|")
    note("  +---------------------------------------------------------+")
    note("  The phone shows the same six digits if the link is genuine.")
    note("  If the digits differ, someone is intercepting the connection:")
    note("  answer 'n' and stop.")
    try:
        answer = input("  Codes match? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


# --------------------------------------------------------------------------- #
# connecting
# --------------------------------------------------------------------------- #
def connect_hint(target: Target) -> str:
    if target.host in {"127.0.0.1", "localhost", "::1"}:
        return (
            "for USB, run `adb forward tcp:8765 tcp:8765` first; otherwise check the "
            "phone's IP and that the bridge is switched on in the app."
        )
    return (
        "check: bridge switched on in the app, phone and PC on the same network, "
        f"port {target.port} not blocked by Windows Firewall."
    )


def reconnect_factory(
    args: Any,
    store: DeviceStore,
    target: Target,
) -> Callable[[], Any]:
    """A callable that opens a fresh authenticated session to ``target``.

    Used by the live commands: when the link drops mid-stream the runner calls
    this, swaps the new session in, and asks the phone for every stream again.
    Pairing is not repeated - an established peer authenticates with its pinned
    key - so the only prompt that can still appear is the one for a phone that
    has lost its pairing, which is a decision, not a retry.
    """

    auto_pair = bool(getattr(args, "auto_pair", False))
    confirm = (lambda _sas: True) if auto_pair else pairing_prompt
    request_timeout = float(getattr(args, "request_timeout", 15.0) or 15.0)
    connect_timeout = float(getattr(args, "connect_timeout", 6.0) or 6.0)

    async def connect() -> Any:
        # Everything is read here, not when the factory is built: this callable
        # is created for every live run, and building it must not touch the
        # keystore (or a caller that never loses the link never needs it).
        if is_loopback(target.host):
            # `adb forward` is removed when the cable comes out, so a re-plug
            # leaves every retry dialling a local port nothing is listening on.
            # Re-create it first, every attempt: it is idempotent, and a failure
            # here must not replace the connection error the user needs to see.
            forwarded, why = ensure_forward(target.port)
            if not forwarded:
                note(f"  adb forward: {why}")
        identity = store.load_or_create_identity()
        uri = websocket_uri(target.host, target.port)
        session = BridgeSession(
            uri,
            identity,
            store,
            confirm_pairing=confirm,
            request_timeout=request_timeout,
            link=WebSocketLink(uri, connect_timeout=connect_timeout),
        )
        await session.open()
        return session

    return connect


async def open_session(
    args: Any,
    store: DeviceStore,
    *,
    quiet: bool = False,
) -> tuple[BridgeSession, Target]:
    """Resolve the target and open an authenticated session.

    Retries (``--retries``) cover transient failures only: a refusal is terminal
    and is reported once instead of hammering a phone that said no.
    """
    target = resolve_target(store, args)
    identity = store.load_or_create_identity()
    uri = websocket_uri(target.host, target.port)

    auto_pair = bool(getattr(args, "auto_pair", False))
    if auto_pair:
        note(
            "warning: --auto-pair accepts the pairing code without comparing it. "
            "That is a test convenience, not a secure pairing."
        )
    confirm = (lambda _sas: True) if auto_pair else pairing_prompt

    retries = max(0, int(getattr(args, "retries", 0) or 0))
    connect_timeout = float(getattr(args, "connect_timeout", 6.0) or 6.0)
    request_timeout = float(getattr(args, "request_timeout", 15.0) or 15.0)

    if not quiet:
        note(f"connecting to {target.describe()} ...")

    attempt = 0
    while True:
        attempt += 1
        session = BridgeSession(
            uri,
            identity,
            store,
            confirm_pairing=confirm,
            request_timeout=request_timeout,
            link=WebSocketLink(uri, connect_timeout=connect_timeout),
        )
        try:
            await session.open()
        except (PairingRejected, TerminalSessionError) as exc:
            # A decision, not a glitch: retrying re-asks a question already
            # answered, and re-handshakes a phone that refused us on purpose.
            raise CliError(str(exc), hint="pairing and authentication are not retried.",
                           code=EXIT_USAGE) from exc
        except (SessionError, LinkError) as exc:
            if attempt <= retries:
                delay = min(0.5 * (2 ** (attempt - 1)), 5.0)
                note(f"  attempt {attempt} failed: {exc}")
                note(f"  retrying in {delay:.1f}s ({retries - attempt + 1} left)")
                await asyncio.sleep(delay)
                continue
            raise CliError(str(exc), hint=connect_hint(target), code=EXIT_USAGE) from exc
        break

    store.remember_endpoint(
        target.transport,
        target.host,
        target.port,
        peer_id=session.phone.device_id if session.phone else None,
    )
    if not quiet and session.phone is not None:
        state = "paired" if session.phone.paired else "newly paired"
        # The build goes on this line because it is the line people read when
        # something later behaves like an older version: an APK built before a
        # feature reports no build at all, and "no build" is a diagnosis.
        build = session.phone.build
        suffix = f" - app {build}" if build else " - app build not reported (APK older than this field)"
        note(f"connected to {session.phone.name} ({state}) over {uri}{suffix}")
    return session, target


def unsupported(method: str, exc: SessionError) -> CliError:
    """Translate ``method_not_found`` into an honest capability error."""
    return CliError(
        f"this phone does not implement {method} ({exc})",
        hint=(
            "the phone app is older than this CLI, or protocol v1 does not define "
            "that operation. `phonebridge capabilities` shows what it does support."
        ),
        code=EXIT_UNSUPPORTED,
    )
