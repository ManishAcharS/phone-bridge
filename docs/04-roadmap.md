# Roadmap and status

Legend: `[x]` implemented and tested · `[~]` partially implemented ·
`[ ]` not started · `[!]` blocked/limited by the platform (explained)

## Release 1.0.0 — code-complete, two gates open

| | |
| --- | --- |
| Version | `1.0.0` (PC package and Android app) |
| Gate status | 1132 tests (1 skipped), ruff, mypy, `verify.py` 8/8 — all green |
| Open blockers | **B1** no physical-device run · **B2** release APK not built here (plus B3–B6: Play `targetSdk`, keystore, Windows/PowerShell run, Kotlin stub compile) |
| Ship/no-ship | **Not shippable until B1 and B2 are cleared.** See [`14-release-readiness.md`](14-release-readiness.md). |

Milestones 1–3 of the hardware roadmap (the one-click Windows controller, and the
USB/hotspot diagnosis pass) are in [`docs/17`](17-connection-controller.md):
connection manager, transport choice, pairing in the window, reconnection, the
five-state capability registry, and the USB verdict engine.

Every feature phase is now implemented in software: audio
([`19`](19-audio.md)), camera ([`20`](20-camera.md)), Bluetooth LE
([`21`](21-bluetooth.md)), sensors ([`22`](22-sensors.md)) and file transfer
([`23`](23-file-transfer.md)). What is still missing is the thing no amount of
software can supply here: **a physical run** (B1). The table below is the honest
position, updated as each phase landed.

Scope delivered in 1.0.0: Phase 0 (feasibility, architecture, protocol), Phase 1
(encrypted PC ↔ phone channel, CLI, Android app), the end-to-end verification
pass ([`12`](12-end-to-end-verification.md)) and the security hardening pass
([`13`](13-security-review.md)).

## Master implementation status

Legend as above. "Device-tested" means it has been run against the Redmi
Note 14 Pro+; nothing in this table is marked tested on hardware unless it was.

| Area | Status | Where it stands |
| --- | --- | --- |
| USB verdict (identify the obstacle, not just "no device") | `[x]` | `controller/usbcheck.py`, staged A–H report, `phonebridge usb`, **Diagnose USB** button; 53 unit tests, **not device-tested** |
| USB forwarding / transport choice, user-owned forwards preserved | `[x]` | `controller/transports.py`; forwards PhoneBridge owned are released, others left alone |
| Android Open Accessory | `[ ]` | investigated and documented in [`16`](16-known-limitations.md), deliberately not wired in |
| Hotspot stability: health checks, bounded backoff, drop explanation, one session per socket | `[x]` | `controller/manager.py`; proven by tests, **not yet proven on a real hotspot** |
| Wi-Fi discovery, pairing, AES-256-GCM, transport switching, saved endpoints | `[x]` | unchanged from 1.0.0; manual endpoint never defaults to loopback |
| Separate logical channels (control / audio / camera / BT / sensors / files) | `[x]` | `stream_id` 0 control, 1/2 audio, 3 video, 4 sensors, 5/6 files; each plane has its own bounded queue, so a stalled plane cannot starve the others |
| Audio PC → phone (WASAPI loopback → AudioTrack) | `[x]` | Phase 2: `phonebridge/audio/*`, GUI Audio section; 62 automated tests, **not device-tested** |
| Audio phone → PC (AudioRecord → PC monitor) | `[x]` | Phase 2; the in-app monitor only — no virtual microphone (documented, not claimed) |
| Camera streaming and Windows preview | `[x]` | Phase 4: `phonebridge/camera/*`, GUI Camera section; 66 automated tests + 10 end-to-end against the reference phone, **not device-tested** |
| Bluetooth LE scan and GATT (read/write/subscribe) | `[x]` | Phase 5: `BleEngine.kt`, `phonebridge/bluetooth/*`, GUI **Bluetooth** tab; 36 unit + end-to-end tests, **not device-tested** |
| Sensors (13 kinds, dynamic discovery, per-sensor rates) | `[x]` | Phase 5: `SensorEngine.kt`, `phonebridge/sensors/*`, GUI **Sensors** tab; 31 unit + end-to-end tests, **not device-tested** |
| File transfer both ways (SAF, chunked, SHA-256, resume, atomic) | `[x]` | Phase 5: `FileTransferEngine.kt`, `phonebridge/files/*`, GUI **Files** tab; 51 unit + 18 end-to-end tests (18 of those cover all three planes), **not device-tested** |
| Launcher `PhoneBridge-Start.bat` | `[x]` | one-click, self-checking, never resets work |
| Window sections | `[x]` | Connection, Diagnostics, Audio, Camera and a Phase 5 notebook (**Bluetooth** / **Sensors** / **Files**) — every section is drawn only because the feature behind it works |
| Android lifecycle audit (compileSdk/targetSdk, foreground-service types, permissions at point of use) | `[ ]` | not started |

No row is marked `[x]` on the strength of a plan, and no window section exists
without the feature behind it.

## Phase 0 — Technical feasibility ✅

- [x] Environment inspection (this sandbox) and a Windows audit script
      (`tools/diagnostics/env_report.ps1`)
- [x] Android Bluetooth profile / audio-routing research → `docs/01-feasibility.md`
- [x] Windows virtual-device research → same document, §3
- [x] Architecture selected → `docs/02-architecture.md`
- [x] Protocol specified → `docs/03-protocol.md`
- [ ] **Run the audit script on your Windows PC and send me the output** —
      this is the one Phase 0 item that needs your machine

## Phase 1 — Communication foundation ✅ (PC ↔ reference phone)

Status detail: [`docs/06-phase1-audit.md`](06-phase1-audit.md) (audit findings and
build readiness) and [`docs/05-connection-verification.md`](05-connection-verification.md)
(the exact procedure and expected outputs for your hardware);
[`docs/07-device-testing-guide.md`](07-device-testing-guide.md) is the same thing
for getting the app installed and running in the first place.

Implemented and verified **here**, against a reference phone implementation:

- [x] Envelope framing, both directions, with size limits
- [x] P-256 identity, ECDH handshake, HKDF, AES-256-GCM channel
- [x] SAS-verified pairing with pinning on both sides
- [x] RPC (request/response), events (phone → PC), PING/PONG RTT
- [x] Binary stream multiplexing + throughput benchmark
- [x] Reconnection supervisor with jittered exponential backoff
- [x] UDP discovery beacon (phone broadcasts, PC listens)
- [x] CLI: `doctor`, `identity`, `discover`, `info`, `ping`, `bench`, `watch`, `forget`
- [x] Gradle wrapper committed (fresh clone builds; `.gitattributes` pins line endings)
- [x] `python tools/verify.py` — tests, lint, vector drift, wrapper and a live CLI
      handshake in one command
- [x] 210 automated tests: framing, crypto, replay/tamper rejection, end-to-end,
      **11 regression tests** (each verified to fail against the pre-fix code),
      **39 conformance tests** that parse the Kotlin sources and compare their
      constants, vectors and message set to the Python reference, and
      **26 Kotlin-source tests** for the two `assembleDebug` errors of Phase 1
      (an enum inside an `inner class`, `PackageManager.applicationInfo`)
- [x] Kotlin implementation of the identical protocol + on-device self-test with
      Python-generated known-answer vectors (`tools/vectorgen/gen_vectors.py`
      writes `vectors.json` **and** `ProtocolVectors.kt`, so they cannot drift)
- [x] Audit pass: 13 defects found and fixed — including a vector that would have
      failed a *correct* Android build, an Android 14 foreground-service
      `SecurityException`, and an unbuildable Compose/compileSdk pairing

Still needs **your hardware** (I cannot compile or run Kotlin in this sandbox —
no JDK, no Android SDK, no device):

- [ ] `./gradlew assembleDebug` succeeds
- [ ] App installs, service starts, phone listens on 8765
- [ ] On-device self-test: all checks pass (proves Kotlin crypto == Python crypto)
- [ ] `phonebridge info` over **USB + adb forward**
- [ ] `phonebridge info` over **Wi-Fi**
- [ ] `phonebridge info` over **USB tethering** (no adb — the standalone path)
- [ ] `phonebridge info` over **phone hotspot**
- [ ] Pairing SAS matches between the phone screen and the CLI prompt
- [ ] Kill the app / toggle airplane mode → supervisor reconnects unattended

Phase 1 is complete when the first three of those pass. Everything after that is
a network check, and `docs/05-connection-verification.md` gives the exact
commands and expected output for each.

## Phase 2 — Bluetooth audio proof of concept

**Pre-work done (measurement only, no relay):**

- [x] `CapabilityProbe` on the phone: device/ROM facts, adapter and profile
      state, declared off-call SCO, legacy (`startBluetoothSco`) and modern
      (`setCommunicationDevice`) routing, playback route, microphone route with
      a silence check, duplex soak, acoustic chirp latency, interruptions
- [x] `audio.capabilities.run` / `.last` RPCs, answered off the read loop
- [x] On-phone probe UI with per-run screen wake, JSON export
- [x] `phonebridge compat run/show/compare/report` and the compatibility report
      with per-device verdicts (`docs/08`, `docs/09`)
- [ ] **Measure the three handsets** — primary first, twice, then the report
      decides the relay design

Phase 2 entry condition: a stored, real (non-simulated) probe record for the
primary device showing duplex audio. `phonebridge info`'s
`capabilities.audio.bluetooth_sco` flag is only a hint; the probe is the evidence. That flag is already implemented (a read-only probe) and is the
cheapest way to find out whether this phone will do off-call SCO **before** any
audio code is written.

- [ ] `AudioRelay` on the phone: `setCommunicationDevice` + `MODE_IN_COMMUNICATION`,
      confirm route via `AudioDeviceCallback`, then open `AudioRecord(VOICE_COMMUNICATION)`
      and `AudioTrack(USAGE_VOICE_COMMUNICATION)`
- [ ] Report negotiated HFP mode (mSBC 16 kHz vs CVSD 8 kHz) to the PC
- [ ] Detect `isBluetoothScoAvailableOffCall == false` and surface it as
      `unsupported_by_device`
- [ ] Mode A (A2DP, output only) and Mode B (HFP, duplex) with an explicit UI toggle
- [ ] PC: WASAPI loopback capture (no driver), Opus encode, stream
- [ ] PC: Opus decode of the mic stream → write to a virtual cable endpoint
- [ ] Latency measurement: click-loopback test, report p50/p95 one-way
- [ ] Decide: keep audio on TCP, or add the UDP media plane

Expected outcome to verify, not to assume: **70–150 ms one-way** duplex, mono,
16 kHz. If a handset refuses off-call SCO, Mode B is unavailable on that handset
and the UI must say so.

## Phase 3 — Desktop and phone interfaces

- [ ] PySide6 dashboard: Home, Connection, Bluetooth headphones, Audio bridge,
      Camera, Microphone, File transfer, Sensors, Settings
- [ ] Live state from the supervisor (connection method, quality, errors)
- [ ] Volume/mute/connection controls that map to real RPC calls
- [ ] Phone UI polish: foreground notification, pairing screen, mode switch
- [ ] Settings persistence on both sides

## Phase 4 — Camera and microphone

Shipped in the camera half (see [`docs/20-camera.md`](20-camera.md)):

- [x] CameraX capture on the phone, front/rear selectable, resolution and fps settings
- [x] JPEG frames over a dedicated `stream_id` (3), 14-byte header, bounded queues
- [x] PC: decode, validate, preview in the existing GUI window, start/stop/switch, stats
- [x] Capability honesty: hardware / permission / implemented / streaming kept apart
- [ ] **Manual hardware run** — `docs/20-camera.md` §9 (no phone in this environment)
- [ ] H.264 (or VP8) as an optional second codec — `codec = 1` is JPEG so this stays additive
- [ ] PC: `pyvirtualcam` (OBS Virtual Camera / Unity Capture) so the phone looks like a webcam
      to Zoom/Meet/OBS — the preview exists, the virtual device does not
- [ ] Phone microphone as a standalone PC mic (independent of the Bluetooth path)
- [ ] Document the VB-CABLE routing once, and check it against Discord/Zoom/Meet/OBS

## Phase 5 — Bluetooth LE, sensors, file transfer ✅ (software)

Shipped; the three documents are [`21-bluetooth.md`](21-bluetooth.md),
[`22-sensors.md`](22-sensors.md) and [`23-file-transfer.md`](23-file-transfer.md).

- [x] File transfer limited to what SAF permits (no root, no storage permission)
- [x] Chunked transfer both ways with progress, pause/resume, cancel, SHA-256 and
      atomic completion; `.part` + sidecar resume
- [x] BLE scan, deduplicated device list, RSSI, advertised services, live GUI updates
- [x] GATT connect / discover / read / write / subscribe / unsubscribe / disconnect,
      with timeouts and clean teardown
      `[!]` bonding and pairing by the app are not attempted: a peripheral that
      requires a bond fails with the platform's own reason, by design
- [x] Sensors: 13 kinds including accelerometer, gyroscope, magnetometer, gravity,
      linear acceleration, rotation vector, game rotation vector, orientation,
      proximity, light, pressure, step counter and step detector; per-sensor
      metadata, selectable rate, live readings with sequence numbers
- [ ] GPS with foreground location permission — **not implemented**: location is
      not part of this phase, and `ACCESS_FINE_LOCATION` is declared only as the
      Android ≤11 spelling of Bluetooth scanning (capped at `maxSdkVersion="30"`)
- [ ] **Manual hardware run** — the checklists in `docs/21` §10, `docs/22` §10 and
      `docs/23` §11 have not been executed

## Phase 6 — Reliability and polish

- [ ] Reconnect through network changes (Wi-Fi ↔ hotspot ↔ USB) without user action
- [ ] Backpressure and drop policy for media (never block control traffic)
- [ ] Session persistence: remember mode, volume, camera choice
- [ ] Security review: key rotation, unpair flows, rate limiting on pairing attempts
- [ ] Battery and CPU profiling on the phone
- [ ] Packaging: a single `run.ps1` for the PC side

## Honest capability matrix

| Requested capability | Status | Reality |
| --- | --- | --- |
| PC audio → BT headphones, stereo | Planned (Phase 2, Mode A) | A2DP; no headset mic in this mode |
| PC audio ↔ BT headset, duplex with headset mic | Planned (Phase 2, Mode B) | HFP/SCO; mono 8/16 kHz, 70–150 ms |
| Simultaneous stereo + headset mic | `[!]` Not possible | The headset switches profiles; no OS can do this |
| Phone as a real Bluetooth adapter for the PC | `[!]` Not possible | A2DP sink is not in the public Android SDK |
| Phone mic → Discord etc. | Planned (Phase 4) | Needs a VB-CABLE-class device on the PC |
| Phone camera → PC preview | ✅ Implemented (Phase 4) | 720p/15 JPEG over the encrypted session; verified against the reference phone, not yet on hardware |
| Bluetooth LE scan / GATT from the PC | ✅ Implemented (Phase 5) | The phone is the radio; the PC drives it over the authenticated session. Real peripherals not yet tested |
| Phone sensors on the PC | ✅ Implemented (Phase 5) | 13 kinds, per-sensor rate and sequence numbers; real sensors not yet tested |
| File transfer both ways | ✅ Implemented (Phase 5) | 64 KiB chunks, SHA-256, resume, atomic rename; real storage not yet tested |
| Phone camera → Zoom/Meet | Planned (later) | Needs OBS Virtual Camera or Unity Capture; the preview is not a virtual device |
| Arbitrary Bluetooth device management | `[!]` Limited | Scan and BLE GATT yes (Phase 5); bonding and classic pairing are not attempted |
| Everything offline, no accounts | ✅ Already true | No network calls leave the machine |

## Dependency notes

Phase 1 (installed already): `websockets`, `cryptography`.
Phase 2: `soundcard` (WASAPI loopback, Windows-only), `sounddevice` (playback to
a specific endpoint), `numpy`, `av` (bundles libopus — avoids a separate libopus
install on Windows).
Phase 3: `PySide6` (not needed — the window stayed tkinter).
Phase 4: `pyvirtualcam` + OBS Studio or Unity Capture; `opencv-python` if any
image processing is needed.
External, not Python: **VB-Audio Cable** (free) or VoiceMeeter for the virtual
microphone; `adb` from Android platform-tools for USB development; Android
Studio or a JDK 17 + Android SDK for building the app.
