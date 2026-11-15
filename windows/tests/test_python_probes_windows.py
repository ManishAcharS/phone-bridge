"""Real Windows regression for the launcher Python probes.

The old checker passed multiline Python here-strings through Windows native
argument quoting and discarded stderr. A child-side quoting/import failure then
collapsed to the bare string ``unknown`` and blocked the launcher even when the
packages were installed. This test runs the actual PowerShell checker on a
Windows runner with the live packages installed, including through ``py -3``.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows PowerShell")

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "windows" / "tests" / "run_python_probes.ps1"


def run_checker(python: str | None = None, *, use_py_launcher: bool = False) -> dict[str, str]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER),
    ]
    if use_py_launcher:
        command.append("-UsePyLauncher")
    else:
        command.extend(["-Python", python or sys.executable])
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"checker runner failed: {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


def assert_audio_check_result(data: dict[str, str]) -> None:
    # A runner without VB-CABLE should produce a positive missing/partial result,
    # not the old bare unknown. If the audio backend itself cannot be queried,
    # the new checker must still explain the child exit/stderr.
    for key in ("cable", "loopback"):
        value = data[key]
        assert value != "unknown", data
        if value.startswith("unknown:"):
            assert "exit=" in value and "stderr=" in value, data
        else:
            assert value.startswith("found:") or value in {"missing", "partial"}, data


def test_installed_live_packages_are_reported_without_unknown() -> None:
    for name in ("pyvirtualcam", "sounddevice", "pyaudiowpatch"):
        importlib.import_module(name)
    data = run_checker()
    assert data["packages"] == "ok", data
    assert_audio_check_result(data)


def test_installed_live_packages_through_py_launcher_are_not_unknown() -> None:
    data = run_checker(use_py_launcher=True)
    assert data["packages"] == "ok", data
    assert_audio_check_result(data)


def test_unknown_includes_child_exit_code_and_stderr(tmp_path: Path) -> None:
    fake = tmp_path / "fake-python.bat"
    fake.write_bytes(b"@echo off\r\necho probe stderr 1>&2\r\nexit /b 42\r\n")
    data = run_checker(str(fake))
    for value in data.values():
        assert value.startswith("unknown:"), data
        assert "exit=42" in value, data
        assert "probe stderr" in value, data
