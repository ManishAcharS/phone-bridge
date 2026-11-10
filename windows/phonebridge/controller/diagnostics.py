"""A diagnostics log that cannot fill the pane with one repeated line.

A reconnect loop produces the same six sentences every few seconds: found,
connecting, missed a heartbeat, reconnecting, found, connecting… A plain append
log turns that into an unreadable wall, and the one line that matters (the
reason) scrolls out of view.

So a line is keyed by its text: the first occurrence keeps its position and its
timestamp, later identical lines only bump a counter. Different text is a new
line. That keeps the order of *events* while collapsing the order of *repetition*.

Nothing here is UI-specific; ``controller/view.py`` renders the result.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class LogLine:
    at: float
    text: str
    count: int = 1
    last_at: float = 0.0

    def render(self) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(self.at))
        if self.count > 1:
            return f"{stamp}  {self.text}  (x{self.count})"
        return f"{stamp}  {self.text}"


class Diagnostics:
    """Bounded, de-duplicated, ordered log."""

    def __init__(self, limit: int = 200, clock: Callable[[], float] | None = None) -> None:
        self._limit = max(1, limit)
        self._clock = clock or time.time
        self._lines: OrderedDict[str, LogLine] = OrderedDict()

    def add(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        now = self._clock()
        existing = self._lines.get(text)
        if existing is not None:
            self._lines[text] = replace(existing, count=existing.count + 1, last_at=now)
            return
        self._lines[text] = LogLine(at=now, text=text, count=1, last_at=now)
        while len(self._lines) > self._limit:
            self._lines.popitem(last=False)

    def clear(self) -> None:
        self._lines.clear()

    def lines(self) -> tuple[LogLine, ...]:
        return tuple(self._lines.values())

    def render(self) -> tuple[str, ...]:
        return tuple(line.render() for line in self._lines.values())

    def __len__(self) -> int:
        return len(self._lines)

    def __iter__(self) -> Iterator[LogLine]:
        return iter(self._lines.values())

    def count_of(self, text: str) -> int:
        line = self._lines.get(text.strip())
        return line.count if line is not None else 0
