# Roadmap and status

Legend: `[x]` implemented and tested · `[~]` partially implemented ·
`[ ]` not started · `[!]` blocked/limited by the platform (explained)

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

Implemented and verified **here**, against a reference phone implementation:

- [x] Envelope framing, both directions, with size limits
- [x] P-256 identity, ECDH handshake, HKDF, AES-256-GCM channel
- [x] SAS-verified pairing with pinning on both sides
- [x] RPC (request/response), events (phone → PC), PING/PONG RTT
- [x] Binary stream multiplexing + throughput benchmark
- [x] Reconnection supervisor with jittered exponential backoff
- [x] UDP discovery beacon (phone broadcasts, PC listens)
- [x] CLI: `doctor`, `identity`, `discover`, `info`, `ping`, `bench`, `watch`, `forget`
- [x] 45 automated tests (framing, crypto, replay/tamper rejection, end-to-end)
- [x] Kotlin implementation of the identical protocol + on-device self-test with
      Python-generated known-answer vectors

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

## Phase 2 — Bluetooth audio proof of concept

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

- [ ] CameraX capture on the phone, front/rear selectable, resolution and fps settings
- [ ] H.264 (or VP8) encode, stream over a dedicated `stream_id`
- [ ] PC: decode → `pyvirtualcam` (OBS Virtual Camera / Unity Capture backend)
- [ ] Phone microphone as a standalone PC mic (independent of the Bluetooth path)
- [ ] Document the VB-CABLE routing once, and check it against Discord/Zoom/Meet/OBS

## Phase 5 — File transfer, Bluetooth manager, sensors

- [ ] File browser limited to what MediaStore/SAF permits (no root)
- [ ] Chunked upload/download with progress, resume, checksums
- [ ] BLE scan + device list + authorised GATT read/write
      `[!]` connecting/pairing arbitrary devices is not exposed to third-party apps
- [ ] Sensors: accelerometer, gyroscope, orientation, rotation vector
- [ ] GPS with foreground location permission

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
| Phone camera → Zoom/Meet | Planned (Phase 4) | Needs OBS Virtual Camera or Unity Capture |
| Arbitrary Bluetooth device management | `[!]` Limited | Scan/BLE GATT yes; connect/pair generally no |
| Everything offline, no accounts | ✅ Already true | No network calls leave the machine |

## Dependency notes

Phase 1 (installed already): `websockets`, `cryptography`.
Phase 2: `soundcard` (WASAPI loopback, Windows-only), `sounddevice` (playback to
a specific endpoint), `numpy`, `av` (bundles libopus — avoids a separate libopus
install on Windows).
Phase 3: `PySide6`.
Phase 4: `pyvirtualcam` + OBS Studio or Unity Capture; `opencv-python` if any
image processing is needed.
External, not Python: **VB-Audio Cable** (free) or VoiceMeeter for the virtual
microphone; `adb` from Android platform-tools for USB development; Android
Studio or a JDK 17 + Android SDK for building the app.
