# Android app completion — permissions, lifecycle, device facts, capabilities

Scope: make the phone app honest and usable in the real world, without starting
any Phase 2+ feature. No protocol change: `PROTOCOL_VERSION` is still 1, and every
new key in `system.info` is additive.

**Verification status:** the Android app still has not been compiled or run in
this environment (no JDK, no Android SDK, no `adb`, no phone). Everything below is
backed by source-level tests (`windows/tests/test_android_completion.py`, 36
tests) and a `kotlinc 1.9.24` declaration check, which reports no diagnostic that
names a symbol this phase introduced. `gradlew assembleDebug` and a device run are
still the gate — see §7.

---

## 1. What was wrong

| # | Defect | Consequence |
| --- | --- | --- |
| 1 | `capabilities()` returned `camera.back`, `camera.front`, `sensors`, `file_transfer` as `true` — hard-coded | A PC had no way to distinguish a working feature from a placeholder; `phonebridge info` advertised capabilities that do not exist |
| 2 | The capability probe launched permission requests and **started probing immediately**, without waiting | With the microphone denied, the probe ran anyway and reported capture failures the user could not explain |
| 3 | Only `shouldShowRequestPermissionRationale` was missing entirely | After a permanent denial, the "Allow" button silently did nothing (Android shows no dialog) |
| 4 | Permission state was read once, at composition | Revoking a permission from system settings while the app was open was invisible |
| 5 | `POST_NOTIFICATIONS` was requested on first launch | A permission prompt with no context, before the user had asked for anything |
| 6 | No permission was ever explained | Nothing told the user *why* the app wanted the microphone |
| 7 | The dashboard showed no capability state at all | The phone gave no way to check what it would report to the PC |
| 8 | Status was three ad-hoc strings in the top bar | "error" outranked nothing; a listening bridge with a connected PC looked the same as one without |
| 9 | The connect address was a placeholder (`ws://<this phone's IP>`) | Users had to find the phone's IP themselves |
| 10 | Only the raw error was shown | `BindException: EADDRINUSE` with no hint about what to do |
| 11 | `BridgeService` launched two notification collectors per `startBridge()` and never cancelled them | Every restart (Save, ACTION_RESTART, retry) left an extra pair notifying forever |
| 12 | `Json` encoded nested maps by handing them to `org.json`, which stores a `Map` as an object | Nested values (the probe report, `system.info`) risked being emitted as the string `{a=1}` instead of JSON |
| 13 | Device info lived in two places (`system.info` inline, the UI inline) | The screen and the PC could disagree |

## 2. Capabilities: measured, not asserted

`net/DeviceFacts.kt` is now the single source. Each capability answers three
questions separately:

| Field | Meaning | How it is obtained |
| --- | --- | --- |
| `hardware.<key>` | Does this phone have it? | Camera enumeration via `CameraManager` + `LENS_FACING`, `SensorManager` sensor list, audio device lists, Bluetooth feature flags. Read-only, no permission needed |
| `<key>` | Can PhoneBridge **use** it today? | `hardware && implemented && permissionGranted` |
| `detail.<key>` | Why not, in one sentence | e.g. "present, not implemented yet (Phase 4) — camera streaming is Phase 4" |

> **Phase 5 update.** This section records the Phase 1 audit, when every
> `implemented` flag was `false`. Since then Phases 2, 4 and 5 have landed:
> `audio.playback`, `audio.capture`, `audio.bluetooth_sco`, `camera.back`,
> `camera.front`, `sensors`, `file_transfer`, `bluetooth_scan` and
> `bluetooth_gatt` are all implemented, and every one has an engine behind it
> (`AudioEngine.kt`, `CameraEngine.kt`, `SensorEngine.kt`,
> `FileTransferEngine.kt`, `BleEngine.kt`) — `test_android_completion.py` fails if
> a flag says `true` while its engine is missing. The rule did not change: a
> `true` flag must be backed by a feature.

Every `implemented` flag was `false` in that phase, and a test fails if one is
ever set to `true` without the feature. The keys are unchanged
(`audio.playback`, `audio.capture`, `audio.bluetooth_sco`, `camera.back`,
`camera.front`, `sensors`, `file_transfer`, `bluetooth_scan`, `bluetooth_gatt`),
so an older PC
still reads them — it just stops being told a comforting lie.

`audio.bluetooth_sco` keeps its meaning at the hardware level (an HFP/SCO-capable
headset is present *right now*), which is the Phase 2 go/no-go signal; the bare
key stays `false` until the relay exists.

## 3. Permissions

`Permissions.kt` holds the model; `PermissionsCard.kt` renders it.

| Permission | When it is requested | If denied | If blocked (permanently denied) |
| --- | --- | --- | --- |
| `POST_NOTIFICATIONS` (13+) | When the user starts the bridge — that is when the foreground notification first matters | Bridge runs; only the notification is missing | Row shows "blocked" + Settings button |
| `RECORD_AUDIO` | When the user runs the capability probe (Phase 2 will ask when capture starts) | Capture/duplex tests are reported as *skipped*, never as passing | Row shows "blocked" + Settings button |
| `BLUETOOTH_CONNECT` (12+) | Same moment as the probe | Bluetooth facts are reported as unavailable | Row shows "blocked" + Settings button |

Rules encoded:

* **Asked when needed.** Nothing is requested at launch. A test asserts there is
  exactly one `launch()` call site in `MainActivity`, inside `startBridge()`.
* **Denied ≠ blocked.** `Permissions.markAsked()` records the first request, so a
  later refusal with `shouldShowRequestPermissionRationale() == false` is
  recognised as blocked; only that case offers "Open app settings"
  (`ACTION_APPLICATION_DETAILS_SETTINGS`).
* **Revocation is noticed.** The card re-reads state every 1.5 s; a
  granted → not-granted transition writes a `WARN` line into the bridge log and
  `system.info.permissions` immediately reports the new state, because
  `DeviceFacts` reads it live.
* **Nothing unused is declared.** No `CAMERA`, `BLUETOOTH_SCAN` or location
  permission is in the manifest; the panel lists them as "not requested yet,
  because nothing uses them", with the phase that will.

## 4. Connection lifecycle

* The service is the only owner of the server: `startBridge()` returns if a
  server exists, `BridgeServer.start()` returns if already running, and
  `onStartCommand` only calls `startBridge()` when `server == null`, so repeated
  taps or repeated `ACTION_START` intents cannot create a second listener.
* `stateJobs` (the notification collectors) are cancelled in `stopBridge()`, so
  restarts no longer accumulate notifiers.
* The UI status is one derivation (`statusOf`) over published state:
  `Stopped` / `Listening` / `Connected to N PCs` / `Error`, with an error
  outranking "running" so a session-level failure is never hidden.
* The exact connect address is shown: `ws://<lan-ip>:<port>` from
  `NetworkInterface` enumeration (no Wi-Fi permission), refreshed every 2 s, plus
  the `adb forward` line for USB.
* Network changes: the discovery beacon is rebound (Phase 1) and the address line
  updates; the listening socket survives, and a failed bind is retried with
  backoff before the service gives up.
* Background/foreground: the foreground service keeps the socket alive with the
  screen off; the activity only reads state, so rotation or returning from the
  background re-renders from the same flows.

## 5. UI

| Card | Contents |
| --- | --- |
| Bridge | Status pill, switch (start/stop), connect address, USB line, raw error **plus** an actionable explanation, Restart button |
| Permissions | One row per runtime permission: state, why it matters, what breaks without it, Allow / Settings |
| This device | Model, codename, Android + SDK + target, app version + build, ROM, protocol version, device id, name, Bluetooth headset and audio profile |
| Capabilities | One row per capability: "present"/"available" plus the reason it is not available |
| Connected PCs | Peers, pinned PCs, forget-all |
| Traffic | Frames/bytes/handshakes |
| Capability probe | Unchanged 30 s probe; now waits for the permission answer and says when a section was skipped |
| Protocol self-test | Unchanged 8-vector check |
| Settings | Name and port, Save and restart |
| Log | The server's ring buffer, including permission revocations |

Error texts are mapped (`explainBridgeError`): `EADDRINUSE` → "Port N is already
used…", bind timeout → VPN/other instance hint, permission/foreground failures →
their own message. The raw text is always shown too — the mapping adds advice, it
does not replace evidence.

## 6. Tests added (36, all runnable here)

`windows/tests/test_android_completion.py`:

* capabilities: every key declared once; **no** `"<key>" to true` anywhere; no
  `implemented = true`; gating on hardware+permission; `hardware.*` and
  `detail.*` published; `BridgeServer` delegates to `DeviceFacts`.
* device info: required keys present; `system.info` and the UI use the same
  block; legacy fields kept.
* permissions: manifest and model agree; nothing requested that is not declared;
  nothing declared that is not used; denied vs blocked; settings route for
  blocked; each permission has a reason and a consequence; revocation is logged;
  exactly one request site, and no `LaunchedEffect` requests anything; the probe
  waits for the answer; Phase 1's audio guards are untouched.
* UI: one status derivation, all sections present, connect address shown,
  actionable errors, capability card fed by `DeviceFacts`, shared layout helpers
  in one file.
* lifecycle/protocol: collectors cancelled, duplicate start is a no-op,
  `PROTOCOL_VERSION = 1`, nested-map JSON conversion.
* docs: the capability keys in `docs/03` match the code.

## 7. What is still required (unchanged by this phase)

1. `cd android && ./gradlew assembleDebug` on a machine with JDK 17 + SDK 34.
2. Install, then run the on-device protocol self-test (expect 8/8).
3. Permission walk-through: start the bridge with notifications denied; run the
   probe with the microphone denied, then blocked (Settings route); revoke
   `RECORD_AUDIO` from system settings while the app is open and watch the log.
4. `phonebridge info` — confirm every bare capability key is `false`, the
   `hardware.*` keys match the device, and `permissions` matches the app.
5. Airplane-mode/Wi-Fi toggle while listening: the address line changes and the
   PC reconnects to the new address.

Nothing in this phase claims those steps have been done.
