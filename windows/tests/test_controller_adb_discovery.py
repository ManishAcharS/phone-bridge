"""Finding adb anywhere, and never calling a network bridge "USB".

The user's PC reports ``adb not on PATH (USB unavailable)`` while a perfectly good
``adb.exe`` sits in ``%LOCALAPPDATA%\\Android\\Sdk\\platform-tools``. Android Studio
does not put it on ``PATH``, so a PATH-only search is a bug, not a limitation.

The second half of the file is the classification table: a wireless-debugging
bridge must never be offered as the USB transport, because the transport row is
the one place a user checks what is actually plugged in.

Paths are composed with ``os.path.join`` here for the same reason the module
composes them that way: these tests also run on Linux, where the separator is
different and the expectation has to follow the code, not Windows' backslashes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from phonebridge.controller.adb import (
    CONNECTION_EMULATOR,
    CONNECTION_USB,
    CONNECTION_WIRELESS,
    AdbDevice,
    AdbLocation,
    candidate_locations,
    find_adb,
    is_wireless_serial,
    parse_devices,
)

LOCAL = os.path.join("C:", "Users", "2026", "AppData", "Local")
PROFILE = os.path.join("C:", "Users", "2026")

WINDOWS_ENV = {
    "LOCALAPPDATA": LOCAL,
    "USERPROFILE": PROFILE,
    "ProgramFiles": os.path.join("C:", "Program Files"),
}


def sdk_adb(base: str) -> str:
    return os.path.join(base, "platform-tools", "adb.exe")


STUDIO_DEFAULT = sdk_adb(os.path.join(LOCAL, "Android", "Sdk"))


# --------------------------------------------------------------------------- #
# where adb is allowed to live
# --------------------------------------------------------------------------- #
def test_no_environment_variables_still_offers_the_standard_sdk_folder() -> None:
    """A PC with no ANDROID_HOME must still find the Android Studio default."""
    paths = [path for path, _ in candidate_locations(WINDOWS_ENV, platform="nt")]
    assert STUDIO_DEFAULT in paths


def test_the_previously_used_path_is_one_of_many_not_the_only_one() -> None:
    paths = [path for path, _ in candidate_locations(WINDOWS_ENV, platform="nt")]
    # PATH is checked first by find_adb, so these four are the on-disk fallbacks.
    assert len(paths) >= 4
    assert len(set(paths)) == len(paths)


def test_every_candidate_reports_where_it_came_from() -> None:
    for path, source in candidate_locations(WINDOWS_ENV, platform="nt"):
        assert path
        assert source


def test_android_home_is_preferred_over_a_stray_sdk() -> None:
    home = os.path.join("D:", "Android", "Sdk")
    env = dict(WINDOWS_ENV, ANDROID_HOME=home)
    paths = [path for path, _ in candidate_locations(env, platform="nt")]
    assert paths[0] == sdk_adb(home)


def test_explicit_override_wins_over_everything() -> None:
    override = os.path.join("D:", "tools", "adb.exe")
    home = os.path.join("C:", "sdk")
    env = dict(WINDOWS_ENV, PHONEBRIDGE_ADB=override, ANDROID_HOME=home)
    found = find_adb(
        env,
        which=lambda _name: sdk_adb(home),
        exists=lambda _path: True,
        platform="nt",
    )
    assert found == AdbLocation(override, "PHONEBRIDGE_ADB")


def test_path_is_used_when_adb_is_actually_on_it() -> None:
    on_path = os.path.join("C:", "adb", "adb.exe")
    found = find_adb(
        WINDOWS_ENV,
        which=lambda name: on_path if name == "adb.exe" else None,
        exists=lambda _path: True,
        platform="nt",
    )
    assert found is not None
    assert found.source == "PATH"
    assert found.path == on_path


def test_local_appdata_sdk_is_found_without_path() -> None:
    found = find_adb(
        WINDOWS_ENV,
        which=lambda _name: None,
        exists=lambda path: path == STUDIO_DEFAULT,
        platform="nt",
    )
    assert found is not None
    assert found.path == STUDIO_DEFAULT
    assert found.source == "LOCALAPPDATA"


def test_sdk_root_variable_is_found() -> None:
    root = os.path.join("E:", "Android", "Sdk")
    target = sdk_adb(root)
    found = find_adb(
        {"ANDROID_SDK_ROOT": root},
        which=lambda _name: None,
        exists=lambda path: path == target,
        platform="nt",
    )
    assert found is not None
    assert found.path == target


def test_program_files_is_searched_last() -> None:
    sources = [source for _, source in candidate_locations(WINDOWS_ENV, platform="nt")]
    assert sources[-1] == "Program Files"


def test_missing_adb_is_none_not_an_exception() -> None:
    assert (
        find_adb({}, which=lambda _name: None, exists=lambda _path: False, platform="nt")
        is None
    )


def test_posix_locations_are_searched_too() -> None:
    paths = [path for path, _ in candidate_locations({}, platform="posix")]
    assert any(path.endswith(os.path.join("platform-tools", "adb")) for path in paths)
    assert not any("adb.exe" in path for path in paths)


def test_a_directory_where_a_file_should_be_is_not_adb() -> None:
    found = find_adb(
        WINDOWS_ENV,
        which=lambda _name: sdk_adb(os.path.join(LOCAL, "Android", "Sdk")),
        exists=lambda _path: False,
        platform="nt",
    )
    assert found is None


# --------------------------------------------------------------------------- #
# classification: cable vs bridge vs emulator
# --------------------------------------------------------------------------- #
DEVICES = """
List of devices attached
R58M12345              device usb:1-2 product:redmi model:Redmi_Note_14_Pro transport_id:1
adb-3A21F9XYZ._adb-tls-connect._tcp device product:panther model:Pixel_7 transport_id:2
192.168.1.9:5555       device product:panther model:Pixel_7 transport_id:3
emulator-5554          device product:sdk_gphone64 model:sdk_phone transport_id:4
ABC123                 unauthorized transport_id:5
"""


def classify() -> dict[str, AdbDevice]:
    return {device.serial: device for device in parse_devices(DEVICES)}


def test_a_cable_device_is_usb() -> None:
    assert classify()["R58M12345"].connection == CONNECTION_USB


def test_a_wireless_debugging_bridge_is_never_usb() -> None:
    devices = classify()
    assert devices["adb-3A21F9XYZ._adb-tls-connect._tcp"].connection == CONNECTION_WIRELESS
    assert devices["192.168.1.9:5555"].connection == CONNECTION_WIRELESS


def test_an_emulator_is_not_a_phone_on_a_cable() -> None:
    assert classify()["emulator-5554"].connection == CONNECTION_EMULATOR


def test_only_the_cabled_device_is_usable_over_usb() -> None:
    usable_usb = [d.serial for d in parse_devices(DEVICES) if d.is_usb and d.usable]
    assert usable_usb == ["R58M12345"]


def test_an_unauthorised_cable_device_is_still_classified_as_usb() -> None:
    """Classification is about the wire; usability is a separate question."""
    device = classify()["ABC123"]
    assert device.connection == CONNECTION_USB
    assert not device.usable


def test_mdns_pairing_serials_count_as_wireless() -> None:
    assert is_wireless_serial("adb-1234._adb-tls-pairing._tcp")
    assert is_wireless_serial("adb-1234._adb-tls-connect._tcp")
    assert not is_wireless_serial("R58M12345")


@pytest.mark.parametrize(
    "serial",
    ["192.168.43.1:37000", "10.63.164.24:8765", "adb-XYZ._adb-tls-connect._tcp"],
)
def test_network_serials_are_wireless(serial: str) -> None:
    assert AdbDevice(serial=serial, state="device").connection == CONNECTION_WIRELESS


@pytest.mark.parametrize("serial", ["R58M12345", "0123456789ABCDEF", "HT7A1A01234"])
def test_hardware_serials_are_usb(serial: str) -> None:
    assert AdbDevice(serial=serial, state="device").connection == CONNECTION_USB


# --------------------------------------------------------------------------- #
# explaining the states separately
# --------------------------------------------------------------------------- #
def test_an_unauthorized_device_explains_the_prompt() -> None:
    device = AdbDevice(serial="ABC123", state="unauthorized")
    assert "accept the USB debugging prompt" in device.explain()


def test_offline_and_no_permissions_say_different_things() -> None:
    offline = AdbDevice(serial="ABC123", state="offline").explain()
    permissions = AdbDevice(serial="ABC123", state="no permissions").explain()
    assert offline != permissions
    assert "replug" in offline
    assert "udev" in permissions


def test_a_ready_device_says_which_transport_it_uses() -> None:
    ready = AdbDevice(serial="R58M12345", state="device", model="Redmi_Note_14_Pro")
    assert "ready over USB" in ready.explain()
    assert "Redmi Note 14 Pro" in ready.display_name


def test_an_unknown_state_still_explains_itself() -> None:
    assert "adb devices" in AdbDevice(serial="X", state="weird").explain()


# --------------------------------------------------------------------------- #
# the module must import on a machine with no SDK at all
# --------------------------------------------------------------------------- #
def test_discovery_runs_without_adb_installed() -> None:
    # Every source is injected: on a Windows PC with an SDK on disk, the default
    # ``exists`` would find a real adb.exe and this assertion would depend on the
    # machine rather than on the code.
    found = find_adb(
        env={}, which=lambda _n: None, exists=lambda _p: False, platform="nt"
    )
    assert found is None
    assert Path(__file__).exists()
