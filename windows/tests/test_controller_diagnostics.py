"""The diagnostics log a user actually reads.

Two properties matter and both are easy to get wrong:

* a message that repeats must not push everything else off the panel, because the
  *first* place an error appeared is usually the useful one;
* a message that repeats must still be visibly repeating, because a silent
  ``x40`` looks identical to "it happened once" when the user is skimming.
"""

from __future__ import annotations

import re
import time

import pytest

from phonebridge.controller import diagnostics as diagnostics_module
from phonebridge.controller.diagnostics import Diagnostics, LogLine, local_stamp

STAMP = re.compile(r"^\d{2}:\d{2}:\d{2}\b")


def render(diag: Diagnostics) -> list[str]:
    return list(diag.render())


def test_first_line_keeps_its_time_and_its_place() -> None:
    diag = Diagnostics(clock=lambda: 1000.0)
    diag.add("connecting")
    diag.add("connected")
    diag.add("connecting")
    rows = render(diag)
    assert rows[0].endswith("connecting  (x2)")
    assert rows[1].endswith("connected")
    assert len(rows) == 2


def test_a_repeat_does_not_reset_the_timestamp() -> None:
    clock = iter([1000.0, 1005.0])
    diag = Diagnostics(clock=lambda: next(clock))
    diag.add("same message")
    diag.add("same message")
    line = diag.lines()[0]
    assert line.at == 1000.0
    assert line.count == 2
    assert line.last_at == 1005.0


def test_the_panel_is_bounded_and_drops_the_oldest() -> None:
    diag = Diagnostics(limit=3, clock=lambda: 0.0)
    for index in range(10):
        diag.add(f"line {index}")
    rows = render(diag)
    assert len(rows) == 3
    assert "line 9" in rows[-1]
    assert "line 0" not in "\n".join(rows)
    assert len(diag) == 3


def test_every_line_is_timestamped() -> None:
    """The stamp is the machine's **local** clock, not a hardcoded UTC value.

    This used to assert ``00:00:00``, which only holds on a UTC runner. On the
    maintainer's Windows PC (UTC+05:30) the same line reads ``05:30:00`` — that
    observation is the bug this test now pins: the expectation must be derived
    from the same clock the code uses, and the shape must be checked separately.
    """
    diag = Diagnostics(clock=lambda: 0.0)
    diag.add("hello")
    line = render(diag)[0]

    assert line.startswith(local_stamp(0.0))
    assert STAMP.match(line), f"{line!r} does not start with HH:MM:SS"
    assert line.endswith("hello")


def test_the_default_stamp_is_localtime_and_would_not_follow_a_switch_to_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force a two-hour gap between local and UTC: the local clock must win.

    Deterministic on every platform, including Windows where ``time.tzset`` does
    not exist — it replaces the two functions instead of the timezone.
    """
    local = time.struct_time((2026, 11, 10, 9, 41, 7, 1, 314, 0))
    utc = time.struct_time((2026, 11, 10, 4, 11, 7, 1, 314, 0))
    monkeypatch.setattr(diagnostics_module.time, "localtime", lambda _at: local)
    monkeypatch.setattr(diagnostics_module.time, "gmtime", lambda _at: utc)

    assert local_stamp(0.0) == "09:41:07"
    diag = Diagnostics(clock=lambda: 0.0)
    diag.add("plugged the cable in")
    assert render(diag)[0].startswith("09:41:07")


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is POSIX-only")
def test_a_non_utc_zone_renders_local_wall_clock_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduces the Windows report exactly: epoch 0 in UTC+05:30 is 05:30:00."""
    original = time.tzset
    monkeypatch.setenv("TZ", "IST-5:30")
    original()
    try:
        assert local_stamp(0.0) == "05:30:00"
        diag = Diagnostics(clock=lambda: 0.0)
        diag.add("hello")
        assert render(diag)[0].startswith("05:30:00")
    finally:
        monkeypatch.undo()
        original()


def test_a_test_can_inject_its_own_stamp() -> None:
    diag = Diagnostics(clock=lambda: 0.0, stamp=lambda _at: "00:00:00")
    diag.add("hello")
    assert render(diag)[0].startswith("00:00:00")
    # and the repeat keeps the same injected stamp
    diag.add("hello")
    assert render(diag)[0].endswith("hello  (x2)")


def test_blank_messages_are_ignored() -> None:
    diag = Diagnostics()
    diag.add("")
    diag.add("   ")
    assert diag.render() == ()


def test_count_of_tells_the_caller_how_often_it_happened() -> None:
    diag = Diagnostics(clock=lambda: 0.0)
    diag.add("boom")
    diag.add("boom")
    assert diag.count_of("boom") == 2
    assert diag.count_of("never seen") == 0


def test_a_line_renders_itself() -> None:
    line = LogLine(at=1.0, text="x", count=1)
    assert line.render().endswith("x")
    assert line.render().startswith(local_stamp(1.0))
    assert LogLine(at=1.0, text="x", count=3).render().endswith("(x3)")


def test_a_line_accepts_a_custom_stamp_function() -> None:
    line = LogLine(at=1.0, text="x", count=2)
    assert line.render(lambda _at: "23:59:59") == "23:59:59  x  (x2)"


def test_clear_empties_the_panel() -> None:
    diag = Diagnostics()
    diag.add("something")
    diag.clear()
    assert diag.render() == ()
