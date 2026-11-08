#!/usr/bin/env python3
"""Run every check this repository can run without a phone or a JDK.

    python tools/verify.py            # all checks
    python tools/verify.py --quick    # skip the end-to-end CLI smoke test

Exit code 0 means every available check passed. It says nothing about the
Android app - see the "cannot be verified here" list this script prints. Being
explicit about that boundary is the point: a green run here must never be read
as "the phone works".
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WINDOWS = REPO / "windows"
SIMULATOR = REPO / "tools" / "simulator" / "phone_sim.py"
VECTORGEN = REPO / "tools" / "vectorgen" / "gen_vectors.py"

OK = "[ ok ]"
FAIL = "[FAIL]"
SKIP = "[skip]"


def _run(cmd: list[str], cwd: Path, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False
    )


def _tail(text: str, lines: int = 25) -> str:
    rows = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join("    " + line for line in rows[-lines:])


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def check_python() -> tuple[str, str]:
    # Kept on purpose: without it an older interpreter fails with a cryptic
    # SyntaxError before this script can explain what to install.
    if sys.version_info < (3, 10):  # noqa: UP036
        return FAIL, f"Python {sys.version.split()[0]} is too old (need 3.10+)"
    try:
        import phonebridge  # noqa: F401  # noqa: PLC0415
    except ImportError:
        return FAIL, (
            "the `phonebridge` package is not importable; run\n"
            "    pip install -e windows"
        )
    where = getattr(phonebridge, "__file__", "?")
    return OK, f"Python {sys.version.split()[0]}, phonebridge at {where}"


def check_tests() -> tuple[str, str]:
    proc = _run([sys.executable, "-m", "pytest"], REPO)
    rows = [line.strip() for line in (proc.stdout + proc.stderr).splitlines() if line.strip()]
    detail = next((line for line in reversed(rows) if "passed" in line or "failed" in line),
                  "(no pytest summary)")
    if proc.returncode != 0:
        return FAIL, f"{detail}\n{_tail(proc.stdout)}"
    return OK, detail


def check_lint() -> tuple[str, str]:
    if _run([sys.executable, "-m", "ruff", "--version"], REPO).returncode != 0:
        return SKIP, "ruff is not installed (pip install ruff)"
    proc = _run([sys.executable, "-m", "ruff", "check", "."], REPO)
    if proc.returncode != 0:
        return FAIL, f"ruff reported problems:\n{_tail(proc.stdout)}"
    return OK, "ruff clean (whole repo, configured by ruff.toml)"


def check_vectors() -> tuple[str, str]:
    proc = _run([sys.executable, str(VECTORGEN), "--check"], REPO)
    if proc.returncode != 0:
        return FAIL, (proc.stdout + proc.stderr).strip()
    files = [
        REPO / "tools" / "vectorgen" / "vectors.json",
        REPO / "android/app/src/main/java/dev/phonebridge/protocol/ProtocolVectors.kt",
    ]
    sizes = ", ".join(f"{p.name} {p.stat().st_size}B" for p in files if p.is_file())
    return OK, f"generator output matches committed files ({sizes})"


def check_wrapper() -> tuple[str, str]:
    jar = REPO / "android/gradle/wrapper/gradle-wrapper.jar"
    scripts = [REPO / "android/gradlew", REPO / "android/gradlew.bat"]
    missing = [p.name for p in [jar, *scripts] if not p.is_file()]
    if missing:
        return FAIL, f"missing Gradle wrapper files: {', '.join(missing)}"
    properties = (REPO / "android/gradle/wrapper/gradle-wrapper.properties").read_text("utf-8")
    url = next(
        (line.split("=", 1)[1] for line in properties.splitlines()
         if line.startswith("distributionUrl=")),
        "?",
    )
    return OK, f"jar {jar.stat().st_size}B, scripts present, {url.strip()}"


def check_cli_smoke() -> tuple[str, str]:
    """Start the reference phone, talk to it over the real CLI, then stop it."""
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="pb-verify-") as tmp:
        sim = subprocess.Popen(
            [sys.executable, str(SIMULATOR), "--port", str(port), "--pair-policy", "auto",
             "--state-dir", str(Path(tmp) / "sim")],
            cwd=str(REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    if sim.poll() is not None:
                        out = sim.stdout.read() if sim.stdout else ""
                        return FAIL, f"simulator exited early:\n{_tail(out)}"
                    time.sleep(0.1)
            else:
                return FAIL, f"simulator did not listen on port {port} within 20s"

            # --config-dir is global; --host/--port belong to the subcommand.
            base = [sys.executable, "-m", "phonebridge", "--config-dir", str(Path(tmp) / "pc")]
            target = ["--host", "127.0.0.1", "--port", str(port)]
            info = _run([*base, "info", *target, "--auto-pair"], REPO, timeout=120)
            if info.returncode != 0:
                return FAIL, f"`phonebridge info` failed:\n{_tail(info.stdout + info.stderr)}"
            try:
                payload = json.loads(info.stdout[info.stdout.index("{"):])
            except ValueError:
                return FAIL, f"`phonebridge info` did not emit JSON:\n{_tail(info.stdout)}"
            if payload.get("protocol_version") != 1:
                return FAIL, f"unexpected protocol_version: {payload.get('protocol_version')!r}"

            ping = _run([*base, "ping", *target, "--count", "5"], REPO, timeout=120)
            if ping.returncode != 0:
                return FAIL, f"`phonebridge ping` failed:\n{_tail(ping.stdout + ping.stderr)}"
            stats = [line for line in ping.stdout.splitlines() if "median" in line]
            return OK, (
                f"paired + authenticated, protocol v{payload['protocol_version']}, "
                f"device {payload.get('device_id')}, {stats[-1].strip() if stats else 'ping ok'}"
            )
        finally:
            sim.terminate()
            try:
                sim.wait(timeout=10)
            except subprocess.TimeoutExpired:
                sim.kill()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
NEXT_STEPS = """\
Not verifiable without hardware - run these on your own PC and phone:

  1. cd android && .\\gradlew.bat assembleDebug          (needs JDK 17 + SDK 34)
  2. adb install -r app\\build\\outputs\\apk\\debug\\app-debug.apk
  3. app -> "Run protocol self-test"                   (expect 8/8)
  4. phonebridge info --host <phone-ip>                (Wi-Fi / USB / hotspot)

Step-by-step instructions and expected output: docs/07-device-testing-guide.md"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="skip the CLI smoke test")
    args = parser.parse_args()

    steps = [
        ("Python environment", check_python),
        ("Android build files", check_wrapper),
        ("Generated vectors", check_vectors),
        ("Unit + integration tests", check_tests),
        ("Lint", check_lint),
    ]
    if not args.quick:
        steps.append(("CLI end-to-end smoke", check_cli_smoke))

    print("PhoneBridge verification")
    print("=" * 72)
    failures = 0
    for index, (label, check) in enumerate(steps, start=1):
        try:
            status, detail = check()
        except Exception as exc:  # noqa: BLE001 - a broken check must not hide the rest
            status, detail = FAIL, f"{type(exc).__name__}: {exc}"
        if status == FAIL:
            failures += 1
        head = f"[{index}/{len(steps)}] {label} ".ljust(46, ".")
        print(f"{head} {status}")
        if detail:
            print(f"       {detail}")

    print("=" * 72)
    if failures:
        print(f"{failures} check(s) failed.")
        return 1
    print("All available checks passed.")
    print()
    print(NEXT_STEPS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
