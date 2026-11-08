# Phase 0 — Technical Feasibility Report

**Question this answers:** can my Android phone act as a low-latency audio bridge
between Windows and Bluetooth headphones connected to the phone — *including*
microphone audio flowing back to Windows?

**Short answer:** yes for the full-duplex case, but only in headset/HFP mode,
which is mono at 8 or 16 kHz and adds Bluetooth latency. It is **impossible** to
get stereo music-quality output *and* the headset microphone at the same time,
on any operating system, because the headset itself switches between the two
profiles. PhoneBridge will therefore implement two explicitly labelled audio
modes instead of promising one impossible one.

Everything below was checked against the Android platform APIs, the Windows
audio architecture, and measured behaviour of the Phase 1 code in this repo.
Claims that are still unverified on real hardware are marked **[needs device]**.

---

## 1. Why the naive plan fails

The intuitive plan — "stream PC audio to the phone, play it on the headphones,
and grab the headset mic" — breaks on one physical fact:

> A Bluetooth headset exposes **two separate profiles**, A2DP (output only,
> high quality) and HFP/HSP (bidirectional, low quality). Most headsets contain
> a hardware switch, not a mixer: they render **one** of them at a time.

| Profile | Direction | Sample rate | Channels | Has mic? |
| --- | --- | --- | --- | --- |
| A2DP | phone → headset | 44.1 / 48 kHz | stereo | **no** |
| HFP wideband (mSBC) | both | 16 kHz | mono | yes |
| HFP narrowband (CVSD) | both | 8 kHz | mono | yes |
| LE Audio / LC3 (API 33+, hardware dependent) | both | higher | stereo | yes |

So the truthful capability statement is:

* **Output-only, good quality** — A2DP. Great for "phone as PC speakers over
  Bluetooth headphones". No headset microphone.
* **Duplex, call quality** — HFP over SCO. This is the only path that carries
  the *headset's* microphone back to the phone, and it happens to be inherently
  full-duplex. This is the mode Discord/Zoom/Meet/OBS need.

Both are useful. Neither is "high-quality stereo plus headset mic", and no
amount of software changes that.

## 2. Android restrictions (what the platform will and will not let an app do)

### 2.1 Routing audio to the headset — supported

* API 31+ (Android 12): `AudioManager.setMode(MODE_IN_COMMUNICATION)` +
  `setCommunicationDevice(device)`.
* API 24–30 fallback: `startBluetoothSco()` + `isBluetoothScoOn = true`
  (deprecated in API 34 but still functional).
* `setCommunicationDevice()` returning `true` means *the request was accepted*,
  not that audio is already flowing. The route must be confirmed by watching
  `AudioDeviceCallback` / `communicationDevice` before starting capture, or the
  first few hundred milliseconds come from the wrong device. This is the single
  most common bug in this area and Phase 2 handles it explicitly.
* SCO outside a phone call is **optional for OEMs** —
  `isBluetoothScoAvailableOffCall` can be `false`. Must be detected and reported
  rather than assumed. **[needs device]**

### 2.2 Capturing the headset microphone — supported, with one catch

`AudioRecord` with `MediaRecorder.AudioSource.VOICE_COMMUNICATION` follows the
communication route, i.e. the headset mic. Using `MIC` or `VOICE_RECOGNITION`
keeps capture on the phone's own microphone on several OEM builds. So the source
must be `VOICE_COMMUNICATION` and the SCO route must be selected *before*
`AudioRecord` is created.

Permissions: `RECORD_AUDIO`, plus `MODIFY_AUDIO_SETTINGS` for `setMode`.
`BLUETOOTH_CONNECT` is only needed to read Bluetooth device objects.

### 2.3 Not possible: acting as a Bluetooth *sink* or emulating an adapter

* `BluetoothA2dpSink` (receiving A2DP) is a **hidden/system API**. A normal app
  cannot make the phone appear as a pair of headphones to another device.
* Nothing in the public SDK lets an app hand raw HCI traffic to the phone's
  radio, so an app cannot present itself to the headphones as a *source* either.
* Consequently PhoneBridge **cannot** make Windows see a real Bluetooth
  headset. Windows will never list "PhoneBridge Headphones" under Bluetooth.
  It will only ever list a virtual audio endpoint (see §3).

This is why the project is scoped as an **audio relay**, not an adapter
emulator. Anything else would be a lie.

### 2.4 LE Audio (worth knowing, cannot be controlled by an app)

On Android 13+ with LE Audio hardware, the platform can do concurrent
microphone + high-fidelity playback — the exact thing HFP cannot. An app does
**not** choose the codec or enable this; it only appears as
`AudioDeviceInfo.TYPE_BLE_HEADSET`, and the OS decides. Phase 2 will detect and
report it, and the dashboard will say "LE Audio detected: duplex quality is
better than HFP on this device" rather than pretending to control it.

### 2.5 Foreground service and power

A plain `Service` will be frozen when the screen locks and the stream dies. The
bridge must run as a `foregroundServiceType="connectedDevice"` foreground service
with a partial wake lock. This is implemented in
`android/app/src/main/java/dev/phonebridge/service/BridgeService.kt`.

## 3. Windows restrictions (what the PC will and will not give you)

### 3.1 PC → phone audio: **no driver required**

WASAPI loopback capture reads whatever is being rendered to a playback device,
in shared mode, with no virtual device involved. That means feature A and D work
with zero kernel drivers: capture the default output (or a chosen endpoint) and
stream it.

Caveat: WASAPI loopback is shared-mode only. Exclusive-mode streams cannot be
captured, and DRM-protected audio may be silenced.

### 3.2 Phone mic → Windows app: a driver **is** required

Discord/Zoom/Meet/OBS read *recording endpoints*. A recording endpoint on
Windows is created by a kernel-mode audio driver (SysVAD / WaveRT style).
Shipping one requires Microsoft attestation signing via Partner Center with an
EV certificate, an installer, and an uninstaller — infrastructure and cost that
a personal project should not take on.

**Decision:** PhoneBridge will not ship a driver. It writes the decoded
microphone audio into an existing virtual cable, and the user picks the cable's
recording side in the target app:

```
Phone mic → PhoneBridge (decode) → render to "CABLE Input (VB-Audio Virtual Cable)"
                                          ↓ (VB-CABLE driver)
Discord / Zoom / Meet / OBS ← "CABLE Output (VB-Audio Virtual Cable)"
```

Required third-party component: **VB-Audio Cable** (free donationware),
VoiceMeeter (free), or Virtual Audio Cable (paid). One of them is needed only
for the *microphone* direction, never for PC → phone audio.

An open question worth testing: whether an audio-processing-object (APO) or a
Media-Foundation-based approach could expose a microphone without a driver. It
cannot — APOs post-process an existing endpoint, they do not create one. This
limitation is real and is recorded here rather than postponed.

### 3.3 Virtual camera (Phase 4): same story, solved the same way

Windows has no user-mode "register a camera" API. Options:

* write a Media Foundation / DirectShow virtual camera driver — rejected for the
  same signing reasons;
* reuse an already-signed one: **OBS Virtual Camera** (ships with OBS Studio) or
  **Unity Capture**, both supported by `pyvirtualcam`.

Decision: `pyvirtualcam`. The user installs OBS Studio (which many already
have) and PhoneBridge feeds its virtual camera. No custom driver.

## 4. Latency budget — what is realistic

Measured and estimated components for the duplex path. **[needs device]** items
must be replaced with real numbers in Phase 2.

| Stage | Best case | Typical | Source of latency |
| --- | --- | --- | --- |
| WASAPI loopback capture (shared, 10 ms period) | 10 ms | 20 ms | OS audio engine buffering |
| Opus encode (20 ms frame) | 5 ms | 10 ms | CPU, negligible on a modern PC |
| PC → phone link (USB via adb forward) | 1 ms | 3 ms | local TCP |
| PC → phone link (Wi-Fi 5, same AP) | 2 ms | 8 ms | Wi-Fi contention |
| Phone Opus decode + AudioTrack pre-buffer | 15 ms | 30 ms | Android audio HAL |
| **SCO link phone → headset (mSBC)** | **40 ms** | **70–100 ms** | Bluetooth classic, 7.5 ms slots, headset buffering |
| Total one-way | ~75 ms | **~140 ms** | |

Phase 1 measured the *protocol overhead only* on loopback: **0.29–0.78 ms
application RTT** and 30 MB/s of encrypted binary payload, so the transport is
not the bottleneck; Bluetooth and OS audio buffering are.

**Honest conclusion:** expect roughly **70–150 ms one-way** in duplex mode. That
is comfortable for conference calls, Discord, OBS monitoring and media
consumption. It is *not* suitable for musical monitoring or competitive gaming
voice-chat sniping. Windows' own Bluetooth stack has comparable or worse numbers
for the same headset, so this is a property of HFP, not of PhoneBridge.

A2DP mode (output only) will be higher latency still — SBC buffering alone is
typically 150–200 ms — but stereo and much better sounding.

## 5. Recommended audio modes (the design that follows from all this)

The Bluetooth screen in the desktop app and the phone app will expose exactly
two modes, with the trade-off visible in the UI:

**Mode A — "Headphones (quality, output only)"**
`PC → phone → headset` over A2DP. Stereo 44.1/48 kHz, Opus 96–160 kbps.
No headset microphone. The phone's own mic can optionally still be sent.

**Mode B — "Headset call mode (duplex, has mic)"**
`PC ↔ phone ↔ headset` over HFP/SCO. Mono 16 kHz (falls back to 8 kHz if the
handset negotiates CVSD). Opus 24–32 kbps. This is the default for the
Bluetooth-headphones feature because it is the only mode with a microphone.

The PC sends one stream; the phone decides which route it lands on, and reports
back which one is actually active so the dashboard never lies. If the user asks
for duplex while only A2DP is available, the phone returns an explicit
`UNSUPPORTED_BY_DEVICE` error with the reason — never a silent downgrade.

## 6. Verdict per requested feature

| Feature | Verdict | Notes |
| --- | --- | --- |
| A. PC audio → BT headphones on phone | **Implementable** | Two modes as above; stereo impossible together with headset mic |
| A. BT headset mic → PC | **Implementable** | HFP/SCO only; needs VB-CABLE-class driver on the PC side |
| B. Phone as webcam | **Implementable** | CameraX + H.264/VP8 → `pyvirtualcam` + OBS/Unity Capture |
| C. Phone as microphone | **Implementable** | No BT involved; virtual cable on the PC side |
| D. Phone speakers as PC speakers | **Implementable** | Simplest audio path; A2DP-class route to speaker/USB-C |
| E. Bluetooth device manager | **Partially implementable** | Scan + name + BLE GATT yes; connect/pair/profiles only where the public API allows |
| F. File transfer | **Implementable** | MediaStore-scoped access, no root; progress over the existing channel |
| G. Sensors | **Implementable** | `SensorManager`; GPS needs foreground location permission |

Nothing on the list is impossible. Two items need an explicit downgrade:
stereo+mic simultaneously (no), and arbitrary Bluetooth device management (no).

## 7. What changed versus the original plan

| Original plan | Adjustment | Why |
| --- | --- | --- |
| "PC → phone → Bluetooth headphones" with mic | Split into Mode A (quality, no mic) and Mode B (duplex, mono) | A2DP and HFP cannot be simultaneous on the headset |
| "Windows virtual audio-device integration" | No custom driver; use WASAPI loopback for capture, VB-CABLE-class driver only for the mic | Kernel drivers need EV-signed attestation; out of scope for personal use |
| "WebRTC or another suitable video transport" | Start with the existing framed channel + H.264/VP8; WebRTC (`aiortc`) only if measurement demands it | No NAT traversal is needed on LAN/USB/hotspot; WebRTC adds megabytes of native code |
| "Bluetooth audio relay vs adapter emulator" | Relay only, and stated as such | A2DP sink is not in the public Android SDK |
| "Final solution should aim for standalone connectivity" | USB tethering (RNDIS) gives USB without adb; adb only for development | The phone is always the server, so any IP path works unchanged |

## 8. Unverified assumptions that Phase 2 must settle on your hardware

1. Whether your handset allows SCO **off-call** (`isBluetoothScoAvailableOffCall`).
2. Whether mSBC (16 kHz) or only CVSD (8 kHz) is negotiated with your headset.
3. Actual one-way latency of the full duplex chain, measured with a loopback
   click test rather than estimated.
4. Whether your specific OEM's audio policy silently keeps capture on the phone
   mic even with `VOICE_COMMUNICATION` + SCO selected.
5. Real achievable throughput and jitter over your Wi-Fi/hotspot/USB paths.

Each of these gets a measurement step in the Phase 2 checklist, and the
dashboard will display the measured value rather than a promise.
