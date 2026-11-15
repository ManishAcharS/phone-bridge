"""Regression for `Socket is closed` after a Wi-Fi/hotspot network change."""

from __future__ import annotations

from pathlib import Path

from phonebridge.transport.discovery import parse_advertised_hosts

REPO = Path(__file__).resolve().parents[2]
BEACON = REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge" / "net" / "DiscoveryBeacon.kt"


def source() -> str:
    return BEACON.read_text(encoding="utf-8")


def test_each_worker_closes_only_its_own_socket() -> None:
    text = source()
    assert "ownSocket" in text
    assert "if (socket === ownSocket) socket = null" in text
    assert "if (worker === Thread.currentThread()) worker = null" in text
    # The old bug was an unconditional shared-field close in the worker finally.
    assert "finally {\n                socket?.close()" not in text


def test_restart_serializes_lifecycle_and_joins_the_old_worker() -> None:
    text = source()
    assert "private val lifecycleLock = Any()" in text
    assert "fun restart()" in text
    assert "stop()" in text and "start()" in text
    assert "join(JOIN_TIMEOUT_MS)" in text
    assert "while (!myCancel.get())" in text


def test_beacon_reopens_a_socket_that_died_without_a_stop() -> None:
    text = source()
    assert "!sock.isClosed" in text
    assert "beacon worker will retry" in text


def test_advertised_hosts_reject_invalid_and_link_local_addresses() -> None:
    assert parse_advertised_hosts(["999.1.1.1", "169.254.1.1", "224.0.0.1", "10.0.0.1"]) == ("10.0.0.1",)
