# Multi-device compatibility testing strategy

Three handsets, one question: **can PhoneBridge carry audio in both directions
through a Bluetooth headset on this device?** The answer is not in any spec sheet
— it is decided at runtime by the ROM, the headset and the current audio policy —
so it is measured, recorded and compared. Nothing in this document is a guess
about your phones; where a fact is vendor-reported it says so, and where a fact
must be measured the probe measures it.

| Device | Role | Status |
| --- | --- | --- |
| **Redmi Note 14 Pro+ 5G** | **primary development device** | not measured |
| Redmi Note 9 Pro | comparison / fallback (model confirmation pending) | not measured |
| Redmi 9 Power | oldest device — the legacy-path test | not measured |

Results live in [`09-compat-report.md`](09-compat-report.md), generated from the
measurements. This document is the method; that one is the output.

---

## 1. Why this cannot be done from specifications

Three concrete reasons, each of which would produce a wrong answer if guessed:

1. **Off-call SCO is a policy, not a radio capability.** `isBluetoothScoAvailableOffCall()`
   exists because some ROMs refuse to open a bidirectional link unless a call is
   active. Two handsets with identical Bluetooth chips can answer differently,
   and a handset that answers `true` can still deliver silence.
2. **The same model is not the same device.** The Redmi 9 Power shipped on
   Android 10 (SDK 29) and finished on Android 12 (SDK 31). SDK 31 is exactly the
   boundary where `startBluetoothSco()` is replaced by `setCommunicationDevice()`,
   so two units of the same model can need different code paths. The probe reports
   the installed build; the strategy branches on the measurement.
3. **A vendor "supports BT 5.x with HFP" line says nothing about duplex.** HFP is
   mandatory for headsets; *simultaneous* playback plus mic capture is the part
   that varies, and no datasheet distinguishes it.

**Rule, enforced in code:** the recorded Bluetooth version is displayed for
reference and is **never** read by the verdict logic. See
`windows/phonebridge/core/compat.py` and its test
`test_bluetooth_version_never_affects_findings_or_verdict`, which runs every
device profile against BT 4.2 / 5.0 / 5.2 / 5.4 / 6.0 / unknown and requires an
identical result.

---

## 2. What is measured, and why each item exists

| # | Measurement | Where it comes from | Why it matters |
| --- | --- | --- | --- |
| 1 | Manufacturer, model, codename, Android release, SDK, build id, fingerprint | `Build.*` | identifies the device **and** selects legacy vs modern API path |
| 2 | ROM name/version, power-save state, battery-optimisation exemption | `SystemProperties` (best effort), `PowerManager` | MIUI/HyperOS is the most likely source of background restrictions |
| 3 | Bluetooth adapter present/enabled, adapter name/address, bonded count | `BluetoothAdapter` | rules out "no radio / radio off" before anything else |
| 4 | HFP and A2DP connected devices | `BluetoothHeadset` / `BluetoothA2dp` profile proxies | proves a headset is actually attached, and on which profile |
| 5 | LE Audio support | `BluetoothAdapter.isLeAudioSupported()` (API 33+) | LE Audio changes the duplex story; recorded, not assumed |
| 6 | Declared off-call SCO | `AudioManager.isBluetoothScoAvailableOffCall()` | a hint — recorded, then **tested** |
| 7 | Route request accepted + time to observe | `setCommunicationDevice()` (31+) or `startBluetoothSco()` + `isBluetoothScoOn` (<31), plus `AudioDeviceCallback` | tells you which path works and how long route setup takes |
| 8 | Playback routed to headset | `AudioTrack.getRoutedDevice()`, `USAGE_VOICE_COMMUNICATION` | PC → phone → headset half of the bridge |
| 9 | Microphone routed **and non-silent** | `AudioRecord.getRoutedDevice()` + RMS/peak over 3 s | headset mic → phone half; routing "success" with silence is a failure |
| 10 | Simultaneous duplex | both streams for the soak duration; underruns, read failures, late chunks | the actual product requirement |
| 11 | Latency | acoustic chirp loopback (cross-correlation) or timestamp-based buffering estimate | decides whether a relay feels usable |
| 12 | Interruptions | `AudioDeviceCallback` removals, route loss, exceptions | a link that works for 3 s and drops is not a link |
| 13 | Bluetooth version | adapter (null on most builds) | **reference only, never used** |

### Two latency numbers, never mixed

| Method | What it includes | Budget (good / acceptable) |
| --- | --- | --- |
| `acoustic_loopback` | phone output + Bluetooth downlink + codec + air + headset mic + Bluetooth uplink + phone input | ≤ 150 ms / ≤ 250 ms round trip |
| `timestamp_estimate` | only the phone's own OS buffering; **excludes** the radio and codecs | ≤ 60 ms / ≤ 120 ms |

The acoustic number is the one that predicts how the bridge feels (add the
network leg and the PC-side buffering later). The estimate is recorded because it
is free and it separates "phone is slow" from "Bluetooth is slow".

---

## 3. Evidence rules

Every statement in the report carries one of these:

| Term | Meaning | May it decide a verdict? |
| --- | --- | --- |
| **measured** | produced by `CapabilityProbe` on this handset | yes |
| **declared** | what an Android API says (`isBluetoothScoAvailableOffCall`, `isLeAudioSupported`) | no — it is a hypothesis |
| **vendor spec** | launch/OS history from public sources, used for planning only | no |
| **unmeasured** | not tested yet | no — renders as `unmeasured` |

When a declaration and a measurement disagree, the measurement wins and the
disagreement is reported as `SCO_DECLARED_BUT_UNUSABLE`. That finding is the most
valuable output of this exercise: it is precisely the case where a design built on
API documentation would fail in your hand.

---

## 4. Runbook — per device, in this order

Do this for the primary device first, then the other two. Roughly 10 minutes each.

### 4.1 Prepare (once per device)

1. Install the debug APK (`docs/07-device-testing-guide.md` §3–§4).
2. Grant notification and Bluetooth permissions when asked.
3. **Pair the headset with the phone** (not with the PC). Use the same headset on
   all three devices — otherwise the comparison mixes handset and headset effects.
4. Turn off battery optimisation for PhoneBridge (MIUI: Settings → Apps →
   PhoneBridge → Battery saver → *No restrictions*), because a background kill
   mid-probe looks like an audio failure.
5. Note the headset model and whether it supports HFP wideband (mSBC). Put it in
   the `--label` text; a "fails on wideband" result is different from "fails".

### 4.2 Measure on the phone

Open PhoneBridge → **Capability probe**:

1. Connect the headset, confirm the app shows it as connected.
2. Press **Run probe (30 s)** with acoustic latency on.
3. **Wear the headset or rest the earpiece against the boom** — the acoustic test
   needs the earpiece to be audible to its own mic. If the result says
   "inconclusive", that is the reason.
4. **Speak for a few seconds** during the microphone test so there is signal to
   detect. Silence is a legitimate measurement and will be reported as such.
5. Keep the screen on (the app does this automatically while probing).

Then copy the JSON (**Copy JSON**) or measure from the PC.

### 4.3 Measure from the PC (preferred — stores the record)

```powershell
phonebridge compat run --host <phone-ip> --label "Redmi Note 14 Pro+ 5G" --acoustic
```

Repeat per device. Then:

```powershell
phonebridge compat compare     # table in the terminal
phonebridge compat report      # rewrites docs/09-compat-report.md
```

### 4.4 Repeat runs (stability)

Run the probe **three times** on the primary device and keep all three records
(different config dirs, or rename `label` to `… run 2`). A capability that works
once and fails twice is a conditional capability, and the report must show that.
Run at least one of them with the screen locked to see whether Android 14 stops
capture from a backgrounded app — record the outcome as an interruption.

---

## 5. What each device is being tested for

### Redmi Note 14 Pro+ 5G — primary

* The result Phase 2 is designed around. Two things matter, in this order:
  **does duplex work at all**, and **how good is the acoustic round trip**.
* If it reports duplex with a round trip ≤ 150 ms → build the relay as designed.
* If duplex works but is slow (> 250 ms) → keep duplex, add the jitter buffer and
  report the number to the user; the relay is still the right design.
* If the microphone is silent despite declared availability → the design must fall
  back to sequential switching (playback, then a mic window) or refuse duplex
  audio in the UI. Do not paper over it.
* Also record: SDK level (34 or 35 changes which APIs exist), LE Audio support,
  and whether HyperOS kills the probe when backgrounded.

### Redmi Note 9 Pro — comparison

* **Confirm the model first.** `curtana` (India) and `joyeuse` (global) are
  different SKUs. The report matches on model/codename; say which one you have.
* Ended on Android 12 / SDK 31 — the first release with
  `setCommunicationDevice()`. If it is on SDK 30 instead, the legacy path applies;
  the probe reports which.
* This device is the interesting middle case: new enough for the modern API, old
  enough that HFP wideband and MIUI's SCO policy are uncertain.

### Redmi 9 Power — legacy path

* The one device that may need `startBluetoothSco()` + `MODE_IN_COMMUNICATION`.
  Its value is proving that the fallback path works, and finding where it doesn't.
* If it is on Android 10/11 (SDK 29/30), expect `setCommunicationDevice()` to be
  unavailable and the legacy path to be used. On Android 12 (SDK 31) it moves to
  the modern path — same model, different code.
* If it cannot do duplex at all, that is a legitimate outcome: it becomes the
  "output-only" reference device, and the UI must say so on that phone.

---

## 6. Decision matrix — what each outcome means for Phase 2

Verdicts are computed by `compat.verdict()`; this table says what to do with them.

| Verdict | Meaning | Phase 2 consequence |
| --- | --- | --- |
| `duplex_supported` | playback + non-silent mic + a clean duplex soak | build the relay as designed; duplex is the product |
| `output_only` | playback works, microphone doesn't (or is silent) | Mode A (playback) everywhere; microphone only via a separate fallback path, clearly labelled per device |
| `no_headset` | nothing attached during the test | re-run with the headset connected — this is not a capability |
| `unsupported` | neither direction routed | this device cannot run the audio bridge; the UI must disable it and explain why |
| `inconclusive` | contradictory or partial measurements | investigate before designing around it (usually a silent mic or a blocked permission) |
| `simulated` | record came from the reference implementation | never used for a design decision; re-run on the phone |
| `unmeasured` | not tested | nothing may be assumed |

---

## 7. Device-specific restrictions to look for on MIUI / HyperOS

These are the known failure modes on Xiaomi ROMs. Each one is visible in the
probe output or in logcat, so record which applies:

| Observed | Where it shows up | What it means |
| --- | --- | --- |
| Probe dies or capture stops when the screen locks | `duplex.read_failures` > 0, or a short capture | Android 14 background-mic restriction, made worse by MIUI battery management — the relay will need a `microphone` foreground service |
| `isBluetoothScoAvailableOffCall()` false | `sco.declared_offcall_available` | the ROM gates SCO behind a call; expect route refusal |
| Route accepted, microphone silent | `route_input.signal_detected` false with `status: pass` | the classic MIUI/SCO trap — this is the case the design must degrade gracefully from |
| Route drops after a few seconds | `interruptions` contains `sco_device_removed` | power management is reclaiming the audio path; needs a foreground service with the right type |
| Second device wins the headset | route flaps across runs | one phone can hold the headset; do not test two phones simultaneously |
| A2DP connected but HFP not | `bluetooth.hfp_connected` empty, `a2dp_connected` set | headset is paired in "media only" mode; re-pair or enable calls (HFP) on the phone |

Also worth recording once per device, by hand: whether MIUI's per-app
"Bluetooth device" permission or "Autostart" toggle affects the result.

---

## 8. What must be true before AudioRelay is written

1. The primary device has a stored record with a real verdict (not
   `unmeasured`, not `simulated`).
2. That record has been reproduced at least twice.
3. `docs/09-compat-report.md` shows the primary device's verdict and the
   consequences in §6 have been accepted.

Until then the probe is the deliverable, and no relay code is written. This is
deliberate: the relay's architecture differs substantially between
"duplex works", "duplex is silent", and "only playback works", and choosing
before measuring would mean rewriting it.

---

## 9. Files

| Path | What it is |
| --- | --- |
| `android/app/src/main/java/dev/phonebridge/net/CapabilityProbe.kt` | the instrument |
| `android/app/src/main/java/dev/phonebridge/ProbeCard.kt` | the on-phone UI |
| `windows/phonebridge/core/compat.py` | schema, findings, verdict rules, report renderer |
| `windows/tests/test_compat.py` | 57 tests, including the "never infer from BT version" rule |
| `docs/09-compat-report.md` | the generated report (currently: nothing measured) |
| `phonebridge compat run / show / compare / report` | the CLI |
