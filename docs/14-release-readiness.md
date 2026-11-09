# 14. Release readiness

Target: **PhoneBridge 1.0.0** — first public release.

How to read this page: every line is either ✅ done *here, with evidence*, or
⛔ blocked *with the reason and the exact command that unblocks it*. Nothing is
marked done because it looks plausible. The two blocking items are B1 (no
physical Android device has run this code) and B2 (the release APK cannot be
built in this environment). Until they are cleared, this release is
**code-complete but not shippable**, and it is not called production-ready.

Last verified: 2026-11-08.

---

## 1. Status at a glance

| Gate | State |
| --- | --- |
| Automated tests (Python) | ✅ 476 passed |
| Lint (ruff) | ✅ clean, whole repo |
| Type check (mypy) | ✅ clean, 19 files |
| Vectors / protocol conformance | ✅ `tools/verify.py` 7/7 |
| Package installs from a clean venv | ✅ verified twice (editable and wheel) |
| CLI commands, help and exit codes | ✅ 28-case sweep, 0 failures |
| Android sources parse, no new compiler diagnostics | ✅ stub compile, differential |
| Android app built into an APK | ⛔ **B2** |
| Android app exercised on a handset | ⛔ **B1** |
| PowerShell workflows executed on Windows | ⛔ **B4** |
| Play Store submission | ⛔ **B3** (targetSdk) + **B5** (keystore/assets) |

---

## 2. Checklist

### 2.1 Source, version and packaging

| Item | Evidence |
| --- | --- |
| ✅ Version is `1.0.0` everywhere it is exposed | `windows/pyproject.toml`, `windows/phonebridge/__init__.py`, `android/app/build.gradle.kts`; `phonebridge --version` → `phonebridge 1.0.0 (protocol v1, Python 3.11.2)` |
| ✅ Package metadata complete | name, version, description, readme, license (SPDX `MIT`), authors, keywords, 14 classifiers, 5 project URLs, `requires-python >=3.10`, console-script entry point, `py.typed` |
| ✅ Dependencies pinned by range and minimal | `websockets>=13`, `cryptography>=42`; nothing else at runtime |
| ✅ Dev extras separated from runtime deps | `[dev]` = pytest, pytest-asyncio, ruff, mypy |
| ✅ `pip install .` works in a clean venv | built `phonebridge-1.0.0` wheel from a fresh `python -m venv`; installed `websockets 17.4`, `cryptography 50.0.2` |
| ✅ Wheel metadata is valid | `pip show` → `License-Expression: MIT`, entry point `phonebridge.cli.main:main`, `Requires: cryptography, websockets` |
| ✅ Fixed during this pass | `license = "MIT"` + the `License :: OSI Approved` classifier made **setuptools ≥77 refuse to build** (PEP 639). The classification was removed; the build now succeeds. This was a hard blocker for `pip install`. |
| ✅ No build artefacts tracked in git | `.gitignore` covers `*.egg-info/`, caches, `android/app/build/`, `*.apk`, `local.properties` |
| ✅ Signing material cannot be committed | `android/keystore.properties`, `*.jks`, `*.keystore` added to `.gitignore` |
| ✅ Android `applicationId` / `namespace` | `dev.phonebridge`, unchanged — required for update compatibility with any earlier install |
| ✅ `versionCode` policy | `1`; increment by at least 1 for every upload, never reuse |
| ✅ `minSdk` / `compileSdk` / `targetSdk` | `26` / `34` / `34` (see B3 for the Play consequence) |

### 2.2 Android pre-release review

| Item | Evidence |
| --- | --- |
| ✅ Manifest surface | one exported component (the launcher activity); service `exported="false"`; `allowBackup="false"`; no storage, camera or media permissions declared |
| ✅ Foreground service correctness | `foregroundServiceType="connectedDevice"` + the two permissions Android 14 requires for it |
| ✅ Permissions UX | `PermissionsCard` distinguishes granted / denied / **permanently denied** (offers system settings, not a dead-end dialog) / not applicable, re-reads state on a timer, and logs revocations into the bridge log |
| ✅ Onboarding | new dismissal-persisted **Start here** card on a fresh install (bridge off, no peers, no pairing) with the three steps that actually matter |
| ✅ Destructive action | "Forget all paired PCs" now asks for confirmation and explains the consequence (re-pairing required) |
| ✅ Error states | bridge errors are shown with a plain-English explanation (`explainBridgeError`) and a **Restart bridge** action; probe and permission failures are surfaced where they happened |
| ✅ Input validation | port field rejects non-numeric input, and an out-of-range port now shows an inline error instead of silently doing nothing |
| ✅ Edge-to-edge / Android 15–16 window handling | `enableEdgeToEdge()` + `Scaffold` insets padding |
| ✅ Predictive back | `android:enableOnBackInvokedCallback="true"`, no custom back handling to break |
| ✅ Android 16 API use | no API removed in 15/16 is used; no native libraries (16 KB page-size rule is moot); no implicit broadcasts; no background activity starts |
| ✅ Kotlin sources parse; no new compiler diagnostics | stub `kotlinc` differential vs the previous baseline: only SDK-cascade noise (see B6) |
| ⛔ Built and installed | **B2**, then **B1** |
| ⛔ Verified on a handset (journey, recovery, dark mode, rotation, TalkBack, battery) | **B1** |

#### 2.2.1 User journey, state by state

Read from the source top to bottom, once per state, with the code that owns each
one. "Verified" here means *the code was traced*, not that a phone displayed it —
that is B1.

| Step | What the user sees | Owned by |
| --- | --- | --- |
| 1. First launch | **Start here**: 1) grant Bluetooth permission 2) tap Start bridge 3) pair from the PC. Dismissed with "Got it" and never shown again. | `OnboardingCard` in `MainActivity.kt`, `phonebridge_settings.onboarding_done` |
| 2. Permissions | Four rows (Bluetooth, notifications, battery, location-not-needed) with live state; a denied permission offers the system dialog, a *permanently* denied one offers system settings, and revocations are logged. | `PermissionsCard.kt` |
| 3. Start bridge | Endpoint (`wss://<ip>:8765`), port, device id, fingerprint prefix, connections count; a foreground notification appears and a "Restart bridge" action is offered if the bind fails. | `BridgeService`, `StatusCard` |
| 4. Self-test | Vector results one by one, each pass/fail with the failing vector named. | `SelfTestCard` |
| 5. Pair from the PC | Full-screen prompt: six-digit code, PC name, Accept / Reject, 60-second expiry, single use, refused automatically for browser origins. | `PairingPrompt` / `PairingState` |
| 6. Connected | Peer shows Connected with its address; session log scrolls; stats (RTT, bytes) available to the PC. | `PeerListCard`, `LogCard` |
| 7. Error | Plain-English explanation plus **Restart bridge**; the previous state is preserved so nothing has to be re-entered. | `explainBridgeError` |
| 8. Network change / drop | Wi-Fi↔mobile changes restart the beacon; the session closes and the PC's `watch` reconnects with jittered backoff, resuming with the same pinned keys (no re-pairing). | `BridgeService.NetworkCallback`, `ReconnectSupervisor` |
| 9. Forget all | Confirmation dialog naming the consequence (every PC must pair again) → all pins dropped, live sessions closed. | `MainActivity.kt` `AlertDialog` |
| 10. Service killed | System re-creates it (`START_STICKY`), including the null-intent case, and the beacon resumes. | `BridgeService.onStartCommand` |
| 11. Reboot | The bridge stays **off** until the app is opened; nothing starts in the background. | no `BOOT_COMPLETED` receiver (by design) |
| 12. Theme / rotation | Compose Material 3 colour scheme follows the system; state is re-read from settings, so a rotation never loses the saved configuration. | `PhoneBridgeTheme`, `remember(port)` |

### 2.3 Python client

| Item | Evidence |
| --- | --- |
| ✅ Clean installation | fresh venv, wheel built and installed, `--version`, `--help` and every subcommand run from the installed console script |
| ✅ All 13 commands expose `--help` and exit 0 | scripted sweep over `doctor, identity, devices, discover, info, capabilities, audio, ping, bench, watch, selftest, forget, compat` |
| ✅ Argument validation | `ping --count 0`, `bench --size 0`, `bench --frames -5`, unknown subcommand → exit 2 with a usage message |
| ✅ Failure handling without a phone | unreachable host → exit 2 with a hint, never a traceback |
| ✅ Exit codes match the documentation | 0 / 1 / 2 / 4 / 5 / 130, checked against `docs/11` §12 |
| ✅ Type checking added and clean | `mypy` 2.4.1, 19 files, zero issues; six real issues found and fixed (an `Optional` dereference in `ping`, a list/str reuse in `doctor`, plus annotation gaps) |
| ✅ `tools/verify.py` runs the whole gate | 7 checks: environment, Android build files, vectors, tests, lint, **types**, CLI smoke |
| ✅ Documented commands cannot drift | `tests/test_release_metadata.py` resolves all **98** documented
invocations in the README and `docs/` against the live argument parser, so a renamed or
mistyped flag fails the suite instead of the user (this caught `--no-discovery` in a draft) |
| ✅ Release metadata is pinned by tests | same file: one version across `pyproject`/
`__init__`/`build.gradle.kts`, no license classifier alongside the SPDX string, real entry point,
keystore patterns ignored, every `docs/*.md` relative link resolves |
| ⛔ PowerShell workflows on a real Windows shell | **B4** |

### 2.4 Documentation

| Item | Evidence |
| --- | --- |
| ✅ README rewritten for release users | what works / what does not, requirements, install (PC + phone), first run, pairing, day-to-day use, capability table, troubleshooting, limitations, verify, layout, security, doc index |
| ✅ Installation instructions | README §Installation + `docs/07` for SDK setup |
| ✅ Android setup instructions | `docs/07-device-testing-guide.md` |
| ✅ Pairing instructions | README §Pairing; `docs/11` §5; `docs/05` |
| ✅ Every supported capability documented | README §Supported capabilities (all 8 keys, what `usable` means); `docs/03` §6 |
| ✅ Troubleshooting | README table (10 rows) + `docs/11` §13 + `docs/07` |
| ✅ Known limitations | `docs/16-known-limitations.md`, summarised in the README |
| ✅ Architecture and protocol | `docs/02-architecture.md`, `docs/03-protocol.md` (normative, three-way enforced by tests) |
| ✅ Security | `docs/13-security-review.md` (13 findings, severities, fixes, residual risk) |
| ✅ Verification matrix | `docs/12-end-to-end-verification.md` (16 workflows) |
| ✅ Compatibility report | `docs/15-compatibility-report.md` |

---

## 3. Producing a release build

These are the exact steps on a machine with JDK 17 and Android SDK 34. They
cannot be run in this environment (no SDK, no Gradle distribution, no network to
the Android/Google Maven repositories), which is why B2 is open.

```powershell
# once: create the signing key (keep it, never commit it, back it up)
keytool -genkeypair -v -keystore android\phonebridge-release.jks `
        -alias phonebridge -keyalg RSA -keysize 4096 -validity 10000

# android\keystore.properties  (git-ignored)
#   storeFile=phonebridge-release.jks
#   storePassword=<password>
#   keyAlias=phonebridge
#   keyPassword=<password>

cd android
.\gradlew.bat clean assembleRelease
# -> app\build\outputs\apk\release\app-release.apk        (signed)
#    app\build\outputs\apk\release\app-release-unsigned.apk (if the keystore is missing)

# verify what you produced before shipping it
& "$env:ANDROID_HOME\build-tools\34.0.0\apksigner.bat" verify --print-certs `
    app\build\outputs\apk\release\app-release.apk
& "$env:ANDROID_HOME\build-tools\34.0.0\aapt2.exe" dump badging `
    app\build\outputs\apk\release\app-release.apk | Select-String "versionName|versionCode|sdkVersion"

adb install -r app\build\outputs\apk\release\app-release.apk
```

Then run the device checklist in
[`docs/12-end-to-end-verification.md`](12-end-to-end-verification.md) §5 with the
**release** APK — not only the debug one. A release build that shrank differently
or was signed differently is a different artefact.

Build settings and why:

* **Minification is off** (`isMinifyEnabled = false`). R8 rules for Compose and a
  reflective WebSocket library cannot be validated without installing and
  exercising the APK; a shrunk app that crashes is worse than a larger one that
  runs. Revisit *with* a device test, not before.
* **No `isShrinkResources`** for the same reason.
* **No ABI splits / no native code** — the app is pure JVM bytecode.

---

## 4. Blockers

| ID | Blocker | Why it blocks | How to clear it |
| --- | --- | --- | --- |
| **B1** | **No physical Android device has run this code** | The whole Android side (UI, service lifecycle, permissions, reconnection, battery behaviour) is verified only by source analysis and a stub compile. A public release cannot claim it works on a phone without that. | Install the APK on a handset and execute `docs/12` §5 (about 20 minutes). Paste the output into `docs/09`/`docs/12`; the "device run outstanding" markers can then be removed. |
| **B2** | **The release APK was not built here** | No Android SDK, no Gradle distribution, and the Android/Google Maven repositories are not reachable from this environment. | Run §3 above on a machine with JDK 17 + SDK 34. |
| **B3** | **`targetSdk` is 34; Google Play requires 36 for new uploads (as of Aug 2026)** | Play will reject the upload. Sideloading is unaffected. | Bump `compileSdk`/`targetSdk` to 36 with AGP 8.13+, Kotlin 2.x + the Compose compiler Gradle plugin, and a Compose BOM ≥ 2025.06; then re-run §3 and §2.2 (edge-to-edge is already handled). Do this as its own change with a device test, not mixed into the release commit. |
| **B4** | **PowerShell workflows were not executed on Windows** | This environment is Linux with no `pwsh` and no Windows host. The Python behaviour underneath every documented PowerShell command *was* executed; the shell syntax itself was reviewed only. | Run `docs/11` §2–§4 and `docs/05` in PowerShell 7 and 5.1 on Windows. |
| **B5** | **No release keystore, no store listing assets** | Signing material must be generated and kept by the maintainer; Play also needs a 512×512 icon, screenshots and a privacy policy URL. | §3 (keystore) + store listing work. |
| **B6** | **Kotlin is compile-checked only against a stub environment** | Without the Android SDK, `kotlinc` reports 1,263 diagnostics, all of them missing-SDK cascades (`unresolved reference: Context`, …). The differential against the pre-release baseline shows **no new class of diagnostic**, but that is weaker than a real build. | Covered by **B2**. |

Non-blocking, tracked for the next phase:

* `docs/09-compat-report.md` — all three handsets still `unmeasured`; Phase 2
  (audio relay) does not start until the primary device is measured.
* `grep`-able hardcoded UI strings instead of `strings.xml` entries — fine for an
  English-only release, a chore for translation. Not a correctness issue.
* The launcher icon is a single vector drawable, not an adaptive icon set.

---

## 5. Release sequence (once B1 and B2 are clear)

1. `git tag -a v1.0.0 -m "PhoneBridge 1.0.0"` on the release commit.
2. Move the "known limitations" list into the GitHub release notes verbatim.
3. Attach: `app-release.apk`, its SHA-256, and `docs/12` §5 output.
4. Publish the Python package (`python -m build && twine upload dist/*`) if
   distributing that way; otherwise document the `pip install .` path only.
5. After release: bump `versionCode` to 2 and `versionName` to `1.0.1` on the next
   commit, so no two builds ever share a version code.

## 6. Rollback

An APK cannot be "un-released": keep the previous signed APK and its keystore
backup for at least one version. For the PC side, `pip install phonebridge==0.9.x`
(if published) or a git tag checkout restores the previous client; the config
directory format is unchanged within 1.x, so no data migration is needed.
