from __future__ import annotations

import re
from pathlib import Path

from ps1_lint import odd_quote_lines

REPO = Path(__file__).resolve().parents[2]
COMMAND = REPO / "scripts" / "python-command.ps1"
PROBES = REPO / "scripts" / "python-probes.ps1"
LAUNCHER = REPO / "scripts" / "start-live.ps1"
SETUP = REPO / "scripts" / "setup-windows.ps1"
RUNNER = REPO / "windows" / "tests" / "run_python_probes.ps1"
COMMAND_RUNNER = REPO / "windows" / "tests" / "run_python_command.ps1"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_windows_helpers_are_crlf_ascii_and_dot_sourced() -> None:
    for path in (COMMAND, PROBES, LAUNCHER, SETUP, RUNNER, COMMAND_RUNNER):
        raw = path.read_bytes()
        assert b"\r\n" in raw, path
        assert raw.count(b"\n") == raw.count(b"\r\n"), path
        raw.decode("ascii")
    assert ". $script:PythonCommandHelpers" in text(LAUNCHER)
    assert ". $script:PythonProbes" in text(LAUNCHER)
    assert ". $commandHelpers" in text(SETUP)
    assert ". $probeHelpers" in text(SETUP)


def test_executable_and_launcher_arguments_are_never_one_command_string() -> None:
    command = text(COMMAND)
    assert "& $Executable @allArguments" in command
    assert "Exe = 'py'; Args = @('-3')" in command
    assert "Exe = 'py'; Args = @()" in command
    for path in (COMMAND, PROBES, LAUNCHER, SETUP, RUNNER, COMMAND_RUNNER):
        source = text(path)
        assert not re.search(r"\$script:PythonCommand\b", source), path
        assert "& 'py -3'" not in source, path
        assert '& "py -3"' not in source, path
    runner = text(COMMAND_RUNNER)
    assert "$pyExe = 'py'" in runner
    assert "$pyArgs = @('-3')" in runner
    assert "Invoke-PythonCommand -Executable $pyExe -Prefix $pyArgs" in runner


def test_probe_transport_avoids_multiline_native_quoting_and_keeps_stderr() -> None:
    source = text(PROBES)
    assert "ToBase64String" in source
    assert "base64.b64decode" in source
    assert "2>&1" in source
    assert "ErrorRecord" in source
    assert "LASTEXITCODE" in source
    assert "2>$null" not in source
    assert "Invoke-PythonCommand -Executable $script:PythonExe -Prefix $script:PythonArgs" in source
    assert "PYTHONIOENCODING" in source
    assert "[Console]::OutputEncoding" in source


def test_probe_arguments_are_base64_too_and_bootstrap_precedes_them() -> None:
    source = text(PROBES)
    assert "$encodedArguments" in source
    assert "$arguments = @('-c', $bootstrap)" in source
    assert "if ($encodedArguments.Count -gt 0) { $arguments += $encodedArguments }" in source


def test_only_a_truly_absent_distribution_is_reported_as_missing() -> None:
    source = text(PROBES)
    assert "except ModuleNotFoundError as exc:" in source
    assert "if exc.name != name:" in source
    assert "raise" in source


def test_the_bridge_itself_uses_the_shared_separate_argument_helper() -> None:
    source = text(LAUNCHER)
    assert "$bridgeArguments = @('-m', 'phonebridge') + $Arguments" in source
    assert "Invoke-PythonCommand -Executable $script:PythonExe -Prefix $script:PythonArgs -Arguments $bridgeArguments" in source
    assert "& $script:PythonCommand" not in source


def test_setup_pip_and_checks_use_the_shared_helper() -> None:
    source = text(SETUP)
    assert "Invoke-PythonCommand -Executable $script:PythonExe -Prefix $script:PythonArgs -Arguments @('-m', 'pip'" in source
    assert "Invoke-PythonProbe" in source
    assert "Format-ProbeUnknown" in source


def test_unknown_results_carry_exit_and_stderr() -> None:
    source = text(PROBES)
    assert "function Format-ProbeUnknown" in source
    assert "unknown: exit=$exit; stderr=$stderr; stdout=$stdout" in source


def test_launcher_treats_unknown_as_warning_and_only_positive_missing_as_failure() -> None:
    source = text(LAUNCHER)
    assert "function Write-Warn" in source
    assert "$missing -like 'unknown*'" in source
    assert "$cable -like 'unknown*'" in source
    assert "$choice -like 'unknown*'" in source
    assert "Write-Warn \"Python package check could not run: $missing\"" in source
    assert "Write-Warn \"VB-CABLE check could not run: $cable\"" in source
    assert "continuing; phonebridge" in source
    assert "$missing -eq 'unknown' -or $missing" not in source


def test_setup_treats_unknown_as_warning_not_a_missing_component() -> None:
    source = text(SETUP)
    assert "function Write-Warn" in source
    assert "$state.Status -eq 'unknown'" in source
    assert "Write-Warn \"$Name check could not run: $($state.Detail)\"" in source
    assert "$loopbacks.Status -eq 'unknown'" in source
    assert "Write-Warn \"VB-CABLE check could not run: $cable\"" in source
    assert "Write-Warn \"speaker capture check could not run: $($loopbacks.Detail)\"" in source
    assert "only inconclusive warnings" in source


def test_every_single_quoted_string_closes() -> None:
    for path in (COMMAND, PROBES, LAUNCHER, SETUP, RUNNER, COMMAND_RUNNER):
        assert odd_quote_lines(text(path)) == [], path
