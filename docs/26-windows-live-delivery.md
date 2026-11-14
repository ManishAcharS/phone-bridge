# 26. Windows live delivery — camera, microphone, speaker

**Status: implemented, tested without hardware, not yet verified on a phone + PC
pair.** Everything below the line marked *Needs your machine* can only be proven
by running it. Nothing in this document claims otherwise.

The three commands take the hardware the phone already has and make it usable
from ordinary Windows applications:

```
phone phone camera  ->  OBS Virtual Camera      ->  Zoom / Meet / OBS / browser
phone phone mic     ->  CABLE Input (VB-CABLE)  ->  "CABLE Output" as the mic
PC system audio     ->  phone AudioTrack        ->  Bluetooth headphones
```

All three ride the session the controller already authenticated: same pairing,
same AES-256-GCM channel, same framing, same reconnect behaviour. No new RPC
method was added and no existing one changed.

---

## 1. What exists in code

| Path | Module |
| --- | --- |
| plans and validation | `phonebridge/live/config.py` |
| jitter buffer, drift and underrun accounting | `phonebridge/live/jitter.py` |
| Windows devices, driver-missing messages | `phonebridge/live/devices.py` |
| wiring onto `CameraStream` / `AudioLink` | `phonebridge/live/runner.py` |
| commands | `phonebridge/cli/live.py` |
| setup | `scripts/setup-windows.ps1` |

Reused unchanged: `phonebridge.camera.stream` (JPEG frames, newest-frame-wins),
`phonebridge.audio.link` (both directions, sequence gate, bounded queues),
`phonebridge.core.session` (handshake, pairing, encryption, reconnect).

---

## 2. Verified here, versus needs your machine

**Verified in this repository (CI on Windows, and locally):**

* plan validation — impossible resolutions, fps, buffer sizes and backends are
  refused with a message;
* jitter buffer — prefill, drop-oldest on overrun, underrun accounting, close
  releasing a blocked reader;
* device lookup — "CABLE Input" must be a *playback* device, a capture device
  with the same name is never chosen, and a missing cable produces the VB-CABLE
  URL plus the list of devices that were seen;
* missing-driver errors — pyvirtualcam, sounddevice and PyAudioWPatch each
  produce an instruction, not a traceback;
* the runner — devices open before the phone is asked to stream, a stalled
  camera blanks after the timeout instead of freezing, one dead path does not
  stop the others, lip-sync delays video only;
* the CLI surface and the setup script (including that it downloads nothing and
  needs no administrator rights).

**Needs your machine (no test can prove these):**

* that OBS's virtual camera accepts these frames and Zoom/Meet/browsers list it;
* that a call client can open `CABLE Output` and hears the phone;
* that the phone's AudioTrack actually reaches the connected Bluetooth
  headphones with the screen off;
* the real end-to-end latency, and whether 720p30 survives your network.

---

## 3. Setup (once)

```powershell
cd C:\path\to\phone-bridge
powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1 -Check
powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1
```

The second command installs `pyvirtualcam`, `sounddevice` and `PyAudioWPatch` and
then tells you what is still missing. It never installs a driver and needs no
administrator rights. The two things you install yourself:

* **OBS Studio** — <https://obsproject.com/download> — run it once so it
  registers *OBS Virtual Camera*;
* **VB-CABLE** — <https://vb-audio.com/Cable/> — **reboot** afterwards, the
  driver only appears after a restart.

Then install the PC client itself:

```powershell
py -3 -m pip install -e .\windows
```

---

## 4. Transports

All three use the same connection code; only the address changes.

```powershell
# USB, with the port forwarded by adb
adb devices
adb forward tcp:8765 tcp:8765
phonebridge camera --virtual --host 127.0.0.1 --port 8765

# Wi-Fi LAN
phonebridge camera --virtual --host 192.168.1.42

# Phone hotspot: the PC is a client of the phone's access point
phonebridge camera --virtual --host 192.168.43.1
```

The phone's address is shown in the app; `phonebridge discover` also lists what
is announcing itself. Pairing is per-phone and remembered, so these are one-time
prompts.

---

## 5. Verification checklist — one command at a time

Run these in order. After each one, write down what you saw; a failure at step 1
makes steps 2-3 meaningless.

### Step 0 — the phone is reachable

```powershell
phonebridge info --host 192.168.1.42
```

Expect device facts as JSON. If this fails, nothing else will work: fix the
address, `adb forward`, or the firewall first.

### Step 1 — camera in Zoom or Meet

```powershell
phonebridge camera --virtual --width 1280 --height 720 --fps 30
```

Leave it running. In the meeting app, choose **OBS Virtual Camera** as the
camera. Expect live phone video and a status line once a second:

```
link up | camera active 29.8 fps
```

Things to try while it runs: cover the lens (picture changes), unplug the phone
from Wi-Fi (the last frame stays, then goes black after ~2 s), reconnect the
phone and start the command again.

Selfie camera instead of the rear one:

```powershell
phonebridge camera --virtual --facing front
```

### Step 2 — microphone in Voice Recorder

```powershell
phonebridge mic --to-cable
```

In **Voice Recorder** (or Sound settings → Input) select
**CABLE Output (VB-Audio Virtual Cable)** and record. Speak into the phone; the
recording must contain your voice. The status line shows the level:

```
link up | mic active -21 dBFS buf 120 ms
```

If it is silent but the app shows a level, the app is reading the wrong device.
If `-inf dBFS`, the phone is not capturing — check the permission prompt on the
phone.

### Step 3 — audio to the Bluetooth headphones

```powershell
phonebridge speaker --latency 200
```

Connect the headphones to the *phone* first (normal Bluetooth pairing, done in
Android — PhoneBridge does not pair anything). Play something on the PC; it
should come out of the headphones. Then turn the phone's screen off: playback
must continue (the app runs in a foreground service).

If the picture and the sound disagree when you run the camera at the same time,
add the offset to the camera side:

```powershell
phonebridge start --camera --mic --speaker --lipsync 120
```

### Step 4 — everything at once

```powershell
phonebridge start --camera --mic --speaker
```

```
link up | camera active 29.9 fps | mic active -24 dBFS buf 120 ms | speaker active 200 ms
```

Join a Meet call with the phone as the camera *and* the microphone. This is the
combination that matters, and the one no test here can stand in for.

---

## 6. Known limits, stated plainly

* **Audio is 48 kHz PCM16, not Opus.** Protocol v1 carries PCM16 with a 20 ms
  frame; adding Opus would change the wire format on both ends and break older
  phones. Stereo playback therefore costs ~1.5 Mbit/s — fine on USB and Wi-Fi,
  tight on a congested hotspot. Opus is a protocol change, not a packaging one.
* **No Bluetooth work in the app.** Android routes `USAGE_MEDIA` output to
  connected A2DP headphones itself, which is why there is no pairing code here.
  A headset in *call* (HFP/SCO) mode adds its own 100-200 ms; use `--lipsync`.
* **Latency is configured, not measured end to end.** The status line shows the
  buffer you asked for; measuring true mouth-to-ear latency needs an acoustic
  loopback, which is what `docs/09-compat-report.md` is for.
* **The webcam carries no mirroring.** Applications mirror their own previews;
  mirroring the device feed would mirror it twice.
* **One phone at a time per command.** Multi-device is the controller's job
  (`phonebridge gui`), not these commands'.
* **Device formats are negotiated, not assumed.** The endpoint's channel count and
  sample rate come from the device (a stereo "CABLE Input" gets duplicated mono; a
  44.1 kHz endpoint gets resampled), because writing the session's shape into a
  device that has its own is how a microphone ends up at half speed. Resampling is
  linear interpolation — exact in duration, modest in quality — and never applied
  when the rates already match.

---

## 7. Troubleshooting

| Symptom | Cause | What to do |
| --- | --- | --- |
| `no virtual camera backend could be opened` | OBS not installed, or never run since install | install OBS, open it once, re-run |
| camera opens but the app does not list it | the app caches its device list | restart the app; check Settings → Bluetooth & devices → Cameras |
| `no playback device matching 'CABLE Input'` | VB-CABLE not installed, or not rebooted | install it, reboot, re-run `setup-windows.ps1 -Check` |
| mic runs but the other app is silent | the app is reading a different input | select `CABLE Output (VB-Audio Virtual Cable)` |
| `capturing PC audio needs PyAudioWPatch` | the package is missing | `py -3 -m pip install PyAudioWPatch` |
| `dtype mismatch: 'bytes…' vs 'int16'` | a sounddevice `OutputStream` (numpy arrays) was used for raw PCM — fixed; you are on an older build | `py -3 -m pip install -e .\windows` from the branch head |
| `the output device accepts none of these formats` | the endpoint will not take 48 kHz mono or stereo | set the device to 48 kHz (Windows sound settings or the VB-CABLE control panel) |
| phone microphone audible but fast/high-pitched | the capture rate was not the device's rate | fixed by negotiation; if you still hear it, note the device's rate in `sounddevice.query_devices()` |
| `the phone stopped answering … no close frame (1006)` | the phone went quiet for 8 s: app frozen or killed, or the network dropped. It is *not* a PC defect, and it is unrelated to a dead PC device | keep PhoneBridge in the phone's Autostart list, out of battery optimisation, on a stable Wi-Fi |
| `microphone stopped: …` but the status line still shows the camera and speaker running | the PC device failed; the session and the other paths are untouched | fix or re-select the device, then restart the command (the error line is printed once) |
| speaker silent while the phone plays nothing | phone volume, or playback not started on the phone | check the app's playback toggle, raise the phone volume |
| status shows `link down` | the session closed | re-run the command; the phone app must be in the foreground once |
