"""Phase 5 sections of the window: Bluetooth, sensors and file transfer.

Three panels, one window. They are deliberately separate classes-in-one-file
rather than one monolith, because each has a different shape:

* **Bluetooth** is a *list and a browser*: scan, pick one device, look at its
  services, read/write/subscribe. Clicking a row is the only way a peripheral is
  ever connected — nothing here auto-connects to something the scan found.
* **Sensors** is a *table of live numbers*: which sensors exist, which are
  streaming, and the newest reading with its sequence number. A sensor that is
  missing is shown as missing rather than hidden.
* **Files** is a *queue*: send, receive, watch, pause, resume, cancel, and the
  integrity verdict that arrives only after the hash verified.

Two rules are shared by all three. Everything that touches the session goes
through :class:`~phonebridge.controller.runtime.BackgroundRuntime`, so the Tk
thread never waits on the network; and every string shown here comes from the
phone's own answer, so a refused operation reads like a sentence, not a code.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any

from .. import planeview as pv


def _fill(tree: ttk.Treeview, rows: list[tuple[str, tuple[Any, ...]]], shown: list[str]) -> bool:
    """Redraw ``tree`` only when the visible rows changed.

    A Treeview rebuild on every UI tick flickers and loses the selection, and a
    live sensor table would rebuild it 12 times a second. Returns whether a
    redraw happened, so the caller can skip re-selecting.
    """
    if shown == [row[0] for row in rows]:
        changed = False
        for iid, values in rows:
            if tuple(tree.item(iid, "values")) != tuple(str(v) for v in values):
                tree.item(iid, values=[str(v) for v in values])
                changed = True
        return changed
    selection = tree.selection()
    for existing in tree.get_children():
        tree.delete(existing)
    for iid, values in rows:
        tree.insert("", "end", iid=iid, values=[str(v) for v in values])
    shown[:] = [row[0] for row in rows]
    keep = [iid for iid in selection if tree.exists(iid)]
    if keep:
        tree.selection_set(keep)
    return True


class PlanesPanel:
    """The three Phase 5 sections, packed into one frame."""

    def __init__(
        self,
        parent: tk.Misc,
        runtime: Any,
        *,
        report: Callable[[str], None] | None = None,
    ) -> None:
        self.runtime = runtime
        self._report = report or (lambda _text: None)
        self.frame = ttk.Frame(parent)

        self._bluetooth_rows: list[str] = []
        self._gatt_rows: list[str] = []
        self._sensor_rows: list[str] = []
        self._file_rows: list[str] = []
        self._notifications_seen = 0
        self._gatt_for: str | None = None

        self._build_bluetooth()
        self._build_sensors()
        self._build_files()

    def tabs(self) -> tuple[tuple[str, tk.Widget], ...]:
        """(title, frame) for each section, so the app can put them in a notebook.

        Three tabs instead of one long column: the window already carries the
        connection, audio and camera sections, and stacking three more panels
        would push the capability table off the bottom of a laptop screen.
        """
        return (
            ("Bluetooth", self._bluetooth_frame),
            ("Sensors", self._sensor_frame),
            ("Files", self._file_frame),
        )

    # ------------------------------------------------------------------ #
    # Bluetooth
    # ------------------------------------------------------------------ #
    def _build_bluetooth(self) -> None:
        frame = ttk.LabelFrame(
            self.frame, text="Bluetooth (the phone is the radio)", padding=8
        )
        frame.pack(fill="both", expand=True)
        self._bluetooth_frame = frame

        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Button(row, text="Scan", command=self._scan).pack(side="left")
        ttk.Button(row, text="Stop scan", command=self._stop_scan).pack(side="left", padx=6)
        self._scan_choice = tk.StringVar(value=pv.SCAN_DURATIONS[1][0])
        ttk.Combobox(
            row,
            textvariable=self._scan_choice,
            values=[label for label, _ms in pv.SCAN_DURATIONS],
            width=12,
            state="readonly",
        ).pack(side="left", padx=(0, 12))

        ttk.Button(row, text="Connect", command=self._connect_picked).pack(side="left")
        ttk.Button(row, text="Disconnect", command=self._disconnect_picked).pack(
            side="left", padx=6
        )
        ttk.Button(row, text="Browse services", command=self._browse).pack(side="left")

        self._adapter_label = tk.Label(
            frame, text="not connected", anchor="w", justify="left", bg="#ffffff",
            fg="#5b6470", wraplength=880,
        )
        self._adapter_label.pack(fill="x", pady=(6, 4))

        columns = ("name", "address", "rssi", "services", "connectable", "seen")
        self._devices = ttk.Treeview(frame, columns=columns, show="headings", height=4)
        for name, heading, width in (
            ("name", "Device", 210),
            ("address", "Address", 150),
            ("rssi", "RSSI", 60),
            ("services", "Advertised services", 230),
            ("connectable", "Connectable", 90),
            ("seen", "Sightings", 70),
        ):
            self._devices.heading(name, text=heading)
            self._devices.column(name, width=width, anchor="w")
        self._devices.pack(fill="x")
        self._devices.bind("<<TreeviewSelect>>", lambda _event: self._device_picked())

        self._gatt_label = tk.Label(
            frame, text="services appear after Connect", anchor="w", justify="left",
            bg="#ffffff", fg="#5b6470", wraplength=880,
        )
        self._gatt_label.pack(fill="x", pady=(6, 2))

        self._gatt = ttk.Treeview(frame, columns=("props",), show="tree headings", height=5)
        self._gatt.heading("#0", text="Service / characteristic")
        self._gatt.heading("props", text="Properties")
        self._gatt.column("#0", width=330)
        self._gatt.column("props", width=190)
        self._gatt.pack(fill="x")
        self._gatt.bind("<<TreeviewSelect>>", lambda _event: self._characteristic_picked())

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(6, 0))
        ttk.Button(actions, text="Read", command=self._read).pack(side="left")
        ttk.Button(actions, text="Subscribe", command=self._subscribe).pack(side="left", padx=6)
        ttk.Button(actions, text="Unsubscribe", command=self._unsubscribe).pack(side="left")
        ttk.Button(actions, text="Write…", command=self._write).pack(side="left", padx=6)
        self._value_label = tk.Label(
            actions, text="", anchor="w", bg="#ffffff", fg="#1c5fa8", wraplength=420
        )
        self._value_label.pack(side="left", padx=(10, 0))

        self._notifications = tk.Text(
            frame, height=3, wrap="none", bg="#fbfbfc", relief="flat",
            font=("TkFixedFont", 9),
        )
        self._notifications.configure(state="disabled")
        self._notifications.pack(fill="x", pady=(6, 0))

    def _picked_device(self) -> str | None:
        selection = self._devices.selection()
        return selection[0] if selection else None

    def _device_picked(self) -> None:
        address = self._picked_device()
        if address is not None:
            self._gatt_for = address

    def _scan_duration(self) -> int:
        label = self._scan_choice.get()
        return next((ms for text, ms in pv.SCAN_DURATIONS if text == label), 10_000)

    def _scan(self) -> None:
        duration = self._scan_duration()
        self._report(
            "scanning for Bluetooth peripherals through the phone"
            if duration
            else "scanning until you press stop"
        )
        self.runtime.start_scan(duration)

    def _stop_scan(self) -> None:
        self.runtime.stop_scan()

    def _connect_picked(self) -> None:
        address = self._picked_device()
        if address is None:
            self._report("pick a device from the scan results first")
            return
        self._gatt_for = address
        self._report(f"connecting to {address} — nothing pairs automatically")
        self.runtime.connect_device(address)

    def _disconnect_picked(self) -> None:
        address = self._picked_device()
        if address is None:
            self._report("pick the connected device first")
            return
        self.runtime.disconnect_device(address)

    def _browse(self) -> None:
        address = self._picked_device()
        if address is None:
            self._report("pick a connected device first")
            return
        self._gatt_for = address
        self.runtime.browse_services(address)

    def _picked_characteristic(self) -> tuple[str, str] | None:
        selection = self._gatt.selection()
        if not selection:
            return None
        iid = selection[0]
        if not iid.startswith("char:"):
            return None
        _prefix, service, characteristic = iid.split(":", 2)
        return service, characteristic

    def _characteristic_picked(self) -> None:
        picked = self._picked_characteristic()
        if picked is not None:
            service, characteristic = picked
            self._value_label.configure(
                text=f"{pv.short_uuid(characteristic)} on {pv.short_uuid(service)}"
            )

    def _read(self) -> None:
        address, picked = self._picked_device(), self._picked_characteristic()
        if address is None or picked is None:
            self._report("pick a characteristic to read")
            return
        service, characteristic = picked
        self.runtime.read_characteristic(address, service, characteristic)

    def _write(self) -> None:
        address, picked = self._picked_device(), self._picked_characteristic()
        if address is None or picked is None:
            self._report("pick a characteristic to write")
            return
        service, characteristic = picked
        dialog = _HexDialog(self.frame, title=f"Write {pv.short_uuid(characteristic)}")
        payload = dialog.payload()
        if payload is None:
            return
        self.runtime.write_characteristic(address, service, characteristic, payload)
        self._report(f"writing {len(payload)} byte(s) to {pv.short_uuid(characteristic)}")

    def _subscribe(self) -> None:
        address, picked = self._picked_device(), self._picked_characteristic()
        if address is None or picked is None:
            self._report("pick a characteristic to subscribe to")
            return
        service, characteristic = picked
        self.runtime.subscribe(address, service, characteristic, True)

    def _unsubscribe(self) -> None:
        address, picked = self._picked_device(), self._picked_characteristic()
        if address is None or picked is None:
            self._report("pick a subscribed characteristic")
            return
        service, characteristic = picked
        self.runtime.subscribe(address, service, characteristic, False)

    def _refresh_bluetooth(self, status: dict[str, Any]) -> None:
        self._adapter_label.configure(text=pv.bluetooth_headline(status))
        _fill(self._devices, pv.device_rows(status), self._bluetooth_rows)

        connections = [c for c in status.get("connections") or () if isinstance(c, dict)]
        connected = {str(c.get("address")): c for c in connections}
        target = self._gatt_for or self._picked_device()
        connection = connected.get(target or "") if target else None
        if connection is None and connected:
            target = next(iter(connected))
            connection = connected[target]
        if connection is None:
            self._gatt_label.configure(
                text=pv.gatt_headline(
                    None,
                    fallback=(
                        "pick a connected device" if connections
                        else "services appear after Connect"
                    ),
                )
            )
            self._fill_gatt([])
            return

        self._gatt_label.configure(text=pv.gatt_headline(connection, fallback=""))
        self._fill_gatt(pv.gatt_rows(connection))

        notifications = [
            note
            for entry in connections
            for note in (entry.get("notifications") or ())
            if isinstance(note, dict)
        ]
        if len(notifications) > self._notifications_seen:
            for note in notifications[self._notifications_seen :]:
                self._append_notification(note)
            self._notifications_seen = len(notifications)

    def _fill_gatt(self, rows: list[tuple[str, str, str, str | None]]) -> None:
        shown = [row[0] for row in rows]
        if shown == self._gatt_rows:
            for iid, text, props, _parent in rows:
                if self._gatt.exists(iid) and self._gatt.item(iid, "text") != text:
                    self._gatt.item(iid, text=text, values=(props,))
            return
        selection = self._gatt.selection()
        for existing in self._gatt.get_children():
            self._gatt.delete(existing)
        for iid, text, props, parent in rows:
            if parent is not None and self._gatt.exists(parent):
                self._gatt.insert(parent, "end", iid=iid, text=text, values=(props,))
            else:
                self._gatt.insert("", "end", iid=iid, text=text, values=(props,), open=True)
        self._gatt_rows = shown
        keep = [iid for iid in selection if self._gatt.exists(iid)]
        if keep:
            self._gatt.selection_set(keep)

    def _append_notification(self, note: dict[str, Any]) -> None:
        self._notifications.configure(state="normal")
        self._notifications.insert("end", pv.notification_line(note) + "\n")
        self._notifications.see("end")
        self._notifications.configure(state="disabled")

    # ------------------------------------------------------------------ #
    # sensors
    # ------------------------------------------------------------------ #
    def _build_sensors(self) -> None:
        frame = ttk.LabelFrame(self.frame, text="Sensors (read on the phone)", padding=8)
        frame.pack(fill="both", expand=True)
        self._sensor_frame = frame

        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Button(row, text="Refresh list", command=self._refresh_sensors).pack(side="left")
        ttk.Button(row, text="Start selected", command=self._start_sensors).pack(
            side="left", padx=6
        )
        ttk.Button(row, text="Stop", command=self._stop_sensors).pack(side="left")
        ttk.Label(row, text="Rate").pack(side="left", padx=(12, 4))
        self._sensor_rate = tk.StringVar(value=pv.SENSOR_RATES[2][0])
        rate = ttk.Combobox(
            row,
            textvariable=self._sensor_rate,
            values=[label for label, _us in pv.SENSOR_RATES],
            width=8,
            state="readonly",
        )
        rate.pack(side="left")
        rate.bind("<<ComboboxSelected>>", lambda _event: self._rate_chosen())

        self._sensor_status = tk.Label(
            frame, text="not started", anchor="w", justify="left", bg="#ffffff",
            fg="#5b6470", wraplength=880,
        )
        self._sensor_status.pack(fill="x", pady=(6, 4))

        columns = ("kind", "state", "rate", "frames", "lost", "reading", "age")
        self._sensors = ttk.Treeview(
            frame, columns=columns, show="headings", height=5, selectmode="extended"
        )
        for name, heading, width in (
            ("kind", "Sensor", 220),
            ("state", "State", 80),
            ("rate", "Measured", 90),
            ("frames", "Frames", 70),
            ("lost", "Lost", 50),
            ("reading", "Newest reading", 250),
            ("age", "Age", 70),
        ):
            self._sensors.heading(name, text=heading)
            self._sensors.column(name, width=width, anchor="w")
        self._sensors.pack(fill="x")

    def _refresh_sensors(self) -> None:
        self.runtime.refresh_sensor_list()

    def _rate_chosen(self) -> None:
        label = self._sensor_rate.get()
        interval = next((us for text, us in pv.SENSOR_RATES if text == label), 20_000)
        self.runtime.set_sensor_interval(interval)
        self._report(
            f"sampling every {interval / 1000:.1f} ms — takes effect on the next start"
        )

    def _start_sensors(self) -> None:
        selection = [iid for iid in self._sensors.selection() if iid.startswith("sensor:")]
        ids = [int(iid.split(":", 1)[1]) for iid in selection]
        if not ids:
            self._report("tick one or more sensors first — nothing is streamed by default")
            return
        self.runtime.start_sensors(ids)

    def _stop_sensors(self) -> None:
        self.runtime.stop_sensors()

    def _refresh_sensor_table(self, status: dict[str, Any]) -> None:
        self._sensor_status.configure(text=pv.sensor_headline(status))
        _fill(self._sensors, pv.sensor_rows(status), self._sensor_rows)

    # ------------------------------------------------------------------ #
    # files
    # ------------------------------------------------------------------ #
    def _build_files(self) -> None:
        frame = ttk.LabelFrame(
            self.frame, text="Files (through the phone, verified)", padding=8
        )
        frame.pack(fill="both", expand=True)
        self._file_frame = frame

        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Button(row, text="Send file…", command=self._send_file).pack(side="left")
        ttk.Button(row, text="Receive…", command=self._receive_file).pack(side="left", padx=6)
        ttk.Button(row, text="Pause", command=lambda: self._file_action("pause")).pack(side="left")
        ttk.Button(row, text="Resume", command=lambda: self._file_action("resume")).pack(
            side="left", padx=6
        )
        ttk.Button(row, text="Cancel", command=lambda: self._file_action("cancel")).pack(
            side="left"
        )
        ttk.Button(row, text="Save incoming to…", command=self._choose_destination).pack(
            side="left", padx=(12, 0)
        )

        self._files_status = tk.Label(
            frame, text="no transfers yet", anchor="w", justify="left", bg="#ffffff",
            fg="#5b6470", wraplength=880,
        )
        self._files_status.pack(fill="x", pady=(6, 4))

        columns = ("name", "direction", "progress", "bytes", "speed", "state", "integrity")
        self._files = ttk.Treeview(frame, columns=columns, show="headings", height=5)
        for name, heading, width in (
            ("name", "File", 240),
            ("direction", "Direction", 90),
            ("progress", "Progress", 110),
            ("bytes", "Bytes", 130),
            ("speed", "Speed", 90),
            ("state", "State", 100),
            ("integrity", "Integrity", 150),
        ):
            self._files.heading(name, text=heading)
            self._files.column(name, width=width, anchor="w")
        self._files.pack(fill="x")

    def _picked_transfer(self) -> int | None:
        selection = self._files.selection()
        if not selection:
            return None
        try:
            return int(selection[0].split(":", 1)[1])
        except (IndexError, ValueError):
            return None

    def _file_action(self, action: str) -> None:
        transfer_id = self._picked_transfer()
        if transfer_id is None:
            self._report("pick a transfer first")
            return
        if action == "pause":
            self.runtime.pause_transfer(transfer_id)
        elif action == "resume":
            self.runtime.resume_transfer(transfer_id)
        else:
            self.runtime.cancel_transfer(transfer_id)

    def _send_file(self) -> None:
        paths = filedialog.askopenfilenames(title="Send to the phone")
        if not paths:
            return
        for path in paths:
            self._report(f"sending {Path(path).name}")
            self.runtime.send_file(path)

    def _receive_file(self) -> None:
        name = _TextDialog(
            self.frame, title="Receive a file", prompt="File name on the phone (blank = the newest)"
        ).result()
        if name is None:
            return
        self.runtime.receive_file(name.strip())

    def _choose_destination(self) -> None:
        directory = filedialog.askdirectory(title="Where should incoming files go?")
        if not directory:
            return
        self.runtime.set_files_destination(directory)
        self._report(f"incoming files will be saved in {directory}")

    def _refresh_files(self, status: dict[str, Any]) -> None:
        _fill(self._files, pv.file_rows(status), self._file_rows)
        self._files_status.configure(text=pv.files_headline(status))

    # ------------------------------------------------------------------ #
    # one tick
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """Redraw all three panels. Reads manager state; never touches the network."""
        for name, draw in (
            ("Bluetooth", lambda: self._refresh_bluetooth(self.runtime.bluetooth_status())),
            ("Sensors", lambda: self._refresh_sensor_table(self.runtime.sensor_status())),
            ("Files", lambda: self._refresh_files(self.runtime.files_status())),
        ):
            try:
                draw()
            except Exception as exc:  # noqa: BLE001 - a panel must not kill the window
                self._report(f"the {name} panel could not draw: {exc}")


class _TextDialog:
    """A one-line question, modal to the window, returning ``None`` on cancel."""

    def __init__(self, parent: tk.Misc, *, title: str, prompt: str) -> None:
        self._value: str | None = None
        self._window = tk.Toplevel(parent)
        self._window.title(title)
        self._window.transient(parent.winfo_toplevel())
        ttk.Label(self._window, text=prompt, padding=8).pack(anchor="w")
        self._entry = ttk.Entry(self._window, width=48)
        self._entry.pack(padx=8, fill="x")
        self._entry.bind("<Return>", lambda _event: self._accept())
        buttons = ttk.Frame(self._window, padding=8)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="OK", command=self._accept).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self._close).pack(side="right", padx=6)

    def _accept(self) -> None:
        self._value = self._entry.get()
        self._close()

    def _close(self) -> None:
        self._window.grab_release()
        self._window.destroy()

    def result(self) -> str | None:
        self._entry.focus_set()
        self._window.grab_set()
        self._window.wait_window()
        return self._value


class _HexDialog(_TextDialog):
    """Ask for bytes as hex, and refuse text that is not hex.

    Writing a characteristic is a real side effect on a real peripheral, so the
    dialog validates before anything is sent: "12 34" and "1234" both work, "abc"
    gives an error instead of a silent zero byte.
    """

    def __init__(self, parent: tk.Misc, *, title: str) -> None:
        self._error: str | None = None
        super().__init__(parent, title=title, prompt="Bytes to write, as hex (e.g. 01 ff)")

    def _accept(self) -> None:
        text = self._entry.get().strip().replace(" ", "").replace("0x", "")
        if len(text) % 2:
            self._error = "an odd number of hex digits is not a byte string"
            self._entry.configure(foreground="#b3261e")
            return
        try:
            self._payload = bytes.fromhex(text)
        except ValueError:
            self._error = "that is not hexadecimal"
            self._entry.configure(foreground="#b3261e")
            return
        self._value = text
        self._close()

    def payload(self) -> bytes | None:
        """The accepted bytes, or ``None`` when the dialog was cancelled."""
        self.result()
        return getattr(self, "_payload", None)

    @property
    def error(self) -> str | None:
        """Why the last attempt was refused, for the caller to log if it cares."""
        return self._error

