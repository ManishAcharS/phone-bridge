"""Compile-level guards for the Kotlin UI, and proof the fixes kept behaviour.

Two real compilation errors were found in the Compose layer while assembling the
release build:

* ``PermissionsCard.kt`` called ``filterValues`` on ``result.entries``. That is a
  ``Set<Map.Entry<..>>``; ``filterValues`` is an extension on ``Map``, so the call
  did not resolve and the destructuring lambda below it lost its parameter type.
* ``ProbeCard.kt`` called ``runProbe()`` from the permission launcher's callback
  while the function itself was declared further down the same composable. A
  *local* function is only visible after its declaration, so the call did not
  resolve.

Neither class of bug is visible to the Python test suite, and there is no Android
SDK in this environment, so these are source-level guards: they pin the shape of
the code that the compiler needs. They are not a substitute for
``gradlew assembleDebug``.

The third test is the counterpart of the other two: it asserts that the repair
did not weaken anything — the probe still runs the real ``CapabilityProbe`` with
the real configuration, still publishes its report, and the permission panel
still watches for revocations.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
KOTLIN_ROOT = REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
PERMISSIONS_CARD = KOTLIN_ROOT / "PermissionsCard.kt"
PROBE_CARD = KOTLIN_ROOT / "ProbeCard.kt"

# Map-only extensions: these do not exist on Set<Map.Entry<..>> or on List.
MAP_ONLY_EXTENSIONS = ("filterValues", "filterKeys", "mapValues")


def read(path: Path) -> str:
    return path.read_text("utf-8")


def code_lines(text: str) -> list[tuple[int, str]]:
    """(1-based line number, line) with comments and KDoc lines removed."""
    result = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("//", "*", "/*")):
            continue
        result.append((number, line.split("//", 1)[0]))
    return result


def test_no_map_only_extension_is_called_on_entries() -> None:
    """``filterValues`` belongs on the map, not on ``.entries``.

    The exact failure this catches (reproduced with kotlinc 1.9.24 against the
    real callback type)::

        error: unresolved reference 'filterValues' on receiver of type
               'Set<Map.Entry<String, Boolean>>'
        error: cannot infer type for implicit value parameter 'it'
    """
    problems = []
    for path in sorted(KOTLIN_ROOT.rglob("*.kt")):
        lines = read(path).splitlines()
        for index, line in enumerate(lines):
            if not re.search(r"\.entries\b", line):
                continue
            # the chain may continue on the same line or on the next one
            tail = line.split(".entries", 1)[1]
            following = lines[index + 1] if index + 1 < len(lines) else ""
            for extension in MAP_ONLY_EXTENSIONS:
                if re.search(rf"\.\s*{extension}\b", tail) or re.match(
                    rf"\s*\.\s*{extension}\b", following
                ):
                    problems.append(f"{path.name}:{index + 1}: .entries.{extension}")
    assert not problems, (
        "Map-only extensions called on a Set<Map.Entry<..>>: " + ", ".join(problems)
    )


def _enclosing_declaration(lines: list[str], index: int) -> str | None:
    """The nearest line at column 0 that opens the scope containing ``index``."""
    for candidate in range(index, -1, -1):
        line = lines[candidate]
        if line and not line[0].isspace() and not line.startswith(("@", ")", "}")):
            return line
    return None


def test_local_functions_are_declared_before_they_are_used() -> None:
    """A local ``fun`` inside a composable is only visible *after* its line.

    This is what made ``runProbe`` unresolvable in ``ProbeCard.kt``: the
    launcher callback called it 7 lines before it was declared. Members of an
    ``object``/``class`` are order-independent, so those are skipped.
    """
    problems = []
    for path in sorted(KOTLIN_ROOT.rglob("*.kt")):
        lines = read(path).splitlines()
        for index, line in enumerate(lines):
            local = re.match(r"^    fun (\w+)\(", line)
            if not local:
                continue
            enclosing = _enclosing_declaration(lines, index)
            if enclosing is None or not re.match(
                r"^(private |internal |public )?(suspend )?fun \w+\(", enclosing
            ):
                continue  # a member of a class/object: order does not matter
            name = local.group(1)
            for number, earlier in enumerate(lines[:index], start=1):
                code = earlier.split("//", 1)[0]
                if code.strip().startswith(("*", "/*")):
                    continue
                call = re.search(rf"(?<![\w.]){re.escape(name)}\s*\(", code)
                if call:
                    problems.append(f"{path.name}:{index + 1}: {name}() used at line {number}")
                    break
    assert not problems, "local functions used before their declaration: " + ", ".join(problems)


def test_the_repair_did_not_weaken_the_probe_or_the_permission_panel() -> None:
    """The smallest fix must not replace measurements with placeholders."""
    probe = read(PROBE_CARD)
    assert "CapabilityProbe.run(" in probe, "the card must still run the real probe"
    assert re.search(
        r"CapabilityProbe\.Config\(seconds = 30, acoustic = acoustic\)", probe
    ), "the probe configuration must still come from the UI state"
    assert "BridgeState.publishProbe(" in probe, "the report must still reach the PC"
    assert "withContext(Dispatchers.IO)" in probe, "the probe must stay off the main thread"
    assert "KeepScreenOn(enabled = running)" in probe, "the screen must stay on while probing"
    # the permission result is what starts the probe, exactly once
    callback = probe[probe.index("val permissionLauncher") : probe.index("KeepScreenOn(enabled")]
    assert "pendingRun = false" in callback and "runProbe()" in callback
    assert callback.index("pendingRun = false") < callback.index("runProbe()")
    # no hardcoded success anywhere in either card
    for path in (PROBE_CARD, PERMISSIONS_CARD):
        text = read(path)
        assert "TODO" not in text
        assert not re.search(r"status\"?\s*[:=]\s*\"(ok|supported|pass)\"", text)

    card = read(PERMISSIONS_CARD)
    assert "Permissions.snapshot(context)" in card, "live permission state is re-read"
    assert "Permissions.explainDenial(" in card, "denials must stay explained"
    assert "Permissions.markAsked(" in card, "asked-state must still be recorded"
    assert "Permissions.openAppSettings(" in card, "blocked permissions need the settings path"
    assert "BridgeState.note(" in card, "revocations must still be logged for the PC"
