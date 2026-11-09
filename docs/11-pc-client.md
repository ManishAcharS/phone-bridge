# The PC client: install, connect, inspect, use

Reference for the `phonebridge` command line: what each command does, which
phone it talks to, what the exit codes mean, and which workflows exist today.
Everything here has been run against the reference phone (`tools/simulator`);
the parts that need a real handset are marked.

No command in this CLI reports success it did not observe. Where protocol v1 has
no operation for a feature, there is no command for it - see §11.

---

## 1. Install

Windows 10/11, Python 3.10+, no administrator rights and no internet access are
needed at runtime.

```powershell
cd windows
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"          # editable: the repo you are editing is what runs
phonebridge --version            # phonebridge 1.0.0 (protocol v1, Python 3.x)
```

Without activating, the same thing is one command:

```powershell
python -m phonebridge --version  # identical; no script on PATH required
```

For a throwaway install that is *not* the checkout:

```powershell
pip install "C:\path\to\phone-bridge\windows"
py -m pipx install "C:\path\to\phone-bridge\windows"    # optional, isolates it
```

Two dependencies only: `websockets` and `cryptography`. They are pure install
concerns; nothing else is imported by the Phase-1 code. The optional packages
for later phases (audio, dashboard, virtual camera) are in
`windows/requirements-phase2plus.txt` and are *not* needed for anything this
document describes.

```powershell
pip install -e "windows[dev]" -r windows\requirements-phase2plus.txt   # Phase 2+
```

Check the installation:

```powershell
phonebridge doctor                 # environment report (text)
phonebridge doctor --json          # same, for scripts
```

`doctor` prints the version, protocol, config directory, whether the console
script is on PATH, which optional packages are missing and what each is for, and
-if it is a Windows machine- audio devices, OBS, adb, JDK and the Android SDK.

A clean-environment install was run as part of this work (fresh virtualenv, no
other packages, `pip install windows` from a directory outside the repo):

```
phonebridge --version            -> phonebridge 1.0.0 (protocol v1, Python 3.11.2)
python -m phonebridge --version  -> identical
pip list                         -> phonebridge 1.0.0, websockets 17.4,
                                    cryptography 50.0.2
phonebridge doctor --json        -> 2 required packages present, 6 optional
                                    packages reported missing, exit 0
```

The build previously emitted a setuptools deprecation warning
(`license = { text = "MIT" }`); `pyproject.toml` now uses the SPDX form
`license = "MIT"` with `setuptools>=77`, and the install is warning-free.

### PowerShell notes

* `phonebridge`, `python -m phonebridge` and `.\\.venv\\Scripts\\phonebridge.exe`
  all work; only PATH differs.
* The CLI's own output is ASCII and it forces `errors="replace"` on stdout, so a
  legacy code page (cp437/cp1252) cannot make a phone's model string crash a
  command.
* JSON goes to **stdout**, progress and warnings to **stderr**, so
  `phonebridge info > phone.json` stays valid JSON.
* `$LASTEXITCODE` is the documented exit code (§12). Use it instead of parsing
  text:
  ```powershell
  phonebridge capabilities --require audio.bluetooth_sco
  if ($LASTEXITCODE -ne 0) { Write-Error "this phone cannot do SCO audio" }
  ```
* In a `cmd.exe` one-liner, `%` needs escaping; prefer PowerShell.

---

## 2. Command map

| Command | What it answers | Needs a phone |
| --- | --- | --- |
| `gui` | desktop controller: one Connect button, auto-reconnect | no |
| `doctor` | is this PC set up? | no |
| `identity` | who is this PC, which phones are pinned? | no |
| `devices` | what do I know, and what is announcing itself? | no (beacons optional) |
| `discover` | who is on this network right now? | no |
| `info` | device facts from the phone (JSON by default) | yes |
| `capabilities` | what the phone can *actually* do today | yes |
| `audio status` | headset, profile, relay state | yes |
| `audio last` | the last capability probe the phone kept | yes |
| `ping` | application-level RTT | yes |
| `bench` | encrypted throughput, integrity check | yes |
| `watch` | live events with automatic reconnect | yes |
| `selftest` | every supported workflow, end to end, as a matrix | yes |
| `forget` | unpin one phone or all | no |
| `compat run/show/compare/report` | multi-device audio capability records | `run` only |

`phonebridge <command> -h` lists that command's options;
`phonebridge --help` prints the whole map and the exit codes.

---

## 2a. The desktop controller (`phonebridge gui`)

The commands above are per-action. `phonebridge gui` is the product: one window,
one Connect button, and it keeps working when the network moves underneath it.

```powershell
phonebridge gui                      # open the window
phonebridge gui --auto-connect       # connect as soon as it opens
python -m phonebridge.controller     # same thing, without the console script
```

What it does that the CLI does not:

- **Finds the phone itself** — no `--host`. It asks three sources in this order:
  a USB device (via an `adb forward` it creates and later removes), the phone's
  UDP beacon, then the last address that worked (probed before use).
- **Pairs in the window** — the six-digit code is shown next to Approve/Reject;
  the answer is applied to that one handshake. Nothing is cached.
- **Survives a drop** — a closed socket or two missed heartbeats put it into
  Reconnecting and it re-discovers (the phone may have moved from Wi-Fi to USB)
  with a jittered 0.5 s → 15 s backoff.
- **Shows the five capability states** — present / permitted / implemented /
  available / active — instead of a single yes/no, so "no camera" is never
  confused with "no permission".
- **Keeps a diagnostics pane** — every discovery decision, address and failure,
  in order.

It is a shell over the same session the CLI uses: same pinning, same SAS, same
AES-256-GCM, same protocol v1. `docs/17-connection-controller.md` is the full
reference; the window needs tkinter, which the python.org Windows installer
includes, and `phonebridge gui` says so plainly if it is missing.

---

## 3. Choosing a phone

Four ways, in priority order:

```powershell
phonebridge info --host 192.168.42.129          # explicit, always wins
phonebridge info --host 127.0.0.1 --port 8765   # USB with adb forward
phonebridge info --device "Pixel 7"             # a pinned phone's saved address
phonebridge info --last                         # most recent endpoint
phonebridge info                                # same as --last, announced on stderr
```

Every successful connection saves its address, tied to the phone's identity, so
`--device` works from then on. `phonebridge devices` shows what is saved:

```
this PC     : DESK-PC (3f2a…)
config dir  : C:\Users\you\AppData\Local\PhoneBridge

pinned phones (1):
  NAME       DEVICE ID         LAST ADDRESS       TRANSPORT  SEEN
  Pixel 7    9c41e0a7b3d26518  192.168.42.129:8765  wifi      2026-11-08T09:12:44Z

live beacons (UDP 47823, 3s):
  - Pixel 7 (9c41e0a7b3d26518) at ws://192.168.42.129:8765 via 192.168.42.129
```

The config directory holds the PC identity, pinned phones and known addresses
(files are mode 0600). Override it with `--config-dir` or, outside Windows,
`PHONEBRIDGE_CONFIG_DIR`:

```powershell
phonebridge --config-dir D:\portable\phonebridge identity
```

---

## 4. Pairing

The first connection to a phone asks you to compare a six-digit code:

```
  +---------------------------------------------------------+
  |  PAIRING CODE:  482913                                  |
  +---------------------------------------------------------+
  The phone shows the same six digits if the link is genuine.
  Codes match? [y/N]
```

* `y` pins the phone's identity key; later sessions skip the prompt and refuse a
  changed key.
* `n` (or anything else) sends `pair_cancel`, stores nothing, and exits 2.
* A terminal is required: with input redirected the CLI refuses instead of
  silently declining (`pairing needs an interactive terminal`).
* `--auto-pair` accepts without comparing. It exists for tests and prints a
  warning every time it is used; it is not a secure pairing and the docs never
  pretend otherwise.

Re-pair after a factory reset or a changed identity:

```powershell
phonebridge identity                       # find the old id
phonebridge forget 9c41e0a7b3d26518
phonebridge info --host 192.168.42.129     # pairs again
```

Refusals are terminal and are not retried: a changed key, a protocol-version
mismatch and "phone refused authentication" all stop immediately with the
reason, because retrying re-handshakes a device that said no.

---

## 5. Device information

```powershell
phonebridge info --host 192.168.42.129               # JSON (unchanged default)
phonebridge info --host 192.168.42.129 --format text # human summary
phonebridge info --device "Pixel 7" --json | Out-File -Encoding utf8 pixel.json
```

The text view is the same data, labelled:

```
device information from Pixel 7:
  device id   : 9c41e0a7b3d26518
  model       : Pixel 7
  manufacturer: Google
  codename    : panther
  android     : 14
  sdk         : 34
  app version : 1.0.0
  protocol    : 1

  runtime permissions:
    BLUETOOTH_CONNECT     : granted
    POST_NOTIFICATIONS    : granted
    RECORD_AUDIO          : denied

  capabilities: 2 of 8 usable today (run `phonebridge capabilities` for the table)
```

The JSON is exactly the `system.info` object of protocol v1 - the old keys
(`device_id`, `name`, `model`, `android_version`, `protocol_version`,
`capabilities`, `app_version`) are all still there, so scripts written for
Phase 1 keep working.

---

## 6. Capability inspection

```powershell
phonebridge capabilities --host 192.168.42.129
```

```
  CAPABILITY           HARDWARE  USABLE  NOTE
  -------------------  --------  ------  --------------------------------------------------
  audio.playback       yes       no      present, not implemented yet (Phase 2)
  audio.capture        yes       no      present, not implemented yet (Phase 2)
  audio.bluetooth_sco  yes       no      present, not implemented yet (Phase 2)
  camera.back          yes       no      present, not implemented yet (Phase 4)
  camera.front         yes       no      present, not implemented yet (Phase 4)
  sensors              yes       no      present, not implemented yet (Phase 5)
  file_transfer        yes       no      present, not implemented yet (Phase 5)
  bluetooth_scan       yes       no      present, not implemented yet (Phase 5)
```

Three separate questions, three separate answers:

* `HARDWARE` - measured by the phone (`hardware.<key>`). Camera enumeration,
  sensor list, audio device list, Bluetooth feature flags. No permission needed.
* `USABLE` - what the phone can deliver **today**: `hardware AND implemented AND
  permitted` (`<key>`). A `no` here is not a fault report; it is the truth.
* `NOTE` - the phone's own explanation (`detail.<key>`), e.g. which phase will
  implement it, or which permission is missing.

A phone running an older app has no `hardware.*` or `detail.*` keys. The table
then prints `?` and says so, rather than guessing:

```
  This phone reports the pre-0.2 capability set: no hardware.* or detail.* keys,
  so USABLE is all it can say about itself.
```

Use it as a gate:

```powershell
phonebridge capabilities --host 192.168.42.129 --require audio.bluetooth_sco
# exit 0  -> usable today
# exit 4  -> "requirement not met: audio.bluetooth_sco (present, not implemented yet (Phase 2))"
```

`--require` may be repeated, and an unknown key is warned about explicitly
instead of silently passing.

Machine-readable form:

```powershell
phonebridge capabilities --json
# {"device_id": …, "capabilities": [{"key": …, "hardware": true, "usable": false,
#  "detail": …}, …], "permissions": {…}, "simulated": false, …}
```

---

## 7. Audio

The relay itself is Phase 2. What exists today is *inspection*, and it says so.

```powershell
phonebridge audio status --host 192.168.42.129
```

```
bluetooth headset (as reported by the phone):
  headset connected : yes
  active profile    : duplex
  communication devices:
    - Bluetooth SCO

audio relay (Phase 2): supported=yes
  Phase 2: SCO routing is exercised by the audio relay, not by this probe
```

`audio status --json` returns `{"headset": {...}, "relay": {...}}`. If the phone
predates the relay method, the CLI says "does not report a relay state (older
app)" rather than inventing one.

Measure what the hardware can do (this really does exercise the phone's audio
stack, and stores the result):

```powershell
phonebridge compat run --host 192.168.42.129 --label "Pixel 7" --seconds 30
phonebridge compat run --host 192.168.42.129 --acoustic        # + chirp latency
phonebridge compat show                                        # stored records + findings
phonebridge compat compare                                     # table only
phonebridge compat report                                      # docs/09-compat-report.md
```

Read the last report straight off the phone, without storing a second copy:

```powershell
phonebridge audio last --host 192.168.42.129
phonebridge audio last --device "Pixel 7" --json
```

If no probe has run, `audio last` exits 1 and tells you which command to run.

---

## 8. Link quality

```powershell
phonebridge ping --host 192.168.42.129 --count 20 --interval 0.1
phonebridge bench --host 192.168.42.129 --frames 3000 --size 1024
phonebridge bench --host 192.168.42.129 --expect-ms 2    # exit 1 if RTT > 2 ms
phonebridge watch --host 192.168.42.129                  # events, auto-reconnect
phonebridge watch --device "Pixel 7" --timeout 30 --json # 30 s, JSON lines
```

`bench` compares every echoed frame byte for byte and prints
`integrity : N frames returned byte-identical`; a corrupt frame aborts the run.
`watch` reconnects with jittered backoff, reports `connecting / connected /
reconnecting / closed`, gives up only on terminal failures, and `--max-attempts`
caps it deliberately.

---

## 9. Self-test: the verification matrix

One command runs every workflow protocol v1 actually specifies and prints what
happened:

```powershell
phonebridge selftest --host 192.168.42.129
phonebridge selftest --device "Pixel 7" --frames 64 --json
```

```
  WORKFLOW                          RESULT           EVIDENCE
  --------------------------------  ---------------  ------------------------------------
  authenticated connection          PASS             Pixel 7 (9c41e0a7b3d26518), pinned, ws://…
  phone information                 PASS             device block complete, Pixel 7 / android 14…
  capability contract               PASS             8 capabilities, 0 usable, 8 present…
  permission snapshot               PASS             POST_NOTIFICATIONS=granted, RECORD_AU...
  session statistics                PASS             in 2 frames / 176 B, out 1 frames…
  bluetooth headset status          PASS             connected=True, profile=…
  audio relay state                 PASS             supported=False (Phase 2: …)
  audio probe report                PASS             available=false (no probe has run yet)
  ping round trip                   PASS             3 pings, median 0.41 ms (min 0.35)
  binary integrity (blob echo)      PASS             16 frames, 37916 B, byte-identical
  error contract (unknown method)   PASS             method_not_found returned; session alive
  timeout handling                  PASS             1 us client timeout fired after 0.14 ms…
  file transfer PC -> phone         NOT IMPLEMENTED  no file operation in protocol v1 (Phase 5)
  … camera, sensors, audio relay also NOT IMPLEMENTED
```

Rules the rows follow:

* `PASS` means the operation ran and its result was checked - never "the code
  exists".
* `NOT IMPLEMENTED` rows are the features protocol v1 does not define. They are
  never `SKIP` and never counted as passing.
* exit code 0 when every supported workflow passed, 1 otherwise, so it works in
  a script.

Full matrix with per-row evidence: [`12-end-to-end-verification.md`](12-end-to-end-verification.md).

## 10. Discovery

```powershell
phonebridge discover                # listen 8 s, print what is heard
phonebridge discover --first        # stop at the first phone
phonebridge discover --json         # JSON array on stdout
phonebridge devices                 # saved phones + live beacons in one view
```

Discovery is a convenience: the beacon carries no keys and grants no trust, and
manual entry always works. If UDP 47823 is already bound, `discover`/`devices`
report that instead of hanging.

---

## 11. What deliberately does not exist

Protocol v1 defines exactly seven RPC methods (`docs/03-protocol.md` §6):
`system.info`, `system.echo`, `session.stats`, `bluetooth.headset.status`,
`audio.route.bluetooth`, `audio.capabilities.run`, `audio.capabilities.last`.
There is no file-transfer method, no camera stream, no sensor subscription and no
audio stream.

So there is no `phonebridge files put`, no `phonebridge camera snapshot` and no
`phonebridge sensors watch`. A command that connected, did nothing and printed
"done" would be a lie, and one that failed on every phone would be noise. What
you get instead:

* `capabilities` reports `file_transfer`, `camera.*`, `sensors`,
  `audio.playback`, `audio.capture` with `USABLE no` and the phase that will
  implement them;
* `capabilities --require file_transfer` exits 4 with that reason, so a script
  can gate on it;
* if a future phone claims a capability the CLI cannot drive, `capabilities`
  prints it and the report says there is no PC command yet.

When those phases land, the verbs will appear here and in the same style.

---

## 12. Exit codes

| Code | Meaning |
| --- | --- |
| 0 | the command did what it said |
| 1 | nothing found / nothing to show (`devices`, `discover`, `audio last`, `forget <id>`, `bench` threshold) |
| 2 | bad arguments, or the phone could not be reached, or pairing was declined |
| 4 | `--require` capability is not usable today |
| 5 | the phone speaks protocol v1 but does not implement that operation |
| 130 | interrupted (Ctrl-C) |

---

## 13. Troubleshooting

| Symptom | What it means | What to do |
| --- | --- | --- |
| `error: cannot connect to ws://127.0.0.1:8765 …` + hint about `adb forward` | nothing is listening on the forwarded port | `adb forward tcp:8765 tcp:8765`, or use the phone's IP |
| connects, then `phone accepted the connection but sent no HELLO_ACK` | something answered that is not PhoneBridge | check the port; the hint names the address |
| `phone closed the connection during setup (1008: …)` | the phone refused, with its reason (e.g. too many connections) | read the reason; close the other session |
| `phone refused authentication` (terminal, not retried) | identity mismatch or version skew | `phonebridge forget <id>` and pair again |
| `pairing needs an interactive terminal` | stdin is not a TTY | run it in a terminal, not a pipe/CI step |
| `requirement not met: …` (exit 4) | the phone cannot do that today | read the note; that is the phase boundary |
| `this phone does not implement …` (exit 5) | older phone app | update the app, or use what it does support |
| `no phone chosen` | no `--host`, no saved endpoint | `phonebridge discover` then `info --host …` |
| `cannot bind UDP 47823` | another PhoneBridge is running | close it, or connect with `--host` |
| `--host must not include a port` | `ip:port` in one flag | `--host 192.168.42.129 --port 8765` |
| Windows Firewall prompt | first inbound UDP for discovery | allow **Private** networks only |
