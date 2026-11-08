"""Static checks on the Kotlin sources.

`test_android_build.py` proves the Gradle configuration is consistent. This file
is about the Kotlin *source*: declarations the compiler rejects outright, and
Android API misuse that only fails on the real build.

Both rules here were learned from an actual failed `assembleDebug` on Windows:

1. `enum class is not allowed here` — an enum was declared inside an `inner
   class` (`BridgeServer.PeerSession`). See `kotlin_scan.py` for the rule and
   the kotlinc repros that established it.
2. `unresolved reference: applicationInfo` — `PackageManager` was asked for
   `applicationInfo`, which only `Context` provides.

`kotlin_scan.py` was validated against kotlinc 1.9.24 over this tree: with the
old `BridgeServer.kt` it reports exactly the one error the compiler reports
(`BridgeServer.kt:419`), and with the current tree it reports nothing while the
compiler's other 869 diagnostics (all caused by compiling without the Android
SDK) stay unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from kotlin_scan import (
    declarations_in_inner_class,
    declarations_nested_in_inner_class,
    mask_source,
)

REPO = Path(__file__).resolve().parents[2]
KOTLIN_DIR = REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
CAPABILITY_PROBE = KOTLIN_DIR / "net" / "CapabilityProbe.kt"
BRIDGE_SERVER = KOTLIN_DIR / "net" / "BridgeServer.kt"


def read(path: Path) -> str:
    return path.read_text("utf-8")


# --------------------------------------------------------------------------- #
# 1. the rule: nothing class-like may be declared inside an `inner class`
# --------------------------------------------------------------------------- #
REJECTED = {
    "enum": "class O {\n    inner class I {\n        private enum class Stage { A, B }\n    }\n}",
    "object": "class O {\n    inner class I {\n        private object S\n    }\n}",
    "interface": "class O {\n    inner class I {\n        interface S { fun f() }\n    }\n}",
    "companion": "class O {\n    inner class I {\n        companion object { const val X = 1 }\n    }\n}",
    "class": "class O {\n    inner class I {\n        private class S\n    }\n}",
    "data-class": "class O {\n    inner class I {\n        private data class S(val a: Int)\n    }\n}",
}

ACCEPTED = {
    "inner-class": "class O {\n    inner class I {\n        inner class S { fun f() = 1 }\n    }\n}",
    "local-class": "class O {\n    inner class I {\n        fun f() { class Local; println(Local()) }\n    }\n}",
    "anonymous-object": (
        "class O {\n"
        "    inner class I {\n"
        "        private val r = object : Runnable { override fun run() {} }\n"
        "    }\n"
        "}"
    ),
    "class-literal": "class O {\n    inner class I {\n        val k = Any::class.java\n    }\n}",
    "nested-in-plain-class": "class O {\n    class S {\n        private enum class Stage { A }\n    }\n}",
    "top-level-enum": "private enum class Stage { A, B }\nclass O { fun f(s: Stage) = s }",
    "in-kdoc": "class O {\n    inner class I {\n        /** enum class Stage is illegal here */\n        val s = 1\n    }\n}",
    "in-string": 'class O {\n    inner class I {\n        val s = "enum class Stage"\n    }\n}',
    "in-line-comment": "class O {\n    inner class I {\n        // enum class Stage was moved out\n        val s = 1\n    }\n}",
    "lambda-default-arg": (
        "class O {\n"
        "    inner class I(\n"
        "        private val onDone: () -> Unit = { },\n"
        "    ) {\n"
        "        fun f() = 1\n"
        "    }\n"
        "}"
    ),
}


@pytest.mark.parametrize("case", sorted(REJECTED))
def test_scanner_flags_declarations_the_compiler_rejects(case: str) -> None:
    found = declarations_in_inner_class(REJECTED[case])
    assert len(found) == 1, f"expected exactly one violation, got {[str(d) for d in found]}"
    assert found[0].inner_class == "I"


@pytest.mark.parametrize("case", sorted(ACCEPTED))
def test_scanner_ignores_legal_nesting(case: str) -> None:
    found = declarations_in_inner_class(ACCEPTED[case])
    assert not found, f"false positive: {[str(d) for d in found]}"


def test_scanner_reports_the_right_kind_name_and_line() -> None:
    """The exact shape that broke the build, in isolation."""
    source = (
        "class BridgeServer {\n"  # 1
        "    internal inner class PeerSession(\n"  # 2
        "        private val conn: String,\n"  # 3
        "    ) {\n"  # 4
        "        private enum class Stage { AWAIT_HELLO, READY }\n"  # 5
        "    }\n"  # 6
        "}\n"  # 7
    )
    found = declarations_in_inner_class(source)
    assert [(d.line, d.kind, d.name, d.inner_class) for d in found] == [
        (5, "enum class", "Stage", "PeerSession")
    ]


def test_depth_accounting_survives_surrounding_method_bodies() -> None:
    """A member declared after a method body is still at member level."""
    source = (
        "class O {\n"  # 1
        "    inner class I {\n"  # 2
        "        fun f() { println(1) }\n"  # 3
        "        private enum class Stage { A }\n"  # 4
        "    }\n"  # 5
        "}\n"  # 6
    )
    assert [d.line for d in declarations_in_inner_class(source)] == [4]


def test_masking_preserves_offsets_and_lines() -> None:
    source = '/* a { */ class O { val s = """{""" // }\n}\n'
    masked = mask_source(source)
    assert len(masked) == len(source)
    assert masked.count("\n") == source.count("\n")
    assert masked.count("{") == 1, "braces in comments and raw strings must be blanked"
    assert "class O" in masked


def test_scanner_finds_nothing_in_the_real_tree() -> None:
    offenders = declarations_nested_in_inner_class(KOTLIN_DIR)
    assert not offenders, "\n".join(
        f"{path.relative_to(REPO)}: {decl}" for path, decls in offenders.items() for decl in decls
    )


def test_the_tree_really_does_contain_inner_classes() -> None:
    """Guard against a scan that trivially finds nothing."""
    inner = {
        path
        for path in KOTLIN_DIR.rglob("*.kt")
        if re.search(r"\binner\s+class\b", mask_source(path.read_text("utf-8")))
    }
    assert BRIDGE_SERVER in inner, "the scanner's main subject disappeared"


def test_scanner_would_catch_the_bug_in_the_real_source() -> None:
    """Re-inject the historical mistake into the real `BridgeServer.kt` text.

    Without this, `test_scanner_finds_nothing_in_the_real_tree` passes both when
    the tree is clean and when the scanner has silently stopped working.
    """
    text = read(BRIDGE_SERVER)
    marker = "    internal inner class PeerSession("
    assert marker in text, "PeerSession moved; update this test"
    body_start = text.index(") {", text.index(marker)) + 2
    assert text[body_start] == "{"
    injected = text[: body_start + 1] + "\n        private enum class Probe { A }" + text[body_start + 1 :]

    found = declarations_in_inner_class(injected)
    assert [(d.kind, d.name, d.inner_class) for d in found] == [("enum class", "Probe", "PeerSession")]
    expected_line = text.count("\n", 0, body_start) + 2
    assert found[0].line == expected_line


# --------------------------------------------------------------------------- #
# 2. PackageManager is not a Context: `applicationInfo` lives on Context
# --------------------------------------------------------------------------- #
def _package_manager_vars(text: str) -> set[str]:
    # `val pm = context.packageManager`, `val pm: PackageManager = ...`
    return set(
        re.findall(r"\bval\s+(\w+)\s*(?::\s*PackageManager\s*)?=\s*[\w.]*\.?packageManager\b", text)
    ) | set(re.findall(r"\bval\s+(\w+)\s*:\s*PackageManager\s*=", text))


def test_package_manager_is_never_asked_for_application_info() -> None:
    """`PackageManager` has no zero-arg `applicationInfo`.

    Every accessor takes a package name (`getApplicationInfo(String, int)`), so
    `pm.applicationInfo` does not resolve. That is the second compiler error:
    `Unresolved reference: applicationInfo`.
    """
    offences: list[str] = []
    for kt in sorted(KOTLIN_DIR.rglob("*.kt")):
        text = kt.read_text("utf-8")
        for var in _package_manager_vars(text):
            for match in re.finditer(rf"\b{re.escape(var)}\.applicationInfo\b", text):
                line = text.count("\n", 0, match.start()) + 1
                offences.append(f"{kt.relative_to(REPO)}:{line} reads {var}.applicationInfo")
    assert not offences, (
        "PackageManager has no applicationInfo; read it from the Context instead:\n"
        + "\n".join(offences)
    )


def test_app_sdk_target_is_read_from_the_context() -> None:
    text = read(CAPABILITY_PROBE)
    assert re.search(r'"app_sdk_target"\s+to\s+context\.applicationInfo\.targetSdkVersion', text), (
        "app_sdk_target must come from Context.getApplicationInfo().targetSdkVersion"
    )


def test_packagemanager_import_is_still_used() -> None:
    """The unused `pm` local was removed; the import must still earn its place."""
    text = read(CAPABILITY_PROBE)
    assert "import android.content.pm.PackageManager" in text
    assert re.search(r"\bPackageManager\.[A-Z_]+", text), (
        "import android.content.pm.PackageManager is unused; remove it"
    )


def test_probe_still_emits_the_device_keys_the_pc_validator_requires() -> None:
    """`compat.py` rejects a record whose device block lacks these three."""
    emitted = set(re.findall(r'"([a-z_0-9]+)"\s+to\s', read(CAPABILITY_PROBE)))
    for key in ("manufacturer", "model", "sdk_int"):
        assert key in emitted, f"the probe no longer reports device.{key}"
