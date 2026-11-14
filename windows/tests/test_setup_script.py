"""The setup script: what it must offer, and what it must never do.

PowerShell cannot run on this build container, so these are source-level checks.
They are written around the properties that matter for a script a user will run
on their own machine: it must name the right packages, it must tell the user
where OBS and VB-CABLE come from, and it must not download or execute an
installer on its own.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "setup-windows.ps1"


def setup_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_the_script_exists_and_is_ascii_for_powershell_5_1() -> None:
    assert SCRIPT.is_file()
    setup_text().encode("ascii")  # a stray non-ASCII character breaks PS 5.1 parsing


def test_it_installs_the_three_python_packages() -> None:
    text = setup_text()

    for package in ("pyvirtualcam", "sounddevice", "PyAudioWPatch"):
        assert f"Ensure-Package '{package}'" in text, f"{package} is not installed by the script"


def test_it_points_at_obs_and_vb_cable_without_installing_them() -> None:
    text = setup_text()

    assert "https://obsproject.com/download" in text
    assert "https://vb-audio.com/Cable/" in text
    # Both are signed installers a human has to accept; the script only reports.
    for forbidden in ("Invoke-WebRequest", "Start-BitsTransfer", "bitsadmin", "msiexec",
                      "Start-Process", "curl "):
        assert forbidden not in text, f"the setup script must not fetch or run installers ({forbidden})"


def test_it_does_not_require_administrator_rights_or_change_security_settings() -> None:
    text = setup_text().lower()

    for forbidden in ("set-executionpolicy", "runas", "enable-psremoting", "set-mppreference",
                      "netsh advfirewall", "reg add"):
        assert forbidden not in text, f"the setup script must not change machine settings ({forbidden})"


def test_it_has_a_check_only_mode_that_changes_nothing() -> None:
    text = setup_text()

    assert "[switch]$Check" in text
    assert "check only: nothing will be installed" in text
    # The install call is unreachable in check mode.
    install_line = "Invoke-Python @('-m', 'pip', 'install', '--quiet', $Name)"
    assert install_line in text
    guarded = text.split("if ($Check) {", 1)[1]
    assert "return" in guarded.split("}", 1)[0]


def test_exit_codes_are_documented_and_distinct() -> None:
    text = setup_text()

    assert "exit 0" in text  # everything present
    assert "exit 1" in text  # a component still needs installing
    assert "exit 2" in text  # Python missing or too old
    assert "Exit codes: 0" in text


def test_it_detects_the_virtual_camera_device_through_the_same_lookup_the_cli_uses() -> None:
    """The script must not answer "VB-CABLE is fine" by guessing."""
    text = setup_text()

    assert "sounddevice.query_devices()" in text
    assert "cable input" in text.lower()
