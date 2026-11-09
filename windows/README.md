# PhoneBridge (PC side)

The `phonebridge` command. Project overview: `../README.md`. Full command
reference, exit codes and examples: `../docs/11-pc-client.md`.

## Install

    cd windows
    python -m venv .venv
    .venv\Scripts\activate            # Windows PowerShell / cmd
    source .venv/bin/activate         # Linux / macOS
    pip install -e ".[dev]"           # or plain `pip install .` for users

`pip install .` is enough to use the CLI; `.[dev]` adds pytest, ruff and mypy so
`python ../tools/verify.py` can run the whole gate.

## Use

Everything below also works without activating the venv, by prefixing the
interpreter (`python -m phonebridge ...`).

    phonebridge --version            # phonebridge 1.0.0 (protocol v1, Python 3.x)
    phonebridge doctor               # interpreter, packages, adb, firewall notes
    phonebridge devices              # pinned phones + live beacons
    phonebridge info --host 127.0.0.1       # after `adb forward tcp:8765 tcp:8765`
    phonebridge capabilities --require audio.bluetooth_sco
    phonebridge selftest             # every supported workflow, end to end

Runtime dependencies are only `websockets` and `cryptography`. Anything else
(audio, dashboard, virtual camera) is listed in `requirements-phase2plus.txt`
and is not imported by this release.

## Test

    python -m pytest -q              # 479 tests
    python -m ruff check ..          # lint, whole repo
    python -m mypy                   # type check, package only
    python ../tools/verify.py        # all of the above + vectors + CLI smoke
