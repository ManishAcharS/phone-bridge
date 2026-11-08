"""Message layer that lives *inside* the encrypted channel.

Plaintext of every SECURE envelope is::

    u8 msg_type | u32 stream_id | payload[]

Control traffic is JSON; media traffic is raw binary.  Keeping both on one
encrypted channel means one congestion/ordering domain for Phase 1, and lets the
media plane move to its own socket later without changing this API.

``stream_id`` multiplexes independent binary flows (one audio stream, one video
stream, N file transfers).  Control frames always use stream 0.
"""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# --- control --------------------------------------------------------------- #
MSG_CONTROL = 0x10  # JSON: RPC request / response / event
MSG_PING = 0x11  # payload = u64 unix nanoseconds
MSG_PONG = 0x12  # payload = u64 echoed nanos | u64 responder monotonic nanos
MSG_BYE = 0x7F  # payload = UTF-8 reason

# --- media (Phase 2+) ------------------------------------------------------ #
MSG_AUDIO = 0x30  # u8 codec | u8 flags | u16 seq | payload
MSG_VIDEO = 0x40
MSG_FILE = 0x50
MSG_SENSOR = 0x60

# --- diagnostic ------------------------------------------------------------ #
MSG_BLOB = 0x21  # opaque bytes, used by `phonebridge bench`

STREAM_CONTROL = 0

CONTROL_REQUEST = "req"
CONTROL_RESPONSE = "res"
CONTROL_EVENT = "evt"


class ProtocolError(Exception):
    """Malformed control payload or an RPC-level failure."""


def handshake_msg(kind: str, **fields: Any) -> bytes:
    """Non-RPC protocol message inside the encrypted channel.

    Handshake/setup messages carry their own ``k`` (``auth``, ``auth_ok``,
    ``pair_required``, ``pair_confirm``, ``pair_ok``, ``pair_fail``,
    ``auth_fail``) and their fields at the top level.  They are deliberately
    distinct from ``req``/``res``/``evt`` so the session layer can tell setup
    traffic from application traffic.
    """
    return json.dumps({"k": kind, **fields}, separators=(",", ":"), default=str).encode(
        "utf-8"
    )


def control_request(rpc_id: int, method: str, params: dict[str, Any] | None = None) -> bytes:
    return json.dumps(
        {"k": CONTROL_REQUEST, "id": rpc_id, "method": method, "params": params or {}},
        separators=(",", ":"),
    ).encode("utf-8")


def control_response_ok(rpc_id: int, result: Any) -> bytes:
    return json.dumps(
        {"k": CONTROL_RESPONSE, "id": rpc_id, "ok": True, "result": result},
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def control_response_err(rpc_id: int, code: str, message: str) -> bytes:
    return json.dumps(
        {"k": CONTROL_RESPONSE, "id": rpc_id, "ok": False,
         "error": {"code": code, "message": message}},
        separators=(",", ":"),
    ).encode("utf-8")


def control_event(name: str, data: dict[str, Any] | None = None) -> bytes:
    return json.dumps(
        {"k": CONTROL_EVENT, "event": name, "data": data or {}},
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def decode_control(payload: bytes) -> dict[str, Any]:
    try:
        msg = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"control payload is not JSON: {exc}") from exc
    if not isinstance(msg, dict) or "k" not in msg:
        raise ProtocolError("control payload must be an object with a 'k' field")
    return msg


def ping_payload(sent_ns: int | None = None) -> bytes:
    import struct

    return struct.pack(">Q", sent_ns if sent_ns is not None else time.time_ns())


def pong_payload(echoed_ns: int) -> bytes:
    """Echo the sender's timestamp plus our own monotonic clock.

    The second field is informational only: RTT is computed from the echoed
    value alone. It uses a monotonic clock (not wall time) to match Android's
    ``System.nanoTime()``, so the two sides mean the same thing.
    """
    import struct

    return struct.pack(">QQ", echoed_ns, time.monotonic_ns())


def parse_ping(payload: bytes) -> int:
    import struct

    if len(payload) != 8:
        raise ProtocolError(f"PING payload must be 8 bytes, got {len(payload)}")
    return struct.unpack(">Q", payload)[0]


def parse_pong(payload: bytes) -> tuple[int, int]:
    import struct

    if len(payload) != 16:
        raise ProtocolError(f"PONG payload must be 16 bytes, got {len(payload)}")
    return struct.unpack(">QQ", payload)


# --------------------------------------------------------------------------- #
# tiny RPC bookkeeping helpers
# --------------------------------------------------------------------------- #
@dataclass
class PendingRequest:
    method: str
    sent_at: float
    future: Any = None


class RpcIdSource:
    def __init__(self) -> None:
        self._ids = itertools.count(1)

    def next(self) -> int:
        return next(self._ids)


Handler = Callable[[dict[str, Any]], Any]


@dataclass
class RpcRouter:
    """Server-side dispatch table for control requests."""

    handlers: dict[str, Handler] = field(default_factory=dict)

    def register(self, method: str, handler: Handler | None = None):
        """Register a handler, usable directly or as a decorator."""

        def _install(fn: Handler) -> Handler:
            self.handlers[method] = fn
            return fn

        if handler is None:
            return _install
        _install(handler)
        return None

    def dispatch(self, msg: dict[str, Any]) -> bytes:
        rpc_id = msg.get("id")
        method = msg.get("method")
        if not isinstance(rpc_id, int) or not isinstance(method, str):
            raise ProtocolError("request needs integer 'id' and string 'method'")
        handler = self.handlers.get(method)
        if handler is None:
            return control_response_err(
                rpc_id, "method_not_found", f"no handler for {method!r}"
            )
        try:
            result = handler(msg.get("params") or {})
        except ProtocolError as exc:
            return control_response_err(rpc_id, "bad_params", str(exc))
        except Exception as exc:  # noqa: BLE001 - never kill the session
            return control_response_err(rpc_id, "internal", f"{type(exc).__name__}: {exc}")
        return control_response_ok(rpc_id, result)
