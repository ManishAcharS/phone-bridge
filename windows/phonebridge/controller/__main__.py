"""``python -m phonebridge.controller`` — open the desktop controller.

Same thing as ``phonebridge gui``; it exists so the window can be launched
without remembering the console-script name, and so a shortcut can point at
``pythonw.exe -m phonebridge.controller``.
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m phonebridge.controller",
        description="PhoneBridge desktop controller (one-click connection).",
    )
    parser.add_argument("--auto-connect", action="store_true",
                        help="connect as soon as the window opens")
    parser.add_argument("--discovery-timeout", type=float, default=3.0, metavar="SECONDS",
                        help="how long to listen for the phone's beacon (default 3)")
    parser.add_argument("--config-dir", default=None,
                        help="override the config directory (portable mode)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        from .ui.app import run_gui
    except ImportError as exc:  # pragma: no cover - depends on the Python build
        print(f"this Python has no tkinter: {exc}", file=sys.stderr)
        print("install Python from python.org (tkinter is part of it), "
              "or use the `phonebridge` command line instead.", file=sys.stderr)
        return 2
    return run_gui(
        config_dir=args.config_dir,
        auto_connect=args.auto_connect,
        discovery_timeout=args.discovery_timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
