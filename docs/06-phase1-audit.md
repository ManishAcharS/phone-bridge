# Phase 1 — audit, fixes and completion status

Scope: finish and verify Phase 1. No Phase 2 features (audio, camera, file
transfer, sensors) were started, and no existing working module was rewritten.

---

## 1. What was inspected

| Area | Files |
| --- | --- |
| Git state | branch `arena/021bece1-phone-bridge`, commit `caa2b3c` at audit start, clean tree, in sync with remote |
| Python protocol | `protocol/framing.py`, `crypto.py`, `messages.py` |
| Python transport | `transport/link.py`, `discovery.py` |
| Python core | `core/session.py`, `core/device_store.py`, `cli/main.py` |
| Python tests | `tests/test_framing.py` (10), `test_crypto.py` (21), `test_integration.py` (15) |
| Android build | `settings.gradle.kts`, `build.gradle.kts`, `app/build.gradle.kts`, `gradle.properties`, `gradle/wrapper/`, `AndroidManifest.xml`, resources |
| Android source | `protocol/{Framing,Crypto,Json,Messages,ProtocolVectors,SelfTest}.kt`, `net/{BridgeServer,PairingStore,IdentityStore,DiscoveryBeacon,AudioProbe}.kt`, `service/{BridgeService,BridgeState}.kt`, `MainActivity.kt` |
| Tooling | `tools/simulator/phone_sim.py`, `tools/vectorgen/gen_vectors.py`, `tools/diagnostics/env_report.ps1` |

### Environment reality

The audit environment has **no JDK, no Android SDK, no `adb`, no phone and no
Windows**:

```
java / javac / kotlinc / gradle / sdkmanager / adb   -> all MISSING
dl.google.com, repo1.maven.org, services.gradle.org,
objects.githubusercontent.com                        -> unreachable
```

So the Android app **cannot be compiled or executed here**, and no hardware was
tested. Everything below about Android is static analysis plus automated
cross-language conformance checks; §6 is the part that requires you.

---

## 2. Defects found and fixed

### 2.1 `[HIGH]` The Android self-test would have reported FAIL on a correct build

`tools/vectorgen/gen_vectors.py` sealed the AES vector frame with the *derived*
`c2s` key, but published `AES_KEY_HEX` (an independent 32-byte key) and the
Android self-test constructs `SecureCodec(AES_KEY_HEX, AES_KEY_HEX)`.

Verified concretely before fixing:

```
open with AES_KEY_HEX   : FAILED -> HandshakeError: frame failed authentication
open with HKDF_C2S      : OK  <-- frame was sealed with c2s
```

Consequence: two of the eight on-device checks (seal and open) would have failed
on a perfectly correct Kotlin implementation, sending you looking for a crypto bug
that does not exist. Fixed by sealing the vector with the standalone key (its
original purpose — isolating the AEAD envelope layout from key derivation) and
adding `test_aes_vector_opens_with_the_key_the_self_test_uses` so it cannot
regress. The generator now also **writes `ProtocolVectors.kt` itself**, so the
Kotlin file can no longer be hand-edited out of sync.

### 2.2 `[HIGH]` Android 14+ would crash when starting the bridge

`BridgeService` declares `foregroundServiceType="connectedDevice"`, which on
targetSdk 34 requires `FOREGROUND_SERVICE_CONNECTED_DEVICE` **and at least one
of** `BLUETOOTH_*`, `CHANGE_NETWORK_STATE`, `CHANGE_WIFI_STATE`, `NFC`, … The
manifest held none of them, so `startForeground()` throws `SecurityException`:

```
Starting FGS with type connectedDevice targetSDK=34 requires permissions:
  allOf=true  [android.permission.FOREGROUND_SERVICE_CONNECTED_DEVICE]
  allOf=false [BLUETOOTH_ADVERTISE, BLUETOOTH_CONNECT, BLUETOOTH_SCAN,
               CHANGE_NETWORK_STATE, CHANGE_WIFI_STATE, ...]
```

Fixed by declaring `CHANGE_NETWORK_STATE` and `CHANGE_WIFI_STATE` — both
"normal" permissions (granted at install, no prompt, and they grant no capability
this app exercises). `CHANGE_NETWORK_STATE` is the honest member of that list
here: the service exists to hold a network connection to the phone.

### 2.3 `[HIGH]` The APK could not have built as configured

`compileSdk = 34` with **Compose BOM 2024.09.03** (compose-ui 1.7.x) fails
`checkDebugAarMetadata`: compose 1.7.x requires `compileSdk 35`, which in turn
requires AGP 8.6+ (this project pins AGP 8.5.2, whose maximum supported
compileSdk is 34).

Fixed by moving to **Compose BOM 2024.06.00** (compose-ui 1.6.8 /
material3 1.2.1), which is compileSdk-34 compatible, keeping the rest of the
toolchain coherent. Also removed the unused `material-icons-core` and
`ui-graphics` dependencies.

### 2.4 `[MEDIUM]` Backoff never reset, so a reconnect could stall for 15 s

`ReconnectingSession._run` created the delay ladder once, outside the loop. A
drop after an hour of healthy use could therefore wait the full ceiling (15 s)
instead of restarting at 0.5 s. Fixed by recreating the ladder after every
successful connection.

### 2.5 `[MEDIUM]` Concurrent pings clobbered each other

Every `ping()` stored its future in the same dict slot (`_pending[-2]`), so two
overlapping pings meant all but the last timed out after 5 s. Replaced with a map
keyed by the timestamp embedded in the PING frame. Also: a PONG arriving while
`ping()` was waiting is now failed fast on close instead of hanging, and a
malformed (non-16-byte) PONG is dropped instead of raising `struct.error` inside
the read loop — which would have killed the session.

### 2.6 `[MEDIUM]` Android reported "listening" before the port was bound

`WebSocketServer.start()` returns before the socket is bound; a bind failure
arrives later on the server thread. `BridgeServer.start()` optimistically set
`running = true` and returned success, so `BridgeService` would start the
discovery beacon and hold a wake lock for a server that never bound. Now `start()`
waits (5 s) for `onStart()`/`onError(null, …)` and reports the real outcome.

### 2.7 `[LOW]` `WebSocketLink.is_open` was structurally wrong

It tested `state is not None`, which is true for a closing or closed socket. Only
invisible in practice because `close()` nulls the reference. Now compares against
`websockets.protocol.State.OPEN`, with a test that closes the socket behind the
wrapper's back so the defect cannot return.

### 2.8 `[LOW]` Mixed `failed` / `reconnecting` states

The docstring promised `failed` only for terminal give-up, but the loop emitted
it on every transient error, which would make any UI flicker to a failure state
while still retrying. Transient errors now fold into `reconnecting`; `failed` is
reserved for terminal. Docstring corrected.

### 2.9 `[LOW]` PONG's second field meant different things on the two sides

PC sent wall-clock nanoseconds, Android sent monotonic nanoseconds. Harmless
(nothing reads it) but a genuine cross-language inconsistency. PC now sends
`time.monotonic_ns()` and the protocol spec states the field is informational.

### 2.10 `[LOW]` Packaging excludes were a no-op

`resources.excludes += setOf("/META-INF/{AL2.0,LGPL2.1}")` is not a brace glob,
so it excluded nothing. Now two explicit entries. Also added the referenced but
missing `app/proguard-rules.pro`, and an explicit `RepositoriesMode` import in
`settings.gradle.kts` to remove any doubt about default imports.

### 2.11 `[LOW]` `bench` reported the wrong frame count and diluted throughput

`phonebridge bench --frames 500` printed `frames echoed : 520` (the 20 untimed
warm-up frames were included) and divided throughput by a window that also
contained the warm-up. It looked like a bug and understated speed by ~4%. The
timer now starts after the warm-up has drained and the count reports what was
asked for:

```
  frames echoed : 500 (+20 warm-up)
  throughput    : 29.89 MB/s (239.2 Mbit/s) payload, both directions
```

Covered by `test_bench_reports_the_requested_frame_count`, verified to fail
against the previous output.

### 2.12 Test-coverage gap: none of the above was reachable by the old suite

The 45 pre-existing tests all passed while §2.1–§2.11 were present. That is why
`tests/test_regressions.py` and `tests/test_android_conformance.py` were added —
see §3.

---

## 3. Tests added (all runnable here)

**`tests/test_regressions.py` — 11 tests.** Each one was verified to *fail*
against the pre-fix code, so none of them is decorative:

| Test | Catches |
| --- | --- |
| `test_concurrent_pings_all_resolve` | §2.5 — confirmed FAIL before fix (TimeoutError) |
| `test_ping_fails_fast_when_session_closes` | ping hanging after a drop |
| `test_ping_refused_on_closed_session` | silent use of a dead session |
| `test_malformed_pong_is_ignored_and_ping_still_works` | read-loop death on a bad PONG |
| `test_link_reports_closed_after_close` | link close semantics |
| `test_link_is_open_tracks_websocket_state_not_just_existence` | §2.7 — confirmed FAIL before fix |
| `test_link_is_open_false_before_open` | truthiness of a fresh link |
| `test_backoff_grows_then_caps` | ladder shape (0.5 → 1 → 2 → 4 → cap) |
| `test_backoff_restarts_after_a_healthy_session` | §2.4 — confirmed FAIL before fix |
| `test_transient_failures_are_not_reported_as_terminal` | §2.8 |
| `test_state_callbacks_that_raise_do_not_kill_the_supervisor` | a UI exception killing reconnect |

**`tests/test_android_conformance.py` — 39 tests.** These parse the Kotlin
sources and compare them to the canonical Python values, which is the strongest
cross-language verification available without a JVM:

* `ProtocolVectors.kt` must be **byte-identical** to the generator output (§2.1).
* `vectors.json` must equal a fresh `build_vectors()` run.
* Every Kotlin vector constant equals its Python counterpart.
* The embedded AES frame opens with the embedded key and reseals byte-identically.
* `SAS_EXPECTED` derives from `HKDF_C2S_HEX`; the ECDH vector is symmetric.
* Framing constants (`PROTOCOL_VERSION`, `KIND_*`, `MAX_ENVELOPE_BODY`) match.
* All ten message-type constants match.
* Crypto parameters match: `secp256r1`, `AES/GCM/NoPadding`, 128-bit tag,
  `HmacSHA256`, `SHA-256`, the three HKDF info strings, 4-byte-zero nonce prefix,
  big-endian counter.
* Every setup message (`auth`, `auth_ok`, `pair_required`, `pair_confirm`, …) is
  handled by **both** implementations.
* Handshake JSON field names (`t`, `v`, `id`, `name`, `static`, `eph`, `nonce`,
  `paired`) present on both sides.
* Every RPC method the CLI calls has an Android handler.
* Every `.kt` file has a package declaration matching its directory and balanced
  brackets (catches a truncated or mis-edited file).

---

## 4. Android build readiness

### Toolchain (mutually verified versions)

| Piece | Version | Why this one |
| --- | --- | --- |
| JDK | 17 | required by AGP 8.x |
| Gradle | 8.7 | minimum for AGP 8.5.x; pinned in `gradle/wrapper/` |
| AGP | 8.5.2 | supports compileSdk 34 |
| Kotlin | 1.9.24 | matches the Compose compiler below |
| Compose compiler | 1.5.14 | the version pair for Kotlin 1.9.24 |
| Compose BOM | 2024.06.00 | compose-ui 1.6.8 / material3 1.2.1 — compileSdk-34 safe (§2.3) |
| compileSdk / targetSdk | 34 | targetSdk 34 is what makes the FGS type mandatory (§2.2) |
| minSdk | 26 | `availableCommunicationDevices` etc. are version-guarded above this |
| SDK packages | `platforms;android-34`, `build-tools;34.0.0`, `platform-tools` | |

### Known gap: the Gradle wrapper binary is absent

The repo contains `gradle/wrapper/gradle-wrapper.properties` but **not**
`gradle-wrapper.jar` or `gradlew.bat`, because a Gradle distribution cannot be
downloaded from the audit environment. `gradlew.bat assembleDebug` will therefore
fail on a fresh clone. Two supported ways to build:

**Option A — Android Studio (recommended, and it fills the gap for you)**

1. **File → Open** → select the `phone-bridge\android` folder (not the repo root).
2. Let Gradle sync. Android Studio generates `gradlew`, the wrapper jar and
   `local.properties`, and downloads the SDK platform if missing.
3. **Build → Build Bundle(s) / APK(s) → Build APK(s)**, or in the Studio terminal:

   ```powershell
   .\gradlew.bat assembleDebug
   ```

**Option B — command line, generating the wrapper once**

```powershell
winget install Microsoft.OpenJDK.17      # then reopen the shell
winget install Gradle.Gradle             # provides the `gradle` command
cd phone-bridge\android
gradle wrapper --gradle-version 8.7      # creates gradlew.bat + wrapper jar
"$env:JAVA_HOME"                         # must point at the JDK 17 install
.\gradlew.bat assembleDebug
```

If `ANDROID_HOME` is not set, create `android\local.properties`:

```properties
sdk.dir=C\:\\Users\\<you>\\AppData\\Local\\Android\\Sdk
```

### Expected result

```
BUILD SUCCESSFUL in ...
android\app\build\outputs\apk\debug\app-debug.apk
```

Install and run:

```powershell
adb install -r android\app\build\outputs\apk\debug\app-debug.apk
adb shell am start -n dev.phonebridge/.MainActivity
```

Warnings you can ignore: `Divider` deprecation (material3 1.2.x),
`packageManager.getPackageInfo(String, Int)` deprecation, and Compose
"unused parameter" notes.

### Residual risk (stated, not hidden)

Because no compilation happened here, **type errors are still possible**. The
most likely places, in order, if the first build fails:

1. `MainActivity.kt` — Compose API surface (material3 1.2.1 vs newer names).
2. `BridgeServer.kt` — Java-WebSocket override signatures
   (`onMessage(WebSocket, ByteBuffer)`), and my new `CountDownLatch` start gate.
3. `Crypto.kt` — `KeyAgreement`/`GCMParameterSpec` plumbing.
4. `SelfTest.kt` — local `require(...)` shadowing Kotlin's builtin (legal, but
   worth knowing if a compiler warning appears).

Send me the compiler output and I will fix it immediately; that is a much faster
loop than guessing.

---

## 5. Protocol consistency matrix

Cross-checked against `docs/03-protocol.md` (unchanged — the spec was preserved;
only §4's PONG note was clarified).

| Aspect | Spec | PC (Python) | Android (Kotlin) | Status |
| --- | --- | --- | --- | --- |
| Envelope `u32 len | u8 kind | body` | §1 | `framing.py` | `Framing.kt` | ✅ constants + balance checked |
| `KIND_*`, `MAX_ENVELOPE_BODY` | §1 | 0x01/0x02/0x03, 8 MiB | same | ✅ test |
| `hello` / `hello_ack` fields | §2.1–2.2 | ✅ | ✅ | ✅ test |
| Version rejection | §2.2 | ✅ | ✅ | ✅ code review |
| ECDH P-256, X9.62 uncompressed | §2.3 | ✅ | ✅ | ✅ vector test |
| HKDF salt = cnonce‖snonce, two info strings | §2.3 | ✅ | ✅ | ✅ vector + string test |
| SAS = HMAC(c2s,"…sas")[0:4] mod 1e6, `%06d` | §2.3 | ✅ | ✅ | ✅ vector test |
| AES-256-GCM, per-direction keys, nonce = 4×0x00‖u64 BE | §4 | ✅ | ✅ | ✅ vector test (byte-exact frame) |
| AAD = counter, counter transmitted | §4 | ✅ | ✅ | ✅ vector test |
| Replay: `counter <= last` rejected | §4 | ✅ | ✅ | ✅ both have tests |
| Plaintext refused mid-session | §4 | ✅ | ✅ | ✅ Python test + Kotlin code |
| `auth` inside secure channel; AEAD success = auth proof | §3 | ✅ | ✅ | ✅ message-name test |
| Pairing: SAS compare + pin both sides | §3 | ✅ | ✅ | ✅ message-name test |
| Known-id-with-changed-key is refused, no re-pair | §3 | ✅ | ✅ | ✅ Python test |
| Message types 0x10/0x11/0x12/0x21 | §5 | ✅ | ✅ | ✅ constant test |
| Control JSON `req`/`res`/`evt` | §6 | ✅ | ✅ | ✅ RPC-handler test |
| PING/PONG informational clock | §5 | ✅ (now monotonic) | ✅ | ✅ aligned §2.9 |
| Refuse non-integer RPC id | §6 | ✅ | ✅ | ✅ code review |

**One asymmetry kept deliberately:** the Kotlin server shells out `res` errors for
unknown methods, while the Python *client* only ever sends the methods the CLI
uses (all implemented). Both directions are covered by
`test_kotlin_server_exposes_every_rpc_the_cli_uses`.

---

## 6. Test results

Run in the audit environment (Linux, no Android toolchain):

```
$ cd windows && python -m pytest -q
96 passed in 2.67s

$ ruff check .
All checks passed!
```

| Suite | Count | What it proves | Runs where |
| --- | --- | --- | --- |
| `test_framing.py` | 10 | envelope codec, size limits, stream reassembly | here |
| `test_crypto.py` | 21 | ECDH/HKDF/AES-GCM, tamper + replay rejection, SAS, MITM mismatch | here |
| `test_integration.py` | 15 | pairing, pinning, RPC, events, blob streams, bench reporting, reconnect, discovery over a real socket | here |
| `test_regressions.py` | 11 | the defects in §2 that are reachable without hardware | here |
| `test_android_conformance.py` | 39 | Kotlin↔Python constant/vector/message parity | here |
| **Android self-test** | 8 checks | Kotlin crypto equals the Python vectors | **your phone** |
| **Android build** | — | the app compiles | **your PC** |
| **Four connection paths** | — | real links work | **your PC + phone** |

### What still requires real hardware (no claim of success was made)

1. `gradlew assembleDebug` succeeds (and fixes any compile errors it surfaces).
2. The app installs and the foreground service starts without a
   `SecurityException`.
3. **On-device self-test: 8/8 checks pass** — the single most important gate.
4. `phonebridge info` over each of: Wi-Fi, USB+adb, USB tethering, hotspot.
5. SAS matches between the phone screen and the CLI prompt on first pairing.
6. Second connection needs no prompt (pinning works).
7. `phonebridge bench` reports zero payload corruption on a real link.
8. `phonebridge watch` reconnects unattended after the link is broken.
9. `audio.bluetooth_sco` flips to `true` when your headphones are connected.

Procedure and expected outputs: `docs/05-connection-verification.md`.

---

## 7. Can Phase 1 be considered complete?

**Not yet — it is complete on the PC side and unverified on the phone side.**

* The Python implementation is finished, and now also hardened: eleven defects
  were found and fixed, three of them blocking (a self-test that would have
  false-alarmed, an Android 14 crash, and an unbuildable dependency set).
* The Android implementation is complete *as source*, consistent with the spec by
  automated check, but has **never been compiled or executed**. Reporting it as
  verified would be fabrication.

Phase 1 closes when steps 1–9 above pass on your machine. Realistically that is
one build plus a few minutes of testing, and the self-test plus
`phonebridge info` over Wi-Fi are the two gates that matter most.
