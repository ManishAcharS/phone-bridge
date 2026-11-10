"""The window's entry points, tested without a window.

tkinter needs a display, and CI has none, so these tests check the wiring that
*feeds* the window: the CLI verb, its arguments, and the clear message a Python
without tkinter gets. The drawing code itself is exercised by hand on Windows
(``docs/17`` §5) and by ``test_controller_view.py``, which holds all the rules.
"""

from __future__ import annotations

import sys
import types

import pytest

from phonebridge.cli.main import main


def test_gui_has_help() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["gui", "--help"])
    assert exit_info.value.code == 0


def test_gui_is_listed_in_the_top_level_help(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "gui" in capsys.readouterr().out


def test_gui_passes_its_arguments_to_the_window(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []
    fake = types.ModuleType("phonebridge.controller.ui.app")

    def run_gui(**kwargs):
        calls.append(kwargs)
        return 0

    fake.run_gui = run_gui  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "phonebridge.controller.ui.app", fake)

    code = main(
        [
            "--config-dir",
            str(tmp_path),
            "gui",
            "--auto-connect",
            "--discovery-timeout",
            "1.5",
        ]
    )
    assert code == 0
    assert calls == [
        {"config_dir": str(tmp_path), "auto_connect": True, "discovery_timeout": 1.5}
    ]


def test_a_python_without_tkinter_explains_itself(monkeypatch, capsys) -> None:
    # a None entry in sys.modules makes the import raise ImportError
    monkeypatch.setitem(sys.modules, "phonebridge.controller.ui.app", None)
    code = main(["gui"])
    captured = capsys.readouterr()
    assert code == 2, "a missing window toolkit is a usage problem, not a crash"
    assert "window" in (captured.err + captured.out).lower()


def test_the_module_entry_point_defaults(tmp_path) -> None:
    from phonebridge.controller.__main__ import build_parser

    args = build_parser().parse_args([])
    assert args.auto_connect is False
    assert args.discovery_timeout == 3.0
    assert args.config_dir is None

    parsed = build_parser().parse_args(
        ["--auto-connect", "--discovery-timeout", "2", "--config-dir", str(tmp_path)]
    )
    assert parsed.auto_connect is True
    assert parsed.discovery_timeout == 2.0
    assert parsed.config_dir == str(tmp_path)


def test_the_controller_package_imports_without_tkinter() -> None:
    """Nothing outside ``controller.ui`` may drag in a display toolkit."""
    import subprocess

    code = "import sys; import phonebridge.controller; print('tkinter' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
    )
    assert out.stdout.strip() == "False", (
        "importing the controller must not require (or load) tkinter"
    )


def test_tkinter_ui_can_be_imported_when_a_toolkit_exists() -> None:
    pytest.importorskip("tkinter")
    from phonebridge.controller.ui import app

    assert hasattr(app, "PhoneBridgeApp")
    assert hasattr(app, "run_gui")
