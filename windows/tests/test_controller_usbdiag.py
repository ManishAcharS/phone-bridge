"""The staged USB report: where the chain stops, and what to do about it.

The case this file is written for: USB debugging is already ON, the app is
running, and the phone still never shows the "Allow USB debugging?" prompt. The
prompt is the *last* link in a chain — Windows must enumerate the phone, then
expose its ADB interface, then the driver must bind, then the adb server must
open the device. If any earlier link is missing, the phone is never *asked*, so
telling the user to accept a prompt is useless. Each test below stops the chain
at a different link and asserts that the report says so.

Everything here is injected: ``pnp`` rows stand in for what ``Get-PnpDevice``
would return, and a stub stands in for ``adb``. Nothing in this file is
device-tested, and it says nothing about the real Redmi.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from phonebridge.controller.adb import AdbDevice
from phonebridge.controller.usbcheck import (
    STAGE_A_NO_PHONE,
    STAGE_B_NO_ADB_INTERFACE,
    STAGE_C_DRIVER,
    STAGE_D_ADB_SERVER,
    STAGE_E_UNAUTHORIZED,
    STAGE_F_OFFLINE,
    STAGE_G_WIRELESS,
    STAGE_H_SETTINGS,
    USB_ADB_MISSING,
    USB_DEBUGGING_OFF,
    USB_DRIVER,
    USB_NO_ADB_INTERFACE,
    USB_NO_DEVICE,
    USB_OFFLINE,
    USB_READY,
    USB_UNAUTHORIZED,
    USB_WIRELESS_ONLY,
    PnpDevice,
    build_report,
    collect,
    format_report,
    parse_pnp_csv,
)

# --------------------------------------------------------------------------- #
# the shapes Windows actually produces
# --------------------------------------------------------------------------- #
#: The composite parent the phone exposes when USB debugging is on.
COMPOSITE = PnpDevice(
    instance_id=r"USB\VID_2717&PID_FF48\4a2b1c3d",
    friendly_name="Android Composite ADB Interface",
    status="OK",
    class_name="AndroidUsbDeviceClass",
    service="usbccgp",
)

#: The specific interface adb binds to, as its own node.
ADB_INTERFACE = PnpDevice(
    instance_id=r"USB\VID_2717&PID_FF48&MI_01\6&2f1e2b3c&0&0001",
    friendly_name="Android ADB Interface",
    status="OK",
    class_name="AndroidUsbDeviceClass",
    service="WinUSB",
)

#: The phone in media-transfer mode: enumerated, and no ADB interface at all.
MTP_PHONE = PnpDevice(
    instance_id=r"USB\VID_2717&PID_FF48\4a2b1c3d",
    friendly_name="Redmi Note 14 Pro+",
    status="OK",
    class_name="WPD",
    service="WUDFRd",
)

CABLE = AdbDevice(serial="R58M12345", state="device", model="Redmi_Note_14_Pro", usb="1-2")
UNAUTHORIZED = AdbDevice(serial="R58M12345", state="unauthorized")
OFFLINE = AdbDevice(serial="R58M12345", state="offline")
WIRELESS = AdbDevice(serial="192.168.1.9:5555", state="device")
MDNS = AdbDevice(serial="adb-3A21F9XYZ._adb-tls-connect._tcp", state="device")
EMULATOR = AdbDevice(serial="emulator-5554", state="device")


def report(devices=(), rows=None, **kwargs):
    return build_report(
        adb_devices=list(devices),
        pnp=list(rows or []),
        system="Windows",
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# stage A: nothing reached Windows
# --------------------------------------------------------------------------- #
def test_a_phone_windows_never_saw_is_the_cable_or_the_port() -> None:
    result = report()
    assert result.kind == USB_NO_DEVICE
    assert result.stage == STAGE_A_NO_PHONE
    assert "cable" in result.action
    assert result.inspected is True
    assert result.phones == ()


def test_a_non_android_usb_device_does_not_count_as_the_phone() -> None:
    hub = PnpDevice(
        instance_id=r"USB\VID_1D6B&PID_0002\0000",
        friendly_name="USB 2.0 Hub",
        status="OK",
        class_name="USB",
    )
    assert report(rows=[hub]).kind == USB_NO_DEVICE


# --------------------------------------------------------------------------- #
# stage B: enumerated as MTP, no ADB interface — the missing prompt
# --------------------------------------------------------------------------- #
def test_mtp_without_an_adb_interface_is_named_as_why_no_prompt_appears() -> None:
    result = report(rows=[MTP_PHONE])
    assert result.kind == USB_NO_ADB_INTERFACE
    assert result.stage == STAGE_B_NO_ADB_INTERFACE
    assert result.mtp is True
    assert result.adb_interface is False
    assert "RSA prompt never appears" in result.headline
    assert "USB debugging" in result.action
    assert "File transfer" in result.action


def test_the_mtp_phone_is_reported_by_its_real_name() -> None:
    result = report(rows=[MTP_PHONE])
    assert result.phones == ("Redmi Note 14 Pro+",)
    assert "Redmi Note 14 Pro+" in " ".join(result.lines())


def test_mtp_does_not_mean_adb_and_the_report_says_both() -> None:
    """The exact confusion this stage exists for."""
    result = report(rows=[MTP_PHONE])
    text = "\n".join(result.lines())
    assert "MTP interface" in text and "yes" in text
    assert "ADB interface" in text and "NO" in text


def test_an_android_row_with_neither_interface_is_reported_as_a_settings_problem() -> None:
    bare = PnpDevice(
        instance_id=r"USB\VID_2717&PID_FF48\4a2b1c3d",
        friendly_name="Mi Phone",
        status="OK",
        class_name="USB",
    )
    result = report(rows=[bare])
    assert result.kind == USB_DEBUGGING_OFF
    assert result.stage == STAGE_H_SETTINGS


# --------------------------------------------------------------------------- #
# stage C: the interface is there, the driver is not usable
# --------------------------------------------------------------------------- #
def test_code_28_on_the_adb_interface_is_a_driver_problem_not_a_cable() -> None:
    broken = PnpDevice(
        instance_id=ADB_INTERFACE.instance_id,
        friendly_name="Android ADB Interface",
        status="Error",
        problem=28,
        class_name="AndroidUsbDeviceClass",
    )
    result = report(rows=[MTP_PHONE, broken])
    assert result.kind == USB_DRIVER
    assert result.stage == STAGE_C_DRIVER
    assert "Code 28" in result.headline or "driver is not installed" in result.action
    assert result.driver_ok is False
    assert "ADB interface" in result.headline


def test_code_43_points_at_the_cable_and_the_port() -> None:
    broken = PnpDevice(
        instance_id=COMPOSITE.instance_id,
        friendly_name="Android Composite ADB Interface",
        status="Error",
        problem=43,
        class_name="AndroidUsbDeviceClass",
    )
    result = report(rows=[broken])
    assert result.kind == USB_DRIVER
    assert "another cable" in result.action or "cable/port" in result.action


def test_a_driver_problem_is_reported_before_the_missing_interface() -> None:
    """A broken interface is evidence; don't tell the user to switch USB mode."""
    broken = PnpDevice(
        instance_id=ADB_INTERFACE.instance_id,
        friendly_name="Android ADB Interface",
        status="Error",
        problem=10,
        class_name="AndroidUsbDeviceClass",
    )
    assert report(rows=[MTP_PHONE, broken]).kind == USB_DRIVER


# --------------------------------------------------------------------------- #
# stage D: adb's own server
# --------------------------------------------------------------------------- #
def test_a_dead_adb_server_is_its_own_stage_with_a_safe_fix() -> None:
    result = report(rows=[ADB_INTERFACE], adb_error="cannot connect to daemon")
    assert result.kind == "adb-server"
    assert result.stage == STAGE_D_ADB_SERVER
    assert "adb kill-server" in result.action
    assert "does not delete your adb keys" in result.action


def test_a_missing_adb_binary_still_wins_over_the_server_stage() -> None:
    result = report(rows=[ADB_INTERFACE], adb_available=False, adb_error="ignored")
    assert result.kind == USB_ADB_MISSING


# --------------------------------------------------------------------------- #
# stages E/F: adb has the device, but not a usable one
# --------------------------------------------------------------------------- #
def test_unauthorized_is_stage_e() -> None:
    result = report(devices=[UNAUTHORIZED], rows=[ADB_INTERFACE])
    assert result.kind == USB_UNAUTHORIZED
    assert result.stage == STAGE_E_UNAUTHORIZED
    assert "accept" in result.action.lower()


def test_offline_is_stage_f() -> None:
    result = report(devices=[OFFLINE], rows=[ADB_INTERFACE])
    assert result.kind == USB_OFFLINE
    assert result.stage == STAGE_F_OFFLINE


# --------------------------------------------------------------------------- #
# stage G: wireless debugging is not a cable
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", [WIRELESS, MDNS])
def test_wireless_debugging_is_stage_g_and_never_usb(device: AdbDevice) -> None:
    result = report(devices=[device], rows=[ADB_INTERFACE])
    assert result.kind == USB_WIRELESS_ONLY
    assert result.stage == STAGE_G_WIRELESS
    assert result.ok is False


def test_an_emulator_is_not_a_phone_on_a_cable() -> None:
    result = report(devices=[EMULATOR], rows=[])
    assert result.kind != USB_READY
    assert result.stage != STAGE_E_UNAUTHORIZED


# --------------------------------------------------------------------------- #
# the usable case, and several devices at once
# --------------------------------------------------------------------------- #
def test_a_usable_cable_device_is_ready_and_skips_the_device_tree() -> None:
    result = report(devices=[CABLE], rows=[MTP_PHONE])
    assert result.kind == USB_READY
    assert result.ok is True
    assert result.stage == ""
    assert "R58M12345" in result.headline or "Redmi" in result.headline


def test_several_devices_report_the_one_that_is_usable() -> None:
    second = AdbDevice(serial="9A1B2C3D", state="device", model="Pixel_8", usb="2-1")
    result = report(devices=[UNAUTHORIZED, second], rows=[ADB_INTERFACE])
    assert result.kind == USB_READY
    assert "Pixel" in result.headline


def test_a_cabled_device_wins_over_a_wireless_one() -> None:
    result = report(devices=[WIRELESS, CABLE], rows=[ADB_INTERFACE])
    assert result.kind == USB_READY


# --------------------------------------------------------------------------- #
# localized and malformed PowerShell output
# --------------------------------------------------------------------------- #
def test_localized_status_text_still_yields_the_problem_code() -> None:
    csv_text = (
        "Status,Class,FriendlyName,InstanceId,Problem,Service\n"
        '"Fehler","AndroidUsbDeviceClass","Android ADB Interface",'
        '"USB\\VID_2717&PID_FF48&MI_01\\6&1","28",""\n'
    )
    rows = parse_pnp_csv(csv_text)
    assert len(rows) == 1
    assert rows[0].problem == 28
    assert rows[0].is_adb_interface is True
    result = report(rows=rows)
    assert result.kind == USB_DRIVER


def test_junk_from_powershell_is_an_empty_tree_not_a_crash() -> None:
    for junk in ("", "   ", "Get-PnpDevice : Access denied", '"""\n,""\n'):
        assert parse_pnp_csv(junk) == []


def test_a_report_with_no_tree_says_not_inspected_rather_than_blaming_the_cable() -> None:
    from phonebridge.controller.usbcheck import USB_UNKNOWN, diagnose

    verdict = diagnose(adb_devices=[], pnp=None, system="Windows")
    assert verdict.kind == USB_UNKNOWN
    assert "not inspected" in verdict.headline


# --------------------------------------------------------------------------- #
# collect(): the whole pipeline, with adb injected
# --------------------------------------------------------------------------- #
@dataclass
class StubAdb:
    """Stands in for ``controller.adb.Adb`` — no process is started."""

    devices: tuple = ()
    error: str = ""
    server: str = "running"
    present: bool = True
    binary: str = r"C:\adb\adb.exe"

    def available(self) -> bool:
        return self.present

    @property
    def path(self) -> str:
        return self.binary

    def version(self) -> str:
        return "Android Debug Bridge version 1.0.41"

    def probe_devices(self):
        return list(self.devices), self.error, self.server


def test_collect_reports_every_stage_from_one_call() -> None:
    result = collect(StubAdb(devices=(CABLE,)), runner=lambda argv: "", system="Windows")
    assert result.kind == USB_READY
    assert result.adb_version.startswith("Android Debug Bridge")
    assert result.adb_server == "running"


def test_collect_survives_a_broken_device_query() -> None:
    result = collect(
        StubAdb(error="cannot connect to daemon", server="cannot connect to daemon"),
        runner=lambda argv: "",
        system="Windows",
    )
    # No device tree from the empty runner -> adb-missing? No: adb is present and
    # the query failed, which is the server stage.
    assert result.kind in {"adb-server", USB_NO_DEVICE}


def test_collect_without_adb_installed_says_so() -> None:
    result = collect(StubAdb(present=False), runner=lambda argv: "", system="Windows")
    assert result.kind == USB_ADB_MISSING


def test_the_report_serialises_to_json_without_a_phone() -> None:
    payload = json.dumps(report(rows=[MTP_PHONE]).as_dict())
    assert "no-adb-interface" in payload
    assert "stage" in payload


def test_the_text_report_names_the_system_and_the_stage() -> None:
    text = format_report(report(rows=[MTP_PHONE]))
    assert text.startswith("PhoneBridge USB report")
    assert "stops at" in text and f"stage {STAGE_B_NO_ADB_INTERFACE}" in text
    assert "next action" in text


# --------------------------------------------------------------------------- #
# the controller's on-demand diagnosis
# --------------------------------------------------------------------------- #
def _manager(**kwargs):
    """A controller with an empty config dir, no providers and no real adb."""
    import tempfile
    from pathlib import Path

    from phonebridge.controller.manager import ConnectionManager
    from phonebridge.core.device_store import DeviceStore

    store = DeviceStore(Path(tempfile.mkdtemp()) / "phonebridge")
    return ConnectionManager(
        store,
        session_factory=lambda *a, **k: None,
        providers=[],
        **kwargs,
    )


def test_the_controller_reruns_the_report_and_publishes_it() -> None:

    async def scenario() -> tuple[str, str, str, str]:
        manager = _manager(usb_probe=lambda: [MTP_PHONE], system="Windows", adb=StubAdb())
        try:
            result = await manager.diagnose_usb()
            snap = manager.snapshot
            return result.kind, snap.usb_diagnosis, snap.usb_action, snap.usb_stage
        finally:
            await manager.aclose()

    kind, diagnosis, action, stage = asyncio.run(scenario())
    assert kind == USB_NO_ADB_INTERFACE
    assert diagnosis == "Windows sees the phone as a media device (MTP) but exposes no ADB interface - this is why the RSA prompt never appears"
    assert "File transfer" in action
    assert stage == STAGE_B_NO_ADB_INTERFACE


def test_the_controller_report_is_logged_line_by_line() -> None:
    async def scenario() -> list[str]:
        manager = _manager(usb_probe=lambda: [MTP_PHONE], system="Windows", adb=StubAdb())
        try:
            await manager.diagnose_usb()
            return list(manager.snapshot.diagnostics)
        finally:
            await manager.aclose()

    lines = asyncio.run(scenario())
    assert any("PhoneBridge USB report" in line for line in lines)
    assert any("MTP interface" in line for line in lines)
    assert any("next action" in line for line in lines)
