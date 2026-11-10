"""The diagnostics log a user actually reads.

Two properties matter and both are easy to get wrong:

* a message that repeats must not push everything else off the panel, because the
  *first* place an error appeared is usually the useful one;
* a message that repeats must still be visibly repeating, because a silent
  ``x40`` looks identical to "it happened once" when the user is skimming.
"""

from __future__ import annotations

from phonebridge.controller.diagnostics import Diagnostics, LogLine


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
    diag = Diagnostics(clock=lambda: 0.0)
    diag.add("hello")
    assert render(diag)[0].startswith("00:00:00")


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
    assert LogLine(at=1.0, text="x", count=3).render().endswith("(x3)")


def test_clear_empties_the_panel() -> None:
    diag = Diagnostics()
    diag.add("something")
    diag.clear()
    assert diag.render() == ()
