"""Find Kotlin declarations that the compiler rejects but a review cannot see.

The Android app is built on a Windows machine; this test suite runs anywhere.
That asymmetry is how `enum class is not allowed here` reached a real Gradle
build: nothing here compiles Kotlin, so nothing here noticed.

Kotlin forbids **any** class-like declaration nested directly inside an
`inner class` body. Verified against kotlinc 1.9.24 (the version this project
pins) with minimal repros::

    class O { inner class I { enum class S { A } } }   error: enum class is not allowed here
    class O { inner class I { object S } }             error: object is not allowed here
    class O { inner class I { interface S } }          error: interface is not allowed here
    class O { inner class I { companion object {} } }  error: companion object is not allowed here
    class O { inner class I { class S } }              error: class is not allowed here
    class O { inner class I { inner class S } }        ok   (an inner member is fine)

The reason is not arbitrary: an `inner` class only exists alongside an instance
of its enclosing class, while enum entries, objects and interfaces are static
singletons, so no such declaration has a meaning there.

What is *not* an error, and must not be reported here: a local class inside a
method body of an inner class (legal), and an anonymous `object : Runnable {}`
in an initialiser (legal). Only **named member declarations at the inner
class's own member level** are rejected, which is exactly what this scanner
reports: it counts brace depth inside the inner class body, so member level is
depth 0 and everything inside a method or lambda is deeper.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Declaration",
    "declarations_in_inner_class",
    "declarations_nested_in_inner_class",
    "mask_source",
    "scan_file",
]


@dataclass(frozen=True)
class Declaration:
    """A class-like declaration found where Kotlin does not allow it."""

    line: int
    kind: str  # "class", "enum class", "interface", "object", "companion object"
    name: str | None
    inner_class: str  # the enclosing `inner class` it was declared in

    def __str__(self) -> str:
        label = f"{self.kind} {self.name}".strip()
        return f"line {self.line}: {label} inside inner class {self.inner_class}"


# --------------------------------------------------------------------------- #
# masking: comments and string literals are not code
# --------------------------------------------------------------------------- #
def mask_source(text: str) -> str:
    """Blank out comments and string/char literals, preserving offsets and lines.

    Replacing (rather than deleting) keeps every index and line number valid, so
    diagnostics can point at the real file. Kotlin block comments nest, and raw
    strings may contain braces and apostrophes, so a regex cannot do this.
    """
    out = list(text)
    n = len(text)
    i = 0

    def blank(start: int, stop: int) -> None:
        for k in range(start, min(stop, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
        elif text.startswith("/*", i):
            depth = 0
            end = i
            while end < n:
                if text.startswith("/*", end):
                    depth += 1
                    end += 2
                elif text.startswith("*/", end):
                    depth -= 1
                    end += 2
                    if depth == 0:
                        break
                else:
                    end += 1
        elif text.startswith('"""', i):
            end = text.find('"""', i + 3)
            end = n if end < 0 else end + 3
        elif text[i] in ('"', "'"):
            quote = text[i]
            end = i + 1
            while end < n:
                if text[end] == "\\":
                    end += 2
                    continue
                if text[end] == quote:
                    end += 1
                    break
                if text[end] == "\n":  # unterminated; stop at end of line
                    break
                end += 1
        else:
            i += 1
            continue
        blank(i, end)
        i = end
    return "".join(out)


# --------------------------------------------------------------------------- #
# declarations
# --------------------------------------------------------------------------- #
_MODIFIERS = frozenset(
    {
        "public",
        "private",
        "internal",
        "protected",
        "open",
        "abstract",
        "sealed",
        "data",
        "inner",
        "enum",
        "annotation",
        "value",
        "inline",
        "companion",
        "actual",
        "expect",
    }
)

_KEYWORD = re.compile(r"\b(class|interface|object)\b")
_INNER_CLASS = re.compile(r"\binner\s+(?:data\s+|sealed\s+)?class\s+([A-Za-z_]\w*)")
_NAME_AFTER = re.compile(r"\s*([A-Za-z_]\w*)")

#: A brace directly behind one of these is not the declaration's body: it is a
#: lambda default argument (`= { 0 }`), a supertype/constructor argument, a
#: lambda call, or the ``{`` of an anonymous object.
_NOT_A_BODY_AFTER = "=(,>:"


def _modifiers_before(masked: str, pos: int) -> list[str]:
    """Modifier words immediately preceding the keyword at `pos`."""
    line_start = masked.rfind("\n", 0, pos) + 1
    found: list[str] = []
    for word in reversed(masked[line_start:pos].split()):
        if word in _MODIFIERS:
            found.append(word)
        else:
            break
    return found


def _kind_of(masked: str, match: re.Match[str]) -> str:
    keyword = match.group(1)
    if keyword == "object":
        return "companion object" if "companion" in _modifiers_before(masked, match.start()) else "object"
    if keyword == "interface":
        return "interface"
    return "enum class" if "enum" in _modifiers_before(masked, match.start()) else "class"


def _body_open(masked: str, start: int) -> int | None:
    """Index of the `{` that opens the declaration body at `start`.

    Skips braces that belong to something else (see [_NOT_A_BODY_AFTER]) and
    returns None for a body-less declaration or when the enclosing body ends
    first.
    """
    paren = 0
    brace = 0
    for i in range(start, len(masked)):
        ch = masked[i]
        if ch in "([":
            paren += 1
        elif ch in ")]":
            paren = max(0, paren - 1)
        elif ch == "{":
            previous = masked[:i].rstrip()[-1:]
            if paren == 0 and brace == 0 and previous not in _NOT_A_BODY_AFTER:
                return i
            brace += 1
        elif ch == "}":
            if brace == 0:
                return None  # enclosing body closed without opening ours
            brace -= 1
    return None


def _matching_brace(masked: str, open_index: int) -> int:
    depth = 0
    for i in range(open_index, len(masked)):
        if masked[i] == "{":
            depth += 1
        elif masked[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(masked)


def declarations_in_inner_class(source: str) -> list[Declaration]:
    """Class-like declarations at member level of any `inner class` body."""
    masked = mask_source(source)
    found: list[Declaration] = []

    for inner in _INNER_CLASS.finditer(masked):
        inner_name = inner.group(1)
        body = _body_open(masked, inner.end())
        if body is None:
            continue
        body_end = _matching_brace(masked, body)
        body_start = body + 1

        for match in _KEYWORD.finditer(masked, body_start, body_end):
            # Member level = no brace has been opened or closed since the body.
            if masked.count("{", body_start, match.start()) != masked.count("}", body_start, match.start()):
                continue  # inside a method body or lambda: a local class is legal
            if masked[match.start() - 2 : match.start()].endswith("::"):
                continue  # class literal: Foo::class
            if masked[match.start() - 1 : match.start()] == ".":
                continue  # qualified reference: Foo.class
            modifiers = set(_modifiers_before(masked, match.start()))
            kind = _kind_of(masked, match)
            name_match = _NAME_AFTER.match(masked, match.end())
            name = name_match.group(1) if name_match else None
            if kind == "class" and "inner" in modifiers:
                continue  # the one nesting Kotlin allows
            if kind == "object" and name is None:
                continue  # anonymous `object : Runnable { ... }` initialiser
            if kind == "class" and name is None:
                continue  # not a declaration
            found.append(
                Declaration(
                    line=masked.count("\n", 0, match.start()) + 1,
                    kind=kind,
                    name=name,
                    inner_class=inner_name,
                )
            )
    return found


def scan_file(path: Path) -> list[Declaration]:
    """Declarations nested in an `inner class` in one file."""
    return declarations_in_inner_class(path.read_text("utf-8"))


def declarations_nested_in_inner_class(kotlin_root: Path) -> dict[Path, list[Declaration]]:
    """Every offending declaration in a source tree, keyed by file."""
    return {
        path: found
        for path in sorted(kotlin_root.rglob("*.kt"))
        if (found := scan_file(path))
    }
