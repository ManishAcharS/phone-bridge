"""One asyncio loop in a background thread.

tkinter must own the main thread, and asyncio must own one thread of its own.
This is the seam between them: the GUI calls plain synchronous methods, the
manager runs entirely inside its loop, and every observation arrives as an
immutable :class:`~phonebridge.controller.manager.Snapshot`.

It is deliberately tiny, and it is not tkinter-specific — the tests drive it
headlessly, which is the only way to exercise the threading without a display.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Awaitable, Callable

from .manager import ConnectionManager, Snapshot

log = logging.getLogger(__name__)

ManagerFactory = Callable[[], ConnectionManager]
SnapshotSink = Callable[[Snapshot], None]


class RuntimeUnavailable(RuntimeError):
    """The runtime is not running, or would not start."""


class BackgroundRuntime:
    """Owns the loop, the manager and the thread that runs them."""

    def __init__(
        self,
        factory: ManagerFactory,
        *,
        on_snapshot: SnapshotSink | None = None,
        name: str = "phonebridge-controller",
    ) -> None:
        self._factory = factory
        self._on_snapshot = on_snapshot
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._manager: ConnectionManager | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self, timeout: float = 10.0) -> None:
        if self._thread is not None:
            raise RuntimeUnavailable("the runtime is already started")
        self._thread = threading.Thread(target=self._main, name=self._name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeUnavailable("the connection controller did not start in time")
        if self._start_error is not None:
            raise RuntimeUnavailable(f"the connection controller failed to start: {self._start_error}")

    def _main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            asyncio.set_event_loop(loop)
            manager = self._factory()
            self._manager = manager
            if self._on_snapshot is not None:
                manager.subscribe(self._on_snapshot)
            self._ready.set()
            loop.run_forever()
        except BaseException as exc:  # noqa: BLE001 - surfaced by start()
            self._start_error = exc
            self._ready.set()
        finally:
            started_manager = self._manager
            if started_manager is not None:
                with contextlib.suppress(Exception):
                    loop.run_until_complete(started_manager.aclose())
            with contextlib.suppress(Exception):
                loop.close()

    def stop(self, timeout: float = 10.0) -> None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout)
        if thread.is_alive():  # pragma: no cover - only if a coroutine blocks the loop
            log.warning("the controller thread did not stop cleanly")
        self._thread = None
        self._loop = None

    # ------------------------------------------------------------------ #
    # access
    # ------------------------------------------------------------------ #
    @property
    def manager(self) -> ConnectionManager:
        if self._manager is None:
            raise RuntimeUnavailable("the runtime is not started")
        return self._manager

    @property
    def running(self) -> bool:
        loop = self._loop
        return loop is not None and loop.is_running()

    def snapshot(self) -> Snapshot:
        manager = self._manager
        return manager.snapshot if manager is not None else Snapshot()

    def submit(self, action: Callable[[ConnectionManager], Awaitable[object]]) -> None:
        """Run ``action(manager)`` inside the loop, ignoring its result."""
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeUnavailable("the runtime is not running")

        def schedule() -> None:
            manager = self.manager

            async def run() -> None:
                await action(manager)

            task = loop.create_task(run())

            def done(finished: asyncio.Task[None]) -> None:
                if finished.cancelled():
                    return
                exc = finished.exception()
                if exc is not None:
                    log.error("controller action failed: %s", exc)

            task.add_done_callback(done)

        loop.call_soon_threadsafe(schedule)

    # ------------------------------------------------------------------ #
    # the three commands a window needs
    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        self.submit(lambda manager: manager.connect())

    def disconnect(self) -> None:
        self.submit(lambda manager: manager.disconnect("disconnected by the user"))

    def reconnect(self) -> None:
        self.submit(lambda manager: manager.reconnect())

    def resolve_pairing(self, approve: bool) -> None:
        self.manager.resolve_pairing(approve)

    def wait_until(self, predicate: Callable[[Snapshot], bool], timeout: float = 30.0) -> bool:
        """Block until ``predicate(snapshot)`` holds. For tests and smoke checks."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.snapshot()):
                return True
            time.sleep(0.02)
        return predicate(self.snapshot())

    def wait_for_state(self, *states: str, timeout: float = 30.0) -> bool:
        wanted = set(states)
        return self.wait_until(lambda snap: snap.state.value in wanted, timeout)
