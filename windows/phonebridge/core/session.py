"""The PC side of an established PhoneBridge session.

Responsibilities, in order:

1. open the link,
2. run the handshake and derive session keys,
3. authenticate (and pair on first use, guarded by a SAS the user compares),
4. multiplex RPC + events + binary streams over the encrypted channel,
5. survive a drop and re-establish the session with backoff.

Everything above this module (audio, camera, files) talks to a ``BridgeSession``
and never touches a socket.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..protocol.crypto import (
    HandshakeError,
    HandshakeInitiator,
    Identity,
    SecureCodec,
)
from ..protocol.framing import (
    KIND_CLOSE,
    KIND_SECURE,
    PROTOCOL_VERSION,
    FramingError,
    decode_envelope,
    encode_json_envelope,
)
from ..protocol.messages import (
    MSG_BLOB,
    MSG_BYE,
    MSG_CONTROL,
    MSG_PING,
    MSG_PONG,
    STREAM_CONTROL,
    ProtocolError,
    RpcIdSource,
    control_event,
    control_request,
    decode_control,
    handshake_msg,
    parse_ping,
    pong_payload,
)
from ..transport.link import LinkError, WebSocketLink, websocket_uri
from .device_store import DeviceStore

log = logging.getLogger("phonebridge.session")


class SessionError(Exception):
    """The session could not be established or was aborted by the user."""


class PairingRejected(SessionError):
    """The user (or the phone) refused to pair."""


@dataclass
class SessionStats:
    packets_sent: int = 0
    packets_received: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    rtt_ms_last: float | None = None
    rtt_ms_avg: float | None = None
    _rtt_samples: list[float] = field(default_factory=list, repr=False)

    def record_rtt(self, ms: float) -> None:
        self.rtt_ms_last = round(ms, 2)
        self._rtt_samples.append(ms)
        del self._rtt_samples[:-32]
        self.rtt_ms_avg = round(sum(self._rtt_samples) / len(self._rtt_samples), 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "packets_sent": self.packets_sent,
            "packets_received": self.packets_received,
            "bytes_sent": self.bytes_sent,
            "bytes_received": self.bytes_received,
            "rtt_ms_last": self.rtt_ms_last,
            "rtt_ms_avg": self.rtt_ms_avg,
        }


@dataclass
class PhoneInfo:
    device_id: str
    name: str
    paired: bool
    sas_digits: str
    protocol_version: int = 1
    capabilities: dict[str, Any] = field(default_factory=dict)


PairingConfirm = Callable[[str], Awaitable[bool] | bool]
EventHandler = Callable[[str, dict[str, Any]], None]
BlobHandler = Callable[[int, bytes], None]


class BridgeSession:
    """One live, encrypted, authenticated connection to the phone."""

    def __init__(
        self,
        uri: str,
        identity: Identity,
        store: DeviceStore,
        *,
        confirm_pairing: PairingConfirm | None = None,
        on_event: EventHandler | None = None,
        on_blob: BlobHandler | None = None,
        request_timeout: float = 15.0,
        link: WebSocketLink | None = None,
    ) -> None:
        self.uri = uri
        self.identity = identity
        self.store = store
        self.confirm_pairing = confirm_pairing
        self.on_event = on_event
        self.on_blob = on_blob
        self.request_timeout = request_timeout

        self._link = link or WebSocketLink(uri)
        self._codec: SecureCodec | None = None
        self._peer_static_bytes: bytes | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._blob_waiters: dict[int, asyncio.Queue[bytes]] = {}
        self._rpc_ids = RpcIdSource()
        self._closed = asyncio.Event()
        self._close_reason: str | None = None

        self.stats = SessionStats()
        self.phone: PhoneInfo | None = None
        self.opened_at: float | None = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def open(self) -> BridgeSession:
        await self._link.open()
        try:
            await self._handshake()
        except (PairingRejected, SessionError):
            await self._link.close("handshake failed")
            raise
        except (HandshakeError, FramingError, LinkError) as exc:
            await self._link.close("handshake failed")
            raise SessionError(str(exc)) from exc
        except Exception:
            await self._link.close("handshake failed")
            raise
        self._read_task = asyncio.create_task(self._read_loop(), name="phonebridge-read")
        self.opened_at = time.time()
        log.info(
            "session established with %s (%s) over %s",
            self.phone.name if self.phone else "?",
            self.phone.device_id if self.phone else "?",
            self.uri,
        )
        return self

    async def close(self, reason: str = "closed by PC") -> None:
        if self._closed.is_set():
            return
        self._close_reason = reason
        self._closed.set()
        if self._codec is not None:
            try:  # noqa: SIM105
                await self._link.send(encode_json_envelope(KIND_CLOSE, {"reason": reason}))
            except Exception:  # noqa: BLE001
                pass
        if self._read_task is not None:
            self._read_task.cancel()
            try:  # noqa: SIM105
                await self._read_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._read_task = None
        self._fail_pending(SessionError(f"session closed: {reason}"))
        await self._link.close(reason)

    async def wait_closed(self) -> str:
        await self._closed.wait()
        return self._close_reason or "closed"

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None and not self._closed.is_set()

    # ------------------------------------------------------------------ #
    # handshake
    # ------------------------------------------------------------------ #
    async def _handshake(self) -> None:
        initiator = HandshakeInitiator(self.identity)
        await self._link.send(initiator.hello())
        ack = decode_envelope(await self._link.recv())

        # Pin check before any key material is derived: find the claimed device
        # id in our local peer list and require the *exact* pinned key.
        try:
            ack_json = ack.as_json()
        except Exception as exc:  # noqa: BLE001
            raise SessionError(f"malformed HELLO_ACK from phone: {exc}") from exc
        claimed_id = str(ack_json.get("id", ""))
        pinned = self.store.find_peer(claimed_id) if claimed_id else None
        expected = pinned.static_key if pinned is not None else None

        keys = initiator.on_hello_ack(ack, expected_peer_static=expected)
        self._peer_static_bytes = initiator.peer_static
        self._codec = SecureCodec.for_client(keys)
        log.debug("handshake keys derived, SAS=%s", keys.sas_digits)

        # Authenticate inside the encrypted channel: successful AEAD decryption
        # already proves the peer holds both static and ephemeral private keys
        # that produced our session keys.
        await self._send_control(
            handshake_msg(
                "auth",
                id=self.identity.device_id,
                name=self.identity.name,
                proto=PROTOCOL_VERSION,
            )
        )
        reply = await self._expect_control({"auth_ok", "pair_required", "auth_fail"})

        if reply["k"] == "auth_fail":
            raise SessionError(f"phone refused authentication: {reply.get('reason')}")

        if reply["k"] == "auth_ok":
            self.phone = PhoneInfo(
                device_id=str(reply.get("id", "")),
                name=str(reply.get("name", "android-phone")),
                paired=True,
                sas_digits=keys.sas_digits,
                capabilities=reply.get("capabilities") or {},
            )
            self._mark_seen()
            return

        # ---- first-time pairing -------------------------------------- #
        self.phone = PhoneInfo(
            device_id=str(reply.get("id", "")),
            name=str(reply.get("name", "android-phone")),
            paired=False,
            sas_digits=keys.sas_digits,
            capabilities=reply.get("capabilities") or {},
        )
        confirmed = False
        if self.confirm_pairing is not None:
            result = self.confirm_pairing(keys.sas_digits)
            confirmed = await result if asyncio.iscoroutine(result) else bool(result)
        if not confirmed:
            await self._send_control(handshake_msg("pair_cancel"))
            raise PairingRejected(
                "pairing declined on the PC side; nothing was stored"
            )

        try:
            await self._send_control(handshake_msg("pair_confirm", sas=keys.sas_digits))
            final = await self._expect_control({"pair_ok", "pair_fail", "auth_fail"})
        except (LinkError, SessionError) as exc:
            raise PairingRejected(f"pairing did not complete: {exc}") from exc
        if final["k"] != "pair_ok":
            raise PairingRejected(
                f"phone rejected pairing: {final.get('reason', final['k'])}"
            )
        if self._peer_static_bytes is None:
            raise SessionError("internal error: peer identity key was not captured")
        self.store.pin_peer(
            self.phone.device_id, self.phone.name, self._peer_static_bytes
        )
        self._mark_seen()
        log.info("paired with %s and pinned its identity key", self.phone.name)

    def _mark_seen(self) -> None:
        if self.phone is None:
            return
        peer = self.store.find_peer(self.phone.device_id)
        if peer is not None:
            self.store.pin_peer(self.phone.device_id, self.phone.name, peer.static_key)

    async def _expect_control(self, accepted: set[str]) -> dict[str, Any]:
        deadline = time.monotonic() + self.request_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SessionError(
                    f"timed out waiting for {sorted(accepted)} from the phone"
                )
            raw = await asyncio.wait_for(self._link.recv(), remaining)
            env = decode_envelope(raw)
            if env.kind == KIND_CLOSE:
                raise SessionError("phone closed the connection during setup")
            if env.kind != KIND_SECURE or self._codec is None:
                raise SessionError("phone sent plaintext during an encrypted session")
            msg_type, _stream, payload = self._codec.unseal(env)
            if msg_type != MSG_CONTROL:
                continue
            msg = decode_control(payload)
            kind = str(msg.get("k"))
            if kind in accepted:
                return _normalise_reply(msg)
            if kind in {"auth_ok", "pair_required", "auth_fail", "pair_ok", "pair_fail"}:
                raise SessionError(f"unexpected handshake message {kind!r}")

    # ------------------------------------------------------------------ #
    # read loop
    # ------------------------------------------------------------------ #
    async def _read_loop(self) -> None:
        reason = "link closed"
        try:
            while True:
                raw = await self._link.recv()
                env = decode_envelope(raw)
                if env.kind == KIND_CLOSE:
                    reason = env.as_json().get("reason", "phone closed") if env.body else "phone closed"
                    break
                if env.kind != KIND_SECURE or self._codec is None:
                    raise SessionError("received plaintext frame mid-session")
                msg_type, stream_id, payload = self._codec.unseal(env)
                self.stats.packets_received += 1
                self.stats.bytes_received += len(raw)
                self._dispatch(msg_type, stream_id, payload)
        except asyncio.CancelledError:
            raise
        except (HandshakeError, ProtocolError, LinkError) as exc:
            reason = str(exc)
            log.warning("session read loop ended: %s", exc)
        except Exception as exc:  # noqa: BLE001
            reason = f"{type(exc).__name__}: {exc}"
            log.exception("unexpected error in read loop")
        finally:
            self._close_reason = reason
            self._closed.set()
            self._fail_pending(SessionError(reason))

    def _dispatch(self, msg_type: int, stream_id: int, payload: bytes) -> None:
        if msg_type == MSG_CONTROL:
            self._handle_control(payload)
        elif msg_type == MSG_PING:
            asyncio.create_task(
                self._safe_send(MSG_PONG, STREAM_CONTROL, pong_payload(parse_ping(payload)))
            )
        elif msg_type == MSG_PONG:
            pending = self._pending.pop(-2, None)
            if pending is not None and not pending.done():
                import struct

                echoed, _remote = struct.unpack(">QQ", payload) if len(payload) == 16 else (0, 0)
                rtt = (time.time_ns() - echoed) / 1e6
                self.stats.record_rtt(rtt)
                pending.set_result(rtt)
        elif msg_type in (MSG_BLOB, 0x30, 0x40, 0x50, 0x60):
            if self.on_blob is not None:
                self.on_blob(stream_id, payload)
            q = self._blob_waiters.get(stream_id)
            if q is not None:
                q.put_nowait(payload)
        elif msg_type == MSG_BYE:
            log.info("phone said bye: %s", payload.decode("utf-8", "replace"))

    def _handle_control(self, payload: bytes) -> None:
        try:
            msg = decode_control(payload)
        except ProtocolError as exc:
            log.warning("dropping malformed control message: %s", exc)
            return
        kind = msg.get("k")
        if kind == "res":
            rpc_id = msg.get("id")
            fut = self._pending.pop(rpc_id if isinstance(rpc_id, int) else -1, None)
            if fut is None or fut.done():
                return
            if msg.get("ok"):
                fut.set_result(msg.get("result"))
            else:
                err = msg.get("error") or {}
                fut.set_exception(
                    SessionError(f"{err.get('code', 'error')}: {err.get('message', '')}")
                )
        elif kind == "evt":
            name = str(msg.get("event", ""))
            data = msg.get("data") or {}
            if self.on_event is not None:
                try:
                    self.on_event(name, data)
                except Exception:  # noqa: BLE001
                    log.exception("event handler for %r raised", name)
        elif kind == "req":
            rpc_id = msg.get("id")
            method = msg.get("method")
            asyncio.create_task(
                self._safe_send_control(
                    control_event(
                        "res_unsupported",
                        {"id": rpc_id, "method": method,
                         "reason": "PC does not expose any RPC methods"},
                    )
                )
            )

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    async def call(self, method: str, params: dict[str, Any] | None = None,
                   timeout: float | None = None) -> Any:
        if not self.is_open:
            raise SessionError("session is not open")
        rpc_id = self._rpc_ids.next()
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rpc_id] = fut
        try:
            await self._send_control(control_request(rpc_id, method, params))
            return await asyncio.wait_for(fut, timeout or self.request_timeout)
        except asyncio.TimeoutError as exc:
            raise SessionError(f"phone did not answer {method!r} in time") from exc
        finally:
            self._pending.pop(rpc_id, None)

    async def ping(self, timeout: float = 5.0) -> float:
        """Application-level round-trip time in milliseconds."""
        import struct

        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[-2] = fut
        sent = time.time_ns()
        await self._safe_send(MSG_PING, STREAM_CONTROL, struct.pack(">Q", sent))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as exc:
            raise SessionError("phone did not answer PING") from exc
        finally:
            self._pending.pop(-2, None)

    async def send_blob(self, data: bytes, stream_id: int = 1) -> None:
        await self._safe_send(MSG_BLOB, stream_id, data)

    def subscribe_blobs(self, stream_id: int) -> asyncio.Queue[bytes]:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        self._blob_waiters[stream_id] = q
        return q

    async def send_event(self, name: str, data: dict[str, Any] | None = None) -> None:
        await self._send_control(control_event(name, data))

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    async def _send_control(self, payload: bytes) -> None:
        await self._safe_send(MSG_CONTROL, STREAM_CONTROL, payload)

    async def _safe_send_control(self, payload: bytes) -> None:
        await self._send_control(payload)

    async def _safe_send(self, msg_type: int, stream_id: int, payload: bytes) -> None:
        if self._codec is None:
            raise SessionError("session keys are not established")
        env = self._codec.seal(msg_type, stream_id, payload)
        await self._link.send(env)
        self.stats.packets_sent += 1
        self.stats.bytes_sent += len(env)

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # ------------------------------------------------------------------ #
    # factory helpers
    # ------------------------------------------------------------------ #
    @classmethod
    def for_host(
        cls,
        host: str,
        port: int,
        identity: Identity,
        store: DeviceStore,
        **kwargs: Any,
    ) -> BridgeSession:
        return cls(websocket_uri(host, port), identity, store, **kwargs)


def _normalise_reply(msg: dict[str, Any]) -> dict[str, Any]:
    """Flatten the small handshake replies into one shape.

    RPC-shaped replies (``{"k": "res", ...}``) carry the interesting data under
    ``result``; event-shaped ones carry it at the top level.
    """
    if msg.get("k") == "res":
        merged = {"k": "auth_ok" if msg.get("ok") else "auth_fail"}
        result = msg.get("result") or {}
        if isinstance(result, dict):
            merged.update(result)
        if not msg.get("ok"):
            merged["reason"] = (msg.get("error") or {}).get("message", "error")
        return merged
    return msg


# --------------------------------------------------------------------------- #
# reconnection
# --------------------------------------------------------------------------- #
StateCallback = Callable[[str, dict[str, Any]], None]


@dataclass
class BackoffPolicy:
    initial_s: float = 0.5
    max_s: float = 15.0
    factor: float = 1.8
    jitter: float = 0.2

    def delays(self):
        delay = self.initial_s
        while True:
            yield delay * (1 + random.uniform(-self.jitter, self.jitter))
            delay = min(delay * self.factor, self.max_s)


class ReconnectingSession:
    """Keeps a live session alive across drops.

    States reported through ``on_state``::

        connecting | handshaking | connected | reconnecting | failed | closed

    ``failed`` is terminal only when ``max_attempts`` is exhausted; otherwise a
    transient failure is reported as ``reconnecting``.  Audio/UI layers read
    ``current`` and ignore ``None``.
    """

    def __init__(
        self,
        uri: str,
        identity: Identity,
        store: DeviceStore,
        *,
        on_state: StateCallback | None = None,
        max_attempts: int | None = None,
        policy: BackoffPolicy | None = None,
        **session_kwargs: Any,
    ) -> None:
        self.uri = uri
        self.identity = identity
        self.store = store
        self.on_state = on_state
        self.max_attempts = max_attempts
        self.policy = policy or BackoffPolicy()
        self._session_kwargs = session_kwargs
        self.current: BridgeSession | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.attempts = 0

    def _emit(self, state: str, **details: Any) -> None:
        if self.on_state is not None:
            try:
                self.on_state(state, details)
            except Exception:  # noqa: BLE001
                log.exception("state callback raised")
        log.info("state=%s %s", state, details or "")

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="phonebridge-supervisor")

    async def _run(self) -> None:
        delays = self.policy.delays()
        last_error: str | None = None
        while not self._stop.is_set():
            self.attempts += 1
            self._emit("connecting", uri=self.uri, attempt=self.attempts)
            try:
                session = BridgeSession(self.uri, self.identity, self.store, **self._session_kwargs)
                await session.open()
                self.current = session
                self.attempts = 1
                self._emit("connected", phone=session.phone.name if session.phone else "?",
                           device_id=session.phone.device_id if session.phone else None)
                reason = await session.wait_closed()
                last_error = reason
                self._emit("reconnecting", reason=reason)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never let a drop kill us
                last_error = f"{type(exc).__name__}: {exc}"
                self._emit("failed", error=last_error, attempt=self.attempts)
            finally:
                self.current = None

            if self._stop.is_set():
                break
            if self.max_attempts is not None and self.attempts >= self.max_attempts:
                self._emit("closed", reason=last_error or "max attempts reached")
                return
            delay = next(delays)
            self._emit("reconnecting", retry_in_s=round(delay, 2), reason=last_error)
            try:  # noqa: SIM105
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()
        if self.current is not None:
            await self.current.close("stopped by user")
        if self._task is not None:
            self._task.cancel()
            try:  # noqa: SIM105
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def wait_connected(self, timeout: float = 15.0) -> BridgeSession:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.current is not None and self.current.is_open:
                return self.current
            await asyncio.sleep(0.05)
        raise SessionError(f"no session within {timeout}s")
