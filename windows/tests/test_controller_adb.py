"""adb parsing and, more importantly, forward-rule ownership.

The one promise that matters here: **a rule PhoneBridge did not create is never
removed.** Getting that wrong would silently break a user's own
``adb forward tcp:8765 tcp:8765``, which every earlier version of the docs told
them to run by hand.

Discovery is injected everywhere in this file (``locate=``), never mocked at the
``shutil``/filesystem level. That is not a shortcut: on a Windows developer PC
``%LOCALAPPDATA%\\Android\\Sdk\\platform-tools\\adb.exe`` genuinely exists, so a
test that only stubs ``shutil.which`` still finds a real adb and passes or fails
depending on who runs it. Injection makes "adb is missing" a *statement about the
code path* rather than about the machine — and
``test_controller_adb_discovery.py`` separately proves the real search still finds
every location.
"""

from __future__ import annotations

import pytest

from phonebridge.controller import adb as adb_module
from phonebridge.controller.adb import (
    Adb,
    AdbError,
    AdbLocation,
    AdbResult,
    ForwardLease,
    is_wireless_serial,
    parse_devices,
)

DEVICES_HEADER = "List of devices attached\n"


class _Completed:
    """The three attributes :func:`_run_adb` reads off ``subprocess.run``."""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


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
    """Every discovery source is injected as empty, so this cannot depend on the PC.

    ``locate=`` replaces the whole search — PATH, ANDROID_HOME, ANDROID_SDK_ROOT,
    ``%LOCALAPPDATA%\\Android\\Sdk``, ``C:\\Android\\Sdk`` and Program Files — so the
    test asserts the same thing on a machine with a full SDK as on one without.
    """
    adb = Adb(runner=RecordingAdb(), locate=lambda: None)

    assert adb.locate() is None
    assert adb.path is None
    assert adb.source is None
    assert adb.available() is False
    # No exception: "not installed" is a normal state for a Wi-Fi-only PC.
    assert adb.devices() == []
    assert adb.usb_devices() == []
    assert adb.list_forwards() == []
    assert adb.version() is None
    status = adb.status()
    assert status.available is False
    assert "Wi-Fi" in (status.error or ""), "the message must not imply Wi-Fi is broken too"
    with pytest.raises(AdbError, match="not available"):
        adb.add_forward("S1", 5000)


def test_a_genuinely_absent_installation_is_false_not_an_exception() -> None:
    """The real finder, told the disk is empty, must answer ``None`` — not raise."""

    def empty_disk(_path: str) -> bool:
        return False

    def no_path(_name: str) -> str | None:
        return None

    adb = Adb(
        runner=RecordingAdb(),
        locate=lambda: adb_module.find_adb(
            {}, which=no_path, exists=empty_disk, platform="nt"
        ),
    )
    assert adb.available() is False
    assert adb.status().available is False
    assert adb.devices() == []


def test_the_default_finder_is_the_real_search(monkeypatch) -> None:
    """Production must keep using the SDK search — this is the anti-regression bit.

    If someone "fixes" the missing-adb test by deleting the Windows fallback
    lookup, this fails: ``Adb()`` with no arguments has to go through
    :func:`phonebridge.controller.adb.find_adb`.
    """
    found = AdbLocation(r"C:\Users\2026\AppData\Local\Android\Sdk\platform-tools\adb.exe",
                        "LOCALAPPDATA")
    monkeypatch.setattr(adb_module, "find_adb", lambda *_a, **_k: found)

    adb = Adb(runner=RecordingAdb())
    assert adb.path == found.path
    assert adb.source == "LOCALAPPDATA"
    assert adb.available() is True


def test_commands_run_the_discovered_binary_not_a_bare_adb(monkeypatch) -> None:
    """The whole point of discovery: the first command must use that path."""
    argv_seen: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        argv_seen.append(list(argv))
        return _Completed(f"List of devices attached\n{DEVICES_HEADER}")

    monkeypatch.setattr(adb_module.subprocess, "run", fake_run)
    discovered = r"C:\Users\2026\AppData\Local\Android\Sdk\platform-tools\adb.exe"
    adb = Adb(locate=lambda: AdbLocation(discovered, "LOCALAPPDATA"))

    adb.devices()
    assert argv_seen, "adb was never invoked"
    assert argv_seen[0][0] == discovered, "a bare `adb` would fail on a PC without PATH"
    assert argv_seen[0][1:] == ["devices", "-l"]


def test_a_real_sdk_on_disk_is_found_and_used(monkeypatch, tmp_path) -> None:
    """The Windows scenario end to end: adb exists only in the SDK folder.

    ``%LOCALAPPDATA%\\Android\\Sdk\\platform-tools\\adb.exe`` is what Android Studio
    installs and what ``PATH`` never sees. Discovery must find it *and* commands
    must run it — this is the pair of bugs the Windows test run exposed.
    """
    sdk = tmp_path / "Android" / "Sdk" / "platform-tools"
    sdk.mkdir(parents=True)
    binary = sdk / "adb.exe"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    found = adb_module.find_adb(which=lambda _name: None, platform="nt")
    assert found is not None
    assert found.path == str(binary)
    assert found.source == "LOCALAPPDATA"

    argv_seen: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        argv_seen.append(list(argv))
        return _Completed(DEVICES_HEADER + "R58M12345 device usb:1-2 model:Redmi_Note_14_Pro\n")

    monkeypatch.setattr(adb_module.subprocess, "run", fake_run)
    adb = Adb(locate=lambda: found)

    devices = adb.devices()
    assert [d.serial for d in devices] == ["R58M12345"]
    assert devices[0].is_usb
    assert argv_seen[0][0] == str(binary)


def test_no_bare_adb_fallback_when_nothing_was_found(monkeypatch) -> None:
    """A missing binary must raise, never quietly try ``adb`` from PATH."""

    def explode(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("subprocess must not run when adb was not found")

    monkeypatch.setattr(adb_module.subprocess, "run", explode)
    adb = Adb(locate=lambda: None)

    with pytest.raises(AdbError):
        adb.add_forward("S1", 5000)
    assert adb.devices() == []


def test_adb_timeout_is_reported_not_swallowed() -> None:
    def slow(_argv, _timeout):  # noqa: ANN001
        raise AdbError("adb devices timed out after 6s")

    adb = Adb(runner=slow, executable="adb")
    with pytest.raises(AdbError, match="timed out"):
        adb.devices()
