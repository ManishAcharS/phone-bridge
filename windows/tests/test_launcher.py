"""``PhoneBridge-Start.bat`` is code, so it gets tests like code.

A double-clicked launcher is the most dangerous script in the repository: it runs
``git`` against a tree that may hold the user's uncommitted work, and it does so
without a person watching. These tests cannot execute ``cmd.exe`` here, so they
pin down the invariants that make it safe — the commands that must be present,
the ones that must never appear, and the stash rule that decides whether an
update is allowed to touch anything at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LAUNCHER = REPO / "PhoneBridge-Start.bat"


def text() -> str:
    return LAUNCHER.read_text(encoding="ascii")


def code() -> str:
    """Just the executable lines: comments and banner echoes are not behaviour."""
    return "\n".join(
        line
        for line in text().splitlines()
        if not line.strip().upper().startswith(("REM ", "@REM", "ECHO", "TITLE"))
    ).lower()


# --------------------------------------------------------------------------- #
# it exists, and Windows will run it
# --------------------------------------------------------------------------- #
def test_the_launcher_sits_at_the_repository_root() -> None:
    assert LAUNCHER.is_file()


def test_it_uses_crlf_line_endings_and_plain_ascii() -> None:
    raw = LAUNCHER.read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), "a stray bare LF breaks some cmd builds"
    raw.decode("ascii")


def test_it_does_not_need_a_console_encoding() -> None:
    assert "chcp" not in text().lower() or "65001" in text()


# --------------------------------------------------------------------------- #
# the four steps the user was promised
# --------------------------------------------------------------------------- #
def test_it_starts_from_its_own_folder() -> None:
    assert 'cd /d "%~dp0"' in text()


def test_it_checks_git_and_python_before_doing_anything() -> None:
    assert "where git" in text().lower()
    assert "py -3" in text()
    assert "python" in text().lower()


def test_it_explains_a_missing_tool_with_a_download_link() -> None:
    assert "git-scm.com" in text()
    assert "python.org" in text()


def test_it_refuses_an_old_python() -> None:
    assert "sys.version_info" in text()


def test_it_creates_the_venv_only_when_it_is_absent() -> None:
    assert "-m venv" in text()
    assert 'if exist "%VENV_PY%" goto :install' in text()


def test_it_installs_through_the_project_dev_config() -> None:
    assert '--editable "windows[dev]"' in text()


def test_it_launches_the_gui() -> None:
    assert "-m phonebridge.controller" in text()


def test_it_keeps_the_window_open_on_error() -> None:
    assert "pause" in text()
    assert ":stop" in text()


# --------------------------------------------------------------------------- #
# the update is allowed to touch nothing it did not create
# --------------------------------------------------------------------------- #
def test_it_uses_a_fast_forward_only_pull() -> None:
    assert "git pull --ff-only origin main" in text()


def test_it_only_updates_main() -> None:
    assert "--abbrev-ref HEAD" in text()
    assert ":not_main" in text()


@pytest.mark.parametrize(
    "forbidden",
    [
        "reset --hard",
        "git reset",
        "git clean",
        "checkout --",
        "stash drop",
        "stash clear",
        "force",
    ],
)
def test_it_never_destroys_work(forbidden: str) -> None:
    assert forbidden not in code()


def test_it_stashes_untracked_files_too() -> None:
    assert "--include-untracked" in text()


def test_a_clean_tree_creates_no_stash() -> None:
    body = text()
    assert "if not defined DIRTY goto :update" in body
    assert "git stash push" in body


def test_it_restores_only_the_stash_it_made() -> None:
    """"Never pop an older stash" is a comparison, not a comment."""
    body = text()
    assert "refs/stash" in body
    assert '%STASH_REF%' in body
    assert 'if not "%TOP%"=="%STASH_REF%"' in body


def test_the_stash_ref_is_recorded_immediately_after_it_is_created() -> None:
    body = text()
    push = body.index("git stash push")
    record = body.index("set \"STASH_REF=%%r\"", push)
    assert record > push
    assert record - push < 300, "the ref must be captured before anything else runs"


def test_it_pops_rather_than_drops() -> None:
    assert "git stash pop" in text()
    assert "stash drop" not in code()


def test_a_conflict_stops_and_keeps_the_stash() -> None:
    body = text()
    assert ":restore_conflict" in body
    assert "still in the stash" in body
    assert "git stash drop" in body, "the recovery instructions must be printed, not run"


def test_the_update_is_skipped_when_the_tree_cannot_be_saved() -> None:
    body = text()
    stash_failed = label_start(body, ":stash_failed")
    section = body[stash_failed : stash_failed + 400]
    assert "nothing was updated" in section.lower()


# --------------------------------------------------------------------------- #
# every goto has a target, and every target is reachable
# --------------------------------------------------------------------------- #
def label_start(body: str, name: str) -> int:
    """Index of the label itself, not of a ``goto`` that mentions it."""
    match = re.search(rf"^{name}\s*$", body, re.MULTILINE)
    assert match is not None, f"{name} is not a label in the launcher"
    return match.start()


def labels() -> set[str]:
    return {
        match.group(1).lower()
        for match in re.finditer(r"^:([A-Za-z_][\w]*)", text(), re.MULTILINE)
    }


def gotos() -> set[str]:
    return {match.group(1).lower() for match in re.finditer(r"goto :([A-Za-z_]\w*)", text())}


def test_every_goto_lands_somewhere() -> None:
    missing = gotos() - labels()
    assert not missing, f"goto targets that do not exist: {sorted(missing)}"


def test_no_label_is_dead_weight() -> None:
    unused = labels() - gotos() - {"restore_stash"}
    assert not unused, f"labels nothing jumps to: {sorted(unused)}"


def test_the_subroutine_returns_through_exit_b() -> None:
    body = text()
    start = label_start(body, ":restore_stash")
    end = label_start(body, ":no_folder")
    section = body[start:end]
    assert "exit /b" in section
    # A subroutine must not fall through into the failure banners: the last
    # command it runs before the next label must be a return.
    statements = [
        line.strip()
        for line in section.splitlines()
        if line.strip() and not line.strip().upper().startswith("REM")
    ]
    assert statements[-1] == "exit /b 0"
