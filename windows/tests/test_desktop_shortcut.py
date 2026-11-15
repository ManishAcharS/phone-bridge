from __future__ import annotations

from pathlib import Path

from ps1_lint import odd_quote_lines

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "create-desktop-shortcut.ps1"


def text() -> str:
    return SCRIPT.read_text(encoding="utf-8-sig")


def test_shortcut_creator_exists_and_is_windows_formatted() -> None:
    raw = SCRIPT.read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n")
    raw.decode("ascii")


def test_shortcut_targets_the_daily_bat_and_keeps_errors_readable() -> None:
    source = text()
    assert "start-live.bat" in source
    assert "WScript.Shell" in source
    assert "/k" in source
    assert "WorkingDirectory" in source
    assert "GetFolderPath('Desktop')" in source


def test_shortcut_creator_does_not_need_admin_or_change_policy() -> None:
    lower = text().lower()
    assert "runas" not in lower
    assert "set-executionpolicy" not in lower
    assert "start-process" not in lower
    assert "-force" in lower


def test_shortcut_creator_refuses_silent_overwrite_and_bad_names() -> None:
    source = text()
    assert "Shortcut already exists" in source
    assert "GetInvalidFileNameChars()" in source
    assert "Arguments must be one line" in source


def test_every_single_quoted_string_closes() -> None:
    assert odd_quote_lines(text()) == []
