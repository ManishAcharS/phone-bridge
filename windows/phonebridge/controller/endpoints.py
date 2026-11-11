"""Parsing and judging the address a user types.

The field is empty by default on purpose: ``127.0.0.1`` as a default would look
like a working address and silently mean "this PC", which is only correct when an
``adb forward`` or the simulator is in front of the phone.

Loopback is therefore *identified*, not forbidden — the USB transport genuinely
uses ``127.0.0.1:<forwarded port>`` — so the window and the log can say what such
an address can and cannot reach.
"""

from __future__ import annotations

import ipaddress

from ..transport.link import DEFAULT_PORT


class EndpointError(ValueError):
    """What the user typed is not an address we can try."""


def is_loopback(host: str) -> bool:
    """True for anything that can only mean *this* machine."""
    cleaned = host.strip().strip("[]").lower()
    if not cleaned:
        return False
    if cleaned in {"localhost", "localhost.localdomain", "::1"}:
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def parse_endpoint(text: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    """``"10.0.0.5"`` or ``"10.0.0.5:8765"`` -> ``(host, port)``.

    Strict about the port, permissive about the host: a hostname is a reasonable
    thing to type, a nonsense port is not.
    """
    cleaned = text.strip()
    if not cleaned:
        raise EndpointError("type the phone's address, for example 192.168.1.20:8765")
    host, separator, port_text = cleaned.partition(":")
    host = host.strip()
    if not host or " " in host:
        raise EndpointError(f"'{cleaned}' is not a host address")
    if not separator or not port_text.strip():
        return host, default_port
    port_text = port_text.strip()
    if not port_text.isdigit():
        raise EndpointError(f"'{port_text}' is not a port number")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise EndpointError(f"port {port} is outside 1-65535")
    return host, port


def describe_endpoint(host: str, port: int) -> str:
    """One sentence about what an address can reach. Used in the log and the GUI."""
    where = f"{host}:{port}"
    if is_loopback(host):
        return (
            f"{where} is this PC, not the phone - it only works with an adb forward "
            "or the simulator; use the phone's Wi-Fi/hotspot address otherwise"
        )
    return f"{where} will be tried first"
