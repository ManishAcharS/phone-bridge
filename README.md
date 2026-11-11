# PhoneBridge

Use an Android phone as an external hardware hub for a Windows PC — a
Bluetooth-headset relay, an encrypted control channel, and a place where the
phone's hardware becomes reachable from the PC over USB, Wi-Fi or the phone's own
hotspot. No cloud, no accounts, no internet connection required.

**Release 1.0.0** is the first public release. It ships a complete, verified PC
client and a complete Android app, with one honest caveat stated up front: the
Android app has never been run on a physical handset by this project (there is no
device in CI and none available to the maintainer's automated checks). Everything
that can be proven without a handset is proven, and
[`docs/14-release-readiness.md`](docs/14-release-readiness.md) lists exactly what
is left, item by item.

| | |
| --- | --- |
| **PC client** | complete, 1132 automated tests, type-checked, linted |
| **Android app** | complete, compiled against a stub environment, source-audited |
| **Protocol** | v1, frozen; wire format in [`docs/03-protocol.md`](docs/03-protocol.md) |
| **Audio relay** | implemented (Phase 2); the phone microphone reaches the PC's speakers, not a virtual mic |
| **Camera streaming** | implemented (Phase 4); verified against the reference phone, **not yet on a handset** |
| **Bluetooth LE, sensors, file transfer** | implemented (Phase 5); verified against the reference phone, **no real peripheral, sensor or storage has been exercised** |

---

## The one-click controller

**Windows: double-click `PhoneBridge-Start.bat` in this folder.** It checks Git
and Python, saves and restores any local changes around a fast-forward-only
update, builds `.venv`, installs the dev extras and opens the window. Safe to run
repeatedly; it never resets, cleans or drops a stash.

```powershell
phonebridge gui          # or, from a terminal: one window, one Connect button
```

The window finds the phone by itself — a USB cable first (it sets up and later
removes its own `adb forward`), then an address you typed, then adb over Wi-Fi,
then the phone's UDP beacon, then the default gateway (on a hotspot the gateway
*is* the phone), then the last address that worked — pairs with the six-digit
code, shows the five capability states, keeps a de-duplicated diagnostics pane,
and reconnects with backoff when the network moves. No IP addresses, no ports, no
repeated terminal commands.

`adb` does **not** have to be on `PATH`: `PATH`, `ANDROID_HOME`,
`ANDROID_SDK_ROOT` and the usual `%LOCALAPPDATA%\Android\Sdk` folders are all
searched, and a wireless-debugging bridge is never labelled USB. If discovery is
blocked (a Public network, or a hotspot that drops UDP) the window says so and
the `Phone address` field takes an IP directly.

It is a shell over the same authenticated session as the CLI (protocol v1,
unchanged). Requires tkinter, which the python.org Windows installer includes.
Full reference: [`docs/17-connection-controller.md`](docs/17-connection-controller.md).

## What works today

| Capability | State |
| --- | --- |
| Encrypted, authenticated PC ↔ phone channel | **works** |
| Pairing with a six-digit code, identity pinning afterwards | **works** |
| USB (`adb forward` or USB tethering), Wi-Fi, phone hotspot | **works** |
| Automatic reconnection with jittered backoff | **works** |
| Discovery beacon (no typing IP addresses) | **works** |
| Phone information, capability and permission reporting | **works** |
| Live session statistics, headset/routing state, RTT and throughput | **works** |
| Binary streams (byte-exact to 1 MiB) | **works** |
| Audio capability probe (measures what the handset can actually do) | **works** |
| Bluetooth audio relay (the sound itself) | **works** (Phase 2, monitor only — no virtual microphone) |
| Camera streaming to the Windows preview (Phase 4) | **works** — synthetic frames in tests, real CameraX on the phone |
| Bluetooth LE scan, GATT read/write/subscribe (Phase 5) | **works** — the phone is the radio, the PC drives it over the same session |
| Sensors: 13 kinds, per-sensor rate, live values (Phase 5) | **works** — no reading is ever invented; a sensor with no frame says so |
| File transfer both ways, hashed and resumable (Phase 5) | **works** — SAF on the phone, `.part` + SHA-256 + atomic rename on the PC |

`phonebridge capabilities` never guesses: it separates what the phone *has*
(`hardware.*`) from what PhoneBridge can *currently deliver* (`usable`) and
explains every "no" with the phase that will implement it. The app's own card
does the same.

---

## Requirements

**PC** — Windows 10 or 11, Python 3.10–3.13. Linux and macOS work for the PC
client too (they are how the test suite is run), but Windows is the supported
target.

**Phone** — Android 8.0 (API 26) or newer. No root, no developer options needed
for Wi-Fi or hotspot; USB development mode needs `adb` on the PC.

---

## Installation

### 1. The PC client

From a PowerShell window, in the repository root:

```powershell
cd windows
python -m venv .venv
.venv\Scripts\activate
pip install .                # or: pip install -e ".[dev]" to work on the code
phonebridge --version        # -> phonebridge 1.0.0 (protocol v1, Python 3.x)
```

`pip install phonebridge` from a package index works the same way once the
package is published. `python -m phonebridge ...` behaves identically if the
`phonebridge` command is not on `PATH`.

Check the environment:

```powershell
phonebridge doctor                                     # interpreter, packages, adb, firewall notes
powershell -ExecutionPolicy Bypass -File tools\diagnostics\env_report.ps1   # fuller audit (optional)
python tools\verify.py                                 # tests + lint + types + vectors + CLI smoke
```

### 2. The Android app

Two ways, detailed in [`docs/07-device-testing-guide.md`](docs/07-device-testing-guide.md):

```powershell
# A. Android Studio: File -> Open -> android\  then Run 'app'
# B. Command line (needs JDK 17 + Android SDK platform 34)
cd android
.\gradlew.bat assembleDebug
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

A **signed release APK** needs your own keystore — see
[`docs/14-release-readiness.md`](docs/14-release-readiness.md) §3. `gradlew
assembleRelease` without `android/keystore.properties` produces an unsigned APK
that cannot be installed.

### 3. First run on the phone

1. Open **PhoneBridge**, read the **Start here** card, and turn **Bridge** on.
2. The status bar shows the address to use, e.g. `ws://192.168.1.42:8765`.
3. Press **Run protocol self-test**: all checks must pass. This proves the
   phone's crypto matches the PC's before you trust anything else.
4. Grant the permissions you actually want. Each one is requested at the moment
   it is needed and the **Permissions** card explains every state, including
   "blocked by Android" (which only the system settings app can change).

---

## Pairing

The phone always listens; the PC always dials out.

```powershell
# USB (development): forward the port, then connect to localhost
adb forward tcp:8765 tcp:8765
phonebridge info --host 127.0.0.1

# Wi-Fi or hotspot: find the phone instead of typing its address
phonebridge discover
phonebridge info --host 192.168.43.1
```

On the first connection **both screens show a six-digit code**. Compare them:

* identical → press **allow** on the phone and answer `y` on the PC;
* different → answer `n` and stop. Someone is intercepting the connection.

After that the two identity keys are pinned. A later key change is refused
outright — re-pairing is an explicit action (**Forget all paired PCs** on the
phone, `phonebridge forget` on the PC), never an automatic one.

`--auto-pair` exists for scripts and tests only: it accepts the code without
comparing it and prints a warning when used.

---

## Day-to-day use

```powershell
phonebridge devices                    # paired phones + live beacons
phonebridge info --device "Pixel 7"    # no IP needed after the first connection
phonebridge capabilities               # hardware vs usable, with reasons
phonebridge audio status               # headset, profile, relay state
phonebridge ping  --count 20           # application-level RTT
phonebridge bench --frames 3000        # encrypted throughput
phonebridge watch                      # events + automatic reconnection
phonebridge selftest                   # every supported workflow, end to end
```

Every command supports `--json`, and `capabilities --require <key>` turns the
capability report into a gate (exit 4) for scripting. Full reference, exit codes
and examples: [`docs/11-pc-client.md`](docs/11-pc-client.md).

---

## Supported capabilities

The phone reports nine capability keys. `usable` means *implemented in
PhoneBridge v1 **and** the hardware is present **and** the Android permission is
granted* — all three, or it is `false`. `active` is a fourth, separate fact: a
radio that scanned a minute ago is not active, and a sensor that exists is not
streaming.

| Key | Meaning | Usable now |
| --- | --- | --- |
| `audio.playback` | audio can be rendered to a communication device | yes — Phase 2 |
| `audio.capture` | microphone can be read | yes — Phase 2 (needs `RECORD_AUDIO`) |
| `audio.bluetooth_sco` | a bidirectional HFP/SCO headset link is available | yes — Phase 2 |
| `camera.back` | rear camera present and permitted | yes — Phase 4 (lens + `CAMERA` permission + built) |
| `camera.front` | front camera present and permitted | yes — Phase 4 |
| `sensors` | motion/environment sensors present and streamable | yes — Phase 5 |
| `file_transfer` | files can be moved in either direction | yes — Phase 5 (needs a granted SAF folder to receive) |
| `bluetooth_scan` | nearby Bluetooth LE devices can be listed | yes — Phase 5 (`BLUETOOTH_SCAN`, or location on Android ≤11) |
| `bluetooth_gatt` | a peripheral can be connected and read/written | yes — Phase 5 (`BLUETOOTH_CONNECT`, or `BLUETOOTH` ≤11) |

What *is* available in 1.0.0 is the measurement itself: the capability probe
(`phonebridge compat run`, or **Capability probe** in the app) records what the
handset really does — including the awkward case where Android *declares*
off-call SCO but delivers silence — and stores it per device in a report
([`docs/09-compat-report.md`](docs/09-compat-report.md)).

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `cannot connect to ws://…: Connection refused` | Bridge is off in the app, or the port is wrong. Check the address on the phone's screen. |
| `timed out connecting …` | Wrong IP (phone changed Wi-Fi), or Windows Firewall is blocking `python.exe` on Private networks. |
| Works on USB, not on Wi-Fi | The access point uses client isolation. Use the phone's hotspot instead, or USB tethering. |
| `discover` finds nothing | Same as above, plus: broadcast does not cross subnets. Type the IP. |
| `phone identity key does not match the pinned key` | The phone was reflashed, or someone is substituting it. If you expect it, `phonebridge forget <id>` and re-pair. Never ignore this. |
| Pairing prompt never appears | Pairing needs an interactive terminal — the code must be compared by a human. Run the command in a terminal window, not from a script. |
| `pinning store … corrupt` | `%LOCALAPPDATA%\PhoneBridge\peers.json` is unreadable. PhoneBridge refuses to guess which phones you trust: fix or delete the file, then re-pair. |
| Phone shows "no LAN address" | Wi-Fi is off and USB tethering is not enabled. Use `adb forward` or join a network. |
| App stopped after a while | Android battery optimisation killed the service. Allow PhoneBridge to run in the background (**Settings → Apps → PhoneBridge → Battery → Unrestricted**). |
| `adb: device unauthorized` | Accept the USB debugging prompt on the phone. |

More: [`docs/11-pc-client.md`](docs/11-pc-client.md) §13 and
[`docs/07-device-testing-guide.md`](docs/07-device-testing-guide.md).

---

## Known limitations

Written down so nobody has to discover them:

* **Bluetooth, sensors and file transfer have never run on a real handset.** Phase 5
  implements all three end to end and verifies them against the reference phone,
  but no real peripheral, real sensor or real storage has been touched. The
  checklists in [`docs/21-bluetooth.md`](docs/21-bluetooth.md) §10,
  [`docs/22-sensors.md`](docs/22-sensors.md) §10 and
  [`docs/23-file-transfer.md`](docs/23-file-transfer.md) §11 are outstanding.
* **The camera has never run on a real handset.** Phase 4 implements it end to end
  and tests it against the reference phone (synthetic frames); the manual hardware
  checklist in [`docs/20-camera.md`](docs/20-camera.md) §9 is still outstanding.
* **The camera preview needs Pillow on the PC.** Without it the stream is still
  received, validated and counted; the GUI prints "pip install pillow".
* **No sensors, file transfer or Bluetooth scanning.** Protocol v1 reserves message
  kinds for these and implements none, and the docs say so rather than shipping
  dead UI.
* **Android app not verified on a handset by this project.** See
  [`docs/14-release-readiness.md`](docs/14-release-readiness.md).
* **Targets Android API 34.** That runs correctly on Android 16, but Google Play
  requires API 36 for new uploads as of August 2026 — a blocker for Play
  distribution, not for sideloading. Upgrade path in the release checklist.
* **Trust on first use.** The six-digit code is the only defence against an
  active attacker on the very first connection.
* **Identity keys are stored unencrypted** (mode 0600 on the PC, app-private
  storage on the phone). A compromised OS can impersonate the device.
* **Any paired PC can call every method.** There is no per-method authorization
  or audit log beyond the in-memory log.
* **One phone per PC at a time** in the CLI; the phone supports up to 8 sessions
  (4 per PC).
* **PowerShell 5.1 quirks** are documented in [`docs/11-pc-client.md`](docs/11-pc-client.md);
  `pwsh` 7 is recommended but not required.

The full list with reasoning: [`docs/13-security-review.md`](docs/13-security-review.md) §6
and [`docs/16-known-limitations.md`](docs/16-known-limitations.md).

---

## Verify the installation

```powershell
cd windows
python -m pytest -q          # 927 tests
python tools\verify.py       # tests + lint + types + vectors + camera path + CLI smoke (8 checks)
```

The reference phone implementation is part of the repository, so the whole
protocol — handshake, pairing, RPC, streams, reconnection — can be exercised on
one machine, with no phone:

```powershell
python tools\simulator\phone_sim.py --port 8765 --pair-policy auto
# second terminal:
phonebridge info --host 127.0.0.1 --auto-pair
phonebridge selftest --host 127.0.0.1
```

A green run here says the **PC side** is correct. It says nothing about the
Android app; that needs a device and the checklist in
[`docs/12-end-to-end-verification.md`](docs/12-end-to-end-verification.md) §5.

---

## Repository layout

```
PhoneBridge-Start.bat       Windows one-click launcher (update, venv, install, run)
docs/                       architecture, protocol, guides, audits, release docs
windows/phonebridge/
  protocol/                 framing, crypto, messages  (canonical reference)
  transport/                WebSocket link, UDP discovery
  core/                     session, reconnection supervisor, device store
  cli/                      the `phonebridge` command
  controller/               discovery, USB/hotspot reliability, the desktop window
windows/tests/              927 tests
android/app/src/main/java/dev/phonebridge/
  protocol/                 byte-identical Kotlin port + known-answer vectors
  net/                      server, pairing store, identity, discovery, probes
  service/                  foreground service + UI state
tools/simulator/            reference phone implementation (testing)
tools/vectorgen/            generates vectors.json AND ProtocolVectors.kt
tools/verify.py             one command: tests + lint + types + vectors + CLI smoke
```

---

## Security in one paragraph

Each device has a long-lived ECDH P-256 identity key. A session mixes static and
ephemeral ECDH through HKDF-SHA256, so a man-in-the-middle cannot derive the
keys; frames are AES-256-GCM with a separate key per direction, the transmitted
counter is bound into the AEAD tag, and monotonic checks reject replays. First
pairing is trust-on-first-use hardened by a six-digit code both sides must match;
afterwards identity keys are pinned and a changed key is refused rather than
silently accepted. Device ids are self-certifying (the hash of the key), browser
origins are refused at the socket, and the pairing code is never written to a
log. Keys never leave the two machines. Full review with severities, fixes and
residual risk: [`docs/13-security-review.md`](docs/13-security-review.md).

## Deliberate non-goals

* **Not a Bluetooth adapter emulator.** Android's A2DP *sink* is not in the
  public SDK, so Windows will never list your headphones as a PhoneBridge device.
  It is a relay, and the docs say so.
* **No custom Windows drivers.** A virtual microphone or camera needs a signed
  kernel driver; PhoneBridge reuses existing ones (VB-Audio Cable, OBS Virtual
  Camera) only where the direction requires it.
* **No cloud, no telemetry, no accounts.** Nothing leaves your two machines.

---

## Documentation

| I want to… | Read |
| --- | --- |
| install and pair, step by step | [`docs/07-device-testing-guide.md`](docs/07-device-testing-guide.md) |
| use the PC command line | [`docs/11-pc-client.md`](docs/11-pc-client.md) |
| know what is verified, and how | [`docs/12-end-to-end-verification.md`](docs/12-end-to-end-verification.md) |
| read the security review | [`docs/13-security-review.md`](docs/13-security-review.md) |
| ship a build / see what blocks release | [`docs/14-release-readiness.md`](docs/14-release-readiness.md) |
| know what runs where | [`docs/15-compatibility-report.md`](docs/15-compatibility-report.md) |
| see every limitation | [`docs/16-known-limitations.md`](docs/16-known-limitations.md) |
| understand the hardware reality | [`docs/01-feasibility.md`](docs/01-feasibility.md) |
| see the module layout | [`docs/02-architecture.md`](docs/02-architecture.md) |
| implement against the wire format | [`docs/03-protocol.md`](docs/03-protocol.md) |
| know what comes next | [`docs/04-roadmap.md`](docs/04-roadmap.md) |
| verify my own setup | [`docs/05-connection-verification.md`](docs/05-connection-verification.md) |
| measure my three phones | [`docs/08-multi-device-test-strategy.md`](docs/08-multi-device-test-strategy.md) |
| see per-device audio results | [`docs/09-compat-report.md`](docs/09-compat-report.md) |
| stream the phone camera | [`docs/20-camera.md`](docs/20-camera.md) |
| scan Bluetooth LE and talk GATT | [`docs/21-bluetooth.md`](docs/21-bluetooth.md) |
| stream the phone's sensors | [`docs/22-sensors.md`](docs/22-sensors.md) |
| send and receive files | [`docs/23-file-transfer.md`](docs/23-file-transfer.md) |
| read the Phase 5 completion report | [`docs/24-phase5-completion-report.md`](docs/24-phase5-completion-report.md) |
| read the Android completion notes | [`docs/10-android-app-completion.md`](docs/10-android-app-completion.md) |

## License

MIT.
