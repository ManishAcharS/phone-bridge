"""End-to-end workflow self-test.

Runs every workflow protocol v1 actually specifies, against the connected phone,
and reports what happened. Two rules govern this file:

* a row is ``PASS`` only because an operation was performed and its result was
  checked - never because the code exists;
* a feature with no protocol operation gets a ``NOT IMPLEMENTED`` row that says
  so. It is never ``SKIP`` (which would suggest "could work but did not") and
  never ``PASS``.

The output is the feature verification matrix: workflow, result, evidence.
"""

from __future__ import annotations

import asyncio
import os
import random
import statistics
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..core import compat
from ..core.session import BridgeSession, SessionError

PASS = "PASS"
FAIL = "FAIL"
NOT_IMPLEMENTED = "NOT IMPLEMENTED"

#: Workflows the protocol deliberately does not have (docs/03 §6).
UNSUPPORTED_WORKFLOWS: tuple[tuple[str, str], ...] = (
    ("file transfer PC -> phone", "no file operation in protocol v1 (Phase 5)"),
    ("file transfer phone -> PC", "no file operation in protocol v1 (Phase 5)"),
    ("sensor data retrieval", "no sensor operation in protocol v1 (Phase 5)"),
)

#: Required keys per method, from the normative table in docs/03 §6.
INFO_KEYS = {
    "device_id": str,
    "name": str,
    "protocol_version": int,
    "capabilities": dict,
    "permissions": dict,
    "model": str,
    "manufacturer": str,
    "android_version": str,
    "sdk_int": int,
    "app_version": str,
    "rom": str,
}

STATS_KEYS = {
    "frames_in": int,
    "frames_out": int,
    "bytes_in": int,
    "bytes_out": int,
    "connections": int,
}

HEADPHONE_KEYS = {"connected": bool, "profile": str, "devices": list}

CAPABILITY_KEYS = (
    "audio.playback",
    "audio.capture",
    "audio.bluetooth_sco",
    "camera.back",
    "camera.front",
    "sensors",
    "file_transfer",
    "bluetooth_scan",
)

PERMISSION_STATES = {"granted", "denied", "permanently_denied", "not_applicable"}

#: Short enough that no socket round trip can beat it (see check_timeout).
IMPOSSIBLE_TIMEOUT_S = 1e-6


@dataclass
class Row:
    workflow: str
    status: str
    evidence: str
    category: str = "supported"

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "status": self.status,
            "evidence": self.evidence,
            "category": self.category,
        }


def _check_types(
    payload: dict[str, Any], schema: Mapping[str, type], what: str
) -> None:
    """Raise AssertionError naming the first key that is missing or wrong."""
    for key, expected in schema.items():
        if key not in payload:
            raise AssertionError(f"{what}: missing required key {key!r}")
        value = payload[key]
        if expected is int and isinstance(value, bool):
            raise AssertionError(f"{what}: {key!r} must be an integer, got a boolean")
        if not isinstance(value, expected):
            raise AssertionError(
                f"{what}: {key!r} must be {expected.__name__}, got {type(value).__name__}"
            )


async def _call(session: BridgeSession, method: str, params: dict[str, Any] | None = None,
                timeout: float = 15.0) -> dict[str, Any]:
    result = await session.call(method, params, timeout=timeout)
    if not isinstance(result, dict):
        raise AssertionError(f"{method}: expected an object, got {type(result).__name__}")
    return result


class WorkflowRunner:
    def __init__(self, session: BridgeSession, *, frames: int = 16, rng: random.Random | None = None):
        self.session = session
        self.frames = max(1, frames)
        self.rng = rng or random.Random(0xC0FFEE)
        self.notes: dict[str, Any] = {}

    async def run(self) -> list[Row]:
        checks = (
            ("authenticated connection", self.check_connection),
            ("phone information", self.check_info),
            ("capability contract", self.check_capabilities),
            ("permission snapshot", self.check_permissions),
            ("session statistics", self.check_stats),
            ("bluetooth headset status", self.check_headset),
            ("audio relay state", self.check_relay),
            ("audio probe report", self.check_probe),
            ("ping round trip", self.check_ping),
            ("binary integrity (blob echo)", self.check_blobs),
            ("error contract (unknown method)", self.check_unknown_method),
            ("timeout handling", self.check_timeout),
        )
        rows: list[Row] = []
        for name, check in checks:
            try:
                evidence = await check()
            except AssertionError as exc:
                rows.append(Row(name, FAIL, str(exc)))
            except SessionError as exc:
                rows.append(Row(name, FAIL, f"{type(exc).__name__}: {exc}"))
            except Exception as exc:  # noqa: BLE001 - one broken check must not hide the rest
                rows.append(Row(name, FAIL, f"{type(exc).__name__}: {exc}"))
            else:
                rows.append(Row(name, PASS, evidence))
        rows.extend(
            Row(name, NOT_IMPLEMENTED, reason, category="unsupported")
            for name, reason in UNSUPPORTED_WORKFLOWS
        )
        return rows

    # -- checks --------------------------------------------------------- #
    async def check_connection(self) -> str:
        phone = self.session.phone
        if phone is None:
            raise AssertionError("no phone identity after a successful open()")
        if not phone.device_id:
            raise AssertionError("the phone reported an empty device id")
        pinned = self.session.store.find_peer(phone.device_id) is not None
        state = "pinned" if pinned else "newly paired"
        self.notes["paired"] = pinned
        return f"{phone.name} ({phone.device_id}), {state}, {self.session.uri}"

    async def check_info(self) -> str:
        info = await _call(self.session, "system.info")
        _check_types(info, INFO_KEYS, "system.info")
        if info["protocol_version"] != 1:
            raise AssertionError(
                f"protocol_version is {info['protocol_version']!r}, expected 1"
            )
        if info["device_id"] != (self.session.phone.device_id if self.session.phone else None):
            # The id in system.info must be the one the handshake authenticated.
            raise AssertionError("system.info device_id does not match the authenticated peer")
        self.notes["info"] = info
        return (
            f"{len(info)} keys, {info['model']} / android {info['android_version']} "
            f"(sdk {info['sdk_int']}), protocol v{info['protocol_version']}"
        )

    async def check_capabilities(self) -> str:
        info = self.notes.get("info") or await _call(self.session, "system.info")
        caps = info["capabilities"]
        usable: list[str] = []
        present: list[str] = []
        for key in CAPABILITY_KEYS:
            if key not in caps:
                raise AssertionError(f"capabilities: missing {key!r}")
            if not isinstance(caps[key], bool):
                raise AssertionError(f"capabilities: {key!r} must be a boolean")
            hardware = caps.get(f"hardware.{key}")
            if not isinstance(hardware, bool):
                raise AssertionError(f"capabilities: hardware.{key} must be a boolean")
            detail = caps.get(f"detail.{key}")
            if not isinstance(detail, str) or not detail.strip():
                raise AssertionError(f"capabilities: detail.{key} must be a non-empty string")
            if hardware:
                present.append(key)
            if caps[key]:
                usable.append(key)
        # The bare key may never be true while the hardware is absent.
        for key in usable:
            if not caps[f"hardware.{key}"]:
                raise AssertionError(f"{key} is usable but the hardware is reported absent")
        self.notes["usable"] = usable
        return (
            f"8 capabilities, {len(usable)} usable ({', '.join(usable) or 'none'}), "
            f"{len(present)} present in hardware"
        )

    async def check_permissions(self) -> str:
        info = self.notes.get("info") or await _call(self.session, "system.info")
        permissions = info["permissions"]
        for name, state in permissions.items():
            if state not in PERMISSION_STATES:
                raise AssertionError(
                    f"permissions: {name} has state {state!r}, "
                    f"expected one of {sorted(PERMISSION_STATES)}"
                )
        self.notes["permissions"] = permissions
        rendered = ", ".join(f"{name}={state}" for name, state in sorted(permissions.items()))
        return rendered or "the phone declares no runtime permissions"

    async def check_stats(self) -> str:
        stats = await _call(self.session, "session.stats")
        _check_types(stats, STATS_KEYS, "session.stats")
        if stats["connections"] < 1:
            raise AssertionError(
                f"session.stats reports {stats['connections']} connections; we are one"
            )
        return (
            f"in {stats['frames_in']} frames / {stats['bytes_in']} B, "
            f"out {stats['frames_out']} frames / {stats['bytes_out']} B, "
            f"{stats['connections']} connection(s)"
        )

    async def check_headset(self) -> str:
        status = await _call(self.session, "bluetooth.headset.status")
        _check_types(status, HEADPHONE_KEYS, "bluetooth.headset.status")
        for device in status["devices"]:
            if not isinstance(device, dict) or "type" not in device:
                raise AssertionError(
                    "bluetooth.headset.status: devices must be objects with at least 'type'"
                )
        self.notes["headset"] = status
        return (
            f"connected={status['connected']}, profile={status['profile']}, "
            f"{len(status['devices'])} communication device(s)"
        )

    async def check_relay(self) -> str:
        route = await _call(self.session, "audio.route.bluetooth")
        if not isinstance(route.get("supported"), bool):
            raise AssertionError("audio.route.bluetooth: 'supported' must be a boolean")
        detail = route.get("detail")
        if not isinstance(detail, str) or not detail.strip():
            raise AssertionError("audio.route.bluetooth: 'detail' must explain the state")
        self.notes["relay"] = route
        return f"supported={route['supported']} ({detail})"

    async def check_probe(self) -> str:
        last = await _call(self.session, "audio.capabilities.last")
        if not isinstance(last.get("available"), bool):
            raise AssertionError("audio.capabilities.last: 'available' must be a boolean")
        if not last["available"]:
            reason = last.get("reason") or "no reason given"
            return f"available=false ({reason}); schema verified, no probe stored yet"
        report = last.get("report")
        if not isinstance(report, dict):
            raise AssertionError("audio.capabilities.last: 'report' must be an object")
        problems = compat.validate_report(report)
        if problems:
            raise AssertionError("stored probe report is malformed: " + "; ".join(problems))
        return (
            f"stored report valid (source={report.get('source')}, "
            f"verdict={compat.verdict(report)})"
        )

    async def check_ping(self) -> str:
        samples = []
        for _ in range(3):
            rtt = await self.session.ping()
            if not (rtt > 0):
                raise AssertionError(f"ping returned a non-positive RTT: {rtt!r}")
            samples.append(rtt)
            await asyncio.sleep(0.01)
        return f"3 pings, median {statistics.median(samples):.2f} ms (min {min(samples):.2f})"

    async def check_blobs(self) -> str:
        queue = self.session.subscribe_blobs(1)
        payloads = [os.urandom(self.rng.randint(1, 4096)) for _ in range(self.frames)]
        sent_bytes = sum(len(p) for p in payloads)
        for payload in payloads:
            await self.session.send_blob(payload, stream_id=1)
        received: list[bytes] = []
        for _ in payloads:
            try:
                blob = await asyncio.wait_for(queue.get(), timeout=20)
            except asyncio.TimeoutError as exc:
                raise AssertionError(
                    f"echo stream stalled after {len(received)} of {len(payloads)} frames"
                ) from exc
            if blob is None:
                raise AssertionError("the session ended while frames were in flight")
            received.append(blob)
        for index, (sent, got) in enumerate(zip(payloads, received, strict=True)):
            if sent != got:
                raise AssertionError(
                    f"frame {index} came back changed "
                    f"({len(sent)} B sent, {len(got)} B received)"
                )
        return (
            f"{len(payloads)} frames, {sent_bytes} B, returned byte-identical "
            "(empty frames excluded: they carry no payload to compare)"
        )

    async def check_unknown_method(self) -> str:
        try:
            await self.session.call("workflow.self_test_no_such_method", timeout=10)
        except SessionError as exc:
            text = str(exc)
            if not text.startswith("method_not_found"):
                raise AssertionError(
                    f"unknown method produced {text!r}, expected method_not_found"
                ) from exc
            # The session must survive a rejected request.
            rtt = await self.session.ping()
            return f"method_not_found returned; session still alive ({rtt:.2f} ms)"
        raise AssertionError("the phone answered a method that does not exist")

    async def check_timeout(self) -> str:
        """A client-side timeout must fail the call, not the session.

        The limit has to be smaller than any possible round trip, otherwise a
        fast link answers first and the check silently proves nothing (a 1 ms
        limit did exactly that on loopback).

        One subtlety makes a single attempt slightly racy: ``call()`` awaits the
        send *before* it arms the timer, so on a loopback link the reply can
        occasionally be read and matched during that await, and the call then
        returns successfully. That is not a bug in the timeout -- the deadline
        was met -- but it is not evidence either, so the attempt is repeated a
        bounded number of times until the timer is the thing that wins. Running
        out of attempts is a failure, not a pass.
        """
        attempts = 0
        for attempts in range(1, 9):
            started = time.perf_counter()
            try:
                await self.session.call(
                    "system.echo", {"value": 1}, timeout=IMPOSSIBLE_TIMEOUT_S
                )
                continue  # the reply beat the timer; arm it again and retry
            except SessionError as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                if "did not answer" not in str(exc):
                    raise AssertionError(f"timeout produced {str(exc)!r}") from exc
                await asyncio.sleep(0.2)  # let any late reply land and be ignored
                rtt = await self.session.ping()
                raced = f"; the reply won the race {attempts - 1}x first" if attempts > 1 else ""
                return (
                    f"{IMPOSSIBLE_TIMEOUT_S * 1e6:.0f} us client timeout fired after "
                    f"{elapsed_ms:.2f} ms; session still usable (rtt {rtt:.2f} ms){raced}"
                )
        raise AssertionError(
            f"a {IMPOSSIBLE_TIMEOUT_S * 1e6:.0f} us timeout did not fire in "
            f"{attempts} attempts: the answer beat the deadline every time, which "
            "means the client is not enforcing it"
        )


async def run_selftest(session: BridgeSession, *, frames: int = 16) -> list[Row]:
    return await WorkflowRunner(session, frames=frames).run()


def summarise(rows: list[Row]) -> dict[str, int]:
    counts = {PASS: 0, FAIL: 0, NOT_IMPLEMENTED: 0}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return counts
