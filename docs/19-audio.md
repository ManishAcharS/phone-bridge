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

"Muted until asked" is a property of the *link*, so the thing that asks is the
caller: the GUI has an unmute button, and `phonebridge speaker` asks once, on
start. It has to — a muted stream is silence carrying `FLAG_MUTED`, and the phone
discards muted frames before they reach `AudioTrack`, so a speaker command that
forgot to ask would look exactly like a phone that cannot play. `--muted` is how
a user says "route it, but keep quiet".

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
| Phone's playback block (`route`, `track_state`, `underruns`, `played_ms`, ...) | **tested** — `tests/test_android_audio_stats.py` checks the Kotlin reads `AudioTrack` and that `BridgeServer` sends every key; `tests/test_live_speaker_stats.py` renders it |
| The test tone (frequency, level, length, end-of-tone, stereo) | **tested** — 18 tests in `tests/test_audio_tone.py` |
| Sound actually comes out of the Redmi's speaker | **not tested** — needs the hardware; `--test-tone` is the shortest way to check |

## 6a. Is the phone actually playing? (`speaker` status and `--test-tone`)

`audio.playback.start` returning `ok` means the phone opened an `AudioTrack`. It
does not mean the device consumed a sample: a muted stream, a route to a headset
that is not on anyone's head, and a track that stopped writing all report `ok`.
So the phone reports what its own track is doing, `audio.status` carries it, and
the PC prints it once a second.

| Field | Where it comes from on the phone | What it tells you |
| --- | --- | --- |
| `route` | `AudioTrack.routedDevice.type` | `speaker`, `bluetooth_a2dp`, `bluetooth_sco`, `wired_headset`, `usb_headset`, `none` |
| `device_name` | `productName` of that device | which headset, by the name Android has |
| `track_state` | `state` + `playState` | `playing`, `paused`, `stopped`, `uninitialised`, `no_track` |
| `buffered_ms` | queued frames x frame length | audio waiting in front of the device |
| `buffer_ms` | `bufferSizeInFrames` | the device buffer's capacity |
| `underruns` | `underrunCount` | how often the device ran dry — clicks you can hear |
| `played_ms` | `playbackHeadPosition` | how much the device has actually played |
| `frames_in` / `frames_written` | counters | received vs handed to `AudioTrack` |

The PC turns those into one line, and derives the two rates that a cumulative
counter cannot give:

```
link up | speaker active pc-buf 200 ms -> bluetooth_a2dp rx 50 f/s wrote 50 f/s played 12.4 s underruns 0
```

Read it as: the phone received 50 frames a second, wrote 50 frames a second into
its track, has played 12.4 s, and Android routed it to A2DP headphones. The two
failure signatures are visible in it:

* **`rx 50 f/s wrote 0 f/s`** — frames arrive and nothing reaches the device. On
  an older PC build this was a muted stream; on any build it is an `AudioTrack`
  that has stopped consuming (check `track_state`).
* **`-> speaker`** when you expected headphones — Android did not route to
  Bluetooth. That is an Android/headset question, not a PhoneBridge one.

### `phonebridge speaker --test-tone`

The speaker path normally proves itself by playing something on the PC, which
depends on a sound card, a loopback device, and the right loopback device. None
of that is the phone. `--test-tone` replaces the capture side with a 440 Hz sine,
at -12 dBFS, for 3 seconds:

```powershell
phonebridge speaker --test-tone
```

```
delivering: speaker from Pixel 8 (usb)
test tone: 440 Hz for 3.0 s, sent to the phone; nothing is captured from this PC, so a
silent phone is a phone problem. Watch the speaker line: route, f/s and played all
come from the phone
link up | speaker active pc-buf 200 ms -> speaker rx 50 f/s wrote 50 f/s played 2.6 s
test tone sent: 440 Hz for 3.0 s - if the phone was silent, the speaker path is the
problem, not the PC's audio
```

The tone is an ordinary `AudioSource` (`phonebridge/audio/tone.py`): its frames go
through the same queue, the same encrypted session and the same protocol frames as
PC audio, so a tone that arrives proves the whole path. It is paced one frame per
20 ms, because the phone's queue is eight frames deep and drops the oldest: a tone
dumped in one burst would be heard as a click, not as a note. It needs no PyAudioWPatch
and no `soundcard`, and it stops by itself when the note has drained instead of
leaving a session open on silence. If you hear the tone but not PC audio, the
phone is fine and the loopback device is the problem — pick it with `--loopback`.

The phone's own log (`adb logcat -s BridgeServer`) has one line per start and stop;
the numbers above are the ones to trust, because they come from the device.

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
| 3a | `phonebridge speaker --test-tone` | a 440 Hz note for 3 s, then the command exits by itself; the line shows `-> speaker` (or `-> bluetooth_a2dp`) and `played` climbing |
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

## 7a. Speaker latency, measured per stage

"About half a second to Bluetooth headphones" is five stages added together, and
only some of them are ours:

| Stage | Before | Now | Where |
| --- | --- | --- | --- |
| PC loopback capture block | 20 ms | 20 ms (one frame; the read returns whole frames) | `PyAudioLoopbackSource.read` |
| PC → phone queue | 200 ms configured, not applied | **60 ms**, sent as `target_ms` | `AudioLink.start_playback` |
| Phone playback queue | 8 frames = **160 ms**, fixed | **3 frames = 60 ms**, adaptive 1-8 | `AudioEngine.startPlayback` |
| `AudioTrack` buffer | 4 frames = 80 ms | 2-5 frames, `PERFORMANCE_MODE_LOW_LATENCY` | `AudioEngine.startPlayback` |
| Bluetooth A2DP | ~100-250 ms | unchanged — this is the floor | Android, not us |

Two things decide the phone's queue, and they are deliberately different:

* the **target** (60 ms) is what the PC asks for and what the queue is sized to;
* the **adaptation** moves that target one frame at a time, and only on evidence:
  up when the device's own underrun counter rises (or the writer found the queue
  empty while audio was flowing), down when a whole 500 ms window had neither. A
  silent PC teaches it nothing — an empty queue during silence is not pressure.

The writer drops the **oldest** frame when the queue is full, so a slow link costs
a click instead of permanent delay. `PERFORMANCE_MODE_LOW_LATENCY` asks for the
fast mixer path, which removes one mixer period on the built-in speaker and on
wired output; on A2DP it changes nothing and is guarded so a refusal cannot cost
the audio.

### Measuring it: `phonebridge speaker --latency-test`

```powershell
phonebridge speaker --latency-test
phonebridge speaker --latency-test --format json   # the same numbers, machine readable
```

It starts playback with the latency probe armed, sends one click, and prints:

```
  speaker latency, measured per stage
  --------------------------------------------------------------
  PC capture block             20 ms   one 20 ms frame: the read returns whole frames
  PC -> phone                   0 ms   no jitter buffer on this path: one frame per pull
  phone queue target           60 ms   1 frames in the queue right now
  arrival -> AudioTrack        74 ms   the phone's own probe, on the phone's clock
  (track buffer reported       40 ms   inside the number above, not on top of it)
  --------------------------------------------------------------
  to the phone's mixer         94 ms
  Bluetooth A2DP           +100-250 ms   not measurable from an app: codec + headphone buffer
  to the ear                ~194-344 ms
  note: Android does not expose the A2DP codec or its buffer to an app, so the
        Bluetooth stage is a range, not a measurement
```

**The Bluetooth floor is real and it is not ours.** `to the ear` is a range because
`BluetoothA2dp.getCodecStatus()` is a system API: an ordinary app is refused the
codec name *and* the codec's buffer, so any single number there would be invented.
100-250 ms covers SBC/AAC as Android usually negotiates them; aptX Low Latency
(~40 ms) needs both ends to support it and is not something PhoneBridge can select.
So "≤300 ms to a Bluetooth headset" is a **wired or built-in-speaker** target.
Over A2DP, 300 ms is the best case of the sum above and depends on the headset.

The probe itself is exact where it applies: the phone records when the marked
frame arrived from the socket, then when `playbackHeadPosition` passed the frame it
wrote. That is arrival → the mixer consuming it. The `AudioTrack` buffer is
*inside* that number, not additional, which is why the table says so.

### 7b. Which device is captured, and why the PC keeps playing

WASAPI loopback copies what an **endpoint renders**. Capturing the default output
is therefore capturing the speakers: the person at the PC hears everything, and
the phone hears the same thing a moment later. Muting that endpoint in Windows
also zeroes what loopback hands over, so "mute it so I do not have to listen" is
the one workaround that cannot work.

The good setup gives the phone its **own** endpoint:

```
  app (Zoom, a player, a browser)  ->  "CABLE-A Input"   (a second virtual cable)
                                             |
                                             +--> WASAPI loopback --> phone --> headphones
                                             |
                                    nothing is connected to its output
                                             |
  PC speakers (default output)     <-  everything else, unchanged
```

1. Install a **second** virtual cable so the microphone keeps its own:
   VB-CABLE's A+B pack gives `CABLE Input`/`CABLE Output` (the microphone, as
   before) plus `CABLE-A Input`/`CABLE-A Output`. VoiceMeeter's virtual inputs work
   too. `scripts/setup-windows.ps1` now says which cables it can see.
2. Send the audio you want on the phone to it — either:
   * **everything**: Settings → System → Sound → set the output to *CABLE-A Input*,
     and keep your actual speakers selected in the app you are listening from; or
   * **one application**: Settings → System → Sound → **Volume mixer** → set that
     app's *Output device* to *CABLE-A Input* (Windows 11 keeps this per app).
3. Capture that device, not the default:

   ```powershell
   phonebridge speaker --list-loopback-devices
   phonebridge speaker --loopback-device "CABLE-A Input"
   phonebridge start --camera --mic --speaker --loopback-device "CABLE-A Input"
   ```

`--list-loopback-devices` marks the default output and every virtual cable it can
see, and says which of them will still be heard in the room:

```
  devices this PC can capture (WASAPI loopback)
  ------------------------------------------------------------------
  Speakers (Realtek(R) Audio)  [default output]
    2 ch, 48000 Hz
  CABLE-A Input (VB-Audio Cable A)  [virtual cable]
    2 ch, 48000 Hz
  CABLE Input (VB-Audio Virtual Cable)  [virtual cable]
    2 ch, 48000 Hz
```

`--loopback-device` matches a **substring** of the name, case-insensitively, so
`--loopback-device cable-a` is enough. `--loopback` is the same flag under its old
name. Nothing is muted or rerouted by PhoneBridge itself: it reads the endpoint the
user chose and leaves Windows' audio configuration alone.

Capturing the same cable the microphone writes to is the one combination to avoid:
the phone would play its own microphone back to itself.

### `rx 0 f/s` is usually not a fault

WASAPI loopback captures what the *endpoint* renders. When nothing on the PC is
playing, the endpoint is idle and the loopback stream delivers **no packets at
all**: `read()` blocks, no frames are sent, and the phone reports `rx 0 f/s` while
every component is working correctly.

The PC now says so, in the status line, next to the number that looked like a
fault:

```
link up | speaker active pc-buf 60 ms -> bluetooth_a2dp pc silent rx 0 f/s ...
```

`--keep-alive` goes one step further and sends silence at the frame cadence while
the PC is silent, so the phone's `AudioTrack` and the Bluetooth link stay warm and
playback does not re-buffer the moment something does start playing. It costs
about 24 KB/s and changes nothing else.

The cadence is the whole point, and it was wrong: the feed ran at about **20
frames a second** for a 20 ms frame, so the track underran continuously — hundreds
of underruns, from a path that was working. Two things were wrong and both are
fixed:

* the feeding shared a loop with the silence *watcher*, so half of every cycle
  went into observing instead of sending;
* it slept *between* frames, so every period was `frame + work`, not `frame`.

The keep-alive now runs on its own loop against an absolute deadline
(`next_at += 20 ms`), which holds 50 frames a second; a late tick resets the
schedule instead of firing a burst the phone cannot use. Two consequences worth
knowing:

* `stale`/dropped frames stay zero while the PC is silent, because silence is now
  paced rather than burst;
* underruns reported **while the PC is silent** are counted apart, as
  `underruns_silent`, and do not inflate `underruns`. A starved track underruns by
  definition; counting that as a fault made a healthy stream look broken. The raw
  counter is still there as `underruns_total`, and the phone (`AudioEngine.adapt`)
  no longer grows its queue for underruns reported while nothing was written — it
  used to sit at eight frames of latency for audio nobody sent.

```
link up | speaker active pc-buf 60 ms -> bluetooth_a2dp pc silent rx 50 f/s
         wrote 50 f/s target 60 ms underruns 0 (silent 312 of 312)
```

`rx 50 f/s` next to `pc silent` *is* the keep-alive working: the phone is receiving a
frame every 20 ms while the PC plays nothing. Without keep-alive the same line says
`pc silent rx 0 f/s`, which is correct too.

### 7c. Which build is on the phone

`speaker --latency-test` used to end with *"the phone never reported the probe
reaching its mixer"* in two very different situations: an APK that predates the
probe, and an APK whose probe is broken. Nothing on the wire carried a version, so
the message could not be more specific.

The APK now names itself — `BuildInfo.id`, `1.0.0 (1) e807c93a` (version, version
code, short git commit, with `nogit` for a source archive) — in three places:

| where | field | why |
| --- | --- | --- |
| handshake (`auth_ok` / `pair_required`) | `build`, `features` | the first thing the PC sees, before any command |
| `system.info` | `build_id`, `git_sha` | `phonebridge info`, and any bug report |
| `audio.status`, `camera.status` | `build_id` | survives a reconnect; travels with the numbers it explains |

`features` is a map of booleans (`latency_probe`, `fps_range`, `adaptive_queue`),
so a capability that is *missing* reads `false` instead of timing out. The PC
prints the build at connect:

```
connected to Xiaomi 24115RA8EI (paired) over usb - app 1.0.0 (1) e807c93a
```

and, on an older APK, says so instead of pretending:

```
connected to Xiaomi 24115RA8EI (paired) over usb - app build not reported (APK older than this field)
phone app build not reported: its APK predates both the build id and the latency
  probe, so there is nothing to time yet.
```

`--latency-test` checks `features.latency_probe` from the handshake **before**
starting the measurement, so the six-second wait for a probe that cannot come is
gone. `phonebridge selftest` treats both keys as optional — an old APK is a
finding, not a failure — and prints `app build not reported (older APK)`.

## 8. Known limitations

| Limitation | Reason |
| --- | --- |
| Virtual microphone needs a driver you install | PhoneBridge writes the phone's microphone into **VB-CABLE**'s "CABLE Input"; applications then record "CABLE Output". No driver is installed by PhoneBridge itself, so without VB-CABLE there is no Windows recording device to write into (`phonebridge mic --to-cable` says so). See `docs/26`. |
| PCM16 only | One codec, one negotiation path. Opus would cut bandwidth ~8× and is the obvious next step. |
| No Bluetooth route for audio | Separate capability (`audio.bluetooth_sco`); it stays `unavailable` unless the phone actually has a headset route. |
| Mono by default | Both directions negotiate mono; stereo is accepted by the format but untested on the phone. |
| Windows libraries unverified here | No Windows host, no sound device in this environment. `backend_status` says so rather than pretending. |
| Kotlin not compiled here | No JDK/SDK here; `gradle test lint assembleDebug` must be run on the PC (`docs/07`). |
