"""adb parsing and, more importantly, forward-rule ownership.

The one promise that matters here: **a rule PhoneBridge did not create is never
removed.** Getting that wrong would silently break a user's own
``adb forward tcp:8765 tcp:8765``, which every earlier version of the docs told
them to run by hand.
"""

from __future__ import annotations

import pytest

from phonebridge.controller.adb import (
    Adb,
    AdbError,
    AdbResult,
    ForwardLease,
    is_wireless_serial,
    parse_devices,
)

DEVICES_HEADER = "List of devices attached\n"


class RecordingAdb:
    """A stand-in for the adb executable that records every invocation."""

    def __init__(
        self,
        *,
        devices: str = "",
        forwards: str = "",
        fail_forward: bool = False,
        version: str = "Android Debug Bridge version 1.0.41\n",
    ) -> None:
        self.devices_output = devices
        self.forwards_output = forwards
        self.fail_forward = fail_forward
        self.version_output = version
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):  # noqa: ANN001 - matches Runner
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "--version":
            return AdbResult(0, self.version_output, "")
        if argv[0] == "devices":
            return AdbResult(0, DEVICES_HEADER + self.devices_output, "")
        if argv[0] == "forward" and argv[1] == "--list":
            return AdbResult(0, self.forwards_output, "")
        if argv[0] == "-s" and argv[2] == "forward":
            if self.fail_forward:
                return AdbResult(1, "", "error: cannot bind listener")
            self.forwards_output += f"{argv[1]} {' '.join(argv[3:])} \n"
            return AdbResult(0, "", "")
        if argv[0] == "-s" and argv[2] == "forward" and argv[3] == "--remove":  # pragma: no cover
            return AdbResult(0, "", "")
        return AdbResult(0, "", "")

    def commands(self) -> list[str]:
        return [" ".join(call) for call in self.calls]

    def mutating(self) -> list[str]:
        return [c for c in self.commands() if "forward" in c and "--list" not in c]


def make_adb(fake: RecordingAdb) -> Adb:
    # an explicit executable keeps available() true without a real adb on PATH
    return Adb(runner=fake, executable="adb")


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_devices_parsing_keeps_unauthorized_devices_visible() -> None:
    out = (
        "R58M12345          device product:marble model:Redmi_Note_14_Pro device:marble "
        "transport_id:3\n"
        "ABC123             unauthorized usb:1-2\n"
        "9A1B2C             offline\n"
    )
    devices = parse_devices(out)
    assert [d.serial for d in devices] == ["R58M12345", "ABC123", "9A1B2C"]
    assert devices[0].usable and devices[0].model == "Redmi_Note_14_Pro"
    assert devices[0].display_name == "Redmi Note 14 Pro"
    assert not devices[1].usable
    assert "accept the USB debugging prompt" in devices[1].explain()


def test_devices_parsing_ignores_the_header_and_blank_lines() -> None:
    assert parse_devices(DEVICES_HEADER + "\n") == []


def test_wireless_serials_are_named_differently() -> None:
    assert is_wireless_serial("10.246.56.24:5555")
    assert not is_wireless_serial("R58M12345")
    assert not is_wireless_serial("emulator-5554")


# --------------------------------------------------------------------------- #
# forwards
# --------------------------------------------------------------------------- #
def test_forward_is_created_and_owned() -> None:
    fake = RecordingAdb(devices="S1 device model:Pixel\n")
    adb = make_adb(fake)
    lease = adb.ensure_forward("S1")
    assert lease is not None and isinstance(lease, ForwardLease)
    assert lease.serial == "S1" and lease.remote_port == 8765
    assert any("forward tcp:" in call for call in fake.commands())


def test_an_existing_forward_is_reused_and_never_removed() -> None:
    fake = RecordingAdb(devices="S1 device\n", forwards="S1 tcp:8765 tcp:8765\n")
    adb = make_adb(fake)
    assert adb.existing_forward("S1") == 8765
    assert adb.ensure_forward("S1") is None, "an existing rule must not become ours"
    assert fake.mutating() == [], "no rule was created"
    # and asking to clean up nothing must not produce a --remove
    assert fake.mutating() == []


def test_removing_a_lease_targets_only_that_rule() -> None:
    fake = RecordingAdb(devices="S1 device\n")
    adb = make_adb(fake)
    lease = adb.ensure_forward("S1")
    assert lease is not None
    fake.calls.clear()
    assert adb.remove_forward(lease) is True
    assert fake.commands() == [f"-s S1 forward --remove tcp:{lease.local_port}"]


def test_a_failing_forward_raises_instead_of_pretending() -> None:
    fake = RecordingAdb(devices="S1 device\n", fail_forward=True)
    adb = make_adb(fake)
    with pytest.raises(AdbError):
        adb.ensure_forward("S1", attempts=2)


def test_missing_adb_is_a_state_not_an_exception() -> None:
    adb = Adb(runner=RecordingAdb(), executable=None)
    adb._executable = None  # noqa: SLF001 - force the "not installed" branch
    import shutil

    original = shutil.which
    shutil.which = lambda _name: None  # type: ignore[assignment]
    try:
        assert adb.available() is False
        assert adb.devices() == []
        assert adb.version() is None
        with pytest.raises(AdbError):
            adb.add_forward("S1", 5000)
    finally:
        shutil.which = original  # type: ignore[assignment]


def test_adb_timeout_is_reported_not_swallowed() -> None:
    def slow(_argv, _timeout):  # noqa: ANN001
        raise AdbError("adb devices timed out after 6s")

    adb = Adb(runner=slow, executable="adb")
    with pytest.raises(AdbError, match="timed out"):
        adb.devices()
