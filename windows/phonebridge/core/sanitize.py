"""Make peer-supplied text safe to display, log and store.

Every human-readable string in protocol v1 (``name``, ids, beacon fields) is
chosen by the *other* side.  A peer that is hostile -- or merely buggy -- can
send a 4 KiB name full of ANSI escape sequences, newlines or NUL bytes.  Those
strings end up in three places that must not be attacker-controlled:

* a terminal (ANSI escapes can rewrite the line the user is reading),
* a log file (newlines forge log entries),
* ``peers.json`` (a stored label is displayed on every later run).

One helper, used at the boundary where the text enters the program, keeps all
three safe.  It never changes how a legitimate name looks: real device names are
short printable text.
"""

from __future__ import annotations

import re

#: CSI / OSC / two-byte escape sequences, i.e. everything that can move a
#: cursor, change colours or retitle a window.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

#: C0 controls (including NUL, BEL, CR, LF, ESC) and DEL, plus C1 controls.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")

_WHITESPACE = re.compile(r"[ \t\u00a0]+")

#: Longest label we keep.  Windows computer names cap at 63 chars, Android
#: device names are far shorter.
MAX_LABEL = 64


def device_label(value: object, *, limit: int = MAX_LABEL, fallback: str = "unnamed") -> str:
    """A peer-supplied label, safe for a terminal, a log and a JSON file.

    ``value`` may be any JSON type: a number is stringified, ``None`` becomes
    ``fallback``.  The result contains no control characters, no escape
    sequences and no leading/trailing whitespace, and is at most ``limit``
    characters long.
    """
    if value is None:
        return fallback
    text = value if isinstance(value, str) else str(value)
    text = _ANSI.sub("", text)
    text = _CONTROL.sub("", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return fallback
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text or fallback


def printable(value: object, *, limit: int = 200) -> str:
    """Like [device_label] but for arbitrary detail text (close reasons, errors).

    Used where a peer-controlled string is echoed into an error message: the
    length is capped harder than a name and control characters are removed, but
    punctuation is left alone so the message stays readable.
    """
    return device_label(value, limit=limit, fallback="")
