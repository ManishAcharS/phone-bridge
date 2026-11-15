from __future__ import annotations

from pathlib import Path

from ps1_lint import odd_quote_lines

REPO = Path(__file__).resolve().parents[2]
PROBES = REPO / "scripts" / "python-probes.ps1"
LAUNCHER = REPO / "scripts" / "start-live.ps1"
RUNNER = REPO / "windows" / "tests" / "run_python_probes.ps1"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_probe_helpers_are_windows_formatted_and_dot_sourced() -> None:
    for path in (PROBES, RUNNER):
        raw = path.read_bytes()
        assert b"\r\n" in raw
        assert raw.count(b"\n") == raw.count(b"\r\n")
        raw.decode("ascii")
    assert ". $script:PythonProbes" in text(LAUNCHER)
    assert "python-probes.ps1" in text(LAUNCHER)


def test_probe_transport_avoids_multiline_native_quoting_and_keeps_stderr() -> None:
    source = text(PROBES)
    assert "ToBase64String" in source
    assert "base64.b64decode" in source
    assert "2>&1" in source
    assert "ErrorRecord" in source
    assert "LASTEXITCODE" in source
    assert "2>$null" not in source
    assert "Select-Object -Skip 1" in source
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


def test_the_bridge_itself_uses_the_same_safe_command_array_invocation() -> None:
    source = text(LAUNCHER)
    assert "$bridgeArguments = @('-m', 'phonebridge') + $Arguments" in source
    assert "& $exe @prefix @bridgeArguments" in source
    assert "& $script:PythonCommand @('-m', 'phonebridge')" not in source


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


def test_every_single_quoted_string_closes() -> None:
    assert odd_quote_lines(text(PROBES)) == []
    assert odd_quote_lines(text(RUNNER)) == []
