# PhoneBridge

Use an Android phone as an external hardware hub for a Windows PC — starting
with Bluetooth headphones, over USB, Wi-Fi or the phone's hotspot. No cloud, no
accounts, no internet access required.

This is a personal-use project for one phone and one PC.

---

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 0 | Feasibility, architecture, protocol | **done** → [`docs/01-feasibility.md`](docs/01-feasibility.md) |
| 1 | Encrypted bidirectional link PC ↔ phone | **PC side verified (184 tests); Android side compiled/run only on your machine** → [`docs/06-phase1-audit.md`](docs/06-phase1-audit.md) |
| 2 | Bluetooth audio relay (the defining feature) | next |
| 3 | Desktop + phone dashboards | planned |
| 4 | Camera and microphone | planned |
| 5 | File transfer, Bluetooth manager, sensors | planned |
| 6 | Reliability and polish | planned |

Details and the per-feature reality check: [`docs/04-roadmap.md`](docs/04-roadmap.md).

### The one thing to know before anything else

PC audio *and* the headset microphone **cannot both be high quality**. A
Bluetooth headset renders one profile at a time:

* **A2DP** — stereo 44.1/48 kHz output, **no microphone**.
* **HFP/SCO** — mono 8/16 kHz, **bidirectional**, so this is the only path that
  carries the headset mic back to your PC, at call quality.

PhoneBridge exposes both as explicitly labelled modes instead of pretending one
of them is something it is not. Expected duplex latency is **70–150 ms
one-way** — fine for Discord, Zoom, Meet and OBS monitoring; not for musical
monitoring. Full reasoning: [`docs/01-feasibility.md`](docs/01-feasibility.md).

---

## What works today (Phase 1)

A real, encrypted, bidirectional channel between the PC and the phone, with:

* P-256 ECDH handshake, HKDF-SHA256 session keys, AES-256-GCM frames
* SAS-verified first-time pairing, identity pinning afterwards
* JSON RPC + events + multiplexed binary streams
* PING/PONG latency measurement
* Automatic reconnection with jittered exponential backoff
* UDP discovery beacon (no typing IP addresses)
* USB (adb forward *or* USB tethering), Wi-Fi and hotspot transports

Measured on loopback with the reference phone implementation:
application RTT **0.29–0.78 ms**, 30 MB/s encrypted payload, **184 tests passing**
(including 39 that check the Kotlin implementation's constants and known-answer
vectors against the Python reference, so the two cannot silently drift).

---

## Quick start — PC side

Requires Windows 10/11 and Python 3.10+.

```powershell
cd windows
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"

phonebridge doctor          # what this PC can do (Phase 0 audit)
phonebridge identity        # this PC's device id and pinned phones

# one command that runs every check this repo can run without a phone:
python tools\verify.py
```

`python -m phonebridge ...` works identically if the `phonebridge` script is not
on PATH.

Also run the deeper Windows audit once and keep the output:

```powershell
powershell -ExecutionPolicy Bypass -File tools\diagnostics\env_report.ps1 -OutFile audit.txt
```

## Quick start — phone side

Requires Android Studio (JDK 17 + Android SDK come with it) and a phone on
Android 8.0+.

The Gradle wrapper is committed, so a fresh clone builds without installing
Gradle. Full instructions — SDK components, phone installation, the on-device
self-test, and connection troubleshooting — are in
[`docs/07-device-testing-guide.md`](docs/07-device-testing-guide.md).

```powershell
# Option A: File -> Open -> phone-bridge\android, then Build APK(s)
# Option B: command line (needs JDK 17 + SDK Platform 34)
cd android
.\gradlew.bat assembleDebug
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

Then in the app: flip **Bridge** on, and press **Run protocol self-test**. All
checks must pass — that proves the phone's crypto matches the PC's. If any check
fails, stop there and send me the output.

## Connecting

The phone always listens on TCP 8765; the PC always dials out. Pick whichever
path you have:

```powershell
# USB, development only (adb must stay running)
adb forward tcp:8765 tcp:8765
phonebridge info --host 127.0.0.1

# USB, standalone: enable "USB tethering" on the phone, then use its new address
phonebridge info --host 192.168.42.129

# Wi-Fi or hotspot: the phone's IP on that network
phonebridge discover                 # listen for the beacon instead of typing an IP
phonebridge info --host 192.168.43.1
```

First connection asks you to compare a six-digit code between the phone screen
and the terminal. If the codes differ, someone is intercepting — answer `n`.

Then:

```powershell
phonebridge ping  --host <ip> --count 20
phonebridge bench --host <ip> --frames 3000 --size 1024
phonebridge watch --host <ip>          # live events + auto-reconnect
```

## Verify without a phone

The reference phone implementation is part of the repo, so the whole protocol,
pairing flow and reconnect logic can be exercised on one machine:

```powershell
python tools\simulator\phone_sim.py --port 8765 --pair-policy auto
# in another terminal:
cd windows
python -m pytest -q                    # 184 tests
```

---

## Repository layout

```
docs/                       feasibility, architecture, protocol, roadmap,
                            Phase-1 audit and connection-verification procedure
windows/phonebridge/
  protocol/                 framing, crypto, messages  (canonical reference)
  transport/                WebSocket link, UDP discovery
  core/                     session, reconnection supervisor, device store
  cli/                      the `phonebridge` command
windows/tests/              184 tests: framing, crypto, replay, end-to-end,
                            regressions, Kotlin<->Python conformance
android/app/src/main/java/dev/phonebridge/
  protocol/                 byte-identical Kotlin port + self-test vectors
  net/                      server, pairing store, identity, discovery, audio probe
  service/                  foreground service + UI state
tools/simulator/            phone reference implementation (testing)
tools/vectorgen/            generates vectors.json AND ProtocolVectors.kt
tools/verify.py             one command: tests + lint + vectors + wrapper + CLI smoke
tools/simulator/            reference phone; can imitate any of the three devices
tools/diagnostics/          Windows environment audit
```

## Security in one paragraph

Each device has a long-lived ECDH P-256 identity. Sessions mix static and
ephemeral ECDH through HKDF-SHA256, so a man-in-the-middle cannot derive the
keys. Frames are AES-256-GCM with a separate key per direction, a transmitted
counter bound into the AEAD tag, and strict monotonic checks that reject replays.
First pairing is trust-on-first-use hardened by a six-digit code both sides must
match; afterwards the identity keys are pinned locally and a changed key is
refused rather than silently accepted. Keys never leave the two machines.
`%LOCALAPPDATA%\PhoneBridge` on the PC (files mode 0600) and app-private storage
on the phone.

## Deliberate non-goals

* **Not a Bluetooth adapter emulator.** Android's A2DP *sink* is not in the
  public SDK, so Windows will never list your headphones as a PhoneBridge
  device. It is a relay, and the docs say so.
* **No custom Windows drivers.** A virtual microphone or camera needs a signed
  kernel driver. PhoneBridge reuses an existing one — VB-Audio Cable for audio,
  OBS Virtual Camera / Unity Capture for video — and only where the direction
  actually requires it. PC → phone audio needs no driver at all (WASAPI loopback).
* **No cloud, no telemetry, no accounts.** Nothing leaves your two machines.

## Where to look next

| I want to… | Read |
| --- | --- |
| understand what the hardware allows | [`docs/01-feasibility.md`](docs/01-feasibility.md) |
| see the module layout and security model | [`docs/02-architecture.md`](docs/02-architecture.md) |
| implement against the wire format | [`docs/03-protocol.md`](docs/03-protocol.md) |
| know what is next | [`docs/04-roadmap.md`](docs/04-roadmap.md) |
| **install and test on my phone** | [`docs/07-device-testing-guide.md`](docs/07-device-testing-guide.md) |
| **measure the three phones** | [`docs/08-multi-device-test-strategy.md`](docs/08-multi-device-test-strategy.md) |
| **see the per-device results** | [`docs/09-compat-report.md`](docs/09-compat-report.md) |
| **verify my own setup** | [`docs/05-connection-verification.md`](docs/05-connection-verification.md) |
| see what was audited and what is still unproven | [`docs/06-phase1-audit.md`](docs/06-phase1-audit.md) |

## License

Personal use. MIT.
