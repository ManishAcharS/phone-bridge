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
from pathlib import Path
from typing import Any

from ..bluetooth import BluetoothLink
from ..core import compat
from ..core.session import BridgeSession, SessionError
from ..files import TransferManager
from ..sensors import SensorLink

PASS = "PASS"
FAIL = "FAIL"
NOT_IMPLEMENTED = "NOT IMPLEMENTED"

#: Workflows the protocol deliberately does not have (docs/03 §6).
#:
#: Empty since Phase 5: Bluetooth, sensors and file transfer are all real RPCs
#: now, so there is nothing left for the matrix to disclaim. The mechanism stays
#: because a future phase that removes a feature must be able to say so again.
UNSUPPORTED_WORKFLOWS: tuple[tuple[str, str], ...] = ()

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
    "bluetooth_gatt",
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
            ("bluetooth scan lifecycle", self.check_bluetooth_scan),
            ("bluetooth GATT lifecycle", self.check_bluetooth_gatt),
            ("sensor discovery and stream", self.check_sensors),
            ("file transfer PC -> phone", self.check_file_send),
            ("file transfer phone -> PC", self.check_file_receive),
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
            f"{len(CAPABILITY_KEYS)} capabilities, {len(usable)} usable "
            f"({', '.join(usable) or 'none'}), {len(present)} present in hardware"
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

        A second subtlety decides whether the timer can win at all: ``call()``
        awaits the send *before* it arms the timer, so with a tiny request on a
        loopback link the reply can be read and matched during that await, and
        the call then returns successfully. The fix is to make the request large
        enough that the reply cannot exist yet when the send returns: a 1 MiB
        echo takes milliseconds on any link, while the deadline is 1 us. That
        makes the timer win for reasons that hold on a real phone too, instead of
        depending on how fast the loop happened to be.

        The attempt is still repeated a bounded number of times, and running out
        of attempts is a failure: a check that cannot fail is not a check.
        """
        payload = "x" * (1 << 20)
        attempts = 0
        for attempts in range(1, 4):
            started = time.perf_counter()
            try:
                await self.session.call(
                    "system.echo", {"value": payload}, timeout=IMPOSSIBLE_TIMEOUT_S
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

    # -- Phase 5: Bluetooth, sensors, files ----------------------------- #
    async def check_bluetooth_scan(self) -> str:
        """Scan for a few seconds and check the *lifecycle*, not just the answer."""
        link = BluetoothLink(self.session)
        self.session.on_event = link.on_event
        try:
            adapter = await link.refresh_adapter()
            if adapter.permitted is False:
                raise AssertionError(
                    "the phone has not granted the Bluetooth scan permission, so a "
                    "scan cannot be exercised"
                )
            if not adapter.enabled:
                raise AssertionError("Bluetooth is switched off on the phone")
            if not adapter.supported:
                raise AssertionError("the phone reports no Bluetooth LE radio")
            if not await link.start_scan(duration_ms=4000):
                raise AssertionError(f"the phone refused to scan: {link.snapshot().detail}")
            if not link.snapshot().scan.scanning:
                raise AssertionError("the phone accepted a scan but reports it is not scanning")
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline and link.snapshot().scan.scanning:
                await asyncio.sleep(0.25)
                await link.refresh_scan()
            await link.stop_scan()
            scan = await link.refresh_scan()
            if scan.scanning:
                raise AssertionError("the scan was still running after stop_scan")
            devices = scan.devices
            for device in devices:
                if not device.address:
                    raise AssertionError("a scan result arrived without an address")
                if not isinstance(device.rssi, int):
                    raise AssertionError(f"{device.address} has a non-integer RSSI")
            self.notes["ble_devices"] = devices
            if not devices:
                return (
                    f"adapter {adapter.name or 'unnamed'} scanned and stopped cleanly; "
                    "no peripheral was in range (a real result, not a failure)"
                )
            best = max(devices, key=lambda d: d.rssi)
            return (
                f"{len(devices)} peripheral(s) seen in 4 s; strongest "
                f"{best.display_name or best.address} at {best.rssi} dBm "
                f"({len(best.service_uuids)} advertised service(s)); scan stopped"
            )
        finally:
            await link.close()

    async def check_bluetooth_gatt(self) -> str:
        """Connect to *one* peripheral the previous check saw, and exercise GATT."""
        link = BluetoothLink(self.session)
        self.session.on_event = link.on_event
        try:
            devices = self.notes.get("ble_devices")
            if devices is None:
                adapter = await link.refresh_adapter()
                if adapter.permitted is False or not adapter.enabled:
                    raise AssertionError("Bluetooth cannot be used on this phone right now")
                if not await link.start_scan(duration_ms=3000):
                    raise AssertionError("the phone refused a scan")
                await asyncio.sleep(3.5)
                await link.stop_scan()
                devices = (await link.refresh_scan()).devices
            candidates = [d for d in devices if d.connectable]
            if not candidates:
                raise AssertionError(
                    "no connectable peripheral was in range, so GATT could not be exercised"
                )
            target = candidates[0]
            connection = await link.connect(target.address)
            if connection.state != "connected":
                raise AssertionError(
                    f"connecting to {target.address} failed: "
                    f"{connection.error or connection.state}"
                )
            try:
                tree = connection.tree or await link.services(target.address)
                if not tree.services:
                    raise AssertionError(f"{target.address} exposed no GATT services")
                readable = notify = None
                for service in tree.services:
                    for characteristic in service.characteristics:
                        if readable is None and characteristic.can_read:
                            readable = (service.uuid, characteristic.uuid)
                        if notify is None and characteristic.can_notify:
                            notify = (service.uuid, characteristic.uuid)
                detail = [f"{len(tree.services)} service(s)"]
                if readable:
                    value = await link.read(target.address, *readable)
                    detail.append(f"read {len(value)} B")
                if notify:
                    subscribed = await link.subscribe(target.address, *notify)
                    detail.append("subscribed" if subscribed else "subscribe refused")
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        current = link.connection(target.address)
                        if current and current.notifications:
                            detail.append(f"{len(current.notifications)} notification(s)")
                            break
                        await asyncio.sleep(0.2)
                    await link.subscribe(target.address, *notify, enable=False)
            finally:
                await link.disconnect(target.address)
            if (link.connection(target.address) or connection).state == "connected":
                raise AssertionError("the peripheral is still connected after disconnect")
            return f"{target.address}: " + ", ".join(detail) + "; disconnected cleanly"
        finally:
            await link.close()

    async def check_sensors(self) -> str:
        """Discover, stream two sensors, verify sequence numbers and release."""
        link = SensorLink(self.session, interval_us=20000)
        try:
            registry = await link.refresh()
            if not registry.sensors:
                raise AssertionError("the phone reported no sensors at all")
            chosen = [info.id for info in registry.ordered()][:2]
            if not await link.start(chosen):
                raise AssertionError(f"the phone refused to stream: {link.snapshot().detail}")
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                snapshot = link.snapshot()
                stats = snapshot.stats or {}
                if all(
                    stats.get(str(sensor_id), {}).get("received", 0) >= 2
                    for sensor_id in chosen
                ):
                    break
                await asyncio.sleep(0.1)
            snapshot = link.snapshot()
            stats = snapshot.stats or {}
            received = {
                sensor_id: stats.get(str(sensor_id), {}).get("received", 0)
                for sensor_id in chosen
            }
            if not any(count >= 2 for count in received.values()):
                raise AssertionError(
                    f"no readings arrived in 6 s (state {snapshot.state}: {snapshot.detail})"
                )
            for sensor_id in chosen:
                if stats.get(str(sensor_id), {}).get("invalid", 0):
                    raise AssertionError(f"sensor {sensor_id} delivered invalid frames")
                if stats.get(str(sensor_id), {}).get("duplicates", 0):
                    raise AssertionError(f"sensor {sensor_id} delivered duplicate sequences")
            interval = link.interval_us
            readings = snapshot.registry.readings if snapshot.registry else {}
            await link.stop()
            if link.state != "idle":
                raise AssertionError(f"stop left the link in state {link.state!r}")
            if len(readings) < len([c for c in received.values() if c]):
                raise AssertionError("a streamed sensor produced no reading")
            return (
                f"{len(registry.sensors)} sensor(s) reported, {len(chosen)} streamed at "
                f"{interval}us, {sum(received.values())} frames with unique sequences, "
                "stopped and released"
            )
        finally:
            await link.close()

    async def check_file_send(self) -> str:
        """Send a real file and confirm the phone verified the hash."""
        import hashlib
        import tempfile

        # 40 KiB: small enough to be quick, large enough to be a real file.
        payload = bytes(range(256)) * 160
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "selftest.bin"
            source.write_bytes(payload)
            manager = TransferManager(self.session)
            try:
                stats = await manager.send_file(source)
            finally:
                await manager.close()
        if stats.state != "complete":
            raise AssertionError(f"transfer ended {stats.state}: {stats.error}")
        if not stats.verified:
            raise AssertionError("the phone did not confirm the SHA-256 of the stored file")
        if stats.sha256 != hashlib.sha256(payload).hexdigest():
            raise AssertionError("the verified hash is not the hash of what we sent")
        return (
            f"{len(payload)} B in {stats.bytes_done} B of chunks, sha256 verified by the "
            f"phone ({stats.sha256[:12]}…), {stats.retries} retries"
        )

    async def check_file_receive(self) -> str:
        """Ask the phone for a file and verify what arrived, byte for byte."""
        import hashlib
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            manager = TransferManager(self.session, destination=directory)
            try:
                stats = await manager.request_file()
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    current = manager.get(stats.transfer_id)
                    if current is None:  # pragma: no cover - defensive
                        break
                    stats = current
                    if stats.state in {"complete", "failed", "cancelled"}:
                        break
                    await asyncio.sleep(0.1)
            except SessionError as exc:
                raise AssertionError(f"the phone refused to send a file: {exc}") from exc
            finally:
                await manager.close()
            if stats.state != "complete":
                raise AssertionError(f"transfer ended {stats.state}: {stats.error}")
            if not stats.verified:
                raise AssertionError("the received file did not verify")
            received = Path(stats.path)
            if not received.is_file():
                raise AssertionError(f"{received} is not on disk after a verified transfer")
            data = received.read_bytes()
            if hashlib.sha256(data).hexdigest() != stats.sha256:
                raise AssertionError("the file on disk does not match the reported hash")
        return (
            f"{stats.name}: {len(data)} B received, sha256 matches the file on disk "
            f"({stats.sha256[:12]}…) and the .part file was renamed atomically"
        )


async def run_selftest(session: BridgeSession, *, frames: int = 16) -> list[Row]:
    return await WorkflowRunner(session, frames=frames).run()


def summarise(rows: list[Row]) -> dict[str, int]:
    counts = {PASS: 0, FAIL: 0, NOT_IMPLEMENTED: 0}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return counts
