"""Re-create the adb forward that a USB re-plug quietly deletes.

`adb forward tcp:8765 tcp:8765` is bound to the *transport*, not to the port: pull
the cable out and every forward on that device is removed with it. The PC side
reconnects on its own (``LiveRunner._reconnect_and_restart``, forever, with backoff
capped at five seconds), but each attempt was dialling a local port nothing was
listening on any more — so "the connection survives a USB re-plug" was true of the
socket code and false in practice.

This module is the missing line. It is deliberately tiny and dependency-free:

* :func:`is_loopback` says whether a host is the USB path at all (a Wi-Fi target
  needs no forward and must not have one invented for it);
* :func:`ensure_forward` runs ``adb forward`` and reports what happened instead of
  raising, because a reconnect attempt must never fail *because* of its own
  bookkeeping — the connection error is the message the user needs.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from typing import Any

#: Hosts that mean "through an adb forward", never "the phone's own address".
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: How long `adb forward` may take before the attempt is abandoned.
FORWARD_TIMEOUT_S = 10.0

__all__ = ["FORWARD_TIMEOUT_S", "LOOPBACK_HOSTS", "ensure_forward", "is_loopback"]


def is_loopback(host: str) -> bool:
    """True when ``host`` is the USB path (an adb forward), not a real address."""
    return host.strip().lower() in LOOPBACK_HOSTS


def ensure_forward(
    port: int,
    *,
    run: Callable[..., Any] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    timeout: float = FORWARD_TIMEOUT_S,
) -> tuple[bool, str]:
    """Run ``adb forward tcp:PORT tcp:PORT``; return ``(worked, why not)``.

    Idempotent: forwarding the same pair twice replaces the entry, so calling it
    before every reconnect attempt is free. Never raises — a machine without adb
    or with a broken USB stack still gets a sentence, and the caller's connection
    error says the rest.
    """
    if port <= 0:
        return False, f"refusing to forward a nonsense port: {port}"
    if which("adb") is None:
        return False, "adb is not on PATH, so the forward could not be re-created"
    try:
        result = run(
            ["adb", "forward", f"tcp:{port}", f"tcp:{port}"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return False, f"adb forward failed: {exc}"
    if int(getattr(result, "returncode", 0) or 0) != 0:
        detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
        return False, f"adb forward exited {result.returncode}: {detail or 'no output'}"
    return True, "forwarded"
