"""One PowerShell pitfall a Python test can catch without PowerShell.

A single-quoted string in PowerShell is delimited by ``'`` and **cannot** contain
an apostrophe: ``'the microphone's cable'`` ends the string at ``microphone`` and
the rest of the line becomes a syntax error. The whole script then fails to parse,
which is silent — ``pwsh -File script.ps1`` prints nothing on stdout and exits 1,
so a launcher looks like it "did nothing" and CI only sees a bare exit code.

That is exactly how ``scripts/setup-windows.ps1`` broke once: a step title about
"the microphone's cable" made a report-only installer unparseable, and nothing
here compiles or parses PowerShell. This module is the cheap half of that gap.

What it checks, per line:

* the number of single quotes **outside double-quoted strings and comments** is
  even (an odd count means a string that does not close);
* here-string bodies (``@'`` … ``'@``) are skipped, because their content is
  literal by definition.

What it cannot check: braces in strings, ``$`` interpolation, or anything else.
It is a guard against one repeated mistake, not a parser.
"""

from __future__ import annotations

__all__ = ["odd_quote_lines"]


def odd_quote_lines(text: str) -> list[tuple[int, str]]:
    """Lines whose single quotes do not pair up, with 1-based line numbers.

    ``''`` (the escape for a literal apostrophe) is two quotes, so it stays even;
    double-quoted strings may contain apostrophes freely and are skipped; a
    trailing ``#`` comment is skipped too.
    """
    offenders: list[tuple[int, str]] = []
    in_here_string = False
    in_block_comment = False
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        # A comment-based help block (``<# ... #>``) is prose, apostrophes and all.
        if in_block_comment:
            if "#>" in line:
                in_block_comment = False
            continue
        if "<#" in line:
            if "#>" not in line.split("<#", 1)[1]:
                in_block_comment = True
            continue
        if in_here_string:
            # The terminator is `'@` at the start of a line, on its own.
            if stripped == "'@":
                in_here_string = False
            continue
        if stripped.endswith("@'") or stripped == "@'":
            in_here_string = True
            continue
        quotes = 0
        in_double = False
        index = 0
        while index < len(line):
            char = line[index]
            if in_double:
                if char == "`":  # a backtick escapes the next character
                    index += 2
                    continue
                if char == '"':
                    in_double = False
            elif char == '"':
                in_double = True
            elif char == "'":
                quotes += 1
            elif char == "#":
                break  # rest of the line is a comment
            elif char == "`":
                index += 2
                continue
            index += 1
        if quotes % 2:
            offenders.append((number, line))
    return offenders
