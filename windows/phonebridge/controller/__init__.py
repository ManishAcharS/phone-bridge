"""The Windows connection controller.

This package is the "one button" layer the CLI could never be: it finds the
phone, picks the best transport, reuses the existing authenticated session, and
survives network changes — then reports all of that as one immutable
:class:`~phonebridge.controller.manager.Snapshot` for a UI to draw.

Nothing here re-implements the protocol. The controller drives the same
:class:`~phonebridge.core.session.BridgeSession` the CLI uses, so pinning, the
SAS prompt, AES-GCM and the version gate behave exactly as they already did.

Layout::

    adb.py           the adb command line, and forward-rule ownership
    transports.py    where the phone might be, ranked (USB > beacon > cached)
    capabilities.py  the five-state capability registry (present/permitted/
                     implemented/available/active)
    manager.py       the single connection state machine
    runtime.py       one asyncio loop in a background thread, for the GUI
    view.py          snapshot -> the strings and flags a widget draws
    ui/app.py        tkinter window (light on logic on purpose)

``ui`` is the only part that needs a display and the only part that imports
tkinter, so everything below it is testable headlessly.
"""

from __future__ import annotations

from .adb import Adb, AdbDevice, AdbError, ForwardLease
from .capabilities import Capability, build_registry, mark_active
from .manager import ConnectionManager, Snapshot, State
from .transports import (
    BeaconCandidates,
    CachedCandidates,
    Candidate,
    CandidateProvider,
    UsbCandidates,
    rank,
    select_first,
)

__all__ = [
    "Adb",
    "AdbDevice",
    "AdbError",
    "BeaconCandidates",
    "CachedCandidates",
    "Candidate",
    "CandidateProvider",
    "Capability",
    "ConnectionManager",
    "ForwardLease",
    "Snapshot",
    "State",
    "UsbCandidates",
    "build_registry",
    "mark_active",
    "rank",
    "select_first",
]
