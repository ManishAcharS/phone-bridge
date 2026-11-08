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
    MSG_BLOB,
    MSG_CONTROL,
    MSG_PING,
    MSG_PONG,
    STREAM_CONTROL,
    ProtocolError,
    RpcRouter,
    control_event,
    decode_control,
    handshake_msg,
    parse_ping,
    pong_payload,
)

log = logging.getLogger("phonebridge.sim")

CAPABILITIES = {
    "audio.playback": True,
    "audio.capture": True,
    "audio.bluetooth_sco": True,  # the real phone decides this at runtime
    "camera.back": True,
    "camera.front": True,
    "sensors": True,
    "file_transfer": True,
    "bluetooth_scan": True,
    "simulated": True,
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
    pair_policy: str = "auto"  # auto | prompt | deny
    host: str = "127.0.0.1"
    port: int = 8765
    beacon: bool = False
    advertise_host: str | None = None

    paired: dict[str, str] = field(default_factory=dict)  # peer_id -> static key b64
    router: RpcRouter = field(default_factory=RpcRouter)
    connections: list[tuple[ServerConnection, SecureCodec]] = field(default_factory=list)
    _server: Any = None
    _beacon_task: asyncio.Task[None] | None = None
    pairing_requests: int = 0
    handshakes: int = 0
    frames_in: int = 0
    bytes_in: int = 0
    blobs_echoed: int = 0
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
            return {
                "device_id": self.identity.device_id,
                "name": self.name,
                "model": "PhoneBridge Simulator",
                "android_version": "simulated",
                "protocol_version": 1,
                "capabilities": CAPABILITIES,
                "uptime_s": round(time.time() - self.started_at, 1),
                "platform": platform.platform(),
            }

        @r.register("system.echo")
        def _echo(params: dict[str, Any]) -> dict[str, Any]:
            return {"echo": params.get("value"), "at_ns": time.time_ns()}

        @r.register("session.stats")
        def _stats(_params: dict[str, Any]) -> dict[str, Any]:
            return {
                "frames_in": self.frames_in,
                "bytes_in": self.bytes_in,
                "blobs_echoed": self.blobs_echoed,
                "handshakes": self.handshakes,
                "paired_peers": len(self.paired),
            }

        @r.register("bluetooth.headset.status")
        def _bt_status(_params: dict[str, Any]) -> dict[str, Any]:
            return {
                "supported": True,
                "connected": False,
                "detail": "simulator has no real Bluetooth radio",
                "note": "on hardware this reports A2DP/HFP state from AudioManager",
            }

        @r.register("system.shutdown")
        def _shutdown(_params: dict[str, Any]) -> dict[str, Any]:
            asyncio.get_running_loop().call_later(0.1, self.stop_soon)
            return {"ok": True}

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
    async def _handle_connection(self, ws: ServerConnection) -> None:
        peer = getattr(ws, "remote_address", ("?", 0))
        log.info("connection from %s", peer)
        try:
            await self._serve_connection(ws)
        except Exception as exc:  # noqa: BLE001
            log.warning("connection from %s ended: %s: %s", peer, type(exc).__name__, exc)
        finally:
            log.info("connection from %s closed", peer)

    async def _serve_connection(self, ws: ServerConnection) -> None:
        responder = HandshakeResponder(self.identity)
        env = decode_envelope(await ws.recv())
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
        log.info(
            "handshake with %s (%s), known=%s, SAS=%s",
            hello.get("name"), hello.get("id"), known, responder.keys.sas_digits,
        )

        if not await self._authenticate(ws, codec, responder, hello, known):
            return

        self.connections.append((ws, codec))
        try:
            await self._serve_session(ws, codec)
        finally:
            self.connections = [c for c in self.connections if c[1] is not codec]

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
        env = decode_envelope(await ws.recv())
        try:
            msg_type, _stream, payload = codec.unseal(env)
        except HandshakeError as exc:
            log.warning("auth frame failed to decrypt: %s", exc)
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
            return False

        peer_id = str(msg.get("id", ""))
        peer_name = str(msg.get("name", "pc"))
        peer_static_b64 = b64e(responder.peer_static or b"")

        if peer_id != responder.peer_device_id:
            # The device id is a hash of the identity key, so a mismatch means a
            # broken or hostile peer.
            await _send(ws, codec, handshake_msg("auth_fail", reason="identity mismatch"))
            return False
        if peer_id in self.paired and self.paired[peer_id] != peer_static_b64:
            await _send(ws, codec, handshake_msg("auth_fail", reason="identity key changed"))
            return False

        if peer_id in self.paired:
            await _send(
                ws,
                codec,
                handshake_msg(
                    "auth_ok",
                    id=self.identity.device_id,
                    name=self.name,
                    capabilities=CAPABILITIES,
                ),
            )
            return True

        # ---- first-time pairing ------------------------------------------ #
        self.pairing_requests += 1
        sas = responder.keys.sas_digits  # type: ignore[union-attr]
        log.warning(
            "PAIRING REQUEST from %r. On the phone the user sees SAS %s; the PC "
            "must show the identical code.",
            peer_name,
            sas,
        )
        await _send(
            ws,
            codec,
            handshake_msg(
                "pair_required",
                id=self.identity.device_id,
                name=self.name,
                sas=sas,
                capabilities=CAPABILITIES,
            ),
        )
        decision = await self._await_pair_confirmation(ws, codec, sas, peer_name)
        if not decision:
            await _send(ws, codec, handshake_msg("pair_fail", reason="declined"))
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
    ) -> bool:
        """Wait for the PC's confirmation, then apply the phone-side policy.

        The confirmation is always read first, even when the policy is 'deny':
        that keeps the exchange symmetric and avoids closing the link on a PC
        that is still writing.
        """
        try:
            env = decode_envelope(await asyncio.wait_for(ws.recv(), 90))
            msg_type, _stream, payload = codec.unseal(env)
        except (asyncio.TimeoutError, HandshakeError):
            return False
        if msg_type != MSG_CONTROL:
            return False
        msg = decode_control(payload)
        if msg.get("k") == "pair_cancel":
            log.info("PC cancelled pairing")
            return False
        if msg.get("k") != "pair_confirm":
            return False
        if msg.get("sas") != sas:
            log.warning("PC confirmed a different SAS (%r vs %r)", msg.get("sas"), sas)
            return False
        if self.pair_policy == "auto":
            return True
        if self.pair_policy == "deny":
            return False
        answer = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: input(f"Allow pairing from {peer_name} (SAS {sas} matches PC)? [y/N] "),
        )
        return answer.strip().lower() in {"y", "yes"}

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
                await ws.send(_seal(codec, pong_payload(parse_ping(payload)), MSG_PONG))
            elif msg_type == MSG_BLOB:
                self.blobs_echoed += 1
                # Echo the blob back on the same stream: a clean, boring way to
                # prove binary round-trips without involving real hardware.
                await ws.send(_seal(codec, payload, MSG_BLOB, stream_id))

    async def _handle_control(
        self, ws: ServerConnection, codec: SecureCodec, payload: bytes
    ) -> None:
        try:
            msg = decode_control(payload)
        except ProtocolError:
            return
        kind = msg.get("k")
        if kind == "req":
            reply = self.router.dispatch(msg)
            await _send(ws, codec, reply)
        elif kind == "evt":
            log.debug("event from PC: %s", msg.get("event"))
        elif kind == "res":
            log.debug("unexpected response from PC: %s", msg.get("id"))

    # ------------------------------------------------------------------ #
    # test/automation helpers
    # ------------------------------------------------------------------ #
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
    p.add_argument("--pair-policy", choices=["auto", "prompt", "deny"], default="prompt")
    p.add_argument("--beacon", action="store_true", help="broadcast a discovery beacon")
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
