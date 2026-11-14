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
| `camera.status` | — | `{streaming, facing, width, height, fps, frames_sent, dropped, permitted, hardware, last_error, stats}` |

`stats` carries the phone's own measurements: `fps`, `encode_ms`, `send_ms`,
`bytes_per_frame`, `quality`, `width`, `height`, `encoded`, `sent`, `dropped`, plus
`path` (`yuv` or `rgba` — the encoder actually in use), `path_reason`,
`capture_width`/`capture_height` (what CameraX really delivered) and
`capture_layout` (format, rotation, crop and per-plane strides of the first
frame). `width`/`height` are the *request*; `capture_*` are the answer, and the two
disagreeing is the explanation for frames that are far bigger than asked for.

**Error codes** (docs/03 §7): `camera_denied` (permission not granted — the lens is
never opened to find out), `no_camera` (this phone has no such lens), `camera_busy`
(another app holds it, or CameraX refused to bind), `bad_config` (outside the
documented ranges), plus the existing `bad_request`/`internal`.

**Encoding.** Two paths, and the fast one is the default.

*The YUV path.* The analysis use case asks for `YUV_420_888` and a resolution via
`ResolutionSelector` + `ResolutionStrategy(CLOSEST_LOWER_THEN_HIGHER)` — `setTargetResolution`
is deprecated and treats the size as an aspect-ratio hint, which lets a 640×480
request come back as 1920×1440 (that is how a stream reaches 400–590 KB per
picture). Frames are packed into NV21 by `Nv21` (honouring every plane's
`rowStride` **and** `pixelStride`, plus the crop rectangle) and handed to
`YuvImage.compressToJpeg`: no `Bitmap`, no RGBA conversion.

*The RGBA fallback.* Asking for `RGBA_8888` costs a native YUV→RGB pass, a
`Bitmap` allocation, a stride crop and an RGB→YCbCr conversion inside the
encoder, so it is the fallback rather than the default — but it is also the path
that predates the YUV one, and a device where the YUV path produces nothing must
not leave the user with a dead camera. The switch happens automatically, and is
logged:

* **the 2 s watchdog** — nothing encoded within 2 s of the bind
  (`FALLBACK_AFTER_MS`) switches paths and rebinds the same camera;
* **any encoder failure** — an exception from the packing or from
  `compressToJpeg` (which used to return `false` and drop the frame silently);
* **a capture much larger than the request** (more than 5/4 in either axis) —
  the RGBA path scales the frame down to the size that was asked for.

`camera.status.stats.path` says which path is live and `path_reason` says why it
changed; both are sticky, so a reconnect does not rediscover a broken encoder.
The self-view thumbnail is refreshed 4 times a second (`PREVIEW_INTERVAL_MS`),
not once per frame: decoding every frame the phone just encoded was measurable
heat for a preview nobody watches at 30 fps.

**Where did it stop?** Five `Log.i` lines under one tag answer that, in order:
`camera.start received`, `camera bound`, `first analyzer frame` (with the real
size, format, rotation, crop and per-plane strides), `first JPEG encoded`,
`first frame sent`. Every frame also logs its own size and encode time at `Log.d`,
and every second the engine prints its averages — `adb logcat -s CameraEngine`:

    D CameraEngine: capture 29.8 fps  yuv->jpeg 12.4 ms  q70  94 KB  send 1.9 ms  target 1280x720@30  capture 1280x720

The same values are returned by `camera.status` under `stats`, which is what
`phonebridge camera --stats` prints beside the PC's side (docs/26 §6).

**Threading.** CameraX binds a `LifecycleRegistry`, and `setCurrentState` is
main-thread-only — `bindToLifecycle`, `unbindAll` and our own owner all reach it.
`camera.start` arrives on a WebSocket thread, so every lifecycle call now hops to
the main thread and waits (`CameraEngine.onMainThread`), bounded at 10 s so a
stuck main thread becomes a `camera_busy`/`internal` sentence rather than a hung
request. The three camera RPCs also answer from their own thread
(`cameraExecutor`), so the phone never waits for the main thread while holding the
peer session's lock. Skipping either half produces the failure seen on the
M2010J19SI: `Method setCurrentState must be called on the main thread`, answered
as `internal`, with a PC that showed "camera error" and no reason.

**When it fails, the phone says where.** Failures are logged as
`E CameraEngine: camera <where>: <detail>`, so on a connected phone:

    adb logcat -s CameraEngine

is the one command that shows why the lens did not open. The PC prints the same
text, once, as `camera stopped: <detail>`.

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
`stats.dropped` and `stats.received` in the GUI tell you which.

A dropped session always brings the camera down (`camera.streaming` false) —
the phone closes the lens when the session that asked for it goes away, so a
reconnected link is a link with no picture. What happens next depends on who is
driving:

* **`phonebridge camera` (and `start`)** reconnects on its own: the runner opens
  a new authenticated session, swaps it in, re-issues `camera.start` and rebuilds
  any audio paths. The status line says `camera reconnecting …` with the reason
  and the attempt number while it works, instead of `camera idle` until the user
  notices.
* **The GUI** does not: `camera.streaming` goes false and the user presses start
  again. That is intentional there — a window that reopens the camera by itself
  is a window the user did not ask for.

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

### 7.1 Delivering the stream as a Windows webcam

`phonebridge camera --virtual` feeds the same decoded frames into **pyvirtualcam**
(OBS Studio's "OBS Virtual Camera", Unity Capture as the fallback) so that Zoom,
Teams, Meet or OBS can select the phone as a camera. It lives in
`phonebridge/live/` and consumes `CameraStream`; nothing in the protocol, the
Android app or the frame path changed.

Two details are deliberate: frames are letterboxed rather than stretched when the
requested camera resolution and the phone's aspect ratio differ, and the
*unmirrored* frame is delivered (applications mirror their own previews). A stall
shows the last frame and then black; the stream resumes on its own.

Full setup, options and the hardware checklist: `docs/26-windows-live-delivery.md`.

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

## 8.1 Preview geometry and orientation (Phase 6, step 1)

Three separate defects decided whether the picture was usable, and they were
independent of each other.

**The preview was a strip.** The Tk `Label` was given the decoded image at its
natural size (1280x720 ≈ 720 pixels of height) inside a window that is already
full, so what survived on screen was a thin slice of it. The label also carried
`width=64, height=8` in *text* units, which says nothing useful about a picture.
The fix is structural: the picture lives in a holder with a real height
(`pack_propagate(False)`) and is **scaled to fit** it, preserving the aspect
ratio, on every `<Configure>`. One dimension always uses the whole box; nothing
is stretched and nothing is enlarged past the source.

**The rotation was never applied.** `preview.decode()` returned a `Preview`, and
the window handed that object straight to `ImageTk.PhotoImage` — which is not a
Pillow image and cannot work — while `Preview.rotated()`, the one function that
interprets the phone's `rotationDegrees`, was never called from the window. The
result is exactly what a raw sensor buffer looks like: the rear lens 90° one way
and the front lens the other, because the two sensors are mounted 180° apart.
The window now goes through `preview.render()`, which rotates *once*, fits
*once*, and returns the dimensions it produced.

**The front lens was not mirrored, and nothing may guess.** A selfie camera is
mirrored on the phone's own screen, so an unmirrored preview reads backwards.
`mirrored_for_display()` mirrors `front` and never `back`; the *virtual camera*
path passes `mirror=False` because every application mirrors its own preview and
a second flip would un-mirror it.

**The metadata is now deterministic.** `rotationDegrees` is relative to the use
case's target rotation, which defaults to 0 for a use case that is not attached
to a window — so the number described the phone's *natural* orientation rather
than the way it is held. `CameraEngine` now sets
`analysisUseCase.targetRotation` from `DisplayManager`, so the number means
"turn the picture this far clockwise to be upright as the phone is held now".

The window's status line reports `rotation N°` — the value the phone sent for the
last frame — so a sideways picture can be diagnosed from a number instead of by
guessing which side is wrong.

**Proven here:** the arithmetic (clockwise turning, mirroring, fitting, one
decode per frame) in `tests/test_camera_preview_geometry.py`; the Kotlin side in
`tests/test_phase6_android_camera.py`.
**Not proven:** that this handset reports the expected values, that the window
looks right on a real display (no Tk in this environment), and that the picture
is upright in an external webcam application (there is none yet).

---

## 8.2 Frame rate: asking for 30, and getting it

Measured on a Xiaomi 24115RA8EI: a 640x480 stream at a 30 fps request arrived at
**20-24 fps**, while the yuv→jpeg encode took **7 ms** — the encoder was not the
limit. The limit was auto-exposure: a camera in ordinary indoor light is allowed to
stretch the exposure beyond one frame period, and it does, because a brighter
picture beats a faster one by default.

CameraX has no public "frame rate" switch. The control is a Camera2 capture
request option, applied through **Camera2Interop** on the `ImageAnalysis` use case:

```kotlin
Camera2Interop.Extender(builder)
    .setCaptureRequestOption(
        CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, Range(fps, fps)
    )
```

The range is chosen against `CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES`, read from
`Camera2CameraInfo` **before** the bind (the characteristics are available from
`availableCameraInfos`, so nobody has to guess):

| Request | Range used | Effect |
| --- | --- | --- |
| `--fps-mode fixed` (default) | exact `(fps, fps)` if listed, else the narrowest range whose upper bound is `fps` | the rate holds; a dim room looks **darker** |
| `--fps-mode variable` | the widest range whose upper bound is `fps` (e.g. `15-30`) | the device may drop to 15 fps for a brighter picture |

Both the request and the device's list are reported, so a refusal is a number
rather than a mystery:

```
  phone 29.6 fps  encode   7.0 ms     34 KB/frame  q70  send  1.2 ms  640x480  yuv
  ae    AE target 30-30  device 30-30,15-30,7-30  warm-up 2400 ms, first frame 5200 B
```

If a vendor ROM refuses the interop, the option is dropped (logged, `fps_range`
empty) and the camera keeps streaming at whatever rate the HAL picks: a refused
frame-rate hint must never cost the picture.

## 8.3 The first six seconds were near-blank

The same device produced **5 KB** (near-blank) JPEGs for about six seconds after
`camera.start`, then normal ones. That is auto-exposure and auto-white-balance
converging: the sensor starts delivering before 3A has a result, so the first
frames are black, and a black 640x480 JPEG is 3-6 KB.

Nothing in CameraX lets an app skip the warm-up, so it is **measured** instead:
every encoded frame is compared against the largest one seen so far, and the phone
records when a frame first reaches a normal size (`warmup_ms`, with
`first_frame_bytes` and `peak_frame_bytes` next to it). A fixed AE range shortens
it, because exposure may not be stretched past one frame period, and the log line
says when the picture became usable:

```
I/CameraEngine: camera warm-up: 2400 ms (first frame 5200 B, now 34000 B)
```

`warmup_ms` is `-1` until it converges, which is the honest answer while the
camera is still warming up. It is reported in `camera.status.stats` and printed by
`--stats`.

## 8.4 Decode spikes: 40-50 ms on a 3 ms path

At 640x480 q70 the report was decode spiking to **40-50 ms** while the mean sat
near 3 ms, with dropped frames climbing. Measuring the pieces separately ruled
out the obvious suspects — `Image.draft("RGB", ...)` was already called before
`convert`, the letterbox canvas is only allocated when the aspect ratio differs,
and a 148 KB 640x480 q70 JPEG decodes and converts in **3.05 ms** with
`rotate(-90, expand=True)` adding **0.26 ms**. The JPEG path was not it.

What was left was the shape of the worker, and two things change:

* **the device send moved to its own thread.** `pyvirtualcam.send()` blocks until
  the consumer collects the frame, and with one thread a slow consumer held the
  *decoder*: incoming frames piled up in the single slot and were counted as
  drops. Now a stalled send costs the one frame in flight, and the decoder keeps
  consuming at the source's rate. `stale` counts frames that were decoded and
  replaced before the device took them — the number that separates "the phone
  stopped sending" from "this PC could not take what it sent";
* **each stage reports its worst sample, not only its mean.** A 45 ms frame among
  fifty 3 ms ones is a 3.8 ms mean and one dropped frame, which is why the report
  said "decode spikes" but the status line said 3 ms. `--stats` now prints the
  stage that spiked:

```
  pc   19.4 fps  rx 20  57 KB/frame  decode   3.1 ms  convert  1.2 ms  send 0.9 ms  queue 0  dropped 0
  peak decode 45.2 ms   (worst in the last second)
```

A peak is named only above 25 ms, because two milliseconds is a measurement, not a
spike. Which stage appears there is the diagnosis: `decode` means allocation or
GC pressure on this PC, `send` means the virtual-camera consumer (OBS or the app
reading it) is slow, `convert` means the numpy copy.

## 9. Manual hardware checklist (not yet done)

Nothing below has been run: this development environment has no phone, no JDK, no
Android SDK and no Pillow. Everything in §10 is what *was* verified.

Run on a real phone and PC, in this order:

1. **Build and install.** `cd android && .\gradlew.bat assembleDebug`, then
   `adb install -r app\build\outputs\apk\debug\app-debug.apk`.
1a. **Preview geometry (Phase 6).** Start a stream and look at the window: the
   picture must fill the width of its section with the correct aspect ratio, and
   resizing the window must rescale it without stretching. The status line shows
   `rotation N°`: note the value for each lens. Rotate the phone to landscape
   while streaming — the picture must stay upright and the reported rotation must
   change. The rear lens must *not* be mirrored; the front lens must be (text in
   the scene reads correctly).
1b. **Record the numbers.** If a lens is still sideways, the rotation figure and
   the lens are the two facts needed to fix it; write them down before changing
   any code, because a wrong sign corrects one lens and breaks the other.
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
