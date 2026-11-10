# 17. The connection controller (Milestone 1)

The one-click Windows controller: what it is, how it decides, what it refuses to
claim, and how to build, launch and test it.

Scope of this milestone: **connection management and the window.** Discovery,
transport choice, pairing, reconnection, diagnostics and the five-state
capability registry. It does not add the microphone, camera, Bluetooth or file
relays — those are Phases 2–5 and the UI says so, per capability, in the table.

Everything below was executed on Linux against the reference phone in
`tools/simulator/`. What needs the Redmi Note 14 Pro+ is in §7 and is marked
there; nothing in this document claims the handset has been tested.

---

## 1. What the user does

1. Start the bridge on the phone (unchanged: the existing app, port 8765).
2. Run `phonebridge gui` on the PC.
3. Press **Connect**.

No IP address, no port, no `adb forward`, no repeated terminal commands. The
window shows the phone it found, how it reached it, the authentication result,
the five-state capability table, and a diagnostics pane.

First-time pairing still needs the human: the phone shows six digits, the window
shows the same six digits, and the user presses Approve on one side. That is the
security model, not a missing feature — it is what stops a machine on the same
Wi-Fi from pairing silently.

---

## 2. Architecture

```
                 ┌──────────────────────────── tkinter window (ui/app.py) ───┐
                 │ buttons ──► BackgroundRuntime   snapshot queue ◄── draw   │
                 └───────────────────────┬───────────────────────────────────┘
                                         │  one asyncio loop, own thread
                 ┌───────────────────────▼───────────────────────────────────┐
                 │ ConnectionManager (manager.py)                            │
                 │   state machine · heartbeats · backoff · one session only │
                 └───────┬───────────────────────────────┬───────────────────┘
                         │ candidates                    │ BridgeSession (unchanged)
        ┌────────────────▼────────────┐                  │  ws + P-256 + AES-GCM
        │ UsbCandidates   (adb.py)    │                  │  pinned key, SAS prompt
        │ BeaconCandidates (UDP 47823)│                  ▼
        │ CachedCandidates (probe)    │             the phone (protocol v1)
        └─────────────────────────────┘
```

Nothing in the protocol changed. The controller drives the same `BridgeSession`
the CLI uses, so pinning, the SAS comparison, the version gate and AES-256-GCM
behave exactly as they already did; `docs/03` remains the reference.

One additive change was needed in `core/session.py`: an optional `on_phase`
callback (`"handshake"`, `"ready"`) so the UI can show *Authenticating* rather
than jumping from *Connecting* to *Connected*. It is observational only — a
callback that raises cannot disturb the session — and defaults to `None`, so
every existing caller is unaffected.

### Files

| File | Lines | What it owns |
| --- | --- | --- |
| `controller/adb.py` | 270 | the adb command line, device parsing, forward-rule ownership |
| `controller/transports.py` | 300 | the three candidate sources, ranking, TCP validation |
| `controller/capabilities.py` | 230 | the five-state registry, drift-checked against `DeviceFacts.kt` |
| `controller/manager.py` | 520 | the state machine, pairing, heartbeat, backoff |
| `controller/runtime.py` | 150 | one asyncio loop in a thread, for the window |
| `controller/view.py` | 210 | snapshot → strings and button flags (no tkinter) |
| `controller/ui/app.py` | 350 | the window: layout only |
| `controller/__main__.py` | 50 | `python -m phonebridge.controller` |

---

## 3. States

| State | Meaning | Leaves on |
| --- | --- | --- |
| `Disconnected` | nothing open, nothing owned | Connect |
| `Discovering` | asking USB, the beacon and the cache | a candidate, or Error |
| `Connecting` | opening the socket to a chosen address | handshake, or the next candidate |
| `Authenticating` | P-256 handshake, pinned-key check | Connected, Pairing required, or the next candidate |
| `Pairing required` | waiting for a human to compare six digits | Approve → Authenticating; Reject/timeout → Error |
| `Connected` | authenticated, `system.info` read, heartbeat running | a drop → Reconnecting |
| `Reconnecting` | the link went away; backoff running, re-discovering | Connected, or Error |
| `Error` | terminal, or nothing left to try | Connect / Reconnect |

Terminal errors — changed key, protocol-version skew, refused pairing — stop
immediately and say why. Retrying them would train the user to click past the one
message that matters.

---

## 4. Transport choice

| Source | Score | Notes |
| --- | --- | --- |
| USB (`adb devices` → `device` state) | 100 | forward created on a free local port; **only rules this controller created are removed** |
| Beacon, phone is pinned | 85 | fresh announcement, identity matches a paired phone |
| Beacon | 80 | fresh announcement from any phone |
| Cached endpoint | 50 | previous address, accepted only after a TCP connect succeeds |

- An existing forward (`adb forward --list` already maps `tcp:8765`) is **reused
  and never removed** — `tests/test_controller_transports.py` pins that.
- A device in `unauthorized` or `offline` state is reported with the fix
  ("accept the USB debugging prompt on the phone"), not retried blindly.
- USB is preferred whenever a usable adb device exists, as requested.
- Discovery is re-run on every reconnect attempt, because the phone may have
  moved between Wi-Fi, hotspot and USB while the link was down.
- Deliberately absent: mDNS (the beacon is cheaper and already shipped — the
  user's decision), and subnet sweeps (they look like a port scan).

---

## 5. Pairing, honestly

The six digits are derived from the fresh ECDH material, so **they change on
every attempt**. The window therefore answers the prompt *inside* the handshake
(the session supports an awaited `confirm_pairing` callback) rather than
approving once and retrying: there is nothing to cache, and a stale "Approve"
cannot answer a later prompt.

If the prompt is not answered within `pairing_timeout` (90 s), the attempt is
refused and the window says the code expired. Rejecting ends in `Error` with
"you declined the pairing on this PC".

Android's own authorisation — the USB debugging RSA prompt, the wireless
debugging pairing dialog — is not bypassed anywhere. The window reports the adb
state it finds and tells the user what to press.

---

## 6. The five capability states

| Column | Source | Honest about |
| --- | --- | --- |
| Hardware | `system.info.capabilities["hardware.<key>"]` | what the device physically has |
| Permission | `system.info.permissions` (only where a permission gates it) | `n/a` when none is required |
| Built | derived, or `implemented.<key>` if a future app publishes it | **`unknown`** when it cannot be proven |
| Usable | the phone's own `available` claim, cross-checked | a claim without hardware is downgraded and annotated |
| Active | runtime, set by a relay | false everywhere in this release |

`Built` is `unknown` when the hardware is absent: the phone reports only the
conjunction (`implemented AND hardware AND permission`), so with no hardware the
PC cannot tell "not implemented" from "cannot be measured". Showing "no" there
would be a guess the user cannot check.

Today, on a real phone, all eight capabilities report `Usable: no` — the app
implements the protocol, not the relays. That is the correct answer, and the
table says `Phase 2` / `Phase 4` / `Phase 5` next to each one.

The PC's capability keys and their gating permissions are parsed out of
`DeviceFacts.kt` by `tests/test_controller_capabilities.py`, so the two sides
cannot drift apart silently.

---

## 7. Windows: build, install, launch

From a PowerShell prompt in the repository root:

```powershell
cd windows
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"          # development; `pip install .` for a plain install
phonebridge --version            # phonebridge 1.0.0 (protocol v1, ...)
phonebridge gui                  # the window
```

Launch without a console window (optional shortcut):

```powershell
.venv\Scripts\pythonw.exe -m phonebridge.controller --auto-connect
```

If Windows Firewall asks about Python, allow it on **private** networks only —
the beacon listens on UDP 47823 and the session connects out to the phone.

For USB: install the platform-tools (`adb` must be on `PATH`), enable USB
debugging, and accept the RSA prompt on the phone once. The controller creates
the forward itself and removes it on Disconnect/exit.

---

## 8. Testing

### Automated (runs anywhere, no phone)

```powershell
cd windows
python -m pytest -q                      # 575 tests, 1 skipped (tkinter, Linux CI)
python -m pytest -q tests/test_controller_manager.py tests/test_controller_e2e.py
cd .. ; python tools/verify.py           # 7 checks
```

| File | Tests | Covers |
| --- | --- | --- |
| `test_controller_adb.py` | 9 | device parsing, forward ownership, cleanup, timeouts |
| `test_controller_transports.py` | 20 | ranking, USB/beacon/cache providers, dead addresses dropped |
| `test_controller_capabilities.py` | 15 | five states, contradictions, drift against Kotlin |
| `test_controller_manager.py` | 24 | every state, pairing, duplicate connect, drop, heartbeat, backoff, cleanup |
| `test_controller_view.py` | 14 | button enablement, tri-state cells, summaries |
| `test_controller_e2e.py` | 8 | **real** WebSocket + crypto against the simulator: pairing, reject, pin, drop-and-reconnect, heartbeat, runtime thread |
| `test_controller_ui.py` | 7 | the `gui` verb, argument passing, missing-tkinter message, import isolation |

The end-to-end file is the one that exercises real cryptography; it uses the
reference phone, not a mock. It still does not prove the Android app.

### Manual, on the Redmi Note 14 Pro+ (required before calling Milestone 1 done)

1. Phone: open PhoneBridge, grant Bluetooth + notifications, press **Start
   bridge**; note the endpoint and that the beacon is announcing.
2. PC: `phonebridge gui` → **Connect**. Expect Disconnected → Discovering →
   Connecting → Authenticating → **Pairing required**.
3. Compare the six digits with the phone's prompt; approve on both. Expect
   Connected, the device name/model filled in, and the transport showing Wi-Fi.
4. Capability table: every row `Usable: no`, hardware `yes` for the camera and
   microphone, `Phase N` in the Why column. Nothing may read "Active".
5. Unplug the router/AP (or turn Wi-Fi off on the PC for ~20 s). Expect
   Reconnecting, then Connected with no re-pairing prompt.
6. Plug in USB (debugging enabled, RSA accepted once), press **Reconnect**.
   Expect transport to change to USB and `adb forward --list` to show exactly one
   PhoneBridge rule; press **Disconnect** and expect it gone, with any rule you
   created beforehand still present.
7. Reboot the PC, reopen the window, press Connect: it must reconnect without a
   prompt (the pin and the address survive a restart).
8. Press **Reject** on a fresh pairing once: expect Error with the decline
   message and no stored key (`phonebridge identity` shows no new pin).

Report anything in 1–8 that differs; that list is the only thing that turns this
document from "tested against the simulator" into "tested on a phone".

---

## 9. What this milestone does not do

| Not done | Why / where it lands |
| --- | --- |
| Microphone, playback, Bluetooth audio relay | Phase 2 — needs the SCO measurements the phone is built to take |
| Camera streaming, Windows preview | Phase 4 |
| BLE scan, GATT, file transfer, sensors | Phase 5 |
| Windows audio input integration | Phase 2, needs the relay first |
| Installer / start-menu shortcut | Milestone 5 (a `.bat` or `pythonw` shortcut works today) |
| mDNS discovery | deliberately not added (user decision); the beacon covers LAN, hotspot and tethering |
| Automatic USB-debugging authorisation | not possible without bypassing Android security, which is out of scope |
| Verified on hardware | §8 checklist — pending the Redmi |
