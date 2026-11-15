"""Real Windows regression for the launcher's Python invocation helper.

The reported failure was PowerShell treating the single string ``'py -3'`` as one
command name. These tests run the shared helper used by start-live.ps1 and
setup-windows.ps1 through the real Windows ``py`` launcher, with ``-3`` kept as a
separate argument.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows PowerShell")

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "windows" / "tests" / "run_python_command.ps1"


def run_mode(mode: str) -> subprocess.CompletedProcess[str]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER),
        "-Mode",
        mode,
    ]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def test_helper_runs_py_launcher_with_separate_arguments() -> None:
    proc = run_mode("helper")
    assert proc.returncode == 0, f"helper failed:\n{proc.stdout}\n{proc.stderr}"
    lines = proc.stdout.strip().splitlines()
    assert len(lines) >= 2, proc.stdout
    major, minor = (int(part) for part in lines[0].split())
    assert (major, minor) >= (3, 10), lines[0]
    assert json.loads(lines[1]) == ["marker with spaces", "second marker"], proc.stdout


def test_helper_can_launch_phonebridge_module_through_py_launcher() -> None:
    proc = run_mode("module")
    assert proc.returncode == 0, f"module failed:\n{proc.stdout}\n{proc.stderr}"
    assert "usage" in proc.stdout.lower(), proc.stdout


def test_automatic_resolution_keeps_py_launcher_executable_and_args_separate() -> None:
    proc = run_mode("resolve")
    assert proc.returncode == 0, f"resolve failed:\n{proc.stdout}\n{proc.stderr}"
    data = json.loads(proc.stdout)
    assert data["exe"] == "py", data
    assert data["args"] == ["-3"], data
    assert data["hasPackage"] is True, data


def test_setup_check_uses_automatic_python_without_a_combined_command_name() -> None:
    script = REPO / "scripts" / "setup-windows.ps1"
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    assert proc.returncode in (0, 1), f"setup -Check failed:\n{proc.stdout}\n{proc.stderr}"
    assert "Python" in proc.stdout, proc.stdout
    assert "'py -3'" not in proc.stdout + proc.stderr, proc.stdout + proc.stderr
    if proc.returncode == 1:
        assert "still to do by hand" in proc.stdout, proc.stdout
