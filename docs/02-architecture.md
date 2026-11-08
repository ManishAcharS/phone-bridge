# Architecture

## 1. One decision that removes a hundred branches

**The phone always listens; the PC always dials.** Every connection method then
collapses into "the PC connects to an IP address":

| Link | How the PC reaches the phone | Notes |
| --- | --- | --- |
| USB (development) | `adb forward tcp:8765 tcp:8765` → `ws://127.0.0.1:8765` | survives only while adb runs |
| USB (standalone) | enable Android **USB tethering** (RNDIS) → `ws://192.168.42.129:8765` | no adb, no root, works on any OS |
| Wi-Fi LAN | `ws://<phone-ip>:8765` | both on the same AP |
| Phone hotspot | PC joins the phone's AP → `ws://192.168.43.1:8765` | no other network needed |

No internet route is used or required in any of the four cases. USB tethering is
the answer to "the final solution should aim for standalone connectivity": it
turns the cable into a private network interface, so the same code path runs
over USB with no adb and no extra protocol.

## 2. Process and module layout

```
┌──────────────────────── Windows PC ────────────────────────┐
│                                                            │
│  PySide6 dashboard (Phase 3)                               │
│      │  reads StateFlow-equivalent state, calls commands   │
│      ▼                                                     │
│  phonebridge.core.session  ── handshake, RPC, streams ──┐  │
│      │                                                  │  │
│      ├── phonebridge.transport.link      (WebSocket)    │  │
│      ├── phonebridge.transport.discovery (UDP beacon)   │  │
│      └── phonebridge.protocol            (framing/crypto) │  │
│                                                            │
│  Phase 2+ sinks/sources:                                   │
│    audio/capture_loopback.py  (WASAPI loopback, no driver) │
│    audio/capture_mic.py       (virtual cable mic → PC)     │
│    audio/opus_codec.py        (PyAV, bundles libopus)      │
│    video/camera_sink.py       (H.264/VP8 → pyvirtualcam)   │
└────────────────────────────────────────────────────────────┘
                            ▲  │
              encrypted WebSocket (control + media)
                            │  ▼
┌──────────────────────── Android phone ─────────────────────┐
│  BridgeService (foreground, connectedDevice)               │
│      ├── BridgeServer   (Java-WebSocket listener)          │
│      │     └── PeerSession: handshake → auth → READY       │
│      ├── DiscoveryBeacon (UDP broadcast every 2 s)         │
│      ├── IdentityStore   (P-256 identity, app-private)     │
│      ├── PairingStore    (pinned PCs)                      │
│      ├── AudioProbe      (AudioManager route inspection)   │
│      └── Phase 2+: AudioRelay, CameraStreamer, FileService │
│  MainActivity (Compose): status, pairing approval, self-test│
└────────────────────────────────────────────────────────────┘
```

Both sides run the *same* protocol library shape:

```
protocol/framing   envelope codec          (byte-identical, Python ↔ Kotlin)
protocol/crypto    identity, handshake, AEAD channel
protocol/messages  message types, RPC/event JSON, helpers
```

The Python implementation is canonical: `tools/vectorgen/gen_vectors.py` emits
known-answer vectors that the Android self-test checks on-device, so the two
implementations cannot silently drift.

## 3. Two planes, one codec

* **Control plane** — JSON RPC + events over the framed WebSocket. Small,
  reliable, ordered. Handles commands, status, pairing, file-transfer progress.
* **Media plane** — binary frames on the same channel for Phase 1, with a
  dedicated stream per flow (`stream_id`). Phase 2 adds a UDP path for audio if
  TCP head-of-line blocking shows up in measurements on Wi-Fi.

The envelope codec is transport-agnostic on purpose: it already supports a raw
byte stream (`EnvelopeStreamParser` in Python), so moving audio to UDP/TCP later
does not touch the message layer or the crypto layer.

## 4. Security model

| Concern | Decision |
| --- | --- |
| Identity | Per-device ECDH **P-256** keypair. Device id = `SHA-256(pubkey)[:16 hex]`. |
| Key agreement | `ikm = ECDH(static,static) ‖ ECDH(ephemeral,ephemeral)`, HKDF-SHA256, per-session nonces. A relay/MITM cannot derive the keys (needs both static private keys). |
| Transport encryption | AES-256-GCM per frame, **separate key per direction** (no nonce reuse by construction), explicit counter in AAD, strict monotonic counter → replay and reordering rejected. |
| Pairing | Trust-on-first-use hardened with a **6-digit SAS** derived from the session key, shown on both devices and compared by the user. A MITM produces different digits on each side. |
| Pinning | After pairing, the identity keys are pinned in local files (`peers.json` on the PC, `PairingStore` on the phone). A changed key is refused, not silently re-paired. |
| Storage | PC: `%LOCALAPPDATA%\PhoneBridge`, files mode 0600. Phone: app-private SharedPreferences. |
| Cloud | None. No accounts, no telemetry, no external calls. |

Why not TLS: the link is already end-to-end authenticated with pinned keys. TLS
on top would add a second, unpinned trust layer plus certificate management on
both sides, for no additional guarantee on a LAN/USB link.

Why not X25519/ChaCha20: not uniformly available through `javax.crypto` on the
Android versions we support. P-256 ECDH + AES-GCM are available from API 23+ and
hardware-accelerated. The design does not depend on the curve choice.

## 5. Threading

* **PC**: `asyncio` throughout. One read task per session, RPC futures, media
  handled by callback/queue. The reconnecting supervisor owns the retry loop.
* **Phone**: Java-WebSocket delivers frames on its own threads, so each
  connection is an **event-driven state machine** (`AWAIT_HELLO → AWAIT_AUTH →
  AWAIT_PAIR_CONFIRM → READY`) guarded by a per-connection lock. No blocking
  reads inside a callback.

## 6. Failure behaviour (designed, not accidental)

| Failure | Behaviour |
| --- | --- |
| Phone app closed | PC supervisor reports `reconnecting`, retries with jittered exponential backoff (0.5 s → 15 s) |
| Wi-Fi drops and returns | Same; pairing and identity persist, so reconnection needs no user action |
| Wrong/rogue device | Handshake fails with a specific message ("identity key changed for known device"), never a silent reconnect |
| Malformed frame | Framing error closes that connection only; the server keeps listening |
| Replayed frame | `ReplayError`, connection closed |
| Pairing declined | Both sides store nothing |
| Port already in use | `start()` returns a failure, UI shows the error, service stops instead of pretending |

## 7. What is deliberately *not* here yet

* No PySide6 window — Phase 3. The CLI exercises every command path first.
* No audio/video modules — Phase 2 and 4. Their interfaces are described in the
  roadmap so the protocol does not need to change to accommodate them.
* No UDP media transport — added in Phase 2 only if measurements justify it.
