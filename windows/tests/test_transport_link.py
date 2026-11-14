"""What the link says when the phone stops answering.

``str(ConnectionClosed)`` is "no close frame received or sent" for every abrupt
death, which reads like a defect in PhoneBridge. It is not: with no close frame
the connection simply stopped, and after a keepalive timeout that means the phone
went quiet. A phone that closes on purpose always sends a code and a reason, so
the two cases can — and must — read differently.
"""

from __future__ import annotations

from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

from phonebridge.transport.link import WebSocketLink


def link(**kwargs: float) -> WebSocketLink:
    options = {"ping_timeout": 8.0} | kwargs
    return WebSocketLink("ws://127.0.0.1:8765", **options)  # type: ignore[arg-type]


def test_an_abrupt_close_says_who_stopped_answering() -> None:
    message = link().explain_close(ConnectionClosedError(None, None))

    assert message != "no close frame received or sent", (
        "the library's opaque sentence must be replaced, not repeated"
    )
    assert "stopped answering" in message, "say who went quiet, in plain words"
    assert "keepalive" in message
    assert "8 s" in message
    assert "Autostart" in message or "killed" in message, "say what to do about it"


def test_a_polite_close_from_the_phone_is_quoted_verbatim() -> None:
    message = link().explain_close(ConnectionClosedOK(Close(1000, "phone stopping"), None))

    assert "the phone closed the session" in message
    assert "phone stopping" in message


def test_a_refusal_keeps_its_code_and_reason() -> None:
    message = link().explain_close(
        ConnectionClosedError(Close(1008, "too many connections from this device"), None)
    )

    assert "1008" in message
    assert "too many connections" in message


def test_the_keepalive_window_is_named_from_the_real_setting() -> None:
    assert "15 s" in link(ping_timeout=15.0).explain_close(ConnectionClosedError(None, None))
