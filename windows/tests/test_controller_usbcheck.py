"""Why the cable does not work — answered from two sources, not guessed.

The report this module exists for: *"ADB is installed but no device is connected"*
and *"no device on USB - plug in the cable and accept the debugging prompt"*, after
the user had already tried several cables. That message is the same whether the
cable is charge-only, the port is dead, the driver is missing, debugging is off, or
the phone was never plugged in — and it sends the user to the one action they have
already tried.

So: adb's view plus Windows' device tree, in a fixed decision order. Every branch
below is a case a real PC produces.
"""

from __future__ import annotations

import phonebridge.controller.usbcheck as usbcheck
from phonebridge.controller.adb import AdbDevice
from phonebridge.controller.usbcheck import (
    USB_ADB_MISSING,
    USB_DEBUGGING_OFF,
    USB_DRIVER,
    USB_KINDS,
    USB_NO_DEVICE,
    USB_OFFLINE,
    USB_READY,
    USB_UNAUTHORIZED,
    USB_UNKNOWN,
    USB_WIRELESS_ONLY,
    PnpDevice,
    UsbDiagnosis,
    diagnose,
    parse_pnp_csv,
    query_pnp,
)

# A Xiaomi phone, in the shapes Windows and adb actually report it.
XIAOMI_VID = r"USB\VID_2717&PID_FF48\4a2b1c3d"
CABLE_READY = AdbDevice(serial="R58M12345", state="device", model="Redmi_Note_14_Pro", usb="1-2")
CABLE_UNAUTHORIZED = AdbDevice(serial="R58M12345", state="unauthorized")
CABLE_OFFLINE = AdbDevice(serial="R58M12345", state="offline")
WIRELESS = AdbDevice(serial="192.168.1.9:5555", state="device")
MDNS = AdbDevice(serial="adb-3A21F9XYZ._adb-tls-connect._tcp", state="device")
EMULATOR = AdbDevice(serial="emulator-5554", state="device")


def win(*rows: PnpDevice) -> list[PnpDevice]:
    return list(rows)


def pnp(problem: int = 0, status: str = "OK", name: str = "Mi Phone") -> PnpDevice:
    return PnpDevice(instance_id=XIAOMI_VID, friendly_name=name, status=status, problem=problem)


def verdict(devices=(), pnp_rows=None) -> UsbDiagnosis:
    return diagnose(adb_devices=list(devices), pnp=pnp_rows, system="Windows")


# --------------------------------------------------------------------------- #
# the happy path first
# --------------------------------------------------------------------------- #
def test_a_usable_cable_device_is_ready_and_names_the_phone() -> None:
    result = verdict([CABLE_READY], [pnp()])
    assert result.kind == USB_READY
    assert result.ok
    assert "Redmi Note 14 Pro" in result.headline


def test_ready_is_decided_without_looking_at_windows_at_all() -> None:
    """No subprocess when the answer is already known."""
    assert verdict([CABLE_READY], None).kind == USB_READY


# --------------------------------------------------------------------------- #
# adb sees the cable but cannot use it
# --------------------------------------------------------------------------- #
def test_unauthorized_says_accept_the_prompt_then_reconnect() -> None:
    result = verdict([CABLE_UNAUTHORIZED], [pnp()])
    assert result.kind == USB_UNAUTHORIZED
    assert "accept the USB debugging prompt" in result.action
    assert "R58M12345" in result.headline


def test_offline_is_its_own_answer() -> None:
    result = verdict([CABLE_OFFLINE], [pnp()])
    assert result.kind == USB_OFFLINE
    assert "replug" in result.action


def test_an_unauthorized_device_wins_over_a_wireless_one() -> None:
    result = verdict([CABLE_UNAUTHORIZED, WIRELESS], [pnp()])
    assert result.kind == USB_UNAUTHORIZED


# --------------------------------------------------------------------------- #
# no cable in adb: Windows decides what is actually wrong
# --------------------------------------------------------------------------- #
def test_wireless_only_is_reported_as_wireless_not_usb() -> None:
    for device in (WIRELESS, MDNS):
        result = verdict([device], [pnp()])
        assert result.kind == USB_WIRELESS_ONLY, device.serial
        assert "no cable session" in result.headline


def test_an_emulator_is_not_a_phone_on_a_cable() -> None:
    """An emulator is neither USB nor a cable phone; it must not be offered as one."""
    assert verdict([EMULATOR], []).kind == USB_NO_DEVICE
    # With a real phone visible to Windows but absent from adb, the emulator
    # changes nothing: debugging is still what is missing.
    assert verdict([EMULATOR], [pnp()]).kind == USB_DEBUGGING_OFF


def test_nothing_enumerated_at_all_points_at_the_cable_and_the_port() -> None:
    result = verdict([], [])
    assert result.kind == USB_NO_DEVICE
    assert "cable" in result.action and "port" in result.action


def test_a_missing_driver_is_named_with_its_windows_code() -> None:
    result = verdict([], [pnp(problem=28, status="Error")])
    assert result.kind == USB_DRIVER
    assert "Code 28" in result.action
    assert "driver" in result.action.lower()


def test_code_43_names_the_cable_or_port_as_the_fastest_test() -> None:
    result = verdict([], [pnp(problem=43, status="Error")])
    assert result.kind == USB_DRIVER
    assert "Code 43" in result.action
    assert "cable" in result.action


def test_an_unknown_problem_code_is_still_reported_verbatim() -> None:
    result = verdict([], [pnp(problem=31, status="Error")])
    assert result.kind == USB_DRIVER
    assert "31" in result.action


def test_a_healthy_device_adb_cannot_see_means_debugging_is_off() -> None:
    result = verdict([], [pnp()])
    assert result.kind == USB_DEBUGGING_OFF
    assert "USB debugging ON" in result.action
    assert "File transfer" in result.action, "the charge-only USB mode is the usual cause"
    assert "Allow USB debugging" in result.action


def test_a_missing_adb_binary_is_reported_before_anything_else() -> None:
    result = diagnose(adb_devices=[], pnp=[], adb_available=False, system="Windows")
    assert result.kind == USB_ADB_MISSING
    assert "platform-tools" in result.action


# --------------------------------------------------------------------------- #
# honesty about what was not inspected
# --------------------------------------------------------------------------- #
def test_a_non_windows_host_does_not_invent_a_windows_conclusion() -> None:
    result = diagnose(adb_devices=[], pnp=[], system="Linux")
    assert result.kind == USB_UNKNOWN
    assert "not inspected" in result.headline


def test_an_uninspected_machine_says_so_instead_of_blaming_the_cable() -> None:
    result = diagnose(adb_devices=[], pnp=None, system="Windows")
    assert result.kind == USB_UNKNOWN


def test_every_kind_is_in_the_published_list() -> None:
    found = {
        verdict([CABLE_READY], [pnp()]).kind,
        verdict([CABLE_UNAUTHORIZED], [pnp()]).kind,
        verdict([CABLE_OFFLINE], [pnp()]).kind,
        verdict([WIRELESS], [pnp()]).kind,
        verdict([], []).kind,
        verdict([], [pnp(problem=28, status="Error")]).kind,
        verdict([], [pnp()]).kind,
        diagnose(adb_devices=[], pnp=[], adb_available=False, system="Windows").kind,
        diagnose(adb_devices=[], pnp=[], system="Linux").kind,
    }
    assert found == set(USB_KINDS)


def test_a_diagnosis_line_is_never_empty() -> None:
    for kind_result in (
        verdict([CABLE_READY], [pnp()]),
        verdict([], []),
        UsbDiagnosis(USB_UNKNOWN, "something"),
    ):
        assert kind_result.line().strip()


# --------------------------------------------------------------------------- #
# parsing Windows' device tree
# --------------------------------------------------------------------------- #
CSV = '''"Status","Class","FriendlyName","InstanceId","Problem"
"OK","USB","USB Composite Device","USB\\VID_2717&PID_FF48\\4a2b1c3d","0"
"Error","USB","Mi Phone","USB\\VID_2717&PID_FF40\\5b3c","43"
"OK","Net","Realtek USB GbE","USB\\VID_0BDA&PID_8153\\001","0"
'''


def test_pnp_csv_rows_are_parsed_with_their_problem_code() -> None:
    rows = parse_pnp_csv(CSV)
    assert [r.problem for r in rows] == [0, 43, 0]
    assert rows[1].friendly_name == "Mi Phone"
    assert rows[0].instance_id.startswith("USB\\VID_2717")


def test_only_phone_shaped_rows_count_as_android() -> None:
    rows = parse_pnp_csv(CSV)
    assert [r.looks_android for r in rows] == [True, True, False]


def test_an_android_class_device_is_recognised_without_a_known_vendor() -> None:
    row = PnpDevice(
        instance_id=r"USB\VID_1D6B&PID_0104\7",
        friendly_name="Android Device",
        class_name="AndroidUsbDeviceClass",
    )
    assert row.looks_android and row.healthy


def test_empty_or_junk_output_is_an_empty_list_not_a_crash() -> None:
    assert parse_pnp_csv("") == []
    assert parse_pnp_csv("\n\n") == []
    assert parse_pnp_csv('"Status"\n"OK"\n') == []


def test_the_device_tree_is_only_queried_on_windows(monkeypatch) -> None:
    def explode(_argv):  # noqa: ANN001
        raise AssertionError("PowerShell must not run on a non-Windows host")

    assert query_pnp(explode, system="Linux") == []
    assert query_pnp(explode, system="Darwin") == []


def test_the_windows_query_asks_powershell_for_a_csv(monkeypatch) -> None:
    seen: list[list[str]] = []

    def runner(argv):  # noqa: ANN001
        seen.append(list(argv))
        return CSV

    rows = query_pnp(runner, system="Windows")
    assert len(rows) == 3
    assert seen and seen[0][0] == "powershell"
    assert "Get-PnpDevice" in seen[0][-1]
    assert "ConvertTo-Csv" in seen[0][-1]


def test_a_failing_powershell_returns_nothing_instead_of_raising(monkeypatch) -> None:
    import subprocess

    def boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise OSError("powershell is missing")

    monkeypatch.setattr(subprocess, "run", boom)
    assert usbcheck._run_powershell(["powershell"]) == ""  # noqa: SLF001
    assert query_pnp(lambda _argv: "", system="Windows") == []
