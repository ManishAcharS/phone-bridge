"""WebSocket link to the phone.

The phone always runs the listener and the PC always dials out.  That single
decision makes every transport path identical from the PC's point of view:

* USB (development)   ``adb forward tcp:8765 tcp:8765`` -> ``ws://127.0.0.1:8765``
* USB (standalone)    Android "USB tethering" gives the phone an address such as
                      ``192.168.42.129`` -> ``ws://192.168.42.129:8765``
* Wi-Fi LAN           ``ws://<phone-wifi-ip>:8765``
* Phone hotspot       PC joined to the phone's AP -> ``ws://192.168.43.1:8765``

No internet route is required in any of those cases.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI
from websockets.protocol import State

from ..protocol.framing import MAX_ENVELOPE_BODY

log = logging.getLogger("phonebridge.link")

DEFAULT_PORT = 8765


class LinkError(Exception):
    """The link could not be established."""


def websocket_uri(host: str, port: int = DEFAULT_PORT, *, secure: bool = False) -> str:
    scheme = "wss" if secure else "ws"
    if ":" in host and not host.startswith("["):  # raw IPv6 literal
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}"


class WebSocketLink:
    def __init__(
        self,
        uri: str,
        *,
        connect_timeout: float = 6.0,
        ping_interval: float = 10.0,
        ping_timeout: float = 8.0,
        max_queue: int = 64,
        close_timeout: float = 1.0,
    ) -> None:
        self.uri = uri
        self.connect_timeout = connect_timeout
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.max_queue = max_queue
        #: How long ``close()`` waits for the peer's close frame. The library
        #: default is 10 s, which a hostile peer can hold open after every
        #: handshake it provokes -- refusing a connection must not cost more
        #: than making it.
        self.close_timeout = close_timeout
        self._ws: Any = None

    async def open(self) -> None:
        try:
            # A generous max_queue lets a short disk stall on the PC side
            # absorb a burst of media frames instead of dropping the link.
            self._ws = await asyncio.wait_for(
                ws_connect(
                    self.uri,
                    max_size=MAX_ENVELOPE_BODY + 64,
                    max_queue=self.max_queue,
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_timeout,
                    open_timeout=self.connect_timeout,
                    close_timeout=self.close_timeout,
                    compression=None,  # audio/video is already compressed
                ),
                timeout=self.connect_timeout + 1.0,
            )
        except asyncio.TimeoutError as exc:
            raise LinkError(
                f"timed out connecting to {self.uri}. Is the phone app running, "
                "the port forwarded, and Windows Firewall allowing it?"
            ) from exc
        except (OSError, InvalidURI, InvalidHandshake) as exc:
            raise LinkError(f"cannot connect to {self.uri}: {exc}") from exc
        log.debug("link open: %s", self.uri)

    def explain_close(self, exc: ConnectionClosed) -> str:
        """Say *who* ended the link, and whether the phone gave a reason.

        ``str(exc)`` is "no close frame received or sent" for every abrupt death,
        which reads like a bug in the PC. It is not: with no close frame the
        connection simply stopped, and the usual cause is this side's own
        keepalive giving up because the phone went quiet (the app frozen or
        killed, or the Wi-Fi link dropping). A phone that closes on purpose
        always sends a frame with a code and a reason, and says so here.
        """
        received = getattr(exc, "rcvd", None)
        if received is None:
            return (
                "the phone stopped answering: no reply to a keepalive for "
                f"{self.ping_timeout:.0f} s, then no close frame (1006). The app was "
                "probably frozen or killed — keep PhoneBridge in the phone's Autostart "
                "list and out of battery optimisation — or the network dropped"
            )
        code = getattr(received, "code", None)
        reason = (getattr(received, "reason", "") or "").strip()
        if code == 1000:
            return f"the phone closed the session ({reason or 'no reason given'})"
        return f"the phone closed the connection ({code}: {reason or 'no reason given'})"

    async def send(self, data: bytes) -> None:
        if self._ws is None:
            raise LinkError("link is not open")
        try:
            await self._ws.send(data)
        except ConnectionClosed as exc:
            raise LinkError(f"link closed while sending: {self.explain_close(exc)}") from exc

    async def recv(self) -> bytes:
        if self._ws is None:
            raise LinkError("link is not open")
        try:
            msg = await self._ws.recv()
        except ConnectionClosed as exc:
            raise LinkError(f"link closed: {self.explain_close(exc)}") from exc
        if isinstance(msg, str):
            raise LinkError("phone sent a text WebSocket frame; protocol requires binary")
        return bytes(msg)

    async def close(self, reason: str = "client closing") -> None:
        if self._ws is not None:
            try:  # noqa: SIM105
                await self._ws.close(code=1000, reason=reason[:120])
            except Exception:  # noqa: BLE001 - best effort teardown
                pass
            self._ws = None

    @property
    def close_info(self) -> tuple[int | None, str]:
        """The peer's close ``(code, reason)``, or ``(None, "")`` if it never closed.

        The phone refuses sessions by closing with 1008 and a human-readable
        reason ("too many connections from this device"). ``str(ConnectionClosed)``
        contains that text, but the caller needs it as data to show the user.
        """
        ws = self._ws
        if ws is None:
            return None, ""
        return ws.close_code, (ws.close_reason or "")

    @property
    def is_open(self) -> bool:
        """True only while the WebSocket is actually in the OPEN state.

        The previous version tested ``state is not None``, which is always true
        on a live object, so a closed link still reported itself as open.
        """
        return self._ws is not None and self._ws.state is State.OPEN
