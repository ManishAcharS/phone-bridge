# 17. The connection controller

The one-click Windows controller: what it is, how it decides, what it refuses to
claim, and how to build, launch and test it.

Milestone 1 built the connection manager and the window. Milestone 2 made the two
paths that did not work on the maintainer's PC work, or at least explain
themselves: a double-click launcher (`PhoneBridge-Start.bat`, §2), and reliable
USB + hotspot connectivity (§5, §6). The protocol, the CLI, pairing, the pinned
key and AES-256-GCM are untouched.

It adds no microphone, camera, Bluetooth or file relay — those are Phases 2–5 and
the UI says so, per capability, in the table.

Everything below was executed on Linux against the reference phone in
`tools/simulator/`. What needs the Redmi Note 14 Pro+ is in §11 and is marked
there; nothing in this document claims a cable or a hotspot has been verified on
hardware. The 0/8 capability table is the honest state of Phase 1 and stays
0/8 until Phase 2 ships a relay.

---

## 1. What the user does

1. Start the bridge on the phone (unchanged: the existing app, port 8765).
2. Double-click **`PhoneBridge-Start.bat`** in the repository root — it updates,
   installs and opens the window (§2). `phonebridge gui` still works if you prefer
   a terminal.
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
                 │   diagnostics · manual endpoint · per-source verdicts     │
                 └───────┬───────────────────────────────┬───────────────────┘
                         │ candidates (ranked)           │ BridgeSession (unchanged)
        ┌────────────────▼────────────┐                  │  ws + P-256 + AES-GCM
        │ UsbCandidates   (adb.py)    │                  │  pinned key, SAS prompt
        │ BeaconCandidates (UDP 47823)│                  ▼
        │ GatewayCandidates(netprobe) │             the phone (protocol v1)
        │ FixedCandidates  (typed)    │
        │ CachedCandidates (probe)    │
        └─────────────────────────────┘
```

Nothing in the protocol changed. The controller drives the same `BridgeSession`
the CLI uses, so pinning, the SAS comparison, the version gate and AES-256-GCM
behave exactly as they already did; `docs/03` remains the reference.

One additive change was needed in `core/session.py` during Milestone 1: an
optional `on_phase` callback (`"handshake"`, `"ready"`) so the UI can show
*Authenticating* rather than jumping from *Connecting* to *Connected*. It is
observational only — a callback that raises cannot disturb the session — and
defaults to `None`, so every existing caller is unaffected.

### The launcher: `PhoneBridge-Start.bat`

Double-clicking it is the whole install procedure. It does four things, in order,
and each one refuses to continue rather than guess:

| Step | What it does | What it refuses to do |
| --- | --- | --- |
| 0. tools | `where git`, then `py -3` / `python`, then a version check for 3.10+ and an `import tkinter` check | continue with a Python that cannot open the window |
| 1. protect | if `git status --porcelain` is non-empty, `git stash push --include-untracked` and **record the exact ref** it just created | stash a clean tree; touch a stash it did not make |
| 2. update | `git pull --ff-only origin main`, but only when the checkout is on `main` | merge, rebase, `reset --hard`, `clean`, force anything |
| 3. restore | pop **only** if `refs/stash` still equals the ref from step 1 | pop someone else's stash; drop the stash on conflict |
| 4. install & run | create `.venv` if absent, `pip install --editable "windows[dev]"`, then `python -m phonebridge.controller` | install into a half-made venv, close the window on an error |

On a conflict it stops, prints the three commands to recover, and leaves the stash
in place — the work is never deleted, even in the failure path. On any error the
window stays open (`pause`) with a sentence naming the cause. `tests/test_launcher.py`
pins these invariants, including that every `goto :label` in the file resolves.

### Files

| File | Lines | What it owns |
| --- | --- | --- |
| `controller/adb.py` | 527 | finding adb anywhere, device parsing, cable-vs-bridge classification, forward-rule ownership |
| `controller/transports.py` | 560 | the five candidate sources, ranking, TCP validation |
| `controller/netprobe.py` | 201 | TCP reachability, default gateways, Windows network profile |
| `controller/diagnostics.py` | 75 | the de-duplicating, bounded log |
| `controller/capabilities.py` | 237 | the five-state registry, drift-checked against `DeviceFacts.kt` |
| `controller/manager.py` | 788 | the state machine, pairing, heartbeat, backoff, per-source diagnostics |
| `controller/runtime.py` | 176 | one asyncio loop in a thread, for the window |
| `controller/view.py` | 278 | snapshot → strings and button flags (no tkinter) |
| `controller/ui/app.py` | 394 | the window: layout only |
| `controller/__main__.py` | 49 | `python -m phonebridge.controller` |
| `PhoneBridge-Start.bat` | 195 | the double-click launcher |

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

Six sources, ranked. The first one that completes the handshake wins; the rest is
diagnostics the window shows so a failure is never just "no phone found".

| Source | Score | Notes |
| --- | --- | --- |
| USB: `adb devices` reports a cable-attached `device` | 100 | forward created on a free local port; **only rules this controller created are removed** |
| An address you typed in the window | 95 | an instruction, not a hint — nothing overrides it |
| adb over TCP: `_adb-tls-connect._tcp` or `IP:port` | 90 | a real network path, but **never called USB** |
| Beacon, phone is pinned | 85 | fresh announcement, identity matches a paired phone |
| Beacon | 80 | fresh announcement from any phone |
| Beacon, an extra interface the phone advertised | 78 | e.g. the hotspot address next to the LAN address |
| **The default gateway, if it answers on 8765** | 60 | on a hotspot the gateway *is* the phone (§6) |
| Cached endpoint | 50 | previous address, accepted only after a TCP connect succeeds |

- An existing forward (`adb forward --list` already maps `tcp:8765`) is **reused
  and never removed** — `tests/test_controller_adb.py` pins that.
- A device in `unauthorized` or `offline` state is reported with the fix
  ("accept the USB debugging prompt on the phone"), not retried blindly.
- Discovery is re-run on every reconnect attempt, because the phone may have
  moved between Wi-Fi, hotspot and USB while the link was down.
- Deliberately absent: mDNS (the beacon is cheaper and already shipped — the
  user's decision), subnet sweeps and port scans (they look like an attack, and
  they are unnecessary once the gateway is tried).

---

## 5. USB: finding adb, and never calling Wi-Fi "USB"

The reported symptom was `adb not on PATH (USB unavailable)` while
`C:\Users\2026\AppData\Local\Android\Sdk\platform-tools\adb.exe` existed.
Android Studio does not add platform-tools to `PATH`, so a PATH-only search is a
bug. `controller/adb.py` searches, in this order, and reports which one answered:

1. `PHONEBRIDGE_ADB` (an explicit override — honoured even over `PATH`)
2. `adb` on `PATH`
3. `ANDROID_HOME`
4. `ANDROID_SDK_ROOT`
5. `%LOCALAPPDATA%\Android\Sdk`
6. `%USERPROFILE%\AppData\Local\Android\Sdk`
7. `%LOCALAPPDATA%\Android\Sdk.1`
8. `C:\Android\Sdk`
9. `%ProgramFiles%\Android\Sdk`

(On Linux/macOS: `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `~/Android/Sdk`,
`/usr/lib/android-sdk`, `/opt/android-sdk`, `/usr/local/share/android-sdk`.)
Duplicate paths are collapsed, so the list never reports the same binary twice.

The window shows the resolved path, so a user can check it against their own SDK.

### Cable, bridge or emulator

`adb devices -l` cannot be taken at face value: a wireless-debugging session looks
like any other device. The serial decides, and the window's `Physical USB` row
follows the classification, never the transport preference:

| Serial shape | Classified as | Offered as |
| --- | --- | --- |
| `R58M12345`, `HT7A1A01234` | USB | the `USB (adb forward)` transport (score 100) |
| `192.168.1.9:5555` | wireless | `adb over Wi-Fi` (score 90), never USB |
| `adb-XXXX._adb-tls-connect._tcp` | wireless | `adb over Wi-Fi` (score 90), never USB |
| `adb-XXXX._adb-tls-pairing._tcp` | wireless | same, and only while pairing |
| `emulator-5554`, `localhost:5555` | emulator | not treated as a phone on a cable |

Explanations are per state, not one generic sentence: `unauthorized` (accept the
prompt on the phone), `offline` (replug, or `adb kill-server`), `no permissions`
(Linux udev rules), `connecting`/`authorizing` (wait a moment). adb being absent
is a *state*, not an exception: Wi-Fi keeps working and the row says so.

---

## 6. Hotspot: why it failed, and what changed

**The beacon never left the phone.** `DiscoveryBeacon` sent every datagram to
`255.255.255.255`, the *limited* broadcast. Android routes that out of the
**default** interface — mobile data, or another Wi-Fi. When the phone is the
hotspot, the hotspot is not the default interface, so the datagram never reached
the PC that had just joined it. From the PC this is indistinguishable from "the
phone app is not running", which is why the old window could not explain it.

Three changes, one on each side and one for the case where discovery stays blocked:

1. **The phone broadcasts per interface.** Every up, non-loopback interface
   contributes its own directed broadcast address (`NetworkInterface` →
   `interfaceAddresses` → `broadcast`), so the datagram is pinned to that
   interface's route whatever the default is. `255.255.255.255` is still sent, for
   access points that forward it. The payload also lists the phone's own IPv4
   addresses in an optional `hosts` array (v1 keys unchanged, additive only; the PC
   validates IPv4 literals and caps the list at 8).
2. **The PC probes the default gateway.** On a hotspot the gateway *is* the phone,
   so one TCP connect to it answers "is the app listening?" with no UDP involved.
   That is bounded — one address per interface, from the OS routing table, never an
   assumed subnet, never a sweep — and it is ranked below every real discovery
   source so it only wins when nothing better exists.
3. **You can always type the address.** The window has a `Phone address` field
   (also `--endpoint 10.0.0.5:8765`); a typed address outranks everything and is
   attempted even when its probe fails, because the user may know something the
   probe cannot see.

Windows-specific: a hotspot is usually classified **Public**, where the firewall
drops inbound UDP by default. When no beacon is heard and
`Get-NetConnectionProfile` says `Public`, the Wi-Fi row says so and names the fix
(allow PhoneBridge, or set that network to Private). This is a diagnosis, not a
bypass: if the network is Public the launcher does not try to change it.

Two things that were **checked and needed no change**, so they are not the cause:

- **The phone's server binds the wildcard address.** `ServerImpl(InetSocketAddress(port))`
  listens on `0.0.0.0:8765`, so it is reachable on the hotspot interface as well as
  on Wi-Fi; the service log says `listening on 0.0.0.0:8765`. The port is unchanged
  and `docs/03` remains correct.
- **The PC's listener binds the wildcard too**; a "listener blocked" message in the
  Wi-Fi row is a bind *failure* (another program on UDP 47823), which is a different
  sentence from "no beacons heard".

Remaining causes it cannot rule out, because they need the hardware: **AP/client
isolation** (some hotspots, and most guest Wi-Fi, drop client-to-client traffic
entirely — then even the gateway probe fails and no address will work), and a
firewall policy that blocks both UDP and inbound sessions. The window distinguishes
those two from a beacon problem, which is what makes them diagnosable at all.

Still not claimed: whether a specific phone's hotspot works. §11 has the steps
that would prove it, and none of them has been run against the handset.

---

## 7. What the window tells you

Every row exists because a different failure used to look the same. All of them
are separate fields in one immutable `Snapshot`, so the tables below are also the
tests' vocabulary.

| Row | Values | Separates |
| --- | --- | --- |
| Transport | `USB (adb forward)`, `adb over Wi-Fi`, `Wi-Fi`, `hotspot`, `typed address`, `saved address` | which path actually carried the session |
| adb | `not found — USB unavailable, Wi-Fi unaffected` / `adb: 1 on USB (Redmi Note 14 Pro)` / a failure reason | adb missing vs adb present with no cable vs adb present and unhappy |
| adb path | the resolved absolute path, or where it looked | "installed but not on PATH" |
| Physical USB | `yes`, `no device on USB — plug in the cable…`, `unavailable — adb is not installed` | a cable from a wireless bridge |
| Wi-Fi discovery | `N phone(s) announcing`, `no beacons heard` (+ the firewall sentence when Public), `listener blocked: …` | silence from a blocked port |
| Phone server reachable | `yes — the phone app answered on TCP`, `no — nothing is listening at that address`, `not checked yet` | discovery failure from reachability failure |
| Authenticated | `yes — pinned key verified, AES-256-GCM in use`, `not authenticated`, `not yet` | *Connected* from *authenticated* |
| Last discovery error | the reason, or `none` | why nothing was found |
| Last connection error | the reason, or `none` | why the attempt failed |

`Not checked yet` and `unknown` are used wherever the honest answer is "nobody has
looked", which is why `Connected` alone no longer prints "Pinned key verified" —
that claim now requires the handshake to have actually finished
(`Snapshot.auth_ok`).

The diagnostics pane is a different design from a plain append log: a line is
keyed by its text, keeps the timestamp and position of its **first** occurrence,
and later identical lines only bump a counter (`12:04:31  connecting…  (x6)`).
The pane is bounded (200 lines), so a reconnect loop cannot push the one line that
matters out of view, and it cannot fill the window with noise.

---

## 8. Pairing, honestly

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

## 9. The five capability states

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

## 10. Windows: build, install, launch

### The one-step way

Double-click **`PhoneBridge-Start.bat`** in the repository root. That is the
supported path: it checks Git and Python, saves and restores your local changes
around a fast-forward-only `git pull`, builds `.venv` on first run, installs the
dev extras, and opens the window (§2). It is safe to run repeatedly, and it never
runs `reset --hard`, `git clean` or a blind `git stash pop`.

Arguments are passed through, so this also works:

```powershell
.\PhoneBridge-Start.bat --auto-connect
.\PhoneBridge-Start.bat --endpoint 10.0.0.5:8765     # when the beacon is blocked
```

### The manual way

From a PowerShell prompt in `windows\`:

```powershell
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

### Firewall

If Windows Firewall asks about Python, allow it on **private** networks only — the
beacon listens on UDP 47823 and the session connects out to the phone. A hotspot
network is usually classified Public, where inbound UDP is dropped; the Wi-Fi row
says so when that is what happened, and setting that network to Private (or
allowing PhoneBridge) fixes it. Nothing in the app changes your firewall.

### USB

Install platform-tools, enable Developer options → USB debugging, and accept the
RSA prompt on the phone once. **`adb` does not have to be on `PATH`** (§5). The
controller finds it, creates the forward itself, and removes it on
Disconnect/exit — an existing rule of your own is reused and left alone.

### Hotspot

On the phone: Settings → Hotspot & tethering → **Connect to a device** (or USB
tethering if you prefer a cable for the network). On the PC: join that Wi-Fi and
press **Connect**. If nothing is found, the window tells you which of the four
failures happened (§7) and `Phone address` is the direct fallback.

---

## 11. Testing

### Automated (runs anywhere, no phone)

```powershell
cd windows
python -m pytest -q                      # 703 tests, 1 skipped (tkinter, Linux CI)
python -m pytest -q tests/test_controller_manager.py tests/test_controller_e2e.py
cd .. ; python tools/verify.py           # 7 checks
```

| File | Tests | Covers |
| --- | --- | --- |
| `test_controller_adb.py` | 9 | device parsing, forward ownership, cleanup, timeouts |
| `test_controller_adb_discovery.py` | 29 | every SDK location, override/PATH order, duplicates, cable vs `_adb-tls-connect._tcp` vs `IP:port` vs emulator, state explanations |
| `test_controller_netprobe.py` | 20 | TCP probes, Windows/macOS/Linux route parsing, no invented gateway, Public/Private profile |
| `test_controller_transports.py` | 20 | ranking, USB/beacon/cache providers, dead addresses dropped |
| `test_controller_hotspot.py` | 34 | directed-broadcast beacon source, advertised hosts, gateway candidates, typed address, per-source manager diagnostics, firewall sentence |
| `test_controller_diagnostics.py` | 8 | de-duplication, first timestamp kept, bounded pane |
| `test_controller_capabilities.py` | 15 | five states, contradictions, drift against Kotlin |
| `test_controller_manager.py` | 24 | every state, pairing, duplicate connect, drop, heartbeat, backoff, cleanup |
| `test_controller_view.py` | 21 | button enablement, tri-state cells, honest authentication line |
| `test_controller_e2e.py` | 8 | **real** WebSocket + crypto against the simulator: pairing, reject, pin, drop-and-reconnect, heartbeat, runtime thread |
| `test_controller_ui.py` | 7 | the `gui` verb, argument passing, missing-tkinter message, import isolation |
| `test_launcher.py` | 30 | the `.bat`: CRLF, tool checks, `--ff-only`, stash ref matching, no destructive command, every `goto` resolves |

The end-to-end file is the one that exercises real cryptography; it uses the
reference phone, not a mock. It still does not prove the Android app.

### Not runnable in this environment

The sandbox has no Windows, no display, no `adb` and no JDK. So, honestly:

- `cmd.exe` was never executed: `test_launcher.py` checks the script statically.
- `adb` discovery is tested with injected filesystems, not a real SDK.
- The Android unit tests and the debug APK build are **not** run here.
- No cable, hotspot or handset was involved in any of the above.

### Manual, on the Redmi Note 14 Pro+ (required before claiming this works)

Milestone 1's checklist still applies (pairing, drop-and-reconnect, reboot,
reject). Milestone 2 adds:

9. **Launcher.** Double-click `PhoneBridge-Start.bat` with (a) a clean tree,
   (b) an edited tracked file, (c) an untracked file, (d) an existing unrelated
   stash. Expect: no stash in (a); your edits back in (b) and (c); the older stash
   untouched in (d); the window opening every time.
10. **Launcher failure paths.** Disconnect the network mid-run: expect a clear pip
    failure and `pause`, and `git status` still showing your work. Make a change
    that conflicts with an incoming commit: expect the conflict message, the stash
    still listed by `git stash list`, and nothing deleted.
11. **USB without PATH.** Confirm `where adb` finds nothing, then connect the
    phone with debugging on. Expect the adb row to show the resolved
    `%LOCALAPPDATA%` path, `Physical USB: yes`, transport `USB (adb forward)`, and
    `adb forward --list` to have exactly one PhoneBridge rule afterwards.
12. **Wireless debugging is not USB.** `adb tcpip 5555` / pair over Wi-Fi, then
    expect the transport to read `adb over Wi-Fi` and `Physical USB: no`.
13. **Hotspot.** Phone as hotspot, PC joined, mobile data on. Expect the beacon to
    be heard (or, if the firewall blocks UDP, the Public-network sentence) and a
    connection without typing anything. Then block UDP deliberately and expect the
    gateway candidate to carry it, and finally a typed address to work.
14. **Address fallback.** Type the phone's current IP in `Phone address`; expect
    `Phone server reachable: yes` before the transport row fills in.

Report anything in 1–14 that differs; that list is the only thing that turns this
document from "tested against the simulator" into "tested on a phone".

---

## 12. What this milestone does not do

| Not done | Why / where it lands |
| --- | --- |
| Microphone, playback, Bluetooth audio relay | Phase 2 — needs the SCO measurements the phone is built to take |
| Camera streaming, Windows preview | Phase 4 |
| BLE scan, GATT, file transfer, sensors | Phase 5 |
| Windows audio input integration | Phase 2, needs the relay first |
| Installer / start-menu shortcut | Milestone 5 (the root `.bat` covers it until then) |
| Changing the Windows firewall or network profile for you | never — the app explains a Public profile and leaves the decision to the user |
| Scanning a subnet for the phone | never — one gateway probe and a typed address instead |
| Verifying a real cable or hotspot | this document says "not verified" until §11 items 9–14 are run on hardware |
| mDNS discovery | deliberately not added (user decision); the beacon covers LAN, hotspot and tethering |
| Automatic USB-debugging authorisation | not possible without bypassing Android security, which is out of scope |
| Verified on hardware | §8 checklist — pending the Redmi |
