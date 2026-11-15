"""The adb forward a USB re-plug deletes, and the launcher that re-creates it.

`adb forward tcp:8765 tcp:8765` lives on the *transport*: unplug the cable and it
is gone. The PC already reconnects forever with backoff, but each attempt dialled a
local port nothing was listening on, so "the connection survives a re-plug" was
true of the socket code and false in practice.

Two pieces are pinned here:

* :mod:`phonebridge.live.usb` — a forward that is re-created before every attempt,
  never raises, and does nothing for a Wi-Fi target;
* ``scripts/start-live.ps1`` + ``.bat`` — the one-command morning, including the
  forward, the three handwritten-install checks and the phone.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ps1_lint import odd_quote_lines

from phonebridge.live.usb import ensure_forward, is_loopback

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
PS1 = SCRIPTS / "start-live.ps1"
COMMAND = SCRIPTS / "python-command.ps1"
PROBES = SCRIPTS / "python-probes.ps1"
BAT = SCRIPTS / "start-live.bat"
TARGET = REPO / "windows" / "phonebridge" / "cli" / "target.py"
README = REPO / "README.md"


class FakeRun:
    """A subprocess.run stand-in that records argv and returns a chosen result."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.commands: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.commands.append(list(argv))
        return subprocess.CompletedProcess(argv, self.returncode, stdout="", stderr=self.stderr)


def adb_present(name: str) -> str | None:
    return "C:/platform-tools/adb.exe" if name == "adb" else None


# --------------------------------------------------------------------------- #
# the forwarder
# --------------------------------------------------------------------------- #
def test_a_loopback_host_is_the_usb_path_and_a_real_ip_is_not() -> None:
    for host in ("127.0.0.1", "localhost", "::1", " 127.0.0.1 "):
        assert is_loopback(host), host
    for host in ("192.168.43.1", "10.0.0.5", "phone.local"):
        assert not is_loopback(host), host


def test_the_forward_is_exactly_the_command_the_readme_documents() -> None:
    run = FakeRun()
    ok, why = ensure_forward(8765, run=run, which=adb_present)
    assert ok, why
    assert run.commands == [["adb", "forward", "tcp:8765", "tcp:8765"]]


def test_a_machine_without_adb_says_so_instead_of_raising() -> None:
    ok, why = ensure_forward(8765, run=FakeRun(), which=lambda name: None)
    assert ok is False
    assert "adb is not on PATH" in why


def test_an_adb_failure_returns_the_reason() -> None:
    ok, why = ensure_forward(
        8765, run=FakeRun(returncode=1, stderr="error: no devices/emulators found"), which=adb_present
    )
    assert ok is False
    assert "no devices/emulators found" in why


def test_an_adb_that_hangs_or_explodes_is_reported_not_raised() -> None:
    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 10.0)

    ok, why = ensure_forward(8765, run=boom, which=adb_present)
    assert ok is False
    assert "adb forward failed" in why


def test_a_nonsense_port_is_refused_before_anything_runs() -> None:
    run = FakeRun()
    ok, why = ensure_forward(0, run=run, which=adb_present)
    assert ok is False
    assert run.commands == []
    assert "nonsense port" in why


def test_the_reconnect_path_re_creates_the_forward_first() -> None:
    """The whole point: a new session over USB starts with a live forward."""
    text = TARGET.read_text(encoding="utf-8")
    start = text.index("async def connect()")
    body = text[start : text.index("return connect", start)]
    assert "is_loopback(target.host)" in body
    assert "ensure_forward(target.port)" in body
    # It runs before the session is built, not after a failed handshake.
    assert body.index("ensure_forward") < body.index("BridgeSession(")


# --------------------------------------------------------------------------- #
# the launcher
# --------------------------------------------------------------------------- #
def ps1() -> str:
    return PS1.read_text(encoding="utf-8-sig")


def code_only(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_the_launcher_and_its_batch_wrapper_exist() -> None:
    assert PS1.is_file(), "scripts/start-live.ps1 is the daily-use entry point"
    assert BAT.is_file(), "the .bat is what people actually double-click"


def test_the_bat_wrapper_calls_the_ps1_and_passes_arguments_through() -> None:
    text = BAT.read_text(encoding="utf-8-sig")
    assert "start-live.ps1" in text
    assert "%*" in text, "arguments must reach the script"
    assert "-ExecutionPolicy Bypass" in text, "an unsigned local script must be runnable"
    assert "NoProfile" in text
    assert "%ERRORLEVEL%" in text, "the exit code must survive the wrapper"


def test_the_launcher_delegates_usb_forward_and_autostart_to_daily_mode() -> None:
    text = code_only(ps1())
    assert "'--daily'" in text
    assert "'--route', $Transport" in text
    assert "'--port', \"$Port\"" in text
    launch = (REPO / "windows" / "phonebridge" / "live" / "launch.py").read_text(encoding="utf-8")
    assert "add_forward" in launch and "am" in launch and "autostart" in launch
    assert "ping" in launch, "am start is not proof; the bridge must answer"


def test_the_launcher_checks_what_must_be_installed_by_hand() -> None:
    text = ps1()
    assert "OBS Studio" in text and "obsproject.com" in text
    assert "VB-CABLE" in text and "vb-audio.com" in text
    assert "OBS Virtual Camera" in text, "say the device name people must select"
    assert "CABLE Output" in text


def test_the_launcher_checks_the_python_packages_and_the_phone() -> None:
    text = code_only(ps1() + "\n" + PROBES.read_text(encoding="utf-8-sig"))
    assert "pyvirtualcam" in text and "sounddevice" in text and "pyaudiowatch" not in text
    assert "pyaudiowpatch" in text
    assert "'--daily'" in text, "daily mode waits for an authenticated ping before streaming"
    assert "'--host', $HostName" in text, "manual fallback must reach the client"


def test_the_launcher_starts_all_three_paths_on_one_session() -> None:
    text = code_only(ps1())
    assert "'--camera', '--mic', '--speaker'" in text
    assert "'--profile', $Profile" in text
    assert "'--fps-mode', $FpsMode" in text


def test_wifi_and_hotspot_use_discovery_with_hostname_fallback() -> None:
    text = ps1()
    assert "$HostName = ''" in text, "empty means auto discovery, not a stale hardcoded IP"
    assert "$Transport = 'auto'" in text
    code = code_only(text)
    assert "'--route', $Transport" in code
    assert "'--host', $HostName" in code
    assert "'--discovery-timeout'" in code


def test_the_launcher_says_the_bridge_reconnects_by_itself() -> None:
    """A launcher that hides the reconnect story invites a restart it does not need."""
    text = ps1()
    assert "reconnects on its own" in text
    assert "USB" in text and "rediscover" in text


def test_skip_checks_is_offered_for_the_case_where_the_checks_are_wrong() -> None:
    text = ps1()
    assert "[switch]$SkipChecks" in text
    assert "-SkipChecks" in text.split("param(", 1)[1]


def test_the_launcher_never_installs_anything_or_needs_elevation() -> None:
    text = code_only(ps1())
    lower = text.lower()
    for line in text.splitlines():
        if "pip install" in line.lower():
            assert "Write-Hint" in line, "install commands are printed fixes, not executed"
    assert "start-process" not in lower
    assert "runas" not in lower
    assert "set-executionpolicy" not in lower


def test_the_launcher_documents_its_exit_codes() -> None:
    text = ps1()
    assert "Exit codes" in text
    assert re.search(r"\b130\b", text), "Ctrl+C is a clean stop, not a failure"


def test_the_launcher_is_windows_formatted() -> None:
    """PowerShell 5.1 reads either, but the repo's other scripts are CRLF."""
    raw = PS1.read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), "mixed line endings"


def test_the_launcher_braces_balance() -> None:
    masked = re.sub(r"<#.*?#>", "", ps1(), flags=re.S)
    assert masked.count("{") == masked.count("}")
    assert masked.count("(") == masked.count(")")


def test_the_readme_has_a_daily_use_section_naming_the_launcher() -> None:
    text = README.read_text(encoding="utf-8")
    assert "Daily use" in text
    section = text[text.index("Daily use") :]
    section = section[: section.index("\n---")]
    assert "start-live.bat" in section
    assert "OBS Virtual Camera" in section
    assert "CABLE Output" in section
    assert "--host" in section or "-HostName" in section


# --------------------------------------------------------------------------- #
# the crash: "Cannot bind argument to parameter 'Path' because it is null"
# --------------------------------------------------------------------------- #
def test_the_launcher_never_derives_its_own_path_from_a_function() -> None:
    """`$MyInvocation.MyCommand.Path` is empty inside a function — that was the crash.

    The launcher asked a helper where it lived; a function has no script path, so
    the value was ``$null``, ``Split-Path -Parent $null`` refused to bind, and the
    script died at ``==> locating Python`` before doing anything at all.
    """
    text = code_only(ps1())

    script_root = text.index("$PSScriptRoot")
    assert "$PSScriptRoot" in text
    # It may still be a *guarded* fallback for hosts that do not set $PSScriptRoot,
    # but only after $PSScriptRoot was tried, and only behind an if().
    for line in text.splitlines():
        if "$MyInvocation.MyCommand.Path" in line:
            assert line.strip().startswith("if ("), line
            assert text.index(line) > script_root, "the script's own folder is tried first"


def test_every_path_root_is_guarded_before_it_is_used() -> None:
    """A null root must produce "no Python found", not a parameter-binding error."""
    text = code_only(ps1() + "\n" + COMMAND.read_text(encoding="utf-8-sig"))

    # Join-Path, Test-Path and Get-ChildItem all reject a null -Path.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("Join-Path", "Get-ChildItem", "Test-Path")):
            assert "$null" not in stripped, stripped
    assert "Test-Path $root" in text or "Test-Path -LiteralPath $root" in text


def test_a_failure_names_the_step_the_expectation_and_the_fix() -> None:
    """The contract the user asked for: readable, not a PowerShell parameter dump."""
    text = ps1()

    assert "trap" in text.lower()
    assert "python -m phonebridge start" in text, (
        "when the launcher itself fails it must print the command that does the work"
    )
    assert "interpreter" in text.lower() and "3.10" in text
    assert "pip install -e" in text or "-m pip install" in text or "'-m', 'pip', 'install'" in text


def test_python_discovery_uses_the_real_interpreter_launchers() -> None:
    text = code_only(COMMAND.read_text(encoding="utf-8-sig"))

    for candidate in ("'py'", "'python'", "'python3'", ".venv", "LOCALAPPDATA", "ProgramFiles"):
        assert candidate in text, candidate
    # The py launcher is an executable plus a separate argument, never one string.
    assert "Exe = 'py'; Args = @('-3')" in text
    assert not re.search(r"\[\s*1\s*\.\.\s*\(", text)
    assert ". $script:PythonCommandHelpers" in ps1()


def test_the_launcher_can_capture_a_second_cable() -> None:
    """The room keeps hearing the PC otherwise: loopback copies the default output."""
    text = code_only(ps1())

    assert "[string]$LoopbackDevice" in text
    assert "'--loopback-device', $LoopbackDevice" in text
    assert "list-loopback-devices" in text, "point at the command that explains the choice"


def test_every_single_quoted_string_closes() -> None:
    """An apostrophe inside 'quotes' stops the whole script from parsing."""
    assert odd_quote_lines(ps1()) == [], "a single-quoted string does not close"
