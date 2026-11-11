# 19. Phase 2: bidirectional audio

Audio in both directions over the session the connection manager already
authenticated. There is no second socket, no second handshake and no second
identity: if the pinned key is not verified, there is nothing to send audio on.

```
Windows PC                                        Android phone
┌───────────────────────┐   MSG_AUDIO, stream 1   ┌──────────────────────┐
│ WASAPI loopback       │ ─────────────────────►  │ AudioTrack (speaker) │
│ (soundcard extra)     │   AES-256-GCM session   │                      │
│                       │                         │                      │
│ sounddevice monitor   │  ◄───────────────────── │ AudioRecord (mic)    │
│ (output device)       │   MSG_AUDIO, stream 2   │                      │
└───────────────────────┘                         └──────────────────────┘
```

## 1. What each direction does

| | PC → phone | Phone → PC |
| --- | --- | --- |
| Capture | WASAPI loopback of the default output (`soundcard`) | `AudioRecord` (`MediaRecorder.AudioSource.MIC`) |
| Playback | `AudioTrack`, `USAGE_MEDIA` / `CONTENT_TYPE_SPEECH` | `sounddevice.RawOutputStream` on the default output |
| Start | "PC audio → phone: start" in the Audio section | "Phone mic → PC: start" |
| Controls | mute (sends silence, keeps timing), volume 0-100 | start / stop |
| Stops when | the user presses stop, or the session ends | same |

Conservative defaults, and they are enforced in code rather than documented as
advice: **muted until asked**, volume starts at **40 %**, and neither direction
ever starts by itself — `AudioLink` is only reachable through an explicit button
press, and the phone refuses the microphone unless `RECORD_AUDIO` is already
granted in the PhoneBridge app.

## 2. Wire format

One frame per `MSG_AUDIO` (0x30), inside the encrypted envelope. The normative
table is in [`03-protocol.md`](03-protocol.md); the short version is an 8-byte
header — `codec`, `flags`, `seq`, `ts_ms` — followed by PCM16LE.

Defaults: **48 000 Hz, mono, 20 ms** (960 samples, 1920 bytes per frame,
~0.08 % protocol overhead). The phone may answer `audio.playback.start` with a
different rate; the PC honours it instead of resampling, because resampling twice
is how audio ends up sounding wrong.

## 3. The parts that make it usable, and why they exist

| Mechanism | Why |
| --- | --- |
| Bounded queue, drop-oldest (8 frames) | A slow link must cost a click, not a growing delay. This is the difference between "usable" and "echo". |
| `SequenceGate` receive window | Tolerates reordering, drops duplicates and frames older than the window, resynchronises after a stream restart. |
| `START` / `STOP` / `MUTED` flags | The receiver can distinguish "muted" from "stalled" from "closed", and a STOP lets `AudioTrack` drain instead of clicking. |
| Frame validation both sides | Unknown codec, truncated header, odd payload, >512 KiB body: counted and dropped, never fatal. |
| One direction failing is contained | A refused microphone playout does not stop PC audio, and neither takes the session down. |
| Compensating stop | Any failed start also sends `audio.playback.stop` / `audio.mic.stop`, because the call may have succeeded on the phone and timed out on the wire. |
| Release on session end | The last session leaving calls `AudioEngine.release()`; no microphone outlives a closed window. |

## 4. Android permissions and lifecycle

| Item | Value |
| --- | --- |
| Permission | `RECORD_AUDIO` (runtime). Requested from the app's own permissions card, at the point of use — never at launch. |
| Refusal | `audio.mic.start` fails with error code `mic_denied` and a sentence naming the app's permission card. The PC shows it verbatim. |
| Foreground service | The bridge service already runs in the foreground with an honest notification; audio adds no new service type. |
| Background capture | Not possible: capture only starts on an explicit PC request *and* an already-granted permission. |
| Cleanup | `AudioEngine.release()` on the last session ending; each direction also stops on its own `stop` RPC. |

No new manifest permission is required for playback. The microphone permission was
already declared (Phase 1 listed it as a permission the build has a use for).

## 5. Windows: two optional libraries, and one honest absence

| Purpose | Library | Status |
| --- | --- | --- |
| Capture system audio | `soundcard` | optional extra; `pip install soundcard` |
| Play the phone's mic on the PC | `sounddevice` | optional extra; `pip install sounddevice` |
| Make the phone a **Windows recording device** | third-party virtual audio driver | **not implemented, not claimed** |

`backend_status()` (surfaced by the Audio section and `phonebridge doctor`) reports
whether each library is importable and always sets `verified_here: false`, because
importability is not the same thing as working capture. The app never installs a
driver and never claims the phone has become a Windows microphone: an in-app
monitor is what this release provides.

## 6. Verification

| Claim | Status |
| --- | --- |
| Frame codec, flags, size/validation rules | **tested** — 29 tests |
| Sequencing: reorder, duplicate, late, wrap, resync | **tested** |
| Stream lifecycle: start/stop/mute/volume, backpressure, refused start, dead session, one-direction failure, teardown | **tested** — 24 tests |
| Phone's RPC answers, format negotiation, `mic_denied`, `bad_params`, `implemented.*` | **tested** — 9 tests against the reference phone |
| Protocol contract: docs/03 ⇄ Kotlin dispatch ⇄ simulator agree | **tested** — `tests/test_protocol_contract.py` |
| GUI Audio section renders and its buttons call real methods | **not rendered here** — no tkinter in this environment; `app.py` parses and is covered by the `View` tests |
| Android `AudioEngine` compiles and runs | **not compiled here** — no JDK/Android SDK in this environment |
| Sound actually comes out of the Redmi's speaker | **not tested** — needs the hardware |

## 7. Device test plan (Redmi Note 14 Pro+, all three transports)

Run once per transport: **USB** (adb forward), **Wi-Fi**, **hotspot**.

```powershell
cd C:\projects\phone-bridge
git pull
.\.venv\Scripts\pip.exe install soundcard sounddevice
.\PhoneBridge-Start.bat
```

Prepare the phone:

1. Install the APK (see `docs/07`): `adb install -r app\build\outputs\apk\debug\app-debug.apk`
2. Open PhoneBridge → permissions card → grant **Microphone**.
3. Tap **Start Bridge**.

Then, for each transport (USB first, then Wi-Fi, then hotspot):

| # | Step | Expected |
| --- | --- | --- |
| 1 | Connect in the GUI | transport row names the path actually used |
| 2 | Press **Diagnose USB** (USB case) | stage `ready`, not `no-device`/`no-adb-interface` |
| 3 | Press **PC audio → phone: start** | phone plays the PC's sound; button reads *stop* |
| 4 | Move the volume slider | phone output changes; Diagnostics shows no error |
| 5 | Press **Mute outgoing** | phone goes silent, direction still `active` |
| 6 | Press **Phone mic → PC: start** | speaking into the phone is audible on the PC |
| 7 | Refuse the phone's Microphone permission, retry | GUI shows the phone's `mic_denied` sentence, nothing captured |
| 8 | Disconnect the phone mid-stream | both directions return to `idle`; no error dialog, one log line |
| 9 | Reconnect, repeat step 3 | works without restarting the app |

Record for each transport: latency you perceive, dropouts (Diagnostics shows
`dropped`), and whether step 6 worked. Those numbers belong in the report; they
are the only thing that can turn any row above from "tested in software" into
"verified on hardware".

## 8. Known limitations

| Limitation | Reason |
| --- | --- |
| No virtual microphone | Needs a kernel audio driver; installing one silently is out of scope. Documented as an optional integration instead. |
| PCM16 only | One codec, one negotiation path. Opus would cut bandwidth ~8× and is the obvious next step. |
| No Bluetooth route for audio | Separate capability (`audio.bluetooth_sco`); it stays `unavailable` unless the phone actually has a headset route. |
| Mono by default | Both directions negotiate mono; stereo is accepted by the format but untested on the phone. |
| Windows libraries unverified here | No Windows host, no sound device in this environment. `backend_status` says so rather than pretending. |
| Kotlin not compiled here | No JDK/SDK here; `gradle test lint assembleDebug` must be run on the PC (`docs/07`). |
