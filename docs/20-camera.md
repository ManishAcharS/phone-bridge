# 20 — Camera streaming (Phase 4)

The phone's camera, live on the PC's screen, over the connection that already
exists. No second socket, no unencrypted media path, no separate application
window, and no frames when nobody asked.

Status: **implemented on both sides; verified against the reference phone and the
PC's own tests; not yet run on a physical handset** (see §9). Pull request:
[#11](https://github.com/ManishAcharS/phone-bridge/pull/11), based on `main`
`7f7349d` (Phase 2 audio, PR #10 — already merged).

---

## 1. What this phase adds

| Piece | Where | What it does |
| --- | --- | --- |
| Camera capture | `android/.../net/CameraEngine.kt` | CameraX `ImageAnalysis` → JPEG → `MSG_VIDEO`, rear/front, configurable resolution and frame rate |
| Wire format | `windows/phonebridge/camera/frames.py` | 14-byte header, one JPEG per message, sequence + timestamp + rotation, strict validation |
| Stream control | `windows/phonebridge/camera/stream.py` | start / stop / switch, bounded queue, newest-frame-wins gate, stats, error text |
| Preview decode | `windows/phonebridge/camera/preview.py` | optional Pillow decode; says so when Pillow is missing |
| RPCs | `BridgeServer.dispatchRpc`, `docs/03` §6 | `camera.start`, `camera.stop`, `camera.switch`, `camera.status` |
| PC GUI | `windows/phonebridge/controller/ui/app.py` | a **Camera** section in the existing window: start/stop, switch lens, resolution + frame-rate pickers, preview, throughput, error line |
| PC manager | `controller/manager.py` | one `CameraStream` per authenticated session, torn down with it |
| Capabilities | `DeviceFacts.kt`, `controller/capabilities.py` | hardware / permission / implemented / streaming kept apart |
| Diagnostics | `cli/capabilities`, `cli/selftest.py`, `tools/verify.py` | the camera is no longer listed as "not implemented", and the header is round-trip checked |

Phases 1–3 are untouched: pairing, authentication, AES-GCM framing, the
connection manager, USB/Wi-Fi/hotspot transports and the Phase 2 audio relay are
all exactly as they were. Video is an *additive* message kind and an *additive*
stream id.

---

## 2. Architecture

```
  phone (Android)                                     PC (Windows)
  ┌──────────────────────┐                          ┌────────────────────────┐
  │ CameraX ImageAnalysis│  JPEG (2 MiB cap)        │ CameraStream           │
  │  KEEP_ONLY_LATEST    │──► VideoFrames.encode ──►│  decode_frame (strict) │
  │  15 fps target       │      MSG_VIDEO, id 3     │  FrameGate (seq/dup)   │
  │  bounded send queue  │      inside AES-256-GCM  │  asyncio.Queue(5)      │
  └──────────────────────┘                          │  newest frame wins     │
             ▲                                      └───────────┬────────────┘
             │ camera.start / stop / switch / status            │ snapshot()
             └──────────── same encrypted session ─────────────┘
                                                                  ▼
                                                     GUI Camera section (Tk)
                                                     preview Label + stats
```

Four properties are structural, not conventions:

1. **One transport.** Video rides the session the connection manager already
   authenticated. There is no `0.0.0.0` listener, no second port and no plaintext
   fallback to add by mistake.
2. **One direction.** Phone → PC in protocol v1. A `MSG_VIDEO` arriving at the
   phone is logged and ignored; the PC accepts only stream 3.
3. **Bounded everywhere.** CameraX keeps no backlog (`STRATEGY_KEEP_ONLY_LATEST`),
   the phone's send queue holds 2 frames, the PC's holds 5. Overflow drops the
   **oldest** frame, so latency stays flat instead of growing into a slideshow of
   the past.
4. **Stop means stop.** `camera.stop`, a closed session, a failed start, a
   phone-side Stop button and service destruction all unbind the CameraX use cases
   and release the lens for other apps.

---

## 3. Protocol

Normative table: `docs/03-protocol.md` §6 (`MSG_VIDEO body`). Summary:

```
offset  size  field
0       1     codec      1 = JPEG
1       1     flags      bit0 START, bit1 STOP, bit2 KEY
2       2     seq        big-endian, wraps at 65535
4       4     ts_ms      big-endian, monotonic ms since this stream started
8       2     width      pixels, as encoded
10      2     height     pixels, as encoded
12      2     rotation   0 / 90 / 180 / 270 — what the receiver must rotate
14      n     JPEG       FF D8 ... FF D9, at most 2 MiB
```

Why JPEG and not a video codec: a video codec needs a stateful encoder and decoder,
so one lost packet corrupts every following frame until a keyframe, and recovery
after a reconnect is a decode loop. With one JPEG per message, a lost frame is one
lost picture, a dropped burst recovers on the next message, and any client that can
decode a JPEG can show the stream. The cost — more bytes per second — is the right
trade for a USB/Wi-Fi link whose bottleneck is usually the phone's encoder, not the
socket. `codec = 1` is defined so that a future H.264 option can be added without
breaking v1 clients.

Rules the PC enforces (`decode_frame`, all covered by tests):

* body shorter than 14 bytes, unknown codec, impossible size, rotation outside
  {0, 90, 180, 270}, payload not starting `FF D8` or not ending `FF D9`, body over
  2 MiB + header → **dropped and counted** (`stats.invalid`), never fatal;
* a duplicate or a frame older than one already shown is dropped, so the preview
  never rewinds (`stats.duplicates`, `stats.late`);
* a forward jump resynchronises instead of stalling (`stats.reordered`);
* `STOP` clears the preview and returns the stream to `idle`; it carries no picture.

**RPCs** (all additive; an old client never calls them):

| method | params | result |
| --- | --- | --- |
| `camera.start` | `config` object (optional) | `{ok, config}` |
| `camera.stop` | — | `{ok, streaming:false}` |
| `camera.switch` | `facing` (`back`/`front`) | `{ok, facing, streaming}` |
| `camera.status` | — | `{streaming, facing, width, height, fps, frames_sent, dropped, permitted, hardware, last_error}` |

**Error codes** (docs/03 §7): `camera_denied` (permission not granted — the lens is
never opened to find out), `no_camera` (this phone has no such lens), `camera_busy`
(another app holds it, or CameraX refused to bind), `bad_config` (outside the
documented ranges), plus the existing `bad_request`/`internal`.

---

## 4. Permissions

`CAMERA` is a normal runtime permission and is requested **when the user presses
"Allow camera" in the Camera card**, never at launch, never in the background and
never because the PC asked. If it is missing:

* `camera.start` fails with `camera_denied` and a sentence the PC shows verbatim
  ("camera permission is not granted: open PhoneBridge on the phone and allow
  Camera");
* the phone's capability map reports `camera.back = false` while
  `hardware.camera.back = true`, and `permitted.camera = false`;
* `Permissions.kt` distinguishes *denied* (ask again) from *permanently denied*
  (open Settings instead of nagging) exactly as it does for the microphone.

The manifest declares `<uses-feature android:name="android.hardware.camera.any"
android:required="false" />` so the APK still installs on a device without a
camera; that device answers `no_camera` instead of crashing.

---

## 5. Configuration

Defaults are deliberately conservative for an ordinary phone on slow Wi-Fi:

| Setting | Default | Allowed |
| --- | --- | --- |
| Resolution | 1280×720 | 320–3840 × 240–2160 (whatever CameraX can bind) |
| Frame rate | 15 fps | 1–60 |
| JPEG quality | 70 | 20–95 |
| Lens | rear | `back`, `front` |

`VideoConfig` validates every field on both sides; an out-of-range value is
`bad_config`, never silently clamped. The PC's answer is authoritative: if the
phone binds 1280×720 when 1920×1080 was asked for, `camera.start` returns what it
really bound and the GUI shows that.

Rough bandwidth at the defaults: 720p JPEG q70 is typically 60–120 KiB per frame,
so 15 fps is ~1–2 MB/s — comfortable over USB and 5 GHz Wi-Fi, and the reason the
default is 720p/15 rather than 1080p/30. Use 640×480 @ 10 for a weak link; use
1080p @ 24 for a still subject on USB.

---

## 6. Transports

The camera does not know which transport it is on — it rides the session. In
practice:

| Transport | Notes |
| --- | --- |
| USB (`adb forward`) | best latency, no Wi-Fi power saving; the default choice for long streams |
| Wi-Fi (LAN) | works at 720p/15; 1080p may stutter on 2.4 GHz |
| Hotspot | the phone is usually the AP, so expect ~5–10 Mbps unless the PC is the AP; start at 640×480 |

If the link dips, the bounded queues mean the preview *skips* rather than lags;
`stats.dropped` and `stats.received` in the GUI tell you which. The connection
manager's existing reconnect ladder still applies: a dropped session brings the
camera down (`camera.streaming` false) and the camera is not restarted
automatically — after reconnecting, press start again. That is intentional: a
camera that comes back on its own is a camera the user did not ask for.

---

## 7. Windows side

* **Decode and validate** — `camera/frames.py` (`decode_frame`, `FrameGate`,
  `VideoStats`).
* **Stream control** — `camera/stream.py` (`CameraStream.start/stop/switch/push`,
  `snapshot()`, `latest_frame()`). Every failure becomes a sentence in
  `snapshot().detail`; nothing raises into the UI thread.
* **GUI** — the Camera section of the existing window (`controller/ui/app.py`),
  driven by the same 100 ms tick as the rest of the UI:
  * start/stop button that reflects the *actual* state (`active`/`starting` →
    "stop");
  * Switch lens (disabled while idle — switching an idle camera would open one);
  * resolution and frame-rate pickers; a change applies to the next start and the
    status line says so;
  * one `Label` preview, refreshed only when the sequence number changes, so a
    slow decode cannot queue up behind itself;
  * a status line with state, lens, resolution, frame rate, frames received and
    any error.
* **No separate window.** The camera lives inside the existing Tk application and
  never opens its own; the UI tick is non-blocking and all RPCs are submitted to
  the runtime's event loop.
* **Preview decode is optional.** Pillow is not a hard dependency. Without it the
  preview label says "frames are arriving, but this PC cannot draw them:
  pip install pillow" — it does not fake a picture and it does not pretend the
  stream failed.

---

## 8. Capabilities and diagnostics

Four facts, never collapsed (`docs/03` §6, `Capability` in
`controller/capabilities.py`):

| Fact | Source | Meaning |
| --- | --- | --- |
| `hardware.camera.back/front` | `CameraManager` on the phone | the lens exists |
| `permitted` | `CAMERA` granted? | the user allowed it |
| `implemented.camera.*` | this build | the feature exists |
| `active` | the live stream | frames are flowing **now** |

`available` = hardware **and** implemented **and** permitted. A phone with a lens
and no permission reports *unavailable*, with the reason in
`detail.camera.back`. The self-test's "not implemented" list no longer contains a
camera row, and `tools/verify.py` gains a "Camera frame path" step that round-trips
the header and reports whether a preview decoder exists.

---

## 9. Manual hardware checklist (not yet done)

Nothing below has been run: this development environment has no phone, no JDK, no
Android SDK and no Pillow. Everything in §10 is what *was* verified.

Run on a real phone and PC, in this order:

1. **Build and install.** `cd android && .\gradlew.bat assembleDebug`, then
   `adb install -r app\build\outputs\apk\debug\app-debug.apk`.
2. **Permission timing.** Launch the app: no camera dialog. Connect a PC, start the
   GUI, press **Camera → start**: the phone shows a permission dialog (first run
   only). Refuse it → the GUI must show `camera permission is not granted` and no
   preview. Grant it → start again → a live picture within ~1 s.
3. **Unmistakable state.** While streaming, the phone's Camera card shows the red
   **● STREAMING TO PC** badge, the lens, the resolution and a live self-view.
   Press **Stop streaming** on the phone: the PC's preview clears within ~100 ms and
   `camera.status` reports `streaming: false`.
4. **Lens switch.** Press **Switch lens** mid-stream: the picture changes to the
   front camera and the badge's resolution/rate stay as configured.
5. **Release.** With the stream running, open the phone's own camera app: it must
   open without an "in use" error after stopping the stream from the PC. Then start
   a stream, kill the PC GUI: the phone must stop within a second (session close →
   release) and the badge must go away.
6. **Contention.** Start the phone's camera app first, then press start on the PC:
   expect `camera_busy` with a readable message, not a hang or a black preview.
7. **Display off.** Stream with the screen off for two minutes: the frames must keep
   coming (foreground service) and the phone must not be hot. Note the battery
   percentage drop; if it exceeds a few percent, lower the resolution.
8. **Transports.** Repeat a 60-second stream over USB, Wi-Fi and a hotspot. Record
   `received`, `dropped` and the observed latency in the GUI's status line. Expect
   USB clean, Wi-Fi smooth at 720p/15, hotspot usable at 640×480/10.
9. **Interruption.** Toggle aeroplane mode mid-stream: the PC reports the drop, the
   phone stops the camera, and after reconnecting a fresh start works.
10. **Rotation.** Hold the phone in landscape while streaming: the picture must stay
    upright (the header's `rotation` field carries the degrees).

Record the results — including failures — in `docs/11-verification-matrix.md`.

---

## 10. What has actually been verified

Run in this repository, on this machine:

| Check | Result |
| --- | --- |
| `pytest` (PC side) | 927 passed, 1 skipped |
| `ruff check .` | clean |
| `mypy` (41 files) | clean |
| `tools/verify.py` | all steps pass, including the new camera frame path |
| Frame round-trip, 34 cases | header/rotation/JPEG validation, duplicates, stale, wrap, oversized |
| Stream lifecycle, 22 cases | start/stop/switch, refusal, drops, duplicates, session end, callback errors |
| Camera end-to-end, 10 cases | **real** WebSocket + handshake + AES-GCM against the reference phone: frames arrive, preview updates, stop reaches the phone, a dead session releases the "camera", malformed frames are counted |
| Android build | **not run** — no JDK/SDK/Gradle in this environment |
| Android unit tests | **not run** — same reason |
| Physical camera | **not tested** — no phone attached |

The end-to-end tests use the reference phone in `tools/simulator/phone_sim.py`,
whose frames are synthetic JPEGs. They prove the PC side of the wire and the
protocol; they prove nothing about CameraX, the lens, or the phone's thermal
behaviour. §9 is the remaining work, and it needs hardware.

---

## 11. Troubleshooting

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| `camera permission is not granted` | `CAMERA` not granted, or granted for another user profile | Allow camera in the Camera card; if it is blocked, open Settings › Apps › PhoneBridge › Permissions |
| `this phone has no front camera` / `no_camera` | lens absent, or the selector is wrong | try the other lens; check `phonebridge info` → `hardware.camera.front` |
| `the camera could not be opened (another app may be using it)` | another app holds the camera | close it, press start again |
| Frames arrive, no picture | Pillow is not installed on the PC | `pip install pillow`; the status line says exactly this |
| Picture freezes but stats still climb | GUI cannot keep up (very large frames) | lower the resolution; check `dropped` — if it rises, the network is the limit |
| Preview lags further and further behind | should not happen — bounded queues drop the oldest frame | if it does, report the `stats` values; it is a bug, not a setting |
| Stream stops right after starting | the session is dropping | check the Diagnostics pane and the connection-manager retry ladder |
| `bad_config` on start | requested a resolution outside 320–3840 × 240–2160, fps outside 1–60, quality outside 20–95 | pick a listed value |
| Phone gets hot on long streams | expected above 1080p/24 | 720p/15, or stop when you are not looking |

---

## 12. Files

Created:

* `windows/phonebridge/camera/{__init__,frames,stream,preview}.py`
* `windows/tests/test_camera_frames.py`, `test_camera_stream.py`, `test_camera_e2e.py`
* `android/app/src/main/java/dev/phonebridge/net/{VideoFrames,CameraEngine}.kt`
* `android/app/src/main/java/dev/phonebridge/CameraCard.kt`
* `docs/20-camera.md`

Modified:

* `windows/phonebridge/core/session.py` (`send_video`, per-stream `subscribe_blobs` maxsize)
* `windows/phonebridge/controller/{manager,runtime,view}.py`, `controller/ui/app.py`
* `windows/phonebridge/controller/capabilities.py` (camera permission mapping)
* `windows/phonebridge/cli/{format,selftest}.py`
* `tools/simulator/phone_sim.py` (`camera.*` RPCs, frame emission, release on disconnect)
* `tools/verify.py`
* `android/app/build.gradle.kts` (CameraX 1.3.4), `AndroidManifest.xml`, `Permissions.kt`,
  `net/{BridgeServer,DeviceFacts}.kt`, `service/BridgeState.kt`, `MainActivity.kt`
* `docs/03-protocol.md`, `docs/04-roadmap.md`, `docs/11-verification-matrix.md`,
  `docs/16-known-limitations.md`, `README.md`
* `windows/tests/{test_protocol_contract,test_android_build,test_android_completion,test_cli,test_controller_capabilities,test_controller_e2e,test_e2e_workflows}.py`
