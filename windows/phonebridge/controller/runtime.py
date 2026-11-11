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

    # ---- Phase 2 audio --------------------------------------------------- #
    def start_audio_playback(self) -> None:
        """Play the PC's audio on the phone. Never called automatically."""
        self.submit(lambda manager: manager.start_audio_playback())

    def stop_audio_playback(self) -> None:
        self.submit(lambda manager: manager.stop_audio_playback())

    def start_mic(self) -> None:
        """Ask the phone for its microphone (the phone may refuse; it says why)."""
        self.submit(lambda manager: manager.start_mic())

    def stop_mic(self) -> None:
        self.submit(lambda manager: manager.stop_mic())

    def set_audio_mute(self, muted: bool) -> None:
        self.submit(lambda manager: manager.set_audio_mute(muted))

    def set_audio_volume(self, percent: int) -> None:
        self.submit(lambda manager: manager.set_audio_volume(percent))

    # ---- Phase 4 camera -------------------------------------------------- #
    def start_camera(self) -> None:
        """Start streaming. Never called automatically: a camera needs a user."""
        self.submit(lambda manager: manager.start_camera())

    def stop_camera(self) -> None:
        self.submit(lambda manager: manager.stop_camera())

    def switch_camera(self) -> None:
        self.submit(lambda manager: manager.switch_to_other_lens())

    def choose_camera_config(self, width: int, height: int, fps: int) -> None:
        """Apply a resolution/rate choice; it takes effect on the next start."""
        manager = self.manager
        if manager is not None:
            manager.set_camera_config(width=width, height=height, fps=fps)

    def camera_frame(self) -> object:
        manager = self.manager
        return manager.camera_frame() if manager is not None else None

    def camera_status(self) -> dict[str, object]:
        manager = self.manager
        return manager.camera_status() if manager is not None else {}

    def audio_status(self) -> dict[str, object]:
        """A snapshot the Audio section can redraw without touching the loop."""
        manager = self._manager
        return manager.audio_status() if manager is not None else {}

    # ---- Phase 5: Bluetooth, sensors, files ------------------------------ #
    def bluetooth_status(self) -> dict[str, object]:
        manager = self._manager
        return manager.bluetooth_status() if manager is not None else {}

    def sensor_status(self) -> dict[str, object]:
        manager = self._manager
        return manager.sensor_status() if manager is not None else {}

    def files_status(self) -> dict[str, object]:
        manager = self._manager
        return manager.files_status() if manager is not None else {}

    def set_files_destination(self, directory: str) -> None:
        manager = self._manager
        if manager is not None:
            manager.set_files_destination(directory)

    def set_sensor_interval(self, interval_us: int) -> None:
        manager = self._manager
        if manager is not None:
            manager.set_sensor_interval(interval_us)

    def start_scan(self, duration_ms: int = 10_000) -> None:
        """Scan on the phone. Nothing is connected; results are just a list."""
        self.submit(lambda manager: manager.start_scan(duration_ms=duration_ms))

    def stop_scan(self) -> None:
        self.submit(lambda manager: manager.stop_scan())

    def connect_device(self, address: str) -> None:
        """Connect GATT to *this* peripheral, because the user picked it."""
        self.submit(lambda manager: manager.connect_device(address))

    def disconnect_device(self, address: str) -> None:
        self.submit(lambda manager: manager.disconnect_device(address))

    def browse_services(self, address: str) -> None:
        self.submit(lambda manager: manager.gatt_services(address))

    def read_characteristic(self, address: str, service: str, characteristic: str) -> None:
        self.submit(lambda manager: manager.gatt_read(address, service, characteristic))

    def write_characteristic(
        self, address: str, service: str, characteristic: str, data: bytes
    ) -> None:
        self.submit(
            lambda manager: manager.gatt_write(address, service, characteristic, data)
        )

    def subscribe(self, address: str, service: str, characteristic: str, enable: bool) -> None:
        self.submit(
            lambda manager: manager.gatt_subscribe(
                address, service, characteristic, enable=enable
            )
        )

    def refresh_sensor_list(self) -> None:
        """Ask the phone which sensors it has. Cheap, and never starts a stream."""

        async def run(manager: ConnectionManager) -> None:
            if manager.sensors is not None:
                await manager.sensors.refresh()

        self.submit(run)

    def start_sensors(self, sensor_ids: list[int] | None = None) -> None:
        self.submit(lambda manager: manager.start_sensors(sensor_ids))

    def stop_sensors(self) -> None:
        self.submit(lambda manager: manager.stop_sensors())

    def send_file(self, path: str) -> None:
        self.submit(lambda manager: manager.send_file(path))

    def receive_file(self, name_hint: str = "") -> None:
        self.submit(lambda manager: manager.request_file(name_hint))

    def pause_transfer(self, transfer_id: int) -> None:
        self.submit(lambda manager: manager.pause_transfer(transfer_id))

    def resume_transfer(self, transfer_id: int) -> None:
        self.submit(lambda manager: manager.resume_transfer(transfer_id))

    def cancel_transfer(self, transfer_id: int) -> None:
        self.submit(lambda manager: manager.cancel_transfer(transfer_id))

    def diagnose_usb(self) -> None:
        """Run the whole USB chain in the controller thread and log the report."""
        self.submit(lambda manager: manager.diagnose_usb())

    def set_endpoint(self, host: str, port: int | None = None) -> None:
        """Point the controller at an address the user typed in by hand."""

        async def apply(manager: ConnectionManager) -> None:
            manager.set_manual_endpoint(host, port)

        self.submit(apply)

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
