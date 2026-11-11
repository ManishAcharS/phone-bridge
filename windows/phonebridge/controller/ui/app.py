"""The tkinter window: layout only.

Everything that can be decided without a widget lives in ``view.py``; this file
puts those strings on the screen and turns three buttons into three calls on
:class:`~phonebridge.controller.runtime.BackgroundRuntime`.

tkinter is deliberate: it ships with CPython on Windows, needs no dependency, and
the whole product is one window with a table and a log. A toolkit that has to be
downloaded and version-matched would be a worse trade for a connection manager.
"""

from __future__ import annotations

import logging
import queue
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any

from ...core.device_store import DeviceStore
from ..endpoints import describe_endpoint
from ..manager import ConnectionManager, Snapshot
from ..runtime import BackgroundRuntime
from ..view import (
    TONE_BUSY,
    TONE_ERROR,
    TONE_IDLE,
    TONE_OK,
    TONE_WARN,
    EndpointError,
    View,
    build_view,
    parse_endpoint,
)

log = logging.getLogger(__name__)

TITLE = "PhoneBridge"
SUBTITLE = "One connection between this PC and your phone"

PALETTE = {
    TONE_OK: "#1b7f3b",
    TONE_BUSY: "#1c5fa8",
    TONE_WARN: "#a86a00",
    TONE_ERROR: "#b3261e",
    TONE_IDLE: "#5b6470",
}
BACKGROUND = "#f4f5f7"
CARD = "#ffffff"


class PhoneBridgeApp:
    """The window, wired to a background runtime."""

    def __init__(
        self,
        root: tk.Tk,
        store: DeviceStore,
        *,
        auto_connect: bool = False,
        discovery_timeout: float = 3.0,
        endpoint: str | None = None,
    ) -> None:
        self.root = root
        self.store = store
        self.auto_connect = auto_connect
        self._queue: queue.Queue[Snapshot] = queue.Queue()
        self._rows: tuple[Any, ...] = ()
        self._last_view: View | None = None

        self.runtime = BackgroundRuntime(
            lambda: ConnectionManager(
                store,
                discovery_timeout=discovery_timeout,
                session_factory=None,
            ),
            on_snapshot=self._queue.put,
        )

        self._build()
        self.runtime.start()
        self.root.after(80, self._pump)
        if endpoint:
            self.endpoint_var.set(endpoint)
            self._use_endpoint()
        elif auto_connect:
            self.runtime.connect()

    # ------------------------------------------------------------------ #
    # layout
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = self.root
        root.title(TITLE)
        root.geometry("960x720")
        root.minsize(820, 600)
        root.configure(bg=BACKGROUND)

        base = tkfont.nametofont("TkDefaultFont")
        self._title_font = base.copy()
        self._title_font.configure(size=17, weight="bold")
        self._state_font = base.copy()
        self._state_font.configure(size=13, weight="bold")
        self._mono = tkfont.nametofont("TkFixedFont").copy()
        self._mono.configure(size=9)

        outer = ttk.Frame(root, padding=14)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        tk.Label(
            header, text=TITLE, font=self._title_font, bg=BACKGROUND, fg="#111418"
        ).pack(side="left")
        tk.Label(
            header, text=SUBTITLE, bg=BACKGROUND, fg="#5b6470"
        ).pack(side="left", padx=(10, 0), pady=(6, 0))
        self.state_label = tk.Label(
            header, text="Disconnected", font=self._state_font, bg=BACKGROUND, fg=PALETTE[TONE_IDLE]
        )
        self.state_label.pack(side="right")

        self.message_label = tk.Label(
            outer, text="", anchor="w", justify="left", bg=BACKGROUND, fg="#5b6470", wraplength=880
        )
        self.message_label.pack(fill="x", pady=(6, 10))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        self.connect_button = ttk.Button(buttons, text="Connect", command=self._connect)
        self.connect_button.pack(side="left")
        self.disconnect_button = ttk.Button(buttons, text="Disconnect", command=self._disconnect)
        self.disconnect_button.pack(side="left", padx=6)
        self.reconnect_button = ttk.Button(buttons, text="Reconnect", command=self._reconnect)
        self.reconnect_button.pack(side="left")

        manual = ttk.Frame(outer)
        manual.pack(fill="x", pady=(10, 0))
        ttk.Label(manual, text="Phone address").pack(side="left")
        self.endpoint_var = tk.StringVar()
        self.endpoint_entry = ttk.Entry(manual, textvariable=self.endpoint_var, width=22)
        self.endpoint_entry.pack(side="left", padx=(8, 6))
        self.endpoint_entry.bind("<Return>", lambda _event: self._use_endpoint())
        self.endpoint_button = ttk.Button(
            manual, text="Use this address", command=self._use_endpoint
        )
        self.endpoint_button.pack(side="left")
        ttk.Button(manual, text="Clear", command=self._clear_endpoint).pack(side="left", padx=6)
        tk.Label(
            manual,
            text="for when the beacon is blocked — IP, or IP:port",
            bg=BACKGROUND,
            fg="#8a929c",
        ).pack(side="left", padx=(6, 0))

        self.pairing_frame = tk.Frame(outer, bg="#fff4d6", highlightthickness=1,
                                      highlightbackground="#e2c274")
        tk.Label(
            self.pairing_frame, text="Pairing", bg="#fff4d6", font=self._state_font, fg="#7a5a00"
        ).pack(side="left", padx=(10, 6), pady=8)
        tk.Label(
            self.pairing_frame, text="Compare this code with the phone, then", bg="#fff4d6"
        ).pack(side="left")
        self.pairing_code = tk.Label(
            self.pairing_frame, text="------", font=("TkFixedFont", 20, "bold"),
            bg="#fff4d6", fg="#3f2d00",
        )
        self.pairing_code.pack(side="left", padx=10)
        ttk.Button(self.pairing_frame, text="Approve", command=lambda: self._pair(True)).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(self.pairing_frame, text="Reject", command=lambda: self._pair(False)).pack(
            side="left", padx=(0, 10)
        )

        details = ttk.LabelFrame(outer, text="Connection", padding=10)
        details.pack(fill="x", pady=(12, 10))
        self.detail_values: dict[str, tk.Label] = {}
        fields = (
            ("device", "Phone"),
            ("transport", "Transport"),
            ("endpoint", "Address"),
            ("authentication", "Authentication"),
            ("connected_for", "Connected for"),
            ("attempts", "Attempts"),
            ("adb", "adb"),
            ("adb_path", "adb path"),
            ("usb", "Physical USB"),
            ("usb_diagnosis", "USB diagnosis"),
            ("usb_action", "USB next step"),
            ("wifi", "Wi-Fi discovery"),
            ("server", "Phone server reachable"),
            ("authentication_ok", "Authenticated"),
            ("discovery_error", "Last discovery error"),
            ("connection_error", "Last connection error"),
        )
        for column, (key, label) in enumerate(fields):
            row, col = divmod(column, 4)
            ttk.Label(details, text=label).grid(row=row * 2, column=col, sticky="w", padx=(0, 12))
            value = tk.Label(
                details, text="—", bg=CARD, anchor="w", justify="left", wraplength=210
            )
            value.grid(row=row * 2 + 1, column=col, sticky="w", padx=(0, 12), pady=(0, 6))
            self.detail_values[key] = value

        self.error_label = tk.Label(
            outer, text="", anchor="w", justify="left", bg=BACKGROUND, fg=PALETTE[TONE_ERROR],
            wraplength=880,
        )

        caps = ttk.LabelFrame(outer, text="Capabilities", padding=10)
        caps.pack(fill="both", expand=True)
        self.summary_label = tk.Label(
            caps, text="", anchor="w", bg=BACKGROUND, fg="#5b6470"
        )
        self.summary_label.pack(fill="x", pady=(0, 6))
        columns = ("name", "present", "permitted", "implemented", "available", "active", "note")
        self.table = ttk.Treeview(caps, columns=columns, show="headings", height=8)
        headings = {
            "name": ("Capability", 190),
            "present": ("Hardware", 80),
            "permitted": ("Permission", 90),
            "implemented": ("Built", 70),
            "available": ("Usable", 70),
            "active": ("Active", 60),
            "note": ("Why", 330),
        }
        for name, (heading, width) in headings.items():
            self.table.heading(name, text=heading)
            self.table.column(name, width=width, anchor="w", stretch=name == "note")
        self.table.pack(fill="both", expand=True)

        log_frame = ttk.LabelFrame(outer, text="Diagnostics", padding=10)
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = tk.Text(
            log_frame, height=8, wrap="none", font=self._mono, bg="#fbfbfc", relief="flat"
        )
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        tk.Label(
            outer,
            text=f"Config: {self.store.path_summary()}",
            anchor="w",
            bg=BACKGROUND,
            fg="#8a929c",
        ).pack(fill="x", pady=(6, 0))

        self.root.protocol("WM_DELETE_WINDOW", self._close)

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    def _connect(self) -> None:
        self.runtime.connect()

    def _disconnect(self) -> None:
        self.runtime.disconnect()

    def _reconnect(self) -> None:
        self.runtime.reconnect()

    def _use_endpoint(self) -> None:
        typed = self.endpoint_var.get().strip()
        if not typed:
            return
        try:
            host, port = parse_endpoint(typed)
        except EndpointError as exc:
            self.message_label.configure(text=str(exc))
            return
        self.message_label.configure(text=describe_endpoint(host, port))
        self.runtime.set_endpoint(host, port)
        self.runtime.connect()

    def _clear_endpoint(self) -> None:
        self.endpoint_var.set("")
        self.runtime.set_endpoint("")

    def _pair(self, approve: bool) -> None:
        self.runtime.resolve_pairing(approve)

    def _close(self) -> None:
        try:
            self.runtime.stop(timeout=8.0)
        finally:
            self.root.destroy()

    # ------------------------------------------------------------------ #
    # pump
    # ------------------------------------------------------------------ #
    def _pump(self) -> None:
        latest: Snapshot | None = None
        while True:
            try:
                latest = self._queue.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            self._apply(build_view(latest))
        self.root.after(80, self._pump)

    def _apply(self, view: View) -> None:
        self._last_view = view
        self.state_label.configure(text=view.state_label, fg=PALETTE.get(view.tone, PALETTE[TONE_IDLE]))
        self.message_label.configure(text=view.message)

        for key, value in (
            ("device", view.device),
            ("transport", view.transport),
            ("endpoint", view.endpoint),
            ("authentication", view.authentication),
            ("connected_for", view.connected_for),
            ("attempts", view.attempts),
            ("adb", view.adb),
            ("adb_path", view.adb_path),
            ("usb", view.usb),
            ("usb_diagnosis", view.usb_diagnosis),
            ("usb_action", view.usb_action),
            ("wifi", view.wifi),
            ("server", view.server),
            ("authentication_ok", view.auth_ok),
            ("discovery_error", view.discovery_error),
            ("connection_error", view.connection_error),
        ):
            widget = self.detail_values.get(key)
            if widget is not None:
                widget.configure(text=value)

        self.connect_button.configure(
            text=view.connect_label, state="normal" if view.connect_enabled else "disabled"
        )
        self.disconnect_button.configure(
            state="normal" if view.disconnect_enabled else "disabled"
        )
        self.reconnect_button.configure(
            state="normal" if view.reconnect_enabled else "disabled"
        )

        if view.pairing_visible:
            self.pairing_code.configure(text=view.pairing_code or "------")
            self.pairing_frame.pack(fill="x", pady=(12, 0), before=self.error_label)
        else:
            self.pairing_frame.pack_forget()

        if view.error_text:
            self.error_label.configure(text=view.error_text)
            self.error_label.pack(fill="x", pady=(10, 0))
        else:
            self.error_label.pack_forget()

        self.summary_label.configure(text=view.summary)
        rows = tuple(
            (r.name, r.present, r.permitted, r.implemented, r.available, r.active, r.note)
            for r in view.capabilities
        )
        if rows != self._rows:
            self._rows = rows
            self.table.delete(*self.table.get_children())
            for row in rows:
                self.table.insert("", "end", values=row)

        if self.log_text.get("1.0", "end").strip() != view.diagnostics.strip():
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("1.0", view.diagnostics)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")


def run_gui(
    *,
    config_dir: str | None = None,
    auto_connect: bool = False,
    discovery_timeout: float = 3.0,
    endpoint: str | None = None,
) -> int:
    """Open the controller window. Returns a process exit code."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # no display, or a broken Tcl install
        print(f"PhoneBridge needs a desktop session: {exc}")
        return 2
    store = DeviceStore(Path(config_dir)) if config_dir else DeviceStore()
    app = PhoneBridgeApp(
        root,
        store,
        auto_connect=auto_connect,
        discovery_timeout=discovery_timeout,
        endpoint=endpoint,
    )
    try:
        root.mainloop()
    except KeyboardInterrupt:  # pragma: no cover - Ctrl-C in a console
        app._close()
    return 0
