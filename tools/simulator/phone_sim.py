"""Simulated Android peer.

This is a *reference implementation of the phone side* of protocol v1, written
in Python so the PC client can be developed and tested without a phone in the
loop.  The Kotlin server in ``android/app`` implements exactly the same wire
behaviour, and ``docs/03-protocol.md`` is the shared contract.

It is also genuinely useful later: it is how you test reconnect logic, the
dashboard and the file-transfer UI when the phone is not plugged in.

Run it::

    python tools/simulator/phone_sim.py --port 8765 --auto-pair
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

from phonebridge.core.sanitize import device_label
from phonebridge.protocol.crypto import (
    HandshakeError,
    HandshakeResponder,
    Identity,
    SecureCodec,
    b64e,
)
from phonebridge.protocol.framing import (
    KIND_CLOSE,
    KIND_HANDSHAKE,
    MAX_ENVELOPE_BODY,
    decode_envelope,
)
from phonebridge.protocol.messages import (
    MSG_AUDIO,
    MSG_BLOB,
    MSG_CONTROL,
    MSG_FILE,
    MSG_PING,
    MSG_PONG,
    MSG_SENSOR,
    MSG_VIDEO,
    STREAM_CONTROL,
    ProtocolError,
    RpcError,
    RpcRouter,
    control_event,
    decode_control,
    handshake_msg,
    parse_ping,
    pong_payload,
)

log = logging.getLogger("phonebridge.sim")

# The protocol-v1 capability contract, as net/DeviceFacts.kt reports it: for each
# feature there is a measured `hardware.<key>`, a `usable today` `<key>`, and a
# `detail.<key>` explaining any gap.  A simulator has no hardware, so the values
# below are declared synthetic (see `simulated` and the CLI's warning).
#
# `audio.bluetooth_sco` stays True on purpose: the simulator is the fixture that
# exercises the "a capability can be usable" path end to end, and
# windows/tests/test_integration.py pins that.  A real handset reports False
# until the Phase 2 relay exists - the CLI prints `simulated` prominently so the
# two are never confused.
CAPABILITY_KEYS = (
    "audio.playback",
    "audio.capture",
    "audio.bluetooth_sco",
    "camera.back",
    "camera.front",
    "bluetooth_gatt",
    "sensors",
    "file_transfer",
    "bluetooth_scan",
)

_HARDWARE = {
    "audio.playback": True,
    "audio.capture": True,
    "audio.bluetooth_sco": True,
    "camera.back": True,
    "camera.front": True,
    "bluetooth_gatt": True,
    "sensors": True,
    "file_transfer": True,
    "bluetooth_scan": True,
}

_DETAIL = {
    "audio.playback": "simulated: no audio device behind this value",
    "audio.capture": "simulated: no microphone behind this value",
    "audio.bluetooth_sco": "simulated: no Bluetooth radio",
    "camera.back": "simulated: no camera behind these synthetic JPEG frames",
    "camera.front": "simulated: no camera behind these synthetic JPEG frames",
    "bluetooth_gatt": "simulated: GATT answers come from a fixed tree, not a radio",
    "sensors": "simulated: readings are generated, not measured",
    "file_transfer": "simulated: files land in the simulator's own directory",
    "bluetooth_scan": "simulated: the device list is fixed, nothing is in the air",
}

# Which runtime permission gates each capability, mirroring DeviceFacts.kt.
# A capability is usable only when it is implemented, the hardware is present
# and the permission is granted - the same three-way rule the phone applies.
_PERMISSION_FOR: dict[str, str] = {
    "audio.capture": "RECORD_AUDIO",
    "camera.back": "CAMERA",
    "camera.front": "CAMERA",
    "bluetooth_scan": "BLUETOOTH_SCAN",
    "bluetooth_gatt": "BLUETOOTH_CONNECT",
}

# Implemented today: only the protocol-level pieces. Everything else is a
# declared hardware row with `usable = false`, exactly like the real app. A test
# may override this to model a phone that implements one more feature.
_IMPLEMENTED: frozenset[str] = frozenset(
    {
        "audio.bluetooth_sco",
        "audio.playback",
        "audio.capture",
        "camera.back",
        "camera.front",
        "sensors",
        "file_transfer",
        "bluetooth_scan",
        "bluetooth_gatt",
    }
)

#: Simulated sensor catalogue. `kind` values are Android `Sensor.TYPE_*`, which is
#: what the wire header carries; the PC maps them to names. These are the sensors
#: an ordinary phone reports, and the detail strings say they are generated.
_SIM_SENSORS: list[dict[str, Any]] = [
    {"id": 1, "kind": 1, "name": "Accelerometer", "vendor": "simulated", "min_delay_us": 5000,
     "max_delay_us": 200000, "max_range": 39.2, "resolution": 0.001, "power_ma": 0.2},
    {"id": 2, "kind": 4, "name": "Gyroscope", "vendor": "simulated", "min_delay_us": 5000,
     "max_delay_us": 200000, "max_range": 34.9, "resolution": 0.001, "power_ma": 0.3},
    {"id": 3, "kind": 2, "name": "Magnetometer", "vendor": "simulated", "min_delay_us": 10000,
     "max_delay_us": 200000, "max_range": 4900.0, "resolution": 0.15, "power_ma": 0.5},
    {"id": 4, "kind": 11, "name": "Rotation Vector", "vendor": "simulated",
     "min_delay_us": 5000, "max_delay_us": 200000, "max_range": 1.0, "resolution": 1e-05,
     "power_ma": 0.3},
    {"id": 5, "kind": 5, "name": "Light", "vendor": "simulated", "min_delay_us": 200000,
     "max_delay_us": 1000000, "max_range": 100000.0, "resolution": 1.0, "power_ma": 0.1},
    {"id": 6, "kind": 8, "name": "Proximity", "vendor": "simulated", "min_delay_us": 200000,
     "max_delay_us": 1000000, "max_range": 5.0, "resolution": 1.0, "power_ma": 0.1},
    {"id": 7, "kind": 19, "name": "Step Counter", "vendor": "simulated", "min_delay_us": 200000,
     "max_delay_us": 1000000, "max_range": 1000000.0, "resolution": 1.0, "power_ma": 0.1},
]

#: A tiny, plausible GATT tree: battery service (readable level + notify) and a
#: vendor service with a writable characteristic. It is fixed data, not a radio.
_SIM_GATT_SERVICES: list[dict[str, Any]] = [
    {
        "uuid": "0000180f-0000-1000-8000-00805f9b34fb",
        "characteristics": [
            {
                "uuid": "00002a19-0000-1000-8000-00805f9b34fb",
                "properties": ["read", "notify"],
                "descriptors": ["00002902-0000-1000-8000-00805f9b34fb"],
            }
        ],
    },
    {
        "uuid": "0000180a-0000-1000-8000-00805f9b34fb",
        "characteristics": [
            {"uuid": "00002a29-0000-1000-8000-00805f9b34fb", "properties": ["read"]},
        ],
    },
    {
        "uuid": "0000ffe0-0000-1000-8000-00805f9b34fb",
        "characteristics": [
            {
                "uuid": "0000ffe1-0000-1000-8000-00805f9b34fb",
                "properties": ["write", "write_no_response", "notify"],
            }
        ],
    },
]

#: Devices a simulated scan reports, with the RSSI values an office would show.
_SIM_BLE_DEVICES: list[dict[str, Any]] = [
    {"address": "C4:7F:51:00:00:01", "name": "Heart Rate Band", "rssi": -54,
     "service_uuids": ["0000180d-0000-1000-8000-00805f9b34fb"], "connectable": True},
    {"address": "E1:2A:9B:00:00:02", "name": "Thermo Beacon", "rssi": -71,
     "service_uuids": ["00001809-0000-1000-8000-00805f9b34fb"], "connectable": True},
    {"address": "F0:0D:1C:00:00:03", "name": "", "rssi": -88,
     "service_uuids": [], "connectable": False},
]


def _default_outbound() -> bytes:
    """A small file the simulator can offer when the PC asks for one."""
    return (
        b"PhoneBridge simulator file\n"
        b"----------------------------------------\n"
        b"This file is generated by tools/simulator/phone_sim.py.\n"
        b"It exists so the PC's receive path, chunking, acknowledgements and\n"
        b"SHA-256 verification can be exercised without a phone.\n"
    )


#: A real, decodable 1x1 JPEG — the simulator has no camera, and says so in the
#: frame's `detail`, but a synthetic picture still exercises the whole receive
#: path (header, validation, gate, preview bookkeeping) end to end.
_SYNTHETIC_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    "080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e"
    "1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c"
    "2e333432ffc0000b080001000101011100ffc4001f000001050101010101"
    "0100000000000000000102030405060708090a0bffc400b5100002010303"
    "020403050504040000017d01020300041105122131410613516107227114"
    "328191a1082342b1c11552d1f02433627282090a161718191a2526272829"
    "2a3435363738393a434445464748494a535455565758595a636465666768"
    "696a737475767778797a838485868788898a92939495969798999aa2a3a4"
    "a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6"
    "d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda00080101"
    "0000003f00fbfe8a28a2803fffd9"
)

# The runtime permission snapshot the app would report (docs/03 §6).
DEFAULT_PERMISSIONS: dict[str, str] = {
    # A simulated Android 14 phone where the user allowed the camera and the two
    # Bluetooth powers but never the microphone: that makes the "usable" and
    # "blocked by permission" paths both reachable in tests.
    "RECORD_AUDIO": "denied",
    "POST_NOTIFICATIONS": "granted",
    "CAMERA": "granted",
    "BLUETOOTH_CONNECT": "granted",
    "BLUETOOTH_SCAN": "granted",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class SimulatedPhone:
    """Accepting side of the protocol: handshake, pairing, RPC, streams."""

    identity: Identity
    state_file: Path | None = None
    name: str = "Pixel-sim"
    pair_policy: str = "auto"  # auto | prompt | deny | wait (never decides)
    # Reported by the simulated capability probe so the multi-device report can
    # be exercised end to end without hardware. Everything here is labelled
    # source="simulator" and never counts as evidence about a real handset.
    model: str = "PhoneBridge Simulator"
    manufacturer: str = "PhoneBridge"
    codename: str = "sim"
    sdk_int: int = 34
    android_release: str = "14"
    headset_connected: bool = True
    permissions: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_PERMISSIONS)
    )
    #: Capabilities this simulated phone claims to implement. The default is
    #: "only what protocol v1 can actually do"; tests override it to model a
    #: phone further along the roadmap.
    implemented: frozenset[str] = _IMPLEMENTED
    probe_profile: str = "duplex"  # duplex | output_only | no_headset | fail
    host: str = "127.0.0.1"
    port: int = 8765
    beacon: bool = False
    advertise_host: str | None = None

    # Reliability limits, mirroring BridgeServer.kt. They exist so the phone
    # cannot be turned into an unbounded resource by whoever can reach the port.
    setup_timeout_s: float = 20.0
    max_sessions: int = 8
    max_sessions_per_device: int = 4

    # Security limits, mirroring BridgeServer.kt.
    #: A pairing request must be answered by a human within this window, or it
    #: expires. Without it a prompt nobody answered stayed valid forever, so a
    #: stale screen (or a forgotten prompt) could still pair a device.
    pairing_window_s: float = 90.0
    #: How many un-answered pairing requests may exist at once. Each one holds a
    #: session, and an attacker on the LAN can produce them faster than a human
    #: can read them.
    max_pending_pairings: int = 4
    #: Reject handshakes that carry a browser ``Origin`` header. Native clients
    #: send none; a browser always does, which is what stops a web page from
    #: driving the bridge (WebSocket is exempt from CORS).
    reject_browser_origins: bool = True
    _pending_pairings: set[str] = field(default_factory=set)
    _probe_in_flight: bool = False
    rejected_origins: int = 0

    paired: dict[str, str] = field(default_factory=dict)  # peer_id -> static key b64
    router: RpcRouter = field(default_factory=RpcRouter)
    connections: list[tuple[ServerConnection, SecureCodec]] = field(default_factory=list)
    _peer_by_connection: dict[ServerConnection, str] = field(default_factory=dict)
    _server: Any = None
    _beacon_task: asyncio.Task[None] | None = None
    pairing_requests: int = 0
    handshakes: int = 0
    last_capability_report: dict[str, Any] | None = None
    frames_in: int = 0
    bytes_in: int = 0
    frames_out: int = 0
    bytes_out: int = 0
    blobs_echoed: int = 0
    #: Phase 2 audio. The simulator has no sound device, so this records what a
    #: real phone would do: accept a format, count frames, and (for the mic) send
    #: them back — which is what makes an end-to-end audio test possible.
    audio: dict[str, Any] = field(
        default_factory=lambda: {
            "playback": "idle",
            "mic": "idle",
            "volume": 40,
            "muted": True,
            "format": {},
        }
    )
    audio_frames_in: int = 0
    audio_frames_out: int = 0
    audio_bad_frames: int = 0
    mic_permission: bool = True
    echo_playback_as_mic: bool = True
    #: Phase 4 camera. Simulated: frames are synthetic, and the reply says so.
    camera: dict[str, Any] = field(
        default_factory=lambda: {
            "streaming": False,
            "facing": "back",
            "width": 1280,
            "height": 720,
            "fps": 15,
        }
    )
    camera_permission: bool = True
    camera_frames_sent: int = 0
    camera_seq: int = 0

    # --- Phase 5 sensors ------------------------------------------------ #
    #: Simulated: readings are computed, never measured, and every reply says so.
    sensor_streaming: dict[str, Any] = field(
        default_factory=lambda: {
            "active": [],
            "interval_us": 20000,
            "seq": {},
            "frames": 0,
            "started_at": 0.0,
        }
    )
    sensor_permission: bool = True
    #: Emit readings on their own while a stream is active, the way a phone does.
    #: A test that wants to drive the frames by hand sets this to False.
    sensor_auto_emit: bool = True

    # --- Phase 5 files -------------------------------------------------- #
    #: Where the simulated phone puts files it receives. A real phone uses SAF.
    #: Unset means a private temporary directory: writing into the process's
    #: working directory used to leave `selftest.bin` in whatever folder a test or
    #: `tools/verify.py` happened to run from, which then looked like a stray
    #: artefact in the checkout.
    file_dir: Path | None = None
    files: dict[int, dict[str, Any]] = field(default_factory=dict)
    #: Private scratch directory used when `file_dir` is unset (see `_file_target`).
    _scratch: Path | None = field(default=None, init=False, repr=False)
    #: Partial transfers kept between attempts, keyed by name — the simulator's
    #: stand-in for the phone's `.part` document in the SAF tree.
    file_parts: dict[str, dict[str, Any]] = field(default_factory=dict)
    file_next_id: int = 1
    file_offers: int = 0
    file_requests: int = 0
    #: Seconds to wait between outbound chunks. 0 streams flat out, which is right
    #: for most tests; a resume test sets it so a transfer can be interrupted in
    #: flight rather than racing the whole file through the socket.
    file_pace_s: float = 0.0
    #: Model an older build that cannot resume: `file.resume` answers zero and the
    #: sender still starts at chunk 0, which is what the PC must survive.
    file_cannot_resume: bool = False
    #: Drop acknowledgements for files arriving from the PC, starting at this
    #: chunk index, so a lost ack (and the sender's retry) is exercised over a
    #: real socket. Acks are cumulative, so a gap has to be *consecutive* to stall
    #: the sender's window: dropping an isolated early ack is invisible, and rightly
    #: so.
    file_drop_ack_from: int = -1
    file_drop_ack_count: int = 0
    file_dropped_acks: int = 0
    #: How long the phone holds its first outbound chunk, waiting for the PC to
    #: say where to resume. A real handset does the same; the delay is generous
    #: here so a test that hashes a large part before asking is not racing it.
    file_resume_grace_s: float = 0.3
    #: Advertise a SHA-256 the file will not match. The receiver must refuse it.
    file_bad_digest: bool = False
    #: Advertise more bytes than the sender actually streams: the receiver ends up
    #: with a valid but short prefix, which must be *kept*, not deleted.
    file_report_size_extra: int = 0
    #: Bytes the simulator will offer when the PC asks for a file.
    outbound_file: tuple[str, bytes] | None = None

    # --- Phase 5 Bluetooth ---------------------------------------------- #
    ble: dict[str, Any] = field(
        default_factory=lambda: {
            "scanning": False,
            "scanned_uuids": [],
            "connected": {},
            "started_at_ms": 0,
            "duration_ms": 0,
            "devices_seen": {},
        }
    )
    ble_permission: bool = True
    ble_enabled: bool = True
    ble_supported_flag: bool = True
    started_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------ #
    def __post_init__(self) -> None:
        self._load_state()
        self._register_default_handlers()

    # ------------------------------------------------------------------ #
    # persistence: pairing survives a restart, exactly like the phone
    # ------------------------------------------------------------------ #
    def _load_state(self) -> None:
        if self.state_file and self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text("utf-8"))
                self.paired = dict(data.get("paired", {}))
            except (json.JSONDecodeError, OSError):
                self.paired = {}

    def _save_state(self) -> None:
        if not self.state_file:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps({"paired": self.paired}, indent=2), "utf-8"
        )

    # ------------------------------------------------------------------ #
    # RPC surface
    # ------------------------------------------------------------------ #
    def _register_default_handlers(self) -> None:
        r = self.router

        @r.register("system.info")
        def _system_info(_params: dict[str, Any]) -> dict[str, Any]:
            # Field for field the same shape as DeviceFacts.deviceInfo() on the
            # phone, so the CLI renders simulator and handset identically.
            return {
                "device_id": self.identity.device_id,
                "name": self.name,
                "model": self.model,
                "manufacturer": self.manufacturer,
                "brand": self.manufacturer,
                "hardware": self.codename,
                "device": self.codename,
                "product": self.codename,
                "android_release": self.android_release,
                "android_version": self.android_release,
                "sdk_int": self.sdk_int,
                "target_sdk": 34,
                "app_version": "simulator",
                "app_version_code": 0,
                "package": "dev.phonebridge.simulator",
                "rom": "none (simulator)",
                "rom_version": "-",
                "is_emulator": True,
                "protocol_version": 1,
                "permissions": dict(self.permissions),
                "capabilities": self.capabilities(),
                "uptime_s": round(time.time() - self.started_at, 1),
                "platform": platform.platform(),
            }

        @r.register("audio.route.bluetooth")
        def _audio_route(_params: dict[str, Any]) -> dict[str, Any]:
            return {
                "supported": self.headset_connected,
                "detail": "Phase 2: SCO routing is exercised by the audio relay, "
                          "not by this probe",
            }

        @r.register("system.echo")
        def _echo(params: dict[str, Any]) -> dict[str, Any]:
            return {"echo": params.get("value"), "at_ns": time.time_ns()}

        @r.register("session.stats")
        def _stats(_params: dict[str, Any]) -> dict[str, Any]:
            # Required keys first, in the order docs/03 lists them; the extra
            # simulator-only counters follow and may be ignored by a client.
            return {
                "frames_in": self.frames_in,
                "frames_out": self.frames_out,
                "bytes_in": self.bytes_in,
                "bytes_out": self.bytes_out,
                "connections": len(self.connections),
                "blobs_echoed": self.blobs_echoed,
                "handshakes": self.handshakes,
                "paired_peers": len(self.paired),
            }

        @r.register("bluetooth.headset.status")
        def _bt_status(_params: dict[str, Any]) -> dict[str, Any]:
            # Same three required keys as AudioProbe on the phone: connected,
            # profile, devices (list of maps). `supported`/`detail` are extras.
            devices = [] if not self.headset_connected else [
                {
                    "id": 1,
                    "type": "TYPE_BLUETOOTH_SCO",
                    "type_id": 7,
                    "product": "simulated headset",
                    "is_sink": True,
                    "is_source": True,
                    "sample_rates": [8000, 16000],
                }
            ]
            return {
                "connected": bool(self.headset_connected),
                "profile": "TYPE_BLUETOOTH_SCO" if self.headset_connected else "none",
                "devices": devices,
                "supported": True,
                "detail": "simulator has no real Bluetooth radio",
                "note": "on hardware this reports A2DP/HFP state from AudioManager",
            }

        @r.register("audio.capabilities.run")
        def _capabilities_run(params: dict[str, Any]) -> dict[str, Any]:
            # One probe at a time: a probe can hold the microphone for minutes,
            # and queueing them without a limit turns a paired PC (or a looping
            # script) into an endless recording. The phone answers `busy`.
            if self._probe_in_flight:
                raise RpcError(
                    "busy", "a capability probe is already running on this phone"
                )
            self._probe_in_flight = True
            try:
                return self._simulated_capability_report(params)
            finally:
                self._probe_in_flight = False

        # --- Phase 2 audio -------------------------------------------------- #
        @r.register("audio.playback.start")
        def _playback_start(params: dict[str, Any]) -> dict[str, Any]:
            fmt = _audio_format(params)
            self.audio["playback"] = "active"
            self.audio["format"] = fmt.as_dict()
            return {"ok": True, "format": fmt.as_dict()}

        @r.register("audio.playback.stop")
        def _playback_stop(_params: dict[str, Any]) -> dict[str, Any]:
            self.audio["playback"] = "idle"
            return {"ok": True, "playback": "idle"}

        @r.register("audio.playback.mute")
        def _playback_mute(params: dict[str, Any]) -> dict[str, Any]:
            self.audio["muted"] = bool((params or {}).get("muted", True))
            return {"ok": True, "muted": self.audio["muted"]}

        @r.register("audio.playback.volume")
        def _playback_volume(params: dict[str, Any]) -> dict[str, Any]:
            try:
                percent = int((params or {}).get("percent"))
            except (TypeError, ValueError) as exc:
                raise ProtocolError("percent must be an integer 0-100") from exc
            if not 0 <= percent <= 100:
                raise ProtocolError(f"percent out of range: {percent}")
            self.audio["volume"] = percent
            return {"ok": True, "percent": percent}

        @r.register("audio.mic.start")
        def _mic_start(params: dict[str, Any]) -> dict[str, Any]:
            if not self.mic_permission:
                raise RpcError(
                    "mic_denied",
                    "microphone permission was refused on the phone, or the PC "
                    "microphone toggle is off in the PhoneBridge app",
                )
            fmt = _audio_format(params)
            self.audio["mic"] = "active"
            return {"ok": True, "format": fmt.as_dict()}

        @r.register("audio.mic.stop")
        def _mic_stop(_params: dict[str, Any]) -> dict[str, Any]:
            self.audio["mic"] = "idle"
            return {"ok": True, "mic": "idle"}

        # --- Phase 5 sensors -------------------------------------------------- #
        @r.register("sensor.list")
        def _sensor_list(params: dict[str, Any]) -> dict[str, Any]:
            return {
                "sensors": _SIM_SENSORS,
                "interval_us": int((params or {}).get("interval_us") or 20000),
                "permitted": self.sensor_permission,
                "simulated": "readings are generated by the simulator, not measured",
            }

        @r.register("sensor.start")
        def _sensor_start(params: dict[str, Any]) -> dict[str, Any]:
            if not self.sensor_permission:
                raise RpcError(
                    "sensor_denied",
                    "no sensor permission on the phone (simulator: sensor_permission=False)",
                )
            params = params or {}
            ids = [int(v) for v in params.get("ids") or []]
            known = {int(entry["id"]) for entry in _SIM_SENSORS}
            unknown = [i for i in ids if i not in known]
            if unknown:
                raise RpcError("bad_params", f"this phone has no sensor(s) {unknown}")
            interval = int(params.get("interval_us") or 20000)
            if not 1000 <= interval <= 10_000_000:
                raise RpcError("bad_params", f"unsupported sampling interval {interval}")
            self.sensor_streaming["active"] = ids
            self.sensor_streaming["interval_us"] = interval
            self.sensor_streaming["seq"] = {i: 0 for i in ids}
            self.sensor_streaming["started_at"] = time.monotonic()
            if self.sensor_auto_emit:
                self._ensure_sensor_emitter()
            return {
                "ok": True,
                "active": ids,
                "interval_us": interval,
                "simulated": True,
            }

        @r.register("sensor.stop")
        def _sensor_stop(_params: dict[str, Any]) -> dict[str, Any]:
            stopped = list(self.sensor_streaming["active"])
            self.sensor_streaming["active"] = []
            self.sensor_streaming["started_at"] = 0.0
            return {"ok": True, "active": [], "stopped": stopped}

        @r.register("sensor.status")
        def _sensor_status(_params: dict[str, Any]) -> dict[str, Any]:
            return {
                "active": list(self.sensor_streaming["active"]),
                "interval_us": self.sensor_streaming["interval_us"],
                "frames_sent": self.sensor_streaming["frames"],
                "permitted": self.sensor_permission,
                "simulated": True,
            }

        # --- Phase 5 files ---------------------------------------------------- #
        @r.register("file.offer")
        def _file_offer(params: dict[str, Any]) -> dict[str, Any]:
            from phonebridge.files.frames import safe_name  # noqa: PLC0415

            params = params or {}
            try:
                name = safe_name(str(params.get("name") or ""))
            except ValueError as exc:
                raise RpcError("bad_name", f"refusing that file name: {exc}") from exc
            size = int(params.get("size") or 0)
            digest = str(params.get("sha256") or "")
            if size < 0:
                raise RpcError("bad_params", "size may not be negative")
            if not digest:
                raise RpcError("bad_params", "a SHA-256 is required for every transfer")
            chunk_size = int(params.get("chunk_size") or 65536)
            transfer_id = self.file_next_id
            self.file_next_id += 1
            target = self._file_target() / name
            # A partial from an earlier attempt with the same identity is resumed
            # in place, at a chunk boundary, exactly as the phone does with its
            # SAF document. The simulator has no SAF, so "the document" is a buffer.
            previous = self.file_parts.get(name)
            resumed = 0
            data = bytearray()
            if (
                previous is not None
                and previous.get("size") == size
                and previous.get("sha256") == digest
                and previous.get("chunk_size") == chunk_size
            ):
                held = len(previous["data"])
                resumed = held - (held % chunk_size)
                data = bytearray(previous["data"][:resumed])
            self.file_parts[name] = {
                "size": size,
                "sha256": digest,
                "chunk_size": chunk_size,
                "data": data,
            }
            self.files[transfer_id] = {
                "name": name,
                "size": size,
                "sha256": digest,
                "direction": "to_phone",
                "state": "receiving",
                "data": data,
                "part": self.file_parts[name],
                "target": str(target),
                "chunk_size": chunk_size,
                "resumed_from": resumed,
                "chunk_count": resumed // chunk_size,
            }
            self.file_offers += 1
            return {
                "ok": True,
                "transfer_id": transfer_id,
                "resume_from": resumed,
                "chunk_size": chunk_size,
                "simulated": "the simulator writes into its own directory, not SAF",
            }

        @r.register("file.request")
        def _file_request(_params: dict[str, Any]) -> dict[str, Any]:
            import asyncio as _asyncio  # noqa: PLC0415
            import hashlib as _hashlib  # noqa: PLC0415

            name, payload = self.outbound_file or ("simulator-note.txt", _default_outbound())
            transfer_id = self.file_next_id
            self.file_next_id += 1
            digest = _hashlib.sha256(payload).hexdigest()
            self.files[transfer_id] = {
                "name": name,
                "size": len(payload),
                "sha256": digest,
                "direction": "to_pc",
                "state": "sending",
                "data": bytearray(payload),
                "chunk_size": 65536,
                "start_index": 0,
                "resumed_from": 0,
                "decided": False,
            }
            self.file_requests += 1
            # The real phone holds its first chunk for a moment so the PC can ask
            # for a resume offset; the simulator models that delay instead of
            # pretending a resume can arrive retroactively.
            _asyncio.get_event_loop().create_task(
                self._later(
                    _asyncio.get_event_loop(),
                    self.file_resume_grace_s,
                    self._send_file_chunks,
                    transfer_id,
                )
            )
            advertised = digest
            if self.file_bad_digest:
                advertised = "0" * 64
            size_reported = len(payload) + max(0, self.file_report_size_extra)
            return {
                "ok": True,
                "transfer_id": transfer_id,
                "name": name,
                "size": size_reported,
                "sha256": advertised,
                "chunk_size": 65536,
                "resume_from": 0,
            }

        @r.register("file.complete")
        def _file_complete(params: dict[str, Any]) -> dict[str, Any]:
            import hashlib as _hashlib  # noqa: PLC0415

            transfer_id = int((params or {}).get("transfer_id") or 0)
            entry = self.files.get(transfer_id)
            if entry is None:
                raise RpcError("bad_params", f"unknown transfer {transfer_id}")
            data = bytes(entry["data"])
            actual = _hashlib.sha256(data).hexdigest()
            verified = actual == entry["sha256"] and len(data) == int(entry["size"])
            entry["state"] = "complete" if verified else "failed"
            if verified:
                # Only a verified file clears the part; a short or wrong one keeps
                # it so the next attempt can resume (or discards it when it is
                # provably wrong, which is what the phone does with a bad hash).
                self.file_parts.pop(entry.get("name") or "", None)
            elif len(data) == int(entry["size"]):
                self.file_parts.pop(entry.get("name") or "", None)
            if verified:
                try:
                    Path(entry["target"]).write_bytes(data)
                except OSError as exc:  # pragma: no cover - disk trouble is not a protocol bug
                    raise RpcError("internal", f"could not store the file: {exc}") from exc
            return {
                "ok": verified,
                "verified": verified,
                "sha256": actual,
                "bytes": len(data),
                "path": entry["target"] if verified else "",
            }

        @r.register("file.progress")
        def _file_progress(params: dict[str, Any]) -> dict[str, Any]:
            transfer_id = int((params or {}).get("transfer_id") or 0)
            entry = self.files.get(transfer_id)
            if entry is None:
                raise RpcError("bad_params", f"unknown transfer {transfer_id}")
            return {
                "transfer_id": transfer_id,
                "state": entry["state"],
                "bytes": len(entry["data"]),
                "size": entry["size"],
                "sha256": entry["sha256"],
            }

        @r.register("file.cancel")
        def _file_cancel(params: dict[str, Any]) -> dict[str, Any]:
            transfer_id = int((params or {}).get("transfer_id") or 0)
            entry = self.files.get(transfer_id)
            if entry is not None:
                entry["state"] = "cancelled"
            return {"ok": True, "transfer_id": transfer_id}

        @r.register("file.pause")
        def _file_pause(params: dict[str, Any]) -> dict[str, Any]:
            transfer_id = int((params or {}).get("transfer_id") or 0)
            entry = self.files.get(transfer_id)
            if entry is not None:
                entry["state"] = "paused"
            return {"ok": True, "transfer_id": transfer_id}

        @r.register("file.resume")
        def _file_resume(params: dict[str, Any]) -> dict[str, Any]:
            transfer_id = int((params or {}).get("transfer_id") or 0)
            entry = self.files.get(transfer_id)
            if entry is None:
                raise RpcError("bad_params", f"unknown transfer {transfer_id}")
            from_bytes = int((params or {}).get("from_bytes") or 0)
            if from_bytes < 0 or from_bytes > int(entry["size"]):
                raise RpcError(
                    "bad_params", f"from_bytes {from_bytes} is outside this transfer"
                )
            chunk_size = int(entry.get("chunk_size") or 65536)
            if self.file_cannot_resume and from_bytes:
                entry["state"] = "sending"
                return {"ok": True, "transfer_id": transfer_id, "from_bytes": 0}
            if entry.get("direction") == "to_pc":
                if chunk_size and from_bytes % chunk_size:
                    raise RpcError(
                        "bad_params",
                        f"from_bytes must be a multiple of the {chunk_size} byte chunk size",
                    )
                if entry.get("decided"):
                    # Streaming has begun: report where it really began, because the
                    # receiver uses this number to decide how much of its part to keep.
                    from_bytes = int(entry.get("resumed_from") or 0)
                else:
                    entry["start_index"] = from_bytes // chunk_size
                    entry["resumed_from"] = from_bytes
                    entry["decided"] = True
            entry["state"] = "sending"
            return {"ok": True, "transfer_id": transfer_id, "from_bytes": from_bytes}

        # --- Phase 5 Bluetooth ------------------------------------------------ #
        @r.register("bluetooth.adapter.status")
        def _ble_adapter(_params: dict[str, Any]) -> dict[str, Any]:
            return {
                "supported": self.ble_supported_flag,
                "enabled": self.ble_enabled,
                "le_supported": self.ble_supported_flag,
                "name": "simulated-radio",
                "address": "00:11:22:33:44:55",
                "permitted": self.ble_permission,
                "detail": "" if self.ble_enabled else "Bluetooth is turned off on the phone",
            }

        @r.register("bluetooth.scan.start")
        def _ble_scan_start(params: dict[str, Any]) -> dict[str, Any]:
            import asyncio as _asyncio  # noqa: PLC0415

            if not self.ble_supported_flag:
                raise RpcError("no_bluetooth", "this device has no Bluetooth radio")
            if not self.ble_permission:
                raise RpcError("bluetooth_denied", "the Bluetooth scan permission is not granted")
            if not self.ble_enabled:
                raise RpcError("bluetooth_off", "Bluetooth is turned off on the phone")
            params = params or {}
            duration = int(params.get("duration_ms") or 0)
            uuids = [str(u) for u in params.get("filter_uuids") or []]
            self.ble.update(
                {
                    "scanning": True,
                    "started_at_ms": int(time.monotonic() * 1000),
                    "duration_ms": duration,
                    "scanned_uuids": uuids,
                }
            )
            _asyncio.get_event_loop().create_task(self._run_ble_scan(duration))
            return {
                "ok": True,
                "scanning": True,
                "started_at_ms": self.ble["started_at_ms"],
                "duration_ms": duration,
                "simulated": "the device list is fixed; nothing is transmitting",
            }

        @r.register("bluetooth.scan.stop")
        def _ble_scan_stop(_params: dict[str, Any]) -> dict[str, Any]:
            self.ble["scanning"] = False
            return {
                "ok": True,
                "scanning": False,
                "devices": list(self.ble["devices_seen"].values()),
            }

        @r.register("bluetooth.scan.results")
        def _ble_scan_results(_params: dict[str, Any]) -> dict[str, Any]:
            return {
                "scanning": self.ble["scanning"],
                "started_at_ms": self.ble["started_at_ms"],
                "duration_ms": self.ble["duration_ms"],
                "devices": list(self.ble["devices_seen"].values()),
                "detail": "simulated device list",
            }

        @r.register("bluetooth.gatt.connect")
        def _ble_connect(params: dict[str, Any]) -> dict[str, Any]:
            if not self.ble_permission:
                raise RpcError("bluetooth_denied", "the Bluetooth permission is not granted")
            address = str((params or {}).get("address") or "")
            if address not in self.ble["devices_seen"]:
                raise RpcError("not_found", f"{address} is not in the scan results")
            if not self.ble["devices_seen"][address].get("connectable", True):
                raise RpcError("not_connectable", f"{address} does not accept connections")
            self.ble["connected"][address] = "connected"
            return {"ok": True, "address": address, "state": "connected"}

        @r.register("bluetooth.gatt.services")
        def _ble_services(params: dict[str, Any]) -> dict[str, Any]:
            address = str((params or {}).get("address") or "")
            if address not in self.ble["connected"]:
                raise RpcError("not_connected", f"{address} is not connected")
            return {
                "address": address,
                "mtu": 185,
                "services": _SIM_GATT_SERVICES,
                "simulated": True,
            }

        @r.register("bluetooth.gatt.read")
        def _ble_read(params: dict[str, Any]) -> dict[str, Any]:
            import base64 as _base64  # noqa: PLC0415

            params = params or {}
            address = str(params.get("address") or "")
            if address not in self.ble["connected"]:
                raise RpcError("not_connected", f"{address} is not connected")
            char = str(params.get("characteristic") or "").lower()
            if "2a19" not in char:
                raise RpcError(
                    "not_supported",
                    "this characteristic is not readable in the simulator "
                    "(only the battery level, 2a19, is)",
                )
            return {
                "ok": True,
                "value_b64": _base64.b64encode(bytes([87])).decode("ascii"),
                "bytes": 1,
                "simulated": True,
            }

        @r.register("bluetooth.gatt.write")
        def _ble_write(params: dict[str, Any]) -> dict[str, Any]:
            import base64 as _base64  # noqa: PLC0415

            params = params or {}
            address = str(params.get("address") or "")
            if address not in self.ble["connected"]:
                raise RpcError("not_connected", f"{address} is not connected")
            try:
                raw = _base64.b64decode(str(params.get("value_b64") or ""), validate=True)
            except Exception as exc:  # noqa: BLE001
                raise RpcError("bad_params", f"value is not base64: {exc}") from exc
            if len(raw) > 512:
                raise RpcError("bad_params", "a characteristic value may not exceed 512 bytes")
            return {"ok": True, "bytes": len(raw), "simulated": True}

        @r.register("bluetooth.gatt.subscribe")
        def _ble_subscribe(params: dict[str, Any]) -> dict[str, Any]:
            import asyncio as _asyncio  # noqa: PLC0415

            params = params or {}
            address = str(params.get("address") or "")
            if address not in self.ble["connected"]:
                raise RpcError("not_connected", f"{address} is not connected")
            enable = bool(params.get("enable", True))
            if enable:
                _asyncio.get_event_loop().create_task(
                    self._emit_ble_notifications(
                        address,
                        str(params.get("service") or ""),
                        str(params.get("characteristic") or ""),
                    )
                )
            return {"ok": True, "subscribed": enable, "simulated": True}

        @r.register("bluetooth.gatt.disconnect")
        def _ble_disconnect(params: dict[str, Any]) -> dict[str, Any]:
            address = str((params or {}).get("address") or "")
            self.ble["connected"].pop(address, None)
            return {"ok": True, "address": address, "state": "disconnected"}

        @r.register("bluetooth.gatt.status")
        def _ble_status(_params: dict[str, Any]) -> dict[str, Any]:
            return {
                "scanning": self.ble["scanning"],
                "connections": [
                    {"address": address, "state": state}
                    for address, state in self.ble["connected"].items()
                ],
                "permitted": self.ble_permission,
                "simulated": True,
            }

        # --- Phase 4 camera -------------------------------------------------- #
        @r.register("camera.start")
        def _camera_start(params: dict[str, Any]) -> dict[str, Any]:
            if not self.camera_permission:
                raise RpcError(
                    "camera_denied",
                    "camera permission is not granted on the phone: open "
                    "PhoneBridge on the phone and grant Camera, then try again",
                )
            cfg = (params or {}).get("config") or {}
            facing = str(cfg.get("facing", "back"))
            if facing not in ("back", "front"):
                raise RpcError("bad_params", f"unknown camera facing {facing!r}")
            width = int(cfg.get("width", 1280))
            height = int(cfg.get("height", 720))
            fps = int(cfg.get("fps", 15))
            if not 320 <= width <= 3840 or not 240 <= height <= 2160 or not 1 <= fps <= 60:
                raise RpcError("bad_params", f"unsupported camera configuration {width}x{height}@{fps}")
            self.camera.update(
                {
                    "streaming": True,
                    "facing": facing,
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "quality": int(cfg.get("quality", self.camera.get("quality", 70))),
                }
            )
            self.camera_seq = 0
            return {
                "ok": True,
                "config": dict(self.camera),
                "simulated": "no camera hardware; frames are synthetic",
            }

        @r.register("camera.stop")
        def _camera_stop(_params: dict[str, Any]) -> dict[str, Any]:
            self.camera["streaming"] = False
            return {"ok": True, "streaming": False}

        @r.register("camera.switch")
        def _camera_switch(params: dict[str, Any]) -> dict[str, Any]:
            facing = (params or {}).get("facing")
            if facing not in ("back", "front"):
                raise RpcError("bad_params", f"unknown camera facing {facing!r}")
            if not self.camera_permission:
                raise RpcError("camera_denied", "camera permission is not granted on the phone")
            self.camera["facing"] = facing
            self.camera_seq = 0
            return {"ok": True, "facing": facing, "streaming": self.camera["streaming"]}

        @r.register("camera.status")
        def _camera_status(_params: dict[str, Any]) -> dict[str, Any]:
            return {
                "streaming": self.camera["streaming"],
                "facing": self.camera["facing"],
                "width": self.camera["width"],
                "height": self.camera["height"],
                "fps": self.camera["fps"],
                "frames_sent": self.camera_frames_sent,
                "permitted": self.camera_permission,
                # The phone's own measurement block, same keys as CameraEngine:
                # `path` is the encoder actually in use, `capture_*` is the size
                # CameraX really delivered. A simulator that omits them lets the
                # PC's stats line look right while the phone's is broken.
                "stats": {
                    "fps": float(self.camera["fps"]),
                    "encode_ms": 0.0,
                    "send_ms": 0.0,
                    "bytes_per_frame": 0,
                    "quality": self.camera.get("quality", 70),
                    "width": self.camera["width"],
                    "height": self.camera["height"],
                    "encoded": self.camera_frames_sent,
                    "sent": self.camera_frames_sent,
                    "dropped": 0,
                    "path": "rgba",
                    "path_reason": "the simulator draws RGB frames",
                    "capture_width": self.camera["width"],
                    "capture_height": self.camera["height"],
                    "capture_layout": "simulated",
                },
            }

        @r.register("audio.status")
        def _audio_status(_params: dict[str, Any]) -> dict[str, Any]:
            # The playback block mirrors what AudioEngine reads off its
            # AudioTrack. Simulated values, but the same shape and the same
            # meaning: a PC that renders these against a fake phone renders them
            # against the real one too.
            playing = self.audio["playback"] == "active"
            return {
                "playback": self.audio["playback"],
                "mic": self.audio["mic"],
                "volume": self.audio["volume"],
                "muted": self.audio["muted"],
                "format": self.audio["format"],
                "frames_in": self.audio_frames_in,
                "frames_out": self.audio_frames_out,
                "permitted": self.mic_permission,
                "route": "speaker" if playing else "none",
                "device_name": "simulated speaker" if playing else "",
                "track_state": "playing" if playing else "no_track",
                "buffered_ms": 0,
                "buffer_ms": 80 if playing else 0,
                "underruns": 0,
                "played_ms": 0,
                "frames_written": self.audio_frames_in if playing else 0,
            }

        @r.register("audio.capabilities.last")
        def _capabilities_last(_params: dict[str, Any]) -> dict[str, Any]:
            if self.last_capability_report is None:
                return {"available": False, "reason": "no probe has run yet"}
            return {"available": True, "report": self.last_capability_report}

        @r.register("system.shutdown")
        def _shutdown(_params: dict[str, Any]) -> dict[str, Any]:
            asyncio.get_running_loop().call_later(0.1, self.stop_soon)
            return {"ok": True}

    # ------------------------------------------------------------------ #
    # simulated capability probe
    # ------------------------------------------------------------------ #
    def _simulated_capability_report(self, params: dict[str, Any]) -> dict[str, Any]:
        """A structurally valid probe report with no hardware behind it.

        Deliberately mimics the Android payload field for field: if the two ever
        drift, the report renderer and its tests catch it.
        """
        seconds = max(1.0, min(float(params.get("seconds") or 30), 300.0))
        acoustic = bool(params.get("acoustic"))
        profile = self.probe_profile
        modern = self.sdk_int >= 31
        headset = self.headset_connected and profile != "no_headset"

        time.sleep(min(seconds, 0.5))  # keep tests fast; the phone really waits

        def status(ok: bool) -> str:
            return "pass" if ok else ("skipped" if not headset else "fail")

        output_ok = headset and profile in ("duplex", "output_only")
        input_ok = headset and profile == "duplex"
        duplex_ok = headset and profile == "duplex"

        report: dict[str, Any] = {
            "schema_version": 1,
            "source": "simulator",
            "device_id": self.identity.device_id,
            "label": self.name,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "device": {
                "manufacturer": self.manufacturer,
                "model": self.model,
                "codename": self.codename,
                "android_release": self.android_release,
                "sdk_int": self.sdk_int,
                "rom": "unknown",
                "rom_version": None,
                "incremental": "sim",
                "is_emulator": True,
            },
            "bluetooth": {
                "supported": True,
                "enabled": headset,
                "adapter_name": "sim-adapter" if headset else None,
                "version": None,
                "hfp_connected": ["00:11:22:33:44:55"] if headset else [],
                "a2dp_connected": ["00:11:22:33:44:55"] if headset else [],
                "bonded_count": 1 if headset else 0,
                "le_audio_supported": None,
            },
            "sco": {
                "path": "modern" if modern else "legacy",
                "declared_offcall_available": True,
                "request_accepted": headset,
                "route_observed_ms": 40 if headset else None,
                "communication_device_after": "bluetooth_sco" if headset else None,
            },
            "route_output": {
                "status": status(output_ok),
                "routed_device": "bluetooth_sco" if output_ok else None,
                "underruns": 0,
                "track_rate_hz": 16000,
            },
            "route_input": {
                "status": status(input_ok),
                "routed_device": "bluetooth_sco" if input_ok else None,
                "record_rate_hz": 16000,
                "rms": 1450 if input_ok else 0,
                "peak": 9120 if input_ok else 0,
                "signal_detected": bool(input_ok),
            },
            "duplex": {
                "status": status(duplex_ok),
                "seconds": seconds,
                "underruns": 0,
                "read_timeouts": 0,
                "route_changes": 0,
            },
            "latency": (
                {"method": "acoustic_loopback", "round_trip_ms": 128.0, "confidence": 5.2}
                if acoustic and duplex_ok
                else {"method": "timestamp_estimate", "estimate_ms": 41.0}
                if duplex_ok
                else {"method": "none"}
            ),
            "interruptions": [],
            "errors": [] if headset else ["no Bluetooth headset attached"],
            "tests_run": ["device", "bluetooth", "sco", "route_output", "route_input",
                          "duplex", "latency"],
        }
        self.last_capability_report = report
        return report

    def stop_soon(self) -> None:
        if self._server is not None:
            self._server.close()

    # ------------------------------------------------------------------ #
    # server lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._server = await serve(
            self._handle_connection,
            self.host,
            self.port,
            max_size=MAX_ENVELOPE_BODY + 64,
            compression=None,
            ping_interval=10,
            ping_timeout=10,
        )
        # Resolve the real bound port (tests ask for port 0).
        for sock in self._server.sockets or ():
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                self.port = sock.getsockname()[1]
                break
        log.info("simulated phone listening on ws://%s:%d", self.host, self.port)
        if self.beacon:
            self._beacon_task = asyncio.create_task(self._beacon_loop(), name="sim-beacon")

    async def stop(self) -> None:
        if self._beacon_task is not None:
            self._beacon_task.cancel()
            try:
                await self._beacon_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _beacon_loop(self) -> None:
        from phonebridge.transport.discovery import BEACON_MAGIC, DISCOVERY_PORT

        advertise = self.advertise_host or _primary_ipv4()
        payload = json.dumps(
            {
                "app": BEACON_MAGIC,
                "v": 1,
                "id": self.identity.device_id,
                "name": self.name,
                "port": self.port,
                "method": "wifi",
            }
        ).encode("utf-8")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            while True:
                sock.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
                sock.sendto(payload, (advertise, DISCOVERY_PORT))
                await asyncio.sleep(2.0)
        finally:
            sock.close()

    # ------------------------------------------------------------------ #
    # per-connection handling
    # ------------------------------------------------------------------ #
    @staticmethod
    def _origin_of(ws: ServerConnection) -> str | None:
        """The browser ``Origin`` header of a handshake, if the client sent one.

        Only browsers send it, and browsers are exactly the clients that must
        not be able to drive the bridge: a WebSocket handshake bypasses the
        same-origin policy (no preflight, no CORS check), so any page the user
        happens to visit can open ``ws://192.168.x.x:8765``. Rejecting the
        header is the whole mitigation, and it costs native clients nothing.
        """
        request = getattr(ws, "request", None)
        headers = getattr(request, "headers", None)
        if headers is None:
            return None
        try:
            value = headers.get("Origin")
        except Exception:  # noqa: BLE001 - headers are optional
            return None
        return value or None

    async def _handle_connection(self, ws: ServerConnection) -> None:
        peer = getattr(ws, "remote_address", ("?", 0))
        log.info("connection from %s", peer)
        origin = self._origin_of(ws)
        if self.reject_browser_origins and origin:
            self.rejected_origins += 1
            log.warning(
                "refusing connection from %s: browser Origin %r (only native clients may connect)",
                peer[0] if isinstance(peer, tuple) else peer,
                device_label(origin, limit=80),
            )
            await ws.close(code=1008, reason="browser origins are not accepted")
            return
        try:
            await self._serve_connection(ws)
        except Exception as exc:  # noqa: BLE001
            log.warning("connection from %s ended: %s: %s", peer, type(exc).__name__, exc)
        finally:
            log.info("connection from %s closed", peer)

    async def _recv_setup(self, ws: ServerConnection):
        """Read one setup frame, refusing peers that stall the handshake.

        Without this, a half-open socket holds a session slot forever: the OS
        will not necessarily reap it, and every such peer is free to keep
        opening more.
        """
        try:
            raw = await asyncio.wait_for(ws.recv(), self.setup_timeout_s)
        except asyncio.TimeoutError:
            log.warning("setup timeout after %.1fs; closing", self.setup_timeout_s)
            await ws.close(code=1002, reason="setup timeout")
            raise
        return decode_envelope(raw)

    def _session_refusal(self, peer_id: str) -> str | None:
        """Why this peer may not have another session, or None if it may."""
        per_peer = sum(1 for p in self._peer_by_connection.values() if p == peer_id)
        if per_peer >= self.max_sessions_per_device:
            return "too many connections from this device"
        if len(self.connections) >= self.max_sessions:
            return "server connection limit reached"
        return None

    async def _serve_connection(self, ws: ServerConnection) -> None:
        responder = HandshakeResponder(self.identity)
        env = await self._recv_setup(ws)
        if env.kind != KIND_HANDSHAKE:
            await ws.close(code=1002, reason="expected handshake")
            return

        hello = env.as_json()
        peer_static = hello.get("static", "")
        known = hello.get("id") in self.paired
        if known and self.paired[hello["id"]] != peer_static:
            # Device id matches but the identity key changed: refuse, the phone
            # must be re-paired deliberately.
            await ws.close(code=1008, reason="identity key changed for known device")
            return

        await ws.send(responder.on_hello(env, known_peer=known))
        assert responder.keys is not None
        codec = SecureCodec.for_server(responder.keys)
        self.handshakes += 1
        peer_id = str(hello.get("id", ""))
        # The name is attacker-chosen text that lands in a log: strip controls
        # and escapes, and never write the pairing code.
        log.info(
            "handshake with %s (%s), known=%s",
            device_label(hello.get("name"), limit=64, fallback="pc"),
            device_label(peer_id, limit=32, fallback="?"),
            known,
        )
        # Capacity is checked before auth_ok: a peer that is about to be refused
        # must not first be told the session is up, then dropped (the PC would
        # report a successful connection and an immediate unexplained close).
        refusal = self._session_refusal(peer_id)
        if refusal is not None:
            log.warning("refusing session from %s: %s", peer_id, refusal)
            await ws.close(code=1008, reason=refusal)
            return

        if not await self._authenticate(ws, codec, responder, hello, known):
            return

        # Re-check: two peers can pass the first check while both are still
        # in the setup phase (auth takes a round trip).
        refusal = self._session_refusal(peer_id)
        if refusal is not None:
            log.warning("refusing session from %s: %s", peer_id, refusal)
            await ws.close(code=1008, reason=refusal)
            return

        self.connections.append((ws, codec))
        self._peer_by_connection[ws] = peer_id
        try:
            await self._serve_session(ws, codec)
        finally:
            self.connections = [c for c in self.connections if c[1] is not codec]
            self._peer_by_connection.pop(ws, None)
            # The Android service releases the camera when the last session goes
            # (BridgeServer.releaseAudioIfIdle). The simulator must behave the same
            # way, or a test would pass here and leak a lens on a real phone.
            if not self.connections:
                self.camera["streaming"] = False
                # Phase 5: a real phone drops its GATT links and its scan with the
                # last session (BridgeServer releases them), so the simulator does.
                self.ble["connected"].clear()
                self.ble["scanning"] = False
                self.sensor_streaming["active"] = []

    async def _authenticate(
        self,
        ws: ServerConnection,
        codec: SecureCodec,
        responder: HandshakeResponder,
        hello: dict[str, Any],
        known: bool,
    ) -> bool:
        """Validate the PC's identity inside the encrypted channel.

        Successful AEAD decryption already proves the peer holds both the static
        and ephemeral private keys that produced the session keys, so there is
        no separate signature step: identity == pinned key + ability to decrypt.
        """
        env = await self._recv_setup(ws)
        try:
            msg_type, _stream, payload = codec.unseal(env)
        except HandshakeError as exc:
            log.warning("auth frame failed to decrypt: %s", exc)
            # Close with a reason: returning from the handler closes the socket
            # without a close frame, which the PC can only report as a drop.
            await ws.close(code=1008, reason="auth frame could not be decrypted")
            return False
        if msg_type != MSG_CONTROL:
            await ws.close(code=1002, reason="expected auth")
            return False
        msg = decode_control(payload)
        if msg.get("k") != "auth":
            await ws.close(code=1002, reason="expected auth")
            return False
        if msg.get("proto") != 1:
            await _send(ws, codec, handshake_msg("auth_fail", reason="protocol version"))
            await ws.close(code=1002, reason="protocol version")
            return False

        peer_id = str(msg.get("id", ""))
        peer_name = str(msg.get("name", "pc"))
        peer_static_b64 = b64e(responder.peer_static or b"")

        if peer_id != responder.peer_device_id:
            # The device id is a hash of the identity key, so a mismatch means a
            # broken or hostile peer.
            await _send(ws, codec, handshake_msg("auth_fail", reason="identity mismatch"))
            await ws.close(code=1008, reason="identity mismatch")
            return False
        if peer_id in self.paired and self.paired[peer_id] != peer_static_b64:
            await _send(ws, codec, handshake_msg("auth_fail", reason="identity key changed"))
            await ws.close(code=1008, reason="identity key changed")
            return False

        if peer_id in self.paired:
            await _send(
                ws,
                codec,
                handshake_msg(
                    "auth_ok",
                    id=self.identity.device_id,
                    name=self.name,
                    capabilities=self.capabilities(),
                ),
            )
            return True

        # ---- first-time pairing ------------------------------------------ #
        if len(self._pending_pairings) >= self.max_pending_pairings:
            log.warning(
                "refusing pairing request from %r: %d already pending",
                peer_name,
                len(self._pending_pairings),
            )
            await _send(
                ws,
                codec,
                handshake_msg("pair_fail", reason="too many pending pairing requests"),
            )
            await ws.close(code=1008, reason="too many pending pairing requests")
            return False
        self.pairing_requests += 1
        sas = responder.keys.sas_digits  # type: ignore[union-attr]
        # The digits belong on the phone's screen, not in a log line.
        log.warning(
            "PAIRING REQUEST from %r: the phone must display the six-digit code "
            "(withheld from logs) for the user to compare.",
            peer_name,
        )
        await _send(
            ws,
            codec,
            handshake_msg(
                "pair_required",
                id=self.identity.device_id,
                name=self.name,
                sas=sas,
                capabilities=self.capabilities(),
            ),
        )
        self._pending_pairings.add(peer_id)
        try:
            reason = await self._await_pair_confirmation(ws, codec, sas, peer_name)
        finally:
            self._pending_pairings.discard(peer_id)
        if reason:
            await _send(ws, codec, handshake_msg("pair_fail", reason=reason))
            return False
        self.paired[peer_id] = peer_static_b64
        self._save_state()
        await _send(
            ws, codec, handshake_msg("pair_ok", id=self.identity.device_id, name=self.name)
        )
        log.info("paired with %s; identity key pinned", peer_name)
        return True

    async def _await_pair_confirmation(
        self,
        ws: ServerConnection,
        codec: SecureCodec,
        sas: str,
        peer_name: str,
    ) -> str:
        """Wait for the PC's confirmation, then apply the phone-side policy.

        Returns ``""`` when pairing may proceed, or a human-readable reason why
        it may not.  The confirmation is always read first, even when the policy
        is 'deny': that keeps the exchange symmetric and avoids closing the link
        on a PC that is still writing.

        Policies: ``auto`` (test/automation: accept), ``deny``, ``prompt`` (asks
        on stdin) and ``wait`` (never decides, so the expiry path can be tested
        without a keyboard -- the request must expire, not hang forever).
        """
        try:
            env = decode_envelope(await asyncio.wait_for(ws.recv(), self.pairing_window_s))
            msg_type, _stream, payload = codec.unseal(env)
        except asyncio.TimeoutError:
            log.warning(
                "pairing request from %r expired after %.0fs with no answer",
                peer_name,
                self.pairing_window_s,
            )
            return "pairing window expired"
        except HandshakeError:
            return "confirmation could not be decrypted"
        if msg_type != MSG_CONTROL:
            return "expected a pairing confirmation"
        msg = decode_control(payload)
        if msg.get("k") == "pair_cancel":
            log.info("PC cancelled pairing")
            return "cancelled by the PC"
        if msg.get("k") != "pair_confirm":
            return "expected a pairing confirmation"
        if msg.get("sas") != sas:
            log.warning("PC confirmed a different SAS (%r vs %r)", msg.get("sas"), sas)
            return "SAS mismatch"
        if self.pair_policy == "auto":
            return ""
        if self.pair_policy == "deny":
            return "declined on the phone"
        if self.pair_policy == "wait":
            # Nobody answers: hold the request for the whole window, then let it
            # expire. This is how the PC sees what a forgotten prompt looks like.
            await asyncio.sleep(self.pairing_window_s)
            log.warning("pairing request from %r expired unanswered", peer_name)
            return "pairing window expired"
        answer = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: input(f"Allow pairing from {peer_name} -- codes match? [y/N] "),
        )
        return "" if answer.strip().lower() in {"y", "yes"} else "declined on the phone"

    async def _serve_session(self, ws: ServerConnection, codec: SecureCodec) -> None:
        while True:
            raw = await ws.recv()
            env = decode_envelope(raw)
            if env.kind == KIND_CLOSE:
                return
            msg_type, stream_id, payload = codec.unseal(env)
            self.frames_in += 1
            self.bytes_in += len(raw)

            if msg_type == MSG_CONTROL:
                await self._handle_control(ws, codec, payload)
            elif msg_type == MSG_PING:
                await self._send_frame(ws, _seal(codec, pong_payload(parse_ping(payload)), MSG_PONG))
            elif msg_type == MSG_AUDIO:
                # MIC_STREAM is a *stream id*, so it lives with the link, not with
                # the frame codec. Importing it from `.frames` raised ImportError on
                # the first audio frame, which closed the whole session — the audio
                # path was therefore never exercised end to end here.
                from phonebridge.audio.frames import (  # noqa: PLC0415
                    AudioFrameError,
                    decode_frame,
                )
                from phonebridge.audio.link import MIC_STREAM  # noqa: PLC0415

                try:
                    frame = decode_frame(payload)
                except AudioFrameError as exc:
                    self.audio_bad_frames += 1
                    log.warning("bad audio frame: %s", exc)
                    continue
                self.audio_frames_in += 1
                # With no sound card, "playback" means: consume the frame, and if
                # the microphone stream is active, send the same audio back. That
                # makes the simulator a real loopback peer for integration tests.
                if self.echo_playback_as_mic and self.audio["mic"] == "active" and frame.payload:
                    self.audio_frames_out += 1
                    echo = frame.__class__(
                        seq=frame.seq & 0xFFFF,
                        timestamp_ms=frame.timestamp_ms,
                        payload=frame.payload,
                        flags=frame.flags,
                    ).encode()
                    await self._send_frame(ws, _seal(codec, echo, MSG_AUDIO, MIC_STREAM))
            elif msg_type == MSG_FILE:
                await self._handle_file_chunk(ws, codec, stream_id, payload)
            elif msg_type == MSG_SENSOR:
                log.debug("ignoring sensor frame from the PC (one-way in v1)")
            elif msg_type == MSG_BLOB:
                self.blobs_echoed += 1
                # Echo the blob back on the same stream: a clean, boring way to
                # prove binary round-trips without involving real hardware.
                await self._send_frame(ws, _seal(codec, payload, MSG_BLOB, stream_id))

    async def _handle_control(
        self, ws: ServerConnection, codec: SecureCodec, payload: bytes
    ) -> None:
        try:
            msg = decode_control(payload)
        except ProtocolError:
            return
        kind = msg.get("k")
        if kind == "req":
            # dispatch() returns the control payload; it still has to be sealed
            # into a SECURE envelope before it goes on the wire.
            reply = self.router.dispatch(msg)
            await self._send_frame(ws, _seal(codec, reply))
        elif kind == "evt":
            log.debug("event from PC: %s", msg.get("event"))
        elif kind == "res":
            log.debug("unexpected response from PC: %s", msg.get("id"))

    def _file_target(self) -> Path:
        """The directory this phone stores received files in, created on demand."""
        if self.file_dir is not None:
            self.file_dir.mkdir(parents=True, exist_ok=True)
            return self.file_dir
        if self._scratch is None:
            import tempfile  # noqa: PLC0415

            self._scratch = Path(tempfile.mkdtemp(prefix="phonebridge-sim-files-"))
        return self._scratch

    @staticmethod
    async def _later(
        loop: Any, delay: float, action: Any, *args: Any
    ) -> None:
        """Run `action(*args)` after `delay`, so a handshake can land first."""
        import asyncio as _asyncio  # noqa: PLC0415

        await _asyncio.sleep(delay)
        result = action(*args)
        if _asyncio.iscoroutine(result):
            await result

    async def _send_frame(self, ws: ServerConnection, data: bytes) -> None:
        """Send one envelope, keeping the outbound counters honest."""
        await ws.send(data)
        self.frames_out += 1
        self.bytes_out += len(data)

    def capabilities(self) -> dict[str, Any]:
        """The capability map, derived the way the phone derives it.

        Implemented AND hardware present AND permission granted - so flipping a
        permission in `self.permissions` changes what `system.info` reports, and
        the PC-side contract can be tested without pretending.
        """
        out: dict[str, Any] = {"simulated": True}
        for key in CAPABILITY_KEYS:
            permission = _PERMISSION_FOR.get(key)
            granted = True if permission is None else self.permissions.get(permission) == "granted"
            usable = bool(_HARDWARE[key] and key in self.implemented and granted)
            out[key] = usable
            out[f"hardware.{key}"] = _HARDWARE[key]
            if usable:
                out[f"detail.{key}"] = _DETAIL[key]
            elif not granted and permission is not None:
                out[f"detail.{key}"] = f"permission {permission} is {self.permissions.get(permission)}"
            else:
                out[f"detail.{key}"] = _DETAIL[key]
        # Declared, not inferred: the PC must be able to tell "not implemented"
        # apart from "no hardware" without guessing (docs/03 §5).
        for key in self.implemented:
            out[f"implemented.{key}"] = True
        return out

    # ------------------------------------------------------------------ #
    # test/automation helpers
    # ------------------------------------------------------------------ #
    async def emit_camera_frames(self, count: int = 3) -> int:
        """Send synthetic camera frames to every connected PC.

        The simulator has no camera; this exists so an integration test can prove
        the *receive* path (header, validation, gate, newest-frame policy) over a
        real authenticated WebSocket. The frame's bytes are a real JPEG, and the
        `camera.start` reply says "simulated" so nothing claims hardware.
        """
        from phonebridge.camera.frames import FLAG_KEY, FLAG_START, VideoFrame  # noqa: PLC0415
        from phonebridge.camera.stream import VIDEO_STREAM  # noqa: PLC0415

        sent = 0
        for ws, codec in list(self.connections):
            for _ in range(count):
                flags = FLAG_KEY | (FLAG_START if self.camera_seq == 0 else 0)
                body = VideoFrame(
                    seq=self.camera_seq & 0xFFFF,
                    timestamp_ms=self.camera_seq * 66,
                    payload=_SYNTHETIC_JPEG,
                    width=self.camera["width"],
                    height=self.camera["height"],
                    flags=flags,
                ).encode()
                self.camera_seq = (self.camera_seq + 1) & 0xFFFF
                self.camera_frames_sent += 1
                try:
                    await self._send_frame(ws, _seal(codec, body, MSG_VIDEO, VIDEO_STREAM))
                    sent += 1
                except Exception:  # noqa: BLE001 - one dead peer must not block others
                    pass
        return sent

    async def emit_camera_body(self, body: bytes) -> int:
        """Put one raw `MSG_VIDEO` body on the wire, valid or not.

        Used by the tests that prove the *receiver* rejects a malformed frame
        instead of dying: a sender that can only send good frames cannot test that.
        """
        from phonebridge.protocol.messages import MSG_VIDEO  # noqa: PLC0415

        sent = 0
        for ws, codec in list(self.connections):
            try:
                await self._send_frame(ws, _seal(codec, body, MSG_VIDEO, 3))
                sent += 1
            except Exception:  # noqa: BLE001 - one dead peer must not block others
                pass
        return sent

    async def emit_camera_stop(self) -> int:
        """Send the documented STOP frame and mark the simulated camera idle."""
        from phonebridge.camera.frames import FLAG_STOP, VideoFrame  # noqa: PLC0415

        self.camera["streaming"] = False
        body = VideoFrame(
            seq=self.camera_seq & 0xFFFF,
            timestamp_ms=0,
            payload=b"",
            width=16,
            height=16,
            flags=FLAG_STOP,
        ).encode()
        return await self.emit_camera_body(body)

    # ------------------------------------------------------------------ #
    # Phase 5: sensors
    # ------------------------------------------------------------------ #
    def _ensure_sensor_emitter(self) -> None:
        """Start the background emitter once, if a stream is active."""
        task = getattr(self, "_sensor_emitter", None)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop in a sync test
            return
        self._sensor_emitter = loop.create_task(
            self._sensor_emit_loop(), name="sim-sensors"
        )

    async def _sensor_emit_loop(self) -> None:
        """Emit one reading per active sensor every sampling interval.

        This is what a phone does: the listener fires on its own schedule. The
        simulator needs it so the PC's measured rates are real measurements of a
        real stream, and so `phonebridge selftest` can exercise one. A test that
        needs exact counts turns [sensor_auto_emit] off and drives the frames
        itself.
        """
        while True:
            active = list(self.sensor_streaming["active"])
            if not active or not self.connections:
                return
            interval_us = max(5_000, int(self.sensor_streaming["interval_us"] or 20000))
            await asyncio.sleep(min(interval_us / 1_000_000, 0.2))
            if not self.sensor_streaming["active"]:
                return
            try:
                await self.emit_sensor_frames(1)
            except Exception as exc:  # noqa: BLE001 - a stream must not kill the phone
                log.warning("simulated sensor emitter stopped: %s", exc)
                return

    async def emit_sensor_frames(self, count: int = 3, *, interval_us: int | None = None) -> int:
        """Send synthetic readings for every active sensor (tests do this).

        The values are computed from a sine wave, not measured, and the capability
        detail says so. What is *real* is the framing: header, per-sensor sequence
        numbers, timestamps and the PC's duplicate/gap accounting.
        """
        from phonebridge.protocol.messages import MSG_SENSOR as _MSG_SENSOR  # noqa: PLC0415
        from phonebridge.sensors.frames import SENSOR_STREAM, SensorFrame  # noqa: PLC0415

        active = list(self.sensor_streaming["active"])
        if not active:
            return 0
        interval = int(interval_us or self.sensor_streaming["interval_us"] or 20000)
        catalogue = {int(entry["id"]): entry for entry in _SIM_SENSORS}
        sent = 0
        for step in range(count):
            for sensor_id in active:
                entry = catalogue.get(sensor_id)
                if entry is None:
                    continue
                seq = int(self.sensor_streaming["seq"].get(sensor_id, 0))
                axes = 1
                if entry["kind"] in {1, 2, 4, 9, 10, 14, 16}:
                    axes = 3
                elif entry["kind"] in {11, 15, 20}:
                    axes = 4
                phase = step * 0.15
                if axes == 1:
                    values = (round(1.0 + step * 0.1, 3),)
                elif axes == 3:
                    values = (
                        round(0.2 * (step + 1), 3),
                        round(9.81 * (1.0 if sensor_id == 1 else 0.1), 3),
                        round(0.05 * sensor_id, 3),
                    )
                else:
                    values = (0.0, 0.0, 0.0, 1.0)
                started = float(self.sensor_streaming.get("started_at") or 0.0)
                elapsed_ms = int((time.monotonic() - started) * 1000) if started else 0
                body = SensorFrame(
                    sensor_id=sensor_id,
                    seq=seq & 0xFFFF,
                    timestamp_ms=elapsed_ms + int(step * interval / 1000),
                    kind=int(entry["kind"]),
                    values=values + (0.0,) * (axes - len(values)),
                ).encode()
                self.sensor_streaming["seq"][sensor_id] = (seq + 1) & 0xFFFF
                for ws, codec in list(self.connections):
                    try:
                        await self._send_frame(
                            ws, _seal(codec, body, _MSG_SENSOR, SENSOR_STREAM)
                        )
                        sent += 1
                    except Exception:  # noqa: BLE001
                        pass
                self.sensor_streaming["frames"] += 1
                del phase
        return sent

    # ------------------------------------------------------------------ #
    # Phase 5: files
    # ------------------------------------------------------------------ #
    async def _handle_file_chunk(
        self, ws: ServerConnection, codec: SecureCodec, stream_id: int, payload: bytes
    ) -> None:
        """Receive one file chunk, acknowledge it, and verify at the end."""
        import hashlib as _hashlib  # noqa: PLC0415

        from phonebridge.files.frames import (  # noqa: PLC0415
            FILE_STREAM_TO_PC,
            FLAG_ACK,
            FLAG_CANCEL,
            FLAG_END,
            FileChunk,
            FileFrameError,
            decode_chunk,
        )
        from phonebridge.protocol.messages import MSG_FILE as _MSG_FILE  # noqa: PLC0415

        try:
            chunk = decode_chunk(payload)
        except FileFrameError as exc:
            log.warning("bad file chunk: %s", exc)
            return
        if chunk.flags & FLAG_ACK:
            return
        entry = self.files.get(chunk.transfer_id)
        if entry is None or entry.get("direction") != "to_phone":
            if not (chunk.flags & FLAG_CANCEL):
                reply = FileChunk(
                    transfer_id=chunk.transfer_id, chunk_index=0, flags=FLAG_CANCEL
                ).encode()
                await self._send_frame(
                    ws, _seal(codec, reply, _MSG_FILE, FILE_STREAM_TO_PC)
                )
            return
        if chunk.flags & FLAG_CANCEL:
            entry["state"] = "cancelled"
            return
        expected = int(entry.get("chunk_count") or 0)
        if chunk.chunk_index < expected:
            # Duplicate: ack it, never append it twice.
            pass
        elif chunk.chunk_index > expected:
            entry["state"] = "failed"
            entry["error"] = f"chunk {chunk.chunk_index} arrived before {expected}"
            return
        else:
            entry["data"].extend(chunk.payload)
            entry["chunk_count"] = expected + 1
        entry["state"] = "receiving"
        if chunk.flags & FLAG_END:
            entry["state"] = "received"
            entry["sha256_actual"] = _hashlib.sha256(bytes(entry["data"])).hexdigest()
        ack = FileChunk(
            transfer_id=chunk.transfer_id,
            chunk_index=chunk.chunk_index,
            flags=FLAG_ACK,
        ).encode()
        if (
            self.file_drop_ack_from >= 0
            and chunk.chunk_index >= self.file_drop_ack_from
            and self.file_dropped_acks < self.file_drop_ack_count
        ):
            # A lost acknowledgement: the PC must notice and retry rather than
            # wait out its window and fail a transfer whose bytes all arrived.
            self.file_dropped_acks += 1
            log.info("dropping ack for chunk %d on purpose", chunk.chunk_index)
            return
        await self._send_frame(ws, _seal(codec, ack, _MSG_FILE, FILE_STREAM_TO_PC))

    async def _send_file_chunks(self, transfer_id: int) -> None:
        """Stream an offered file to every connected PC, acknowledging nothing."""
        import asyncio as _asyncio  # noqa: PLC0415

        from phonebridge.files.frames import (  # noqa: PLC0415
            FILE_STREAM_TO_PC,
            FLAG_END,
            FLAG_START,
            FileChunk,
        )
        from phonebridge.protocol.messages import MSG_FILE as _MSG_FILE  # noqa: PLC0415

        entry = self.files.get(transfer_id)
        if entry is None:
            return
        data = bytes(entry["data"])
        chunk_size = int(entry.get("chunk_size") or 65536)
        total = max(1, -(-len(data) // chunk_size))
        entry["decided"] = True
        start_index = int(entry.get("start_index") or 0)
        for index in range(start_index, total):
            block = data[index * chunk_size : (index + 1) * chunk_size]
            flags = 0
            if index == 0:
                flags |= FLAG_START
            if index == total - 1:
                flags |= FLAG_END
            body = FileChunk(
                transfer_id=transfer_id, chunk_index=index, flags=flags, payload=block
            ).encode()
            for ws, codec in list(self.connections):
                try:
                    await self._send_frame(ws, _seal(codec, body, _MSG_FILE, FILE_STREAM_TO_PC))
                except Exception:  # noqa: BLE001
                    pass
            if self.file_pace_s:
                await _asyncio.sleep(self.file_pace_s)

    # ------------------------------------------------------------------ #
    # Phase 5: Bluetooth
    # ------------------------------------------------------------------ #
    async def _run_ble_scan(self, duration_ms: int) -> None:
        """Report the fixed device list, one event per device, then stop."""
        import asyncio as _asyncio  # noqa: PLC0415

        for index, device in enumerate(_SIM_BLE_DEVICES):
            await _asyncio.sleep(0.02)
            now_ms = int(time.monotonic() * 1000)
            payload = dict(device)
            payload.setdefault("sightings", 1)
            payload["first_seen_ms"] = now_ms
            payload["last_seen_ms"] = now_ms
            # Each device is seen twice: that is how duplicate handling gets
            # exercised over a real socket rather than only in a unit test.
            for _repeat in range(2):
                existing = self.ble["devices_seen"].get(payload["address"])
                if existing is not None:
                    existing["sightings"] = int(existing.get("sightings", 1)) + 1
                    existing["rssi"] = payload["rssi"] - 3
                    existing["last_seen_ms"] = now_ms
                else:
                    self.ble["devices_seen"][payload["address"]] = dict(payload)
                await self.push_event(
                    "bluetooth.device",
                    dict(self.ble["devices_seen"][payload["address"]]),
                )
                await _asyncio.sleep(0.01)
            del index
        self.ble["scanning"] = False
        await self.push_event("bluetooth.scan", {"state": "stopped", "detail": ""})
        if duration_ms:
            log.debug("simulated scan finished after %d ms", duration_ms)

    async def _emit_ble_notifications(
        self, address: str, service: str, characteristic: str
    ) -> None:
        import asyncio as _asyncio  # noqa: PLC0415
        import base64 as _base64  # noqa: PLC0415

        for level in (87, 86, 84):
            await _asyncio.sleep(0.02)
            await self.push_event(
                "bluetooth.notification",
                {
                    "address": address,
                    "service": service
                    or "0000180f-0000-1000-8000-00805f9b34fb",
                    "characteristic": characteristic
                    or "00002a19-0000-1000-8000-00805f9b34fb",
                    "value_b64": _base64.b64encode(bytes([level])).decode("ascii"),
                    "ts_ms": level,
                },
            )

    async def push_event(self, name: str, data: dict[str, Any]) -> int:
        """Send an unsolicited event to every connected PC.  Returns the count.

        Used by the dashboard/telemetry tests and by ``phonebridge watch`` to
        prove the phone -> PC push direction works.
        """
        sent = 0
        for ws, codec in list(self.connections):
            try:
                await ws.send(_seal(codec, control_event(name, data)))
                sent += 1
            except Exception:  # noqa: BLE001 - one dead peer must not block others
                pass
        return sent


def _audio_format(params: dict[str, Any] | None):
    """Parse the proposed format, turning a bad one into a documented error code."""
    from phonebridge.audio.frames import AudioFormat, AudioFrameError  # noqa: PLC0415

    try:
        return AudioFormat.from_dict(params.get("format") if isinstance(params, dict) else None)
    except (AudioFrameError, ValueError) as exc:
        raise RpcError("bad_params", f"unusable audio format: {exc}") from exc


def _seal(codec: SecureCodec, payload: bytes, msg_type: int = MSG_CONTROL,
          stream_id: int = STREAM_CONTROL) -> bytes:
    return codec.seal(msg_type, stream_id, payload)


async def _send(ws: ServerConnection, codec: SecureCodec, payload: bytes,
                msg_type: int = MSG_CONTROL) -> None:
    await ws.send(_seal(codec, payload, msg_type))


def _primary_ipv4() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _identity_for(path: Path, name: str) -> Identity:
    if path.exists():
        return Identity.from_pem(path.read_bytes(), name=name)
    ident = Identity.generate(name=name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ident.to_pem())
    os.chmod(path, 0o600)
    return ident


async def main_async(args: argparse.Namespace) -> None:
    state_dir = Path(args.state_dir)
    ident = _identity_for(state_dir / "phone_identity.pem", args.name)
    phone = SimulatedPhone(
        identity=ident,
        state_file=state_dir / "phone_paired.json",
        name=args.name,
        pair_policy=args.pair_policy,
        host=args.host,
        port=args.port,
        beacon=args.beacon,
        model=args.model,
        manufacturer=args.manufacturer,
        codename=args.codename,
        sdk_int=args.sdk_int,
        android_release=args.android_release,
        headset_connected=args.headset == "yes",
        probe_profile=args.probe_profile,
        setup_timeout_s=args.setup_timeout,
        max_sessions=args.max_sessions,
        max_sessions_per_device=args.max_sessions_per_device,
    )
    await phone.start()
    print(f"Simulated phone '{args.name}' id={ident.device_id} on ws://{args.host}:{phone.port}")
    print(f"Pair policy: {args.pair_policy}. Pairing state: {state_dir}")
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        pass
    finally:
        await phone.stop()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--name", default="Pixel-sim")
    p.add_argument("--state-dir", default=".phone-sim")
    p.add_argument(
        "--pair-policy",
        choices=["auto", "prompt", "deny", "wait"],
        default="prompt",
        help="auto accepts, prompt asks on stdin, deny refuses, wait never decides "
        "(the request expires, which is how a forgotten prompt behaves)",
    )
    p.add_argument("--beacon", action="store_true", help="broadcast a discovery beacon")
    p.add_argument("--model", default="PhoneBridge Simulator",
                   help="model name reported by the simulated capability probe")
    p.add_argument("--manufacturer", default="PhoneBridge")
    p.add_argument("--codename", default="sim")
    p.add_argument("--sdk-int", type=int, default=34,
                   help="reported SDK level; <31 exercises the legacy SCO path")
    p.add_argument("--android-release", default="14")
    p.add_argument("--headset", choices=["yes", "no"], default="yes",
                   help="whether a headset is attached in the simulated probe")
    p.add_argument("--setup-timeout", type=float, default=20.0,
                   help="seconds a peer gets to finish the handshake (default 20)")
    p.add_argument("--max-sessions", type=int, default=8,
                   help="total concurrent sessions the phone will accept")
    p.add_argument("--max-sessions-per-device", type=int, default=4,
                   help="concurrent sessions allowed from one PC device id")
    p.add_argument("--probe-profile", default="duplex",
                   choices=["duplex", "output_only", "no_headset", "fail"],
                   help="simulated outcome of the capability probe")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
