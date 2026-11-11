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
USB_NO_ADB_INTERFACE = "no-adb-interface"
USB_ADB_SERVER = "adb-server"
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
    USB_NO_ADB_INTERFACE,
    USB_ADB_SERVER,
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

#: Where the chain of events stops, in the order the operating system does them.
#: The letter is what the window and the CLI print, so a user can say "it stops
#: at C" and the answer is the same every time.
STAGE_A_NO_PHONE = "A"  # Windows sees no Android hardware at all
STAGE_B_NO_ADB_INTERFACE = "B"  # the phone is enumerated, its ADB interface is not
STAGE_C_DRIVER = "C"  # the interface exists, Windows cannot use it
STAGE_D_ADB_SERVER = "D"  # adb itself cannot talk to its own server
STAGE_E_UNAUTHORIZED = "E"  # adb has the device, the PC is not authorised
STAGE_F_OFFLINE = "F"  # adb has the device, it does not answer
STAGE_G_WIRELESS = "G"  # only a wireless-debugging device is attached
STAGE_H_SETTINGS = "H"  # enumerated and healthy, adb blind: settings on the phone

CommandRunner = Callable[[Sequence[str]], str]

#: ``Get-PnpDevice`` filtered in PowerShell. CSV so a comma in a friendly name
#: cannot split a row.
_PNP_COMMAND = (
    "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue "
    "| Where-Object { $_.Class -eq 'USB' -or $_.Class -eq 'AndroidUsbDeviceClass' "
    "-or $_.Class -eq 'WPD' -or $_.Class -eq 'PortableDevice' "
    "-or $_.FriendlyName -match 'Android|ADB|MTP|Portable|Xiaomi|Redmi|POCO|Pixel|Samsung|OnePlus' } "
    "| Select-Object Status,Class,FriendlyName,InstanceId,Problem,Service "
    "| ConvertTo-Csv -NoTypeInformation"
)

#: Driver services that mean the ADB interface is bound correctly. ``usbccgp`` is
#: the generic composite parent; the child interface is what adb needs.
_ADB_SERVICES = {"androidwinusb", "androidusb", "winusb"}

#: Name fragments Windows uses for the two interfaces of one phone.
_ADB_NAME_HINTS = ("adb interface", "adb", "android adb")
_MTP_NAME_HINTS = ("mtp", "portable device", "portable media", "media device",
                   "file transfer", "portable player")
_MTP_CLASSES = {"wpd", "portabledevice", "portable devices", "image"}


@dataclass(frozen=True)
class PnpDevice:
    """One row of Windows' device tree."""

    instance_id: str
    friendly_name: str = ""
    status: str = ""
    problem: int = 0
    class_name: str = ""
    #: The bound driver service (``WinUSB``, ``usbccgp``, ``MTP``...). Empty when
    #: the row came from an older query or the field was blank.
    service: str = ""

    @property
    def _haystack(self) -> str:
        return f"{self.instance_id} {self.friendly_name} {self.class_name}".lower()

    @property
    def is_adb_interface(self) -> bool:
        """The specific interface node adb binds to — not merely "an Android row"."""
        if any(hint in self.friendly_name.lower() for hint in _ADB_NAME_HINTS):
            return True
        return self.service.lower() in _ADB_SERVICES and "adb" in self._haystack

    @property
    def is_mtp(self) -> bool:
        """The media-transfer interface: the phone is enumerated, but for files.

        MTP and ADB are two interfaces of one physical phone. Seeing MTP proves
        the cable and port are fine and proves nothing about adb.
        """
        if self.class_name.strip().lower() in _MTP_CLASSES:
            return True
        return any(hint in self.friendly_name.lower() for hint in _MTP_NAME_HINTS)

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
                service=(row.get("Service") or "").strip(),
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


@dataclass(frozen=True)
class UsbReport:
    """Every stage of the USB chain, and where it stopped.

    The fields are separate on purpose: "Windows sees the phone" and "adb has a
    transport" are different facts, and the old single string conflated them.
    """

    kind: str
    headline: str
    action: str = ""
    stage: str = ""
    evidence: tuple[str, ...] = ()

    #: ``None`` means "this host's device tree was not inspected" (not Windows,
    #: or the query failed) — never "nothing is attached".
    inspected: bool = False
    system: str = ""
    phones: tuple[str, ...] = ()
    mtp: bool = False
    adb_interface: bool = False
    problems: tuple[str, ...] = ()

    adb_available: bool = False
    adb_path: str = ""
    adb_version: str = ""
    adb_server: str = "unknown"
    adb_error: str = ""
    adb_devices: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.kind == USB_READY

    @property
    def driver_ok(self) -> bool | None:
        """``None`` when there is nothing to judge, so the window can say so."""
        if not self.inspected:
            return None
        if not self.phones:
            return None
        return not self.problems

    @property
    def diagnosis(self) -> UsbDiagnosis:
        return UsbDiagnosis(self.kind, self.headline, self.action, self.evidence)

    def line(self) -> str:
        return self.diagnosis.line()

    def lines(self) -> list[str]:
        """The full report, as the CLI and the window's log print it."""
        def yes_no(value: bool | None) -> str:
            return "not inspected" if value is None else ("yes" if value else "NO")

        rows = [
            ("system", self.system or platform.system()),
            ("adb", self.adb_path or "not found"),
            ("adb version", self.adb_version or "unknown"),
            ("adb server", self.adb_server),
            ("adb devices", ", ".join(self.adb_devices) if self.adb_devices else "none"),
            ("phone in Windows", ", ".join(self.phones) if self.phones else "no"),
            ("MTP interface", yes_no(self.mtp) if self.inspected else "not inspected"),
            ("ADB interface", yes_no(self.adb_interface) if self.inspected else "not inspected"),
            ("driver status", (
                "not inspected" if not self.inspected
                else ("OK" if self.driver_ok else "; ".join(self.problems))
            )),
            ("stops at", f"stage {self.stage}" if self.stage else "-"),
        ]
        out = [f"  {name:<17}: {value}" for name, value in rows]
        out.append(f"  {'decision':<17}: {self.headline}")
        if self.action:
            out.append(f"  {'next action':<17}: {self.action}")
        if self.adb_error:
            out.append(f"  {'adb error':<17}: {self.adb_error}")
        if self.evidence:
            out.append("  evidence:")
            out.extend(f"    - {item}" for item in self.evidence)
        return out

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "stage": self.stage,
            "headline": self.headline,
            "action": self.action,
            "system": self.system,
            "inspected": self.inspected,
            "phones": list(self.phones),
            "mtp": self.mtp,
            "adb_interface": self.adb_interface,
            "driver_ok": self.driver_ok,
            "problems": list(self.problems),
            "adb_available": self.adb_available,
            "adb_path": self.adb_path,
            "adb_version": self.adb_version,
            "adb_server": self.adb_server,
            "adb_error": self.adb_error,
            "adb_devices": list(self.adb_devices),
            "evidence": list(self.evidence),
        }


#: What to do about each stage, phrased as the thing the user can act on now.
_ACTION_A = (
    "nothing Android-shaped reached Windows: use the cable the phone shipped "
    "with, a port on the PC itself rather than a hub or a monitor, unlock the "
    "phone, then plug it in and run this again"
)
_ACTION_B = (
    "Windows sees the phone but not its ADB interface, so no debugging request "
    "can reach it: on the phone open Settings > Developer options > USB "
    "debugging ON (on HyperOS also check 'USB debugging (Security settings)' if "
    "it is listed), set USB configuration to 'File transfer', then replug. The "
    "'Allow USB debugging?' prompt appears only after that interface exists"
)
_ACTION_C = (
    "the interface is there and Windows cannot drive it: in Device Manager "
    "right-click the phone's entry > Update driver, or install the phone's own "
    "USB driver. Do not accept a driver from an unknown bundle"
)
_ACTION_D = (
    "adb cannot reach its own server: in a terminal run `adb kill-server` then "
    "`adb start-server`, and press Diagnose USB again. This does not delete your "
    "adb keys, so an existing authorisation survives"
)
_ACTION_G = (
    "adb only has a wireless-debugging device. That is not a cable: plug the "
    "cable in, enable USB debugging, and confirm the RSA prompt"
)
_ACTION_H = (
    "the phone is enumerated and healthy, yet adb has no transport: on the phone "
    "open Settings > Developer options and switch USB debugging ON, set the USB "
    "configuration to 'File transfer', then replug and press Diagnose USB. If the "
    "'Allow USB debugging?' prompt never appears, the ADB interface is the "
    "blocker (stage B) and the prompt was never the problem"
)


def build_report(
    *,
    adb_devices: Sequence[AdbDevice] = (),
    pnp: Sequence[PnpDevice] | None = None,
    adb_available: bool = True,
    adb_path: str = "",
    adb_version: str = "",
    adb_server: str = "unknown",
    adb_error: str = "",
    system: str | None = None,
) -> UsbReport:
    """Turn "what adb says" and "what Windows sees" into one staged verdict.

    The order below is the order the chain breaks in. Each branch is a real PC's
    output, and each carries the one action that moves it forward.
    """
    system = system or platform.system()
    inspected = pnp is not None and system == "Windows"
    rows = list(pnp or [])
    evidence = tuple(row.describe() for row in rows)
    adb_rows = tuple(d.explain() for d in adb_devices)

    def report(kind: str, headline: str, action: str, stage: str = "", **extra: object) -> UsbReport:
        base: dict[str, object] = {
            "kind": kind,
            "headline": headline,
            "action": action,
            "stage": stage,
            "evidence": evidence,
            "inspected": inspected,
            "system": system,
            "phones": tuple(row.friendly_name or row.instance_id for row in android_rows),
            "mtp": any(row.is_mtp for row in android_rows),
            "adb_interface": any(row.is_adb_interface for row in android_rows),
            "problems": tuple(
                f"{row.friendly_name or row.instance_id}: {row.problem_help()}"
                for row in android_rows
                if not row.healthy
            ),
            "adb_available": adb_available,
            "adb_path": adb_path,
            "adb_version": adb_version,
            "adb_server": adb_server,
            "adb_error": adb_error,
            "adb_devices": adb_rows,
        }
        base.update(extra)
        return UsbReport(**base)  # type: ignore[arg-type]

    # The phone's own interfaces, as opposed to "some Android-looking row".
    android_rows = [row for row in rows if row.looks_android or row.is_mtp]

    cabled = _cable_devices(adb_devices)
    usable = [d for d in cabled if d.usable]
    if usable:
        names = ", ".join(d.display_name for d in usable)
        return report(USB_READY, f"ready over USB: {names}", "")

    blocked = [d for d in cabled if not d.usable]
    if blocked:
        first = blocked[0]
        kind = USB_UNAUTHORIZED if first.state == "unauthorized" else USB_OFFLINE
        stage = STAGE_E_UNAUTHORIZED if kind == USB_UNAUTHORIZED else STAGE_F_OFFLINE
        return report(
            kind,
            f"the cable is seen, but {first.serial} is {first.state}",
            f"{state_help(first.state)}, then press Reconnect",
            stage,
        )

    wireless = [d for d in adb_devices if d.connection == CONNECTION_WIRELESS]
    if wireless:
        return report(
            USB_WIRELESS_ONLY,
            "adb only sees a wireless-debugging device, so there is no cable session",
            _ACTION_G,
            STAGE_G_WIRELESS,
        )

    if not adb_available:
        return report(
            USB_ADB_MISSING,
            "adb is not installed, so USB cannot work at all",
            "install Android platform-tools (Android Studio > SDK Manager > SDK "
            "Tools > Android SDK Platform-Tools), or set PHONEBRIDGE_ADB",
        )

    if not inspected:
        return report(
            USB_UNKNOWN,
            "adb sees no device, and this system's device tree was not inspected",
            "plug the phone in with a data-capable cable and accept the debugging "
            "prompt; run this on Windows for the full USB report",
        )

    if not android_rows:
        return report(
            USB_NO_DEVICE,
            "Windows sees no Android device at all on USB",
            _ACTION_A,
            STAGE_A_NO_PHONE,
        )

    broken = [row for row in android_rows if not row.healthy]
    if adb_error and not broken:
        # adb never got as far as looking at a device.
        return report(
            USB_ADB_SERVER,
            f"adb cannot query its server: {adb_error}",
            _ACTION_D,
            STAGE_D_ADB_SERVER,
        )

    if broken:
        worst = broken[0]
        named = "ADB interface" if worst.is_adb_interface else "device"
        return report(
            USB_DRIVER,
            f"Windows sees the phone's {named} but cannot use it: {worst.describe()}",
            f"{worst.problem_help()}. {_ACTION_C}" if worst.problem not in _PROBLEM_HELP
            else f"{worst.problem_help()}; {_ACTION_C}",
            STAGE_C_DRIVER,
        )

    has_adb = any(row.is_adb_interface for row in android_rows)
    has_mtp = any(row.is_mtp for row in android_rows)
    if has_mtp and not has_adb:
        return report(
            USB_NO_ADB_INTERFACE,
            "Windows sees the phone as a media device (MTP) but exposes no ADB "
            "interface - this is why the RSA prompt never appears",
            _ACTION_B,
            STAGE_B_NO_ADB_INTERFACE,
        )

    if not has_adb and not has_mtp:
        # Enumerated but neither interface is exposed: the phone is charging, or
        # the USB mode is "no data transfer".
        return report(
            USB_DEBUGGING_OFF,
            "Windows sees the phone, but adb does not - debugging is not enabled "
            "or not authorised",
            _ACTION_H,
            STAGE_H_SETTINGS,
        )

    return report(
        USB_DEBUGGING_OFF,
        "the ADB interface is present and healthy, but adb has no transport for it",
        _ACTION_H,
        STAGE_H_SETTINGS,
    )


def diagnose(
    *,
    adb_devices: Sequence[AdbDevice] = (),
    pnp: Sequence[PnpDevice] | None = None,
    adb_available: bool = True,
    system: str | None = None,
) -> UsbDiagnosis:
    """The window's two rows: one sentence and one action.

    Kept as a thin wrapper so the GUI never has to know how many stages exist.
    """
    return build_report(
        adb_devices=adb_devices,
        pnp=pnp,
        adb_available=adb_available,
        system=system,
    ).diagnosis


def format_report(report: UsbReport) -> str:
    """The whole staged report as text, for the CLI and the diagnostics pane."""
    header = "PhoneBridge USB report"
    return "\n".join([header, "=" * len(header), *report.lines()])


def collect(
    adb: object | None = None,
    *,
    runner: CommandRunner | None = None,
    system: str | None = None,
) -> UsbReport:
    """Run every stage for real: adb presence, server, devices, Windows' tree.

    ``adb`` is an :class:`~phonebridge.controller.adb.Adb`, injected so tests can
    state the machine instead of depending on it. Nothing here kills a server,
    deletes a key, installs a driver or changes a setting.
    """
    system = system or platform.system()
    pnp = query_pnp(runner, system=system) if system == "Windows" else None

    available = False
    path = ""
    version = ""
    server = "not checked"
    error = ""
    devices: list[AdbDevice] = []
    if adb is not None:
        probe = getattr(adb, "probe_devices", None)
        available = bool(getattr(adb, "available", lambda: False)())
        path = str(getattr(adb, "path", "") or "")
        if available:
            version = str(getattr(adb, "version", lambda: "")() or "")
            if probe is not None:
                devices, error, server = probe()
            else:  # pragma: no cover - any real Adb has probe_devices
                server = "unknown"

    return build_report(
        adb_devices=devices,
        pnp=pnp,
        adb_available=available if adb is not None else False,
        adb_path=path,
        adb_version=version,
        adb_server=server,
        adb_error=error,
        system=system,
    )
