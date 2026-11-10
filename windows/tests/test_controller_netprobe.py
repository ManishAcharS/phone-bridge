"""Reachability without the beacon: a TCP connect, and the default gateway.

The hotspot case is the reason this module exists. When the phone runs the
hotspot, the phone *is* the PC's default gateway, so one connect to that address
answers "is the app listening?" even if every UDP datagram is being dropped.
These tests pin down that the parsing is per-platform and that no address is ever
invented: the answer comes from the OS, or there is no answer.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from phonebridge.controller.netprobe import (
    Gateway,
    _parse_gateways,
    default_gateways,
    network_profile,
    tcp_probe,
)


# --------------------------------------------------------------------------- #
# TCP
# --------------------------------------------------------------------------- #
async def test_tcp_probe_finds_a_listening_port() -> None:
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await tcp_probe("127.0.0.1", port, timeout=2.0) is True
    finally:
        server.close()
        await server.wait_closed()


async def test_tcp_probe_reports_a_closed_port_as_false() -> None:
    # Bind and close, so the port is very likely unused and nothing is listening.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert await tcp_probe("127.0.0.1", port, timeout=1.0) is False


async def test_tcp_probe_always_answers_a_bool_and_never_raises() -> None:
    """A probe that raises would turn "unreachable" into a crash in the retry ladder."""
    result = await tcp_probe("192.0.2.1", 8765, timeout=0.2)
    assert result is False or result is True


# --------------------------------------------------------------------------- #
# Windows route table
# --------------------------------------------------------------------------- #
WINDOWS_ROUTE = """
===========================================================================
IPv4 Route Table
===========================================================================
Active Routes:
Network Destination        Netmask          Gateway       Interface  Metric
          0.0.0.0          0.0.0.0      192.168.137.1    192.168.137.99     25
          0.0.0.0          0.0.0.0       10.63.164.1      10.63.164.24     50
        127.0.0.0        255.0.0.0         On-link         127.0.0.1    331
===========================================================================
"""


def test_windows_default_routes_are_read_per_interface() -> None:
    gateways = _parse_gateways(WINDOWS_ROUTE, "Windows")
    assert [g.address for g in gateways] == ["192.168.137.1", "10.63.164.1"]
    assert gateways[0].interface == "192.168.137.99"


def test_a_hotspot_gateway_is_kept_even_though_it_is_not_192_168_43_1() -> None:
    """The subnet is never assumed: whatever the OS says is the gateway wins."""
    text = "0.0.0.0  0.0.0.0  172.20.10.1  172.20.10.2  25\n"
    assert [g.address for g in _parse_gateways(text, "Windows")] == ["172.20.10.1"]


def test_on_link_rows_are_not_gateways() -> None:
    gateways = _parse_gateways(WINDOWS_ROUTE, "Windows")
    assert "On-link" not in [g.address for g in gateways]
    assert "127.0.0.1" not in [g.address for g in gateways]


def test_duplicate_gateways_appear_once() -> None:
    text = (
        "0.0.0.0  0.0.0.0  192.168.1.1  192.168.1.9  25\n"
        "0.0.0.0  0.0.0.0  192.168.1.1  192.168.1.9  30\n"
    )
    assert [g.address for g in _parse_gateways(text, "Windows")] == ["192.168.1.1"]


# --------------------------------------------------------------------------- #
# macOS / Linux
# --------------------------------------------------------------------------- #
def test_macos_route_get_is_parsed() -> None:
    text = """
   route to: default
destination: default
       mask: default
    gateway: 192.168.1.1
  interface: en0
"""
    gateways = _parse_gateways(text, "Darwin")
    assert gateways == [Gateway("192.168.1.1", "en0")]


def test_linux_default_route_is_parsed() -> None:
    text = "default via 10.42.0.1 dev wlp2s0 proto dhcp metric 600\n"
    gateways = _parse_gateways(text, "Linux")
    assert gateways == [Gateway("10.42.0.1", "wlp2s0")]


def test_linux_route_without_a_device_still_yields_the_address() -> None:
    text = "default via 172.20.10.1 metric 100\n"
    assert [g.address for g in _parse_gateways(text, "Linux")] == ["172.20.10.1"]


# --------------------------------------------------------------------------- #
# default_gateways: the OS is asked, never guessed
# --------------------------------------------------------------------------- #
def test_powershell_answer_is_used_without_falling_back() -> None:
    calls: list[list[str]] = []

    def runner(argv, timeout):  # noqa: ANN001
        del timeout
        calls.append(list(argv))
        if argv[0] == "powershell":
            return "192.168.137.1\n"
        raise AssertionError("route print should not be needed")

    assert [g.address for g in default_gateways(runner=runner, system="Windows")] == [
        "192.168.137.1"
    ]
    assert len(calls) == 1


def test_route_print_is_the_fallback_when_powershell_says_nothing() -> None:
    def runner(argv, timeout):  # noqa: ANN001
        del timeout
        return "" if argv[0] == "powershell" else WINDOWS_ROUTE

    assert [g.address for g in default_gateways(runner=runner, system="Windows")] == [
        "192.168.137.1",
        "10.63.164.1",
    ]


def test_no_output_means_no_gateway_rather_than_a_guess() -> None:
    assert default_gateways(runner=lambda argv, timeout: "", system="Windows") == []


def test_powershell_loopback_is_not_a_gateway() -> None:
    def runner(argv, timeout):  # noqa: ANN001
        del timeout
        return "0.0.0.0\n127.0.0.1\n" if argv[0] == "powershell" else ""

    # Nothing usable came out of PowerShell, so the table is consulted and the
    # result is honestly empty.
    assert default_gateways(runner=runner, system="Windows") == []


# --------------------------------------------------------------------------- #
# firewall profile
# --------------------------------------------------------------------------- #
def test_public_profile_is_reported_verbatim() -> None:
    def runner(argv, timeout):  # noqa: ANN001
        del argv, timeout
        return "Public\n"

    assert network_profile("Windows", runner=runner) == "Public"


def test_profile_is_unknown_when_powershell_is_unavailable() -> None:
    assert network_profile("Windows", runner=lambda argv, timeout: "") is None


def test_profile_is_not_a_windows_question_on_other_systems() -> None:
    def runner(argv, timeout):  # noqa: ANN001
        raise AssertionError("must not run commands on a non-Windows host")

    assert network_profile("Linux", runner=runner) is None


@pytest.mark.parametrize("address", ["0.0.0.0", "255.255.255.255", "127.0.0.1"])
def test_unusable_addresses_are_never_offered_as_gateways(address: str) -> None:
    assert _parse_gateways(f"0.0.0.0  0.0.0.0  {address}  10.0.0.5  25\n", "Windows") == []
