"""Why is the cable not working? Windows can answer that; adb alone cannot.

``adb devices -l`` has a blind spot: when the phone does not appear at all it says
nothing about *why*. "No device" is the same output for a charge-only cable, a
dead port, a missing USB driver, USB debugging switched off, and a phone that was
never plugged in — and the user has tried "a few cable attempts" by the time they
read the message.

So this module asks Windows what it can see (`Get-PnpDevice`) and combines that
with what adb can see:

===========================  ===================================================
Windows enumerates           adb sees                   Conclusion
===========================  ===================================================
nothing Android-shaped       nothing                    cable/port/phone: nothing
                                                        was ever attached
device with a problem code   nothing                    driver problem; the code
                                                        says which (28, 43, 10)
device, healthy              nothing                    enumerated but not debugged:
                                                        USB debugging off, "no data
                                                        transfer" mode, or the RSA
                                                        prompt never accepted
device, healthy              unauthorized               accept the prompt on the
                                                        phone, then replug
device, healthy              offline                    replug / ``adb kill-server``
anything                     a cable ``device``         ready
anything                     only wireless/emulator     no cable session (and the
                                                        transport must not say USB)
===========================  ===================================================

Two honesty rules:

* The PnP query is **Windows-only**. On another system the diagnosis reports only
  what adb said rather than inventing a Windows conclusion.
* Nothing here installs a driver, changes a device, or claims USB works. It names
  the obstacle and the single next action.
"""

from __future__ import annotations

import csv
import io
import logging
import platform
import subprocess  # noqa: S404 - fixed argv, never a shell string
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .adb import CONNECTION_USB, CONNECTION_WIRELESS, AdbDevice, state_help

log = logging.getLogger(__name__)

#: Kinds of answer. The window shows the headline; tests assert the kind.
USB_READY = "ready"
USB_UNAUTHORIZED = "unauthorized"
USB_OFFLINE = "offline"
USB_DRIVER = "driver"
USB_DEBUGGING_OFF = "debugging-off"
USB_NO_DEVICE = "no-device"
USB_WIRELESS_ONLY = "wireless-only"
USB_ADB_MISSING = "adb-missing"
USB_UNKNOWN = "unknown"

#: Every kind :func:`diagnose` can return, so callers and tests can enumerate them.
USB_KINDS = (
    USB_READY,
    USB_UNAUTHORIZED,
    USB_OFFLINE,
    USB_DRIVER,
    USB_DEBUGGING_OFF,
    USB_NO_DEVICE,
    USB_WIRELESS_ONLY,
    USB_ADB_MISSING,
    USB_UNKNOWN,
)

#: Devices that could plausibly be the phone. Instance ids carry the vendor id;
#: friendly names carry the marketing name.
_ANDROID_HINTS = (
    "android",
    "adb",
    "xiaomi",
    "redmi",
    "poco",
    "pixel",
    "google",
    "samsung",
    "oneplus",
    "oppo",
    "vivo",
    "realme",
    "motorola",
    "nothing phone",
)

#: Vendor ids seen on phones (``USB\VID_2717...``). Kept short on purpose: an
#: unknown vendor still matches by name, and guessing is worse than reporting
#: what was found.
_ANDROID_VIDS = {
    "2717",  # Xiaomi / Redmi / POCO
    "18d1",  # Google
    "04e8",  # Samsung
    "22b8",  # Motorola
    "12d1",  # Huawei
    "2a70",  # OnePlus / OPPO / realme
    "0e8d",  # MediaTek (some OEMs)
}

_PROBLEM_HELP = {
    10: "the device cannot start (Code 10) - usually a charge-only cable, a dead "
        "port or a stale driver: try another cable and another port",
    28: "the driver is not installed (Code 28) - install the Android/OEM USB driver",
    43: "Windows stopped the device (Code 43) - the device reported a failure: "
        "another cable/port is the fastest test",
    52: "the driver's signature could not be verified (Code 52)",
}

CommandRunner = Callable[[Sequence[str]], str]

#: ``Get-PnpDevice`` filtered in PowerShell. CSV so a comma in a friendly name
#: cannot split a row.
_PNP_COMMAND = (
    "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue "
    "| Where-Object { $_.Class -eq 'USB' -or $_.Class -eq 'AndroidUsbDeviceClass' "
    "-or $_.FriendlyName -match 'Android|ADB|Xiaomi|Redmi|POCO|Pixel|Samsung|OnePlus' } "
    "| Select-Object Status,Class,FriendlyName,InstanceId,Problem "
    "| ConvertTo-Csv -NoTypeInformation"
)


@dataclass(frozen=True)
class PnpDevice:
    """One row of Windows' device tree."""

    instance_id: str
    friendly_name: str = ""
    status: str = ""
    problem: int = 0
    class_name: str = ""

    @property
    def looks_android(self) -> bool:
        haystack = f"{self.instance_id} {self.friendly_name} {self.class_name}".lower()
        if "androidusbdeviceclass" in haystack:
            return True
        for vid in _ANDROID_VIDS:
            if f"vid_{vid}" in haystack:
                return True
        return any(hint in haystack for hint in _ANDROID_HINTS)

    @property
    def healthy(self) -> bool:
        return self.problem == 0 and self.status.lower() in {"ok", "started", ""}

    def describe(self) -> str:
        name = self.friendly_name or self.instance_id
        if self.healthy:
            return f"{name} (Windows: OK)"
        code = f"Code {self.problem}" if self.problem else self.status or "unknown"
        return f"{name} (Windows: {code})"

    def problem_help(self) -> str:
        return _PROBLEM_HELP.get(
            self.problem, f"Windows reports problem code {self.problem} for this device"
        )


@dataclass(frozen=True)
class UsbDiagnosis:
    """The obstacle, and the one action that removes it."""

    kind: str
    headline: str
    action: str = ""
    evidence: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.kind == USB_READY

    def line(self) -> str:
        return f"{self.headline} - {self.action}" if self.action else self.headline


# --------------------------------------------------------------------------- #
# Windows device tree
# --------------------------------------------------------------------------- #
def parse_pnp_csv(text: str) -> list[PnpDevice]:
    """Parse ``ConvertTo-Csv`` output. Anything unparseable is skipped, not fatal."""
    rows = list(csv.DictReader(io.StringIO(text.strip()))) if text.strip() else []
    devices: list[PnpDevice] = []
    for row in rows:
        instance = (row.get("InstanceId") or "").strip()
        if not instance:
            continue
        problem_text = (row.get("Problem") or "").strip()
        try:
            problem = int(problem_text) if problem_text else 0
        except ValueError:
            problem = 0
        devices.append(
            PnpDevice(
                instance_id=instance,
                friendly_name=(row.get("FriendlyName") or "").strip(),
                status=(row.get("Status") or "").strip(),
                problem=problem,
                class_name=(row.get("Class") or "").strip(),
            )
        )
    return devices


def _run_powershell(argv: Sequence[str]) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            list(argv), capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("PowerShell query failed: %s", exc)
        return ""
    return done.stdout or ""


def query_pnp(runner: CommandRunner | None = None, *, system: str | None = None) -> list[PnpDevice]:
    """Windows' view of the USB devices present right now (empty elsewhere).

    ``None`` and ``[]`` mean different things to :func:`diagnose`: on a non-Windows
    host the answer is "we did not look", not "nothing is attached".
    """
    if (system or platform.system()) != "Windows":
        return []
    run = runner or _run_powershell
    text = run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PNP_COMMAND]
    )
    return parse_pnp_csv(text)


# --------------------------------------------------------------------------- #
# the decision
# --------------------------------------------------------------------------- #
def _cable_devices(devices: Sequence[AdbDevice]) -> list[AdbDevice]:
    return [d for d in devices if d.connection == CONNECTION_USB]


def diagnose(
    *,
    adb_devices: Sequence[AdbDevice] = (),
    pnp: Sequence[PnpDevice] | None = None,
    adb_available: bool = True,
    system: str | None = None,
) -> UsbDiagnosis:
    """Explain why there is (or is not) a USB session, in one actionable sentence.

    ``pnp`` is only read on Windows: ``None`` and an empty list both mean "this
    platform's device tree was not inspected", so Linux and macOS get an honest
    "not inspected" instead of a Windows conclusion they cannot support.
    """
    inspected = pnp is not None and (system or platform.system()) == "Windows"
    cabled = _cable_devices(adb_devices)
    usable = [d for d in cabled if d.usable]
    if usable:
        names = ", ".join(d.display_name for d in usable)
        return UsbDiagnosis(USB_READY, f"ready over USB: {names}")

    blocked = [d for d in cabled if not d.usable]
    if blocked:
        first = blocked[0]
        hint = state_help(first.state)
        kind = USB_UNAUTHORIZED if first.state == "unauthorized" else USB_OFFLINE
        return UsbDiagnosis(
            kind,
            f"the cable is seen, but {first.serial} is {first.state}",
            f"{hint}, then press Reconnect",
            tuple(d.explain() for d in blocked),
        )

    wireless = [d for d in adb_devices if d.connection == CONNECTION_WIRELESS]
    if wireless:
        return UsbDiagnosis(
            USB_WIRELESS_ONLY,
            "adb only sees a wireless-debugging device, so there is no cable session",
            "plug the cable in (and accept the debugging prompt) for the USB transport",
            tuple(d.explain() for d in wireless),
        )

    if not adb_available:
        return UsbDiagnosis(
            USB_ADB_MISSING,
            "adb is not installed, so USB cannot work at all",
            "install Android platform-tools (Android Studio > SDK Manager > SDK Tools)",
        )

    if not inspected:
        return UsbDiagnosis(
            USB_UNKNOWN,
            "adb sees no device, and this system's device tree was not inspected",
            "plug the phone in with a data-capable cable and accept the debugging prompt",
        )

    android_rows = [row for row in (pnp or []) if row.looks_android]
    evidence = tuple(row.describe() for row in android_rows)

    if not android_rows:
        return UsbDiagnosis(
            USB_NO_DEVICE,
            "Windows sees no Android device at all on USB",
            "this is the cable, the port, or the phone: use the cable that came with "
            "the phone, a port on the PC rather than a hub, and unlock the phone "
            "before plugging it in",
            evidence,
        )

    broken = [row for row in android_rows if not row.healthy]
    if broken:
        worst = broken[0]
        return UsbDiagnosis(
            USB_DRIVER,
            f"Windows sees the phone but cannot use it: {worst.describe()}",
            worst.problem_help(),
            evidence,
        )

    return UsbDiagnosis(
        USB_DEBUGGING_OFF,
        "Windows sees the phone, but adb does not - debugging is not enabled or "
        "not authorised",
        "on the phone: Settings > About phone > tap Build number 7 times, then "
        "Developer options > USB debugging ON; set the USB mode to 'File transfer', "
        "replug, and accept the 'Allow USB debugging?' prompt",
        evidence,
    )
