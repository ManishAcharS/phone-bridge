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
    PeerKeyChangedError,
    ProtocolVersionError,
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
    control_response_err,
    decode_control,
    handshake_msg,
    parse_ping,
    pong_payload,
)
from ..transport.link import LinkError, WebSocketLink, websocket_uri
from .device_store import DeviceStore, StoreCorruptError
from .sanitize import device_label, printable

log = logging.getLogger("phonebridge.session")

#: How many frames / bytes the phone may send before the handshake completes.
#: Everything before that point is unauthenticated, so a peer that opens a socket
#: and then streams thousands of junk frames costs us CPU and memory for free.
#: A genuine phone sends exactly three frames (HELLO is ours; HELLO_ACK, the
#: auth reply and sometimes one empty frame arrive from it).
HANDSHAKE_FRAME_BUDGET = 32
HANDSHAKE_BYTE_BUDGET = 256 * 1024


class SessionError(Exception):
    """The session could not be established or was aborted by the user."""


class TerminalSessionError(SessionError):
    """A failure that retrying cannot fix.

    The supervisor stops on this and reports ``closed`` instead of looping:
    a changed identity key, a protocol-version mismatch, or the phone refusing
    authentication. Without it, ``watch`` would reconnect forever against a
    device that is deliberately being refused - and, worse, a substituted
    device would keep getting handshakes.
    """


class PairingRejected(TerminalSessionError):
    """The user (or the phone) refused to pair.

    Terminal on purpose: retrying would re-prompt the user on every attempt for
    a decision they already made. Re-pairing is an explicit action, not a retry.
    """


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
        # PING/PONG waiters keyed by the timestamp we sent, so concurrent pings
        # cannot clobber each other (they used to share a single dict slot).
        self._pong_waiters: dict[int, asyncio.Future[Any]] = {}
        self._blob_waiters: dict[int, asyncio.Queue[bytes | None]] = {}
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
        try:
            await self._link.open()
        except LinkError as exc:
            # A refused TCP connection is an establishment failure like any
            # other. It used to escape as a raw LinkError, so every caller that
            # documented "SessionError on failure" - the CLI included - got a
            # traceback instead of the message it already knows how to print.
            raise SessionError(str(exc)) from exc
        try:
            await self._handshake()
        except TerminalSessionError:
            await self._link.close("handshake failed")
            raise
        except (PeerKeyChangedError, ProtocolVersionError) as exc:
            # Both are permanent disagreements with this peer, so they must
            # survive as terminal errors instead of collapsing into SessionError.
            await self._link.close("handshake failed")
            raise TerminalSessionError(str(exc)) from exc
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
            try:
                await self._link.send(encode_json_envelope(KIND_CLOSE, {"reason": reason}))
            except Exception as exc:  # noqa: BLE001
                # Expected when the link is already gone; not silent.
                log.debug("could not send CLOSE frame: %s", exc)
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
        try:
            raw_ack = await asyncio.wait_for(self._link.recv(), self.request_timeout)
        except asyncio.TimeoutError as exc:
            # A peer that accepts the socket and then says nothing used to hang
            # open() forever: there was a timeout for every later step but not
            # for the very first one.
            raise SessionError(
                f"phone accepted the connection but sent no HELLO_ACK within "
                f"{self.request_timeout:.0f}s"
            ) from exc
        ack = decode_envelope(raw_ack)

        # Pin check before any key material is derived: find the claimed device
        # id in our local peer list and require the *exact* pinned key.
        try:
            ack_json = ack.as_json()
        except Exception as exc:  # noqa: BLE001
            raise SessionError(f"malformed HELLO_ACK from phone: {exc}") from exc
        claimed_id = ack_json.get("id")
        try:
            pinned = (
                self.store.find_peer(claimed_id)
                if isinstance(claimed_id, str) and claimed_id
                else None
            )
        except StoreCorruptError as exc:
            # Fail closed: without a readable pin list we cannot tell a pinned
            # phone from a substituted one, so we do not connect at all.
            raise TerminalSessionError(str(exc)) from exc
        expected = pinned.static_key if pinned is not None else None

        keys = initiator.on_hello_ack(ack, expected_peer_static=expected)
        self._peer_static_bytes = initiator.peer_static
        self._codec = SecureCodec.for_client(keys)
        # The SAS is a value for a human to compare, not something to write into
        # a log file: anyone who can read the log would learn the code that the
        # comparison is supposed to be independent of.
        log.debug("handshake keys derived (pairing code withheld from the log)")

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
        reply = await self._expect_control(
            {"auth_ok", "pair_required", "auth_fail", "pair_fail"}
        )

        if reply["k"] == "pair_fail":
            # The phone can refuse before it ever asks to pair: too many pending
            # requests, or pairing switched off. That is a decision, not a
            # protocol error, and its reason belongs in front of the user.
            raise TerminalSessionError(
                "phone refused pairing: "
                f"{printable(reply.get('reason'), limit=120) or 'no reason given'}"
            )

        if reply["k"] == "auth_fail":
            # The phone refused this identity outright (version skew, identity
            # mismatch, or a pin it no longer honours). Retrying is pointless.
            raise TerminalSessionError(
                f"phone refused authentication: {printable(reply.get('reason'), limit=120)}"
            )

        # The device id was certified by HELLO_ACK (sha256 of the key we are now
        # talking to). Every later message must name the same device, or the
        # phone is trying to be two devices at once.
        peer_id = initiator.peer_device_id
        reply_id = reply.get("id")
        if peer_id is None or reply_id != peer_id:
            raise SessionError(
                "phone identified itself inconsistently: HELLO_ACK claimed "
                f"{peer_id!r}, the authenticated reply claimed {reply_id!r}"
            )
        phone_name = device_label(reply.get("name"), fallback="android-phone")

        if reply["k"] == "auth_ok":
            self.phone = PhoneInfo(
                device_id=peer_id,
                name=phone_name,
                paired=True,
                sas_digits=keys.sas_digits,
                capabilities=reply.get("capabilities") or {},
            )
            # auth_ok means the phone has *us* pinned. If our own pin list does
            # not know it (fresh profile, restored backup, deleted peers.json),
            # that must not silently downgrade this PC to unpinned trust: the
            # key we just proved is the phone's key, and the phone trusted us
            # first, so recording it restores mutual pinning.
            if pinned is None:
                static_bytes = self._peer_static_bytes
                if static_bytes is None:  # pragma: no cover - set just above
                    raise SessionError("internal error: peer identity key missing")
                try:
                    self.store.pin_peer(peer_id, phone_name, static_bytes)
                    log.info(
                        "%s was already paired with this PC but not pinned here; "
                        "re-pinned its identity key", phone_name,
                    )
                except (StoreCorruptError, ValueError) as exc:
                    raise TerminalSessionError(str(exc)) from exc
            self._mark_seen()
            return

        # ---- first-time pairing -------------------------------------- #
        self.phone = PhoneInfo(
            device_id=peer_id,
            name=phone_name,
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
        static_bytes = self._peer_static_bytes
        if static_bytes is None:
            raise SessionError("internal error: peer identity key was not captured")
        try:
            self.store.pin_peer(self.phone.device_id, self.phone.name, static_bytes)
        except (StoreCorruptError, ValueError) as exc:
            # The phone already stored us; without a local pin this PC would
            # reconnect unpinned, so stop and make the user fix the store.
            raise TerminalSessionError(str(exc)) from exc
        self._mark_seen()
        log.info("paired with %s and pinned its identity key", self.phone.name)

    def _close_detail(self) -> str:
        """Human-readable close code/reason for error messages."""
        info = getattr(self._link, "close_info", None)
        if info is None:
            return "no close detail"
        code, reason = info
        if code is None:
            return "no close frame"
        return f"{code}: {reason}" if reason else str(code)

    def _mark_seen(self) -> None:
        if self.phone is None:
            return
        peer = self.store.find_peer(self.phone.device_id)
        if peer is not None:
            self.store.pin_peer(self.phone.device_id, self.phone.name, peer.static_key)

    async def _expect_control(self, accepted: set[str]) -> dict[str, Any]:
        deadline = time.monotonic() + self.request_timeout
        frames = 0
        received = 0
        while True:
            frames += 1
            if frames > HANDSHAKE_FRAME_BUDGET:
                raise SessionError(
                    "phone sent more than "
                    f"{HANDSHAKE_FRAME_BUDGET} frames without finishing the "
                    "handshake; refusing (nothing is authenticated yet)"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SessionError(
                    f"timed out waiting for {sorted(accepted)} from the phone"
                )
            try:
                raw = await asyncio.wait_for(self._link.recv(), remaining)
            except LinkError as exc:
                # The phone refuses a session by closing with 1008 and a reason
                # ("too many connections from this device"). Dropping that text
                # left the user with "phone closed the connection" and no cause.
                raise SessionError(
                    f"phone closed the connection during setup ({self._close_detail()}): {exc}"
                ) from exc
            received += len(raw)
            if received > HANDSHAKE_BYTE_BUDGET:
                raise SessionError(
                    "phone sent more than "
                    f"{HANDSHAKE_BYTE_BUDGET} bytes without finishing the "
                    "handshake; refusing"
                )
            env = decode_envelope(raw)
            if env.kind == KIND_CLOSE:
                reason = "phone closed the connection during setup"
                try:
                    detail = str(env.as_json().get("reason") or "")
                except FramingError:
                    detail = ""
                raise SessionError(f"{reason} ({detail})" if detail else reason)
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
                    try:
                        reason = str(env.as_json().get("reason") or "phone closed")
                    except FramingError:
                        # A malformed CLOSE body is still a clean close.
                        reason = "phone closed"
                    break
                if env.kind != KIND_SECURE or self._codec is None:
                    raise SessionError("received plaintext frame mid-session")
                msg_type, stream_id, payload = self._codec.unseal(env)
                self.stats.packets_received += 1
                self.stats.bytes_received += len(raw)
                self._dispatch(msg_type, stream_id, payload)
        except asyncio.CancelledError:
            raise
        except LinkError as exc:
            reason = f"{exc} ({self._close_detail()})"
            log.warning("session read loop ended: %s", reason)
        except (HandshakeError, ProtocolError) as exc:
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
            import struct

            if len(payload) != 16:
                log.warning("ignoring malformed PONG (%d bytes)", len(payload))
                return
            echoed, _remote = struct.unpack(">QQ", payload)
            waiter = self._pong_waiters.pop(echoed, None)
            if waiter is not None and not waiter.done():
                rtt = (time.time_ns() - echoed) / 1e6
                self.stats.record_rtt(rtt)
                waiter.set_result(rtt)
        elif msg_type in (MSG_BLOB, 0x30, 0x40, 0x50, 0x60):
            if self.on_blob is not None:
                self.on_blob(stream_id, payload)
            q = self._blob_waiters.get(stream_id)
            if q is not None:
                q.put_nowait(payload)
        elif msg_type == MSG_BYE:
            reason = payload.decode("utf-8", "replace").strip() or "phone said bye"
            # A BYE is the phone's clean shutdown: honour it as a close so the
            # supervisor reconnects with the real reason in hand.
            log.info("phone said bye: %s", reason)
            self._close_reason = reason
            self._closed.set()
            self._fail_pending(SessionError(reason))

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
            # The PC exposes no RPC methods yet, but a request must still get a
            # *response*: answering with an event left the phone waiting for a
            # reply that never came (docs/03-protocol.md §6).
            rpc_id = msg.get("id")
            asyncio.create_task(
                self._safe_send_control(
                    control_response_err(
                        rpc_id if isinstance(rpc_id, int) else 0,
                        "method_not_found",
                        "the PC exposes no RPC methods",
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
            effective = self.request_timeout if timeout is None else timeout
            return await asyncio.wait_for(fut, effective)
        except asyncio.TimeoutError as exc:
            raise SessionError(f"phone did not answer {method!r} in time") from exc
        finally:
            self._pending.pop(rpc_id, None)

    async def ping(self, timeout: float = 5.0) -> float:
        """Application-level round-trip time in milliseconds.

        Safe to call concurrently: each in-flight ping waits on its own future
        keyed by the timestamp embedded in the frame.
        """
        import struct

        if not self.is_open:
            raise SessionError("session is not open")
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        sent = time.time_ns()
        self._pong_waiters[sent] = fut
        try:
            await self._safe_send(MSG_PING, STREAM_CONTROL, struct.pack(">Q", sent))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as exc:
            raise SessionError("phone did not answer PING") from exc
        finally:
            self._pong_waiters.pop(sent, None)

    async def send_blob(self, data: bytes, stream_id: int = 1) -> None:
        await self._safe_send(MSG_BLOB, stream_id, data)

    def subscribe_blobs(self, stream_id: int) -> asyncio.Queue[bytes | None]:
        """Frames from `stream_id`; ``None`` once the session has ended."""
        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
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
        for fut in list(self._pong_waiters.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pong_waiters.clear()
        self._release_blob_streams()

    def _release_blob_streams(self) -> None:
        """Wake every blob consumer with ``None`` after the session ends.

        A closed session used to leave a subscriber blocked in ``queue.get()``
        until its own timeout expired, which is how `bench` could hang for a
        minute after a drop. ``None`` is the documented end-of-stream marker.
        """
        for queue in list(self._blob_waiters.values()):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                # Make room: the oldest frame is worthless if we are closing.
                try:  # noqa: SIM105
                    queue.get_nowait()
                    queue.put_nowait(None)
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                    log.warning("blob subscriber queue could not be released")
        self._blob_waiters.clear()

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

        connecting | connected | reconnecting | closed

    ``reconnecting`` always carries a ``reason`` and usually a ``retry_in_s``.
    A single failed *attempt* is not a state of its own: it is folded into
    ``reconnecting`` so the UI never flickers to a scary terminal state while
    the supervisor is still working.  ``closed`` is the terminal state and is
    emitted when ``max_attempts`` is exhausted or ``stop()`` is called.
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
                # A healthy session resets the backoff ladder. Without this the
                # delay kept growing across sessions, so a drop after an hour of
                # use could wait the full 15 s instead of restarting at 0.5 s.
                delays = self.policy.delays()
                self._emit("connected", phone=session.phone.name if session.phone else "?",
                           device_id=session.phone.device_id if session.phone else None)
                reason = await session.wait_closed()
                last_error = reason
                self._emit("reconnecting", reason=reason)
            except asyncio.CancelledError:
                raise
            except TerminalSessionError as exc:
                # A changed identity key, a protocol skew, or a refusal by the
                # phone: retrying cannot help and would spam the peer forever.
                self._emit("closed", reason=str(exc), terminal=True)
                return
            except Exception as exc:  # noqa: BLE001 - never let a drop kill us
                last_error = f"{type(exc).__name__}: {exc}"
                # "failed" is reserved for a terminal give-up (max_attempts
                # exhausted). A transient error while retrying is "reconnecting".
                log.info("connection attempt %d failed: %s", self.attempts, last_error)
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
