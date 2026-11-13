# 25. System-wide integration: audit and architecture plan

Phase 6 asks a different question from Phases 1–5. Those made the phone's hardware
*reachable over the bridge*. This phase asks whether Windows and third-party
applications can use that hardware as if it were their own: a webcam in Teams, a
microphone in Audacity, a speaker for a browser, sensors for a native app.

That question has two very different answers depending on the device, and this
document separates them before any code is written:

* **Video can be exposed system-wide without a kernel driver.** Windows 11 22H2+
  has a public Media Foundation virtual-camera API; Windows 10 needs a
  DirectShow source filter. Both are user-mode COM objects.
* **Audio cannot.** Windows has no user-mode API that creates a capture or render
  endpoint. Every device an application can *select* comes from a kernel-mode
  audio driver. Anything else is a compromise that has to be named honestly.

Everything below was established by reading this repository at `c2ccdab`, not by
running hardware. Nothing in this phase has been executed on a Windows machine,
with a phone, or with a compiler for the native parts.

---

## 1. Existing feature audit

| Area | State | Where | Evidence in this repo | GUI-only today? |
| --- | --- | --- | --- | --- |
| Transport, pairing, AES-256-GCM session | Complete | `windows/phonebridge/core`, `protocol`, `transport`; `android/.../net/BridgeServer.kt` | `docs/03`, `docs/05`, `tests/test_e2e_workflows.py` | No |
| Discovery, manual IP, USB/ADB, hotspot | Complete | `controller/manager.py`, `adb.py`, `endpoints.py` | `docs/17`, `docs/18` | No (CLI + GUI) |
| Audio PC → phone (system audio) | Implemented | `audio/link.py` (`start_playback`), `backends.WasapiLoopback` | `docs/19`, `tests/test_audio_*` | **Yes** — plays through the phone, invisible to Windows as a device |
| Audio phone → PC (mic monitor) | Implemented | `audio/link.py` (`start_mic`), `backends.SoundDeviceSink` | `docs/19` | **Yes** — a *render* stream to the PC's own speakers |
| Camera preview | Implemented, two defects | `camera/stream.py`, `camera/preview.py`, `ui/app.py` | §3 below | **Yes** — a Tk label, not a camera device |
| BLE scan / GATT | Complete | `bluetooth/link.py`, `net/BleEngine.kt` | `docs/21` | No (CLI + GUI), but *not* a Windows Bluetooth device |
| Sensors | Complete | `sensors/link.py`, `net/SensorEngine.kt` | `docs/22` | No (CLI + GUI), no Windows Sensor API provider |
| File transfer both ways, resume, SHA-256 | Complete | `files/transfer.py`, `net/FileTransferEngine.kt` | `docs/23`, `tests/test_phase51_transfer_resume.py` | No (CLI + GUI), no Explorer integration |
| Windows-native device components | **None** | — | no `ctypes`/`comtypes`/Win32 device code; `pyvirtualcam` is listed as a *future* optional extra and is never imported | — |
| Installer / service | **None** | — | `docs/16` §4 | — |

Two documentation drifts worth fixing when this phase touches them:
`docs/16` still says there is no desktop GUI and no audio relay (both exist since
Phases 2–3), and `docs/16` lists sensor visualisation as absent while `docs/22`
documents the numeric table.

---

## 2. Current architecture

```
  Android (Kotlin)                 Windows (Python)                    Windows apps
 ┌──────────────────┐        ┌──────────────────────────┐
 │ CameraEngine     │ MSG_VIDEO │ CameraStream ──► UI Label│            (nothing)
 │ AudioEngine      │ MSG_AUDIO │ AudioLink ──► sounddevice│            (nothing)
 │ BleEngine        │ MSG_CTL   │ BluetoothLink            │
 │ SensorEngine     │ MSG_SENSOR│ SensorLink               │
 │ FileTransfer     │ MSG_FILE  │ TransferManager          │
 └────────┬─────────┘           └───────────┬──────────────┘
          │  AES-256-GCM over WebSocket     │
          └───────── one authenticated session ─────────┘
                                            │
                                   Tkinter window (controller/ui)
```

Everything ends inside the Tk process. There is no path from a frame or a PCM
buffer to an operating-system device object, which is precisely why no external
application can see the phone's camera or microphone.

---

## 3. Why camera and microphone are GUI-only — the exact reasons

### Camera

1. `CameraStream` hands each `VideoFrame` to the controller; the GUI decodes the
   JPEG and puts it in a `tk.Label`. A Tk label is a widget, not a device: it has
   no CLSID, no device interface, and nothing enumerates it.
2. Windows discovers cameras by enumerating **Media Foundation** device sources
   (Frame Server) and, for older apps, **DirectShow** `CLSID_VideoInputDeviceCategory`.
   A device therefore has to be one of:
   * an MF virtual camera, created with `MFCreateVirtualCamera` — a COM media
     source DLL, Windows 11 22H2 (build 22621) or later, registered per machine
     (administrator), **no kernel driver and no driver signing**; or
   * a DirectShow capture source filter — user-mode COM, works on Windows 10 and
     for DirectShow-only hosts (OBS, ffmpeg, Electron/Chromium), registered with
     `regsvr32` (administrator).
   PhoneBridge has neither, so there is nothing for an application to select.
3. `pyvirtualcam` (listed in `requirements-phase2plus.txt`) is *not* a device
   either: it feeds a device that something else installed (OBS Virtual Camera,
   Unity Capture). Using it would make OBS a hidden dependency; it is a
   fallback, not an architecture.

### Microphone

1. The phone's PCM currently ends in `sounddevice.RawOutputStream` — a **render**
   stream. Playing audio is not a capture endpoint; Windows will never show a
   recording device for it.
2. Windows has **no user-mode API that creates a capture endpoint.** Applications
   choose microphones through WASAPI/MMDevice, which enumerates kernel audio
   devices only. A device with PhoneBridge's name in the Sound control panel
   requires a kernel-mode audio miniport (SysVAD/WaveRT-style), Microsoft
   attestation signing through Partner Center with an EV certificate, an
   installer and an uninstaller — plus the fact that a driver bug crashes the
   whole machine, not one application.
3. Therefore the honest ladder is:
   * **route into an existing signed virtual cable** (VB-CABLE, VoiceMeeter):
     applications select the cable's capture side; PhoneBridge renders into its
     render side over WASAPI. Nothing new is installed by us, it is reversible,
     and it works with every WASAPI application including browsers and Teams;
   * **ship our own signed driver** later, if the cable's extra device name is
     unacceptable. That is infrastructure work, not a coding task.

The same reasoning applies to **the phone as a Windows speaker**: a selectable
*output* endpoint is also a kernel device. Without one, the two routes are
(a) Windows' own "listen to this device"/app-level routing into the cable, and
(b) capturing a specific app with process loopback and forwarding it to the
phone's `AudioTrack`.

---

## 4. Recommended Windows-native architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│ PhoneBridge (Python)                                                      │
│  session ── camera frames ──► orient/scale ──► NV12 ↔ JPEG ──► SHM writer │
│          └─ mic PCM ───────► resample ───────► WASAPI render (cable)      │
│          └─ speaker ←────── WASAPI capture (cable) ←── PCM (phone)        │
└───────────────┬──────────────────────────────┬───────────────────────────┘
                │ shared memory (no socket)    │ WASAPI (in-box)
                ▼                              ▼
   ┌────────────────────────────┐    ┌──────────────────────────────┐
   │ PhoneBridgeCam (C++ COM)   │    │ existing signed virtual cable│
   │  · MF virtual camera  W11  │    │  (user installs; we never do)│
   │  · DShow source filter W10 │    │  CABLE Input ⇄ CABLE Output  │
   └────────────────────────────┘    └──────────────────────────────┘
```

Decisions, and why:

* **Shared memory, not another socket.** The frame hand-off is a named section
  (`Local\PhoneBridgeCam`), a four-slot ring with a mutex and a producer
  heartbeat. No port, no listener, no unauthenticated local server, no second
  copy through the network stack. A stalled consumer shows the last frame; a
  dead producer shows a status frame rather than a hang.
* **NV12 output.** It is what the Frame Server pipeline wants; converting once in
  Python (numpy) is cheaper than letting MF rescale RGB per consumer.
* **Both video DLLs, chosen by OS build.** MF alone would exclude OBS and every
  Electron app (DirectShow); DirectShow alone would exclude Windows 11's Camera
  app path and Frame Server sharing. This is what OBS Virtual Camera and the
  newer open-source virtual cameras do.
* **Orientation is applied exactly once, in Python, before the shared memory.**
  The GUI and the virtual camera then receive the same upright image, and a bug
  in rotation cannot appear in one and not the other.
* **Everything stays in one process tree.** Python coordinates, the DLLs are
  loaded by the Frame Server (MF) or the consuming application (DShow). No
  service is required for video; an optional tray/service is a packaging
  decision, not an architectural one.

---

## 5. Required drivers, SDKs, tools and permissions

| Need | Requirement | Notes |
| --- | --- | --- |
| Build the native video component | MSVC 2022 Build Tools, Windows SDK 10.0.22621+, CMake ≥ 3.25 | Cannot be built in this Linux container; `windows-native/` must be built on Windows |
| MF virtual camera | Windows 11 22H2+ (build 22621) | `MFCreateVirtualCamera` does not exist on Windows 10 — no polyfill |
| DirectShow filter | Windows 10 1809+ / 11 | For OBS, ffmpeg, Electron/Chromium hosts |
| Registration | **Administrator** (`regsvr32`, HKLM COM registration, `MFCreateVirtualCamera` requires an elevated token) | Per-machine, reversible with `regsvr32 /u`; document UAC |
| Driver signing | **None** for video (user-mode COM DLL) | Code signing recommended so SmartScreen does not warn; not required to load |
| Virtual microphone / speaker endpoint | Kernel-mode audio driver (SysVAD/WaveRT) + Microsoft attestation signing (EV certificate) **or** a user-installed virtual cable | We will not ship a kernel driver in this phase; see §3 |
| Virtual cable (optional dependency) | VB-CABLE / VoiceMeeter, installed by the user | Detected and offered, never installed silently |
| File Explorer integration | WinFsp (signed user-mode file-system framework) or a kernel FS filter | WinFsp + a small file-system host is the realistic path |
| Python | Pillow (preview, already optional), numpy (NV12/audio conversion) | No new network dependencies |

---

## 6. What can be done in Python/Kotlin alone

* Preview sizing, aspect ratio, rotation and mirroring — the defect in §3.
* Camera orientation metadata being deterministic on the phone
  (`ImageAnalysis.setTargetRotation`).
* JPEG → NV12 conversion and the shared-memory producer.
* All protocol work: camera switch while streaming, resolution/fps negotiation,
  reconnection, per-feature lifecycle.
* WASAPI routing into/out of a virtual cable (`soundcard`/`sounddevice` already
  in use; a `soundcard` *recorder* on the cable, a `sounddevice` stream on its
  render side).
* BLE, sensors and file transfer improvements, including a documented "PhoneBridge
  API" for sensors no Windows API can consume.
* Registration *driving* (`regsvr32`, `MFCreateVirtualCamera` via a small helper
  executable) — but the DLL itself is native.

## 7. What requires a native Windows component

| Capability | Why it cannot be Python |
| --- | --- |
| A selectable camera | The device object *is* a COM media source; Python cannot implement `IMFMediaSource`/DirectShow pins |
| A selectable microphone or speaker | Kernel audio miniport, or a third-party cable's endpoints |
| Explorer drive for phone files | WinFsp file-system host (native) or a kernel FS filter |
| Windows Sensor API provider | No public user-mode sensor provider API; sensors are kernel/USB/BLE-backed |

---

## 8. Risks and compatibility limitations

1. **Windows 10 has no MF virtual camera.** Users on Windows 10 get the
   DirectShow device only; Windows 11's Camera app may not see it, OBS will.
2. **Application coverage is not uniform.** Teams/Zoom/Chrome use MF; OBS and
   older capture tools use DirectShow. Shipping one DLL silently halves the
   support matrix, which is why both are planned.
3. **Format negotiation.** A virtual camera advertises a fixed list of native
   media types. Applications may request anything in that list; every extra size
   is another scaling path to test. Plan: advertise 640×480, 1280×720, 1920×1080
   at 30/15 fps NV12, letterbox rather than crop, and refuse the rest explicitly.
4. **Camera privacy setting.** MF virtual cameras respect it (good, and a
   surprise worth documenting); a DirectShow filter is not subject to the same
   switch, so the DLL must honour its own "off" state.
5. **Shared memory is same-session only.** Any process in the user's session that
   knows the name can read the frames. That is the same threat model as a
   screen-capture API, but it must be stated, and a "pause the virtual camera"
   switch must actually stop writing.
6. **Latency.** Phone encode (~15–30 ms) + network + Python decode/convert
   (~5–15 ms) + MF buffers; expect 100–250 ms glass-to-glass on USB, more on
   Wi-Fi. Fine for calls, not for 60 fps gaming capture.
7. **Virtual-cable audio adds its own buffer** (~30–80 ms at 48 kHz shared mode)
   and its own resampling; sample-rate mismatches must be converted, not assumed.
8. **Kernel drivers are out of scope** and would change the trust story: signed
   binaries, attestation, and a crash surface that is the whole machine.
9. **Nothing here can be verified in this environment** (Linux, no MSVC, no
   Windows, no phone). Everything native is "written and reviewed", never
   "tested", until it is built on Windows.

---

## 9. Phased plan, with acceptance criteria

Each phase keeps the suite green and reports what still needs hardware.

### Phase A — audit and the preview defect *(this change)*
*Audit above. Fix the two real defects found in the preview path.*
- [x] Every feature mapped to a file and a state; native gaps named.
- [x] `docs/20` §8.1 records the three preview defects and the fix.
- [x] The GUI applies rotation exactly once and mirrors the front camera.
- [x] The preview scales to fit, preserves aspect ratio, and survives window
      resizing; no per-frame allocation when nothing changed.
- [x] `ImageAnalysis.setTargetRotation` makes the phone's rotation metadata
      deterministic.
- [ ] **Hardware:** both lenses upright in the GUI on the handset.

### Phase B — virtual camera (native), feature-flagged
*`windows-native/vcam/` (MF + DirectShow), Python producer.*
- [ ] `phonebridge vcam install|uninstall|status` drives `regsvr32` and explains
      UAC; refuses to run without a Windows 11 22H2 build for the MF half.
- [ ] The device appears in `Get-PnpDevice -Class Camera` under the documented
      name; a DirectShow enumerator lists the filter.
- [ ] Live frames in Windows Camera and OBS; switching lens mid-stream; phone
      disconnect leaves a status frame, reconnect resumes without restarting the
      consumer.
- [ ] **Hardware + Windows only:** enumeration and every application above.

### Phase C — audio endpoints
*Cable-based microphone and speaker, no driver.*
- [ ] Cable detected and offered; the user installs it themselves.
- [ ] Phone mic → cable render → apps record it; phone speaker ← cable capture.
- [ ] Independent mute/volume per direction; no feedback loop (the phone's own
      playback is excluded from what is captured).
- [ ] **Hardware + Windows only:** Audacity/Teams/OBS behaviour, latency figure.
- [ ] Documented decision: no kernel driver in this phase, with the reasoning
      and what shipping one would require.

### Phase D — BLE and sensors, honestly scoped
- [ ] BLE: stress the existing path (reconnect, notify flood, bond-required
      refusal) and document that the phone's own paired headset is not exposed.
- [ ] Sensors: a documented "PhoneBridge sensor API" (CLI + local read-only JSON)
      and an explicit statement that no Windows Sensor API provider is possible.

### Phase E — files
- [ ] Duplicate-name policy, speed/ETA, retry after reconnect surfaced in the GUI.
- [ ] Investigate WinFsp; ship only if a read-only, authenticated view can be
      built and cannot escape the SAF tree.

### Phase F — integration and release
- [ ] Camera + microphone together in one application, then disconnect/reconnect.
- [ ] Installer/uninstaller for native parts, documented admin steps, no silent
      driver installation.
- [ ] `docs/26-device-integration.md`: complete hardware checklist.

---

## 10. Status: phase A closed, phase B proof of concept ready to build

### Phase A (preview geometry and camera orientation) — complete, unverified on hardware

Three defects were found and fixed, each recorded with its cause in
`docs/20-camera.md` §8.1:

| Defect | Cause | Fix |
| --- | --- | --- |
| Preview was a thin horizontal strip | the preview container was laid out by the image's own size, not by the space available | fixed-size holder frame (`camera_preview_box`), `<Configure>` binding, aspect-fit inside it |
| Preview was rotated wrongly or not at all | `PhotoImage` was built from the unrotated frame; `rotationDegrees` was interpreted in one place and applied in another | `Preview.rotated()` is now the only interpreter of `rotationDegrees`; the GUI renders `Preview.render()` |
| Both cameras sideways | Android set `targetRotation` from the sensor's own rotation, not the display's | `ImageAnalysis.targetRotation = displayRotation()` |

Related: `Preview.mirrored_for_display()` mirrors the front camera only,
`fit_size()`/`matching_scale()` never upscale, and the status line reports the
rotation actually applied, so a wrong orientation in the field is diagnosable
from a screenshot instead of guesswork.

Verified by: `windows/tests/test_camera_preview_geometry.py` (20 cases, including
portrait-in-landscape-box and landscape-in-portrait-box),
`windows/tests/test_phase6_android_camera.py` (3 source contracts on
`CameraEngine.kt`), the full suite (1190 passed, 2 skipped), ruff, mypy and
`tools/verify.py` (8/8). The GUI itself cannot be opened in this environment, so
the fix is proven by geometry and source contract, **not** by looking at it. The
manual checklist is `docs/20-camera.md` §9, items 1a and 1b.

### Phase B (virtual camera) — proof of concept written, not compiled

`windows-native/vcam-poc/` contains a complete Media Foundation software-camera
sample: COM media source (`IMFMediaSource` + `IMFMediaEventGenerator` +
`IMFMediaStream`), `IMFActivate` + real `IMFAttributes` store, class factory,
DLL exports, and a host that registers the CLSID per user, creates the camera with
`MFCreateVirtualCamera`, starts it, and removes everything on exit.

It synthesises a 1280×720@30 NV12 pattern with a moving bar. **It deliberately
has no dependency on the phone, the protocol or Python**, so a failure here is a
failure of the device plumbing alone — which is exactly the question that has to
be answered before the pixel path is built on top of it.

Not compiled: the authoring environment has no MSVC and no Windows SDK. Build and
run instructions, expected output, the acceptance sequence (Camera app → OBS →
browser → meeting app) and a failure-to-cause table are in
`windows-native/vcam-poc/README.md`.

**Target machine (confirmed by the user, 2026-11-08):** Windows 11 Pro, version
26H2, build **26300.9974**, x64. That is above the documented 22000 floor for
`MFCreateVirtualCamera`, so the Media Foundation path is viable on paper; the API
being *present* is the thing `--check` proves, not the build number alone.

Still unknown, and only readable on that machine:

1. whether the MSVC x64 C++ tools, a Windows 11 SDK with `mfvirtualcamera.h`, and
   CMake are installed — `windows-native/vcam-poc/tools/run-check.bat` reports all
   three, read only, without registering anything;
2. whether the camera appears in Camera/OBS/a browser once built.

Nothing in this phase has been executed on Windows: the authoring environment is
Linux, with no access to the target machine and no MSVC. `--check` is a command
for the user to run, not a result that exists yet.

## 11. What this phase will not do

* It will not install a driver or a service, and will not download binaries.
* It will not claim a device exists because a preview window shows pixels.
* It will not call the phone's paired Bluetooth headset a Windows audio device:
  exposing A2DP/HFP sinks needs the phone to *be* a sink, which Android's public
  SDK does not offer. The honest statement is in `docs/16` and stays there.
