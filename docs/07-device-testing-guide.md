# PhoneBridge — Android device testing guide

Everything you need to go from a fresh clone to a phone that answers its PC.
Written for **you** on Windows with a phone on Android 8.0+.

Nothing in this guide has been executed on real hardware by me: the environment I
work in has no JDK, no Android SDK and no phone. Every version pairing and
permission requirement below is either checked by `python tools/verify.py` or
cross-checked against the platform documentation. Where a step can fail for a
reason I cannot reproduce, §8 tells you what to collect.

---

## 0. Fast path (if you already have Android Studio and the SDK)

```powershell
git clone https://github.com/ManishAcharS/phone-bridge.git
cd phone-bridge\android
.\gradlew.bat :app:assembleDebug                 # ~3-6 min the first time
adb install -r app\build\outputs\apk\debug\app-debug.apk
adb shell am start -n dev.phonebridge/.MainActivity
```

Then: flip **Bridge** on, run the **protocol self-test** (expect 8/8), and from
the repo root:

```powershell
cd ..\windows
python -m venv .venv; .venv\Scripts\activate
pip install -e .
phonebridge info --host <phone-ip>
```

If anything deviates from that, work through §1–§8 in order.

Before you start, one command tells you whether the repository itself is sound:

```powershell
python tools\verify.py
```

It runs the test suite, lint, the vector-drift check, the wrapper check and a
real end-to-end CLI handshake against the simulator. If that is green, every
remaining problem is environment or device — not code.

---

## 1. Open the Android project in Android Studio

### Requirements

| Item | Requirement | Why |
| --- | --- | --- |
| Android Studio | **Koala 2024.1.1 or newer** (Ladybug 2024.2.x recommended) | AGP 8.5.2 ships with Koala; older Studio refuses to sync |
| JDK | 17 (bundled) — 17 to 21 also works for the CLI | the project targets Java/Kotlin 17 |
| Disk | ~3 GB | Gradle distribution, SDK platform, dependencies |
| Internet | **first sync only** | Gradle 8.7 + Maven artifacts. Runtime needs none |

### Steps

1. **File → Open** and select the `phone-bridge\android` folder.

   > Select `android`, **not** the repository root. The root has no
   > `settings.gradle.kts`, so Studio would import nothing and you would get an
   > empty project.

2. Let the **Gradle sync** finish. First sync downloads Gradle 8.7 (~130 MB) plus
   AGP, Kotlin 1.9.24 and the AndroidX/Compose artifacts.

   The repository now ships the Gradle wrapper (`gradlew`, `gradlew.bat`,
   `gradle/wrapper/gradle-wrapper.jar`), so Studio does **not** need to generate
   anything. If Studio offers to *upgrade* the wrapper or AGP, **decline** — the
   version set is deliberately pinned (see `docs/06-phase1-audit.md` §4).

3. Confirm the sync result: the **Build** panel says `BUILD SUCCESSFUL` /
   *"Gradle sync finished"*. The project tree should show one module, `app`.

4. `android/local.properties` is created by Studio (it holds your absolute SDK
   path) and is git-ignored. Never commit it.

### If the sync fails

| Message | Meaning | Fix |
| --- | --- | --- |
| `Plugin [id: 'com.android.application', version: '8.5.2'] was not found` | Studio cannot reach Google's Maven | check proxy/VPN, retry sync |
| `Minimum supported Gradle version is 8.7` | wrapper file edited or replaced | `git checkout android/gradle/wrapper` |
| `The project is using an incompatible version of AGP` | Studio too old | update Studio, or let it use the bundled JDK 17 |
| `Unsupported class file major version 65` | Studio running on JDK 21 with an older Gradle | upgrade wrapper to 8.7 (already pinned) or point Studio at JDK 17 |
| `SDK location not found` | no `local.properties`, `ANDROID_HOME` unset | §2 |

---

## 2. Install the required SDK components

### Through the GUI (recommended)

**Tools → SDK Manager**:

* **SDK Platforms** tab: tick **Android 14.0 ("UpsideDownCake") — API 34**.
* **SDK Tools** tab: tick **Android SDK Build-Tools 34**, **Android SDK
  Platform-Tools**, and **Android SDK Command-line Tools (latest)**.
  (An emulator image is optional — this app needs real Bluetooth and network
  interfaces, so an emulator is of limited use.)

Then **Apply**. Accept licences when prompted.

### Through the command line

```powershell
# sdkmanager lives in  %LOCALAPPDATA%\Android\Sdk\cmdline-tools\latest\bin
sdkmanager --list_installed
sdkmanager "platforms;android-34" "build-tools;34.0.0" "platform-tools"
sdkmanager --licenses
```

### Exact versions this project expects

| Component | Version | Notes |
| --- | --- | --- |
| SDK Platform | **android-34** | `compileSdk = 34`, `targetSdk = 34` |
| Build-Tools | **34.0.0** | newer build-tools are fine |
| Platform-Tools (adb) | any recent | needed to install and to read logs |
| minimum device | **API 26** (Android 8.0) | `minSdk = 26` |

Deliberately **not** required: NDK, CMake, API 35. The project avoids the
compileSdk-35 requirement by pinning Compose BOM `2024.06.00`; do not bump the
BOM alone or the build will demand API 35 and AGP ≥ 8.6.

### JDK

* From Android Studio: nothing to do, it uses the bundled runtime.
* For a command-line build, install JDK 17 and point `JAVA_HOME` at it:

  ```powershell
  winget install Microsoft.OpenJDK.17
  # new terminal, then:
  java -version          # expect 17.x
  "JAVA_HOME=$env:JAVA_HOME"
  ```

If you have several JDKs, `android/gradle.properties` can pin one for this
project only (do not commit a machine-specific path):

```properties
# android/gradle.properties  (local, do not commit)
org.gradle.java.home=C\:\\Program Files\\Microsoft\\jdk-17.0.11.9-hotspot
```

### Tell Gradle where the SDK is (CLI builds only)

Studio writes `android/local.properties` for you. For a pure CLI build:

```properties
# android/local.properties
sdk.dir=C\:\\Users\\<you>\\AppData\\Local\\Android\\Sdk
```

(Or set the `ANDROID_HOME` environment variable to the same directory.)

---

## 3. Build the debug APK

### Android Studio

**Build → Build App Bundle(s) / APK(s) → Build APK(s)**. When it finishes, click
**locate** in the notification. The APK is at:

```
android\app\build\outputs\apk\debug\app-debug.apk
```

### Command line

```powershell
cd phone-bridge\android
.\gradlew.bat :app:assembleDebug
```

Expected tail:

```
BUILD SUCCESSFUL in 2m 41s
42 actionable tasks: 42 executed
```

Then confirm the artifact exists:

```powershell
Get-Item app\build\outputs\apk\debug\app-debug.apk | Select-Object Name,Length,LastWriteTime
```

Useful variants:

```powershell
.\gradlew.bat :app:assembleDebug --stacktrace        # full error trace
.\gradlew.bat :app:lintDebug                         # Android Lint (HTML report)
.\gradlew.bat clean :app:assembleDebug               # from scratch
start app\build\reports\lint-results-debug.html      # lint findings
```

### Warnings you can ignore

* `'Divider(...)' is deprecated` — material3 1.2.1, will move to `HorizontalDivider` when the BOM moves.
* `getPackageInfo(String, Int)` deprecation — API 33 overload exists, current form still works on minSdk 26.
* `InlinedApi` for `TYPE_BLE_HEADSET` / `VERSION_CODES.TIRAMISU` — deliberately guarded by `SDK_INT` checks; the constants are inlined at compile time.

### Errors that are *not* environment problems

| Error | Cause | Fix |
| --- | --- | --- |
| `checkDebugAarMetadata ... requires compileSdk 35` | Compose BOM was bumped | restore BOM `2024.06.00` or move compileSdk+AGP together |
| `Unresolved reference: BridgeState` | a source file got truncated by a bad merge | `git status`, restore the file |
| `error: cannot find symbol BuildConfig` | buildConfig disabled and code uses it | it is not used in Phase 1 |
| `Execution failed for task ':app:processDebugManifest'` | manifest edited by hand | `git diff android/app/src/main/AndroidManifest.xml` |
| `SDK location not found` | §2 | create `local.properties` |

Report the exact task name and the first `Caused by:` line if you hit anything
else.

---

## 4. Install the APK on the phone

### Option A — adb (preferred; you will want adb anyway)

1. **Settings → About phone → tap "Build number" 7×** → developer options unlock.
2. **Settings → System → Developer options → USB debugging: on**.
3. Plug in USB, tap **Allow** on the "Allow USB debugging?" prompt.

   ```powershell
   adb devices
   # R58M12ABCDE   device      <- "unauthorized" means the prompt was not accepted
   ```

4. Install and launch:

   ```powershell
   adb install -r android\app\build\outputs\apk\debug\app-debug.apk
   adb shell am start -n dev.phonebridge/.MainActivity
   adb shell pm list packages | findstr phonebridge
   ```

### Option B — copy the file

Copy `app-debug.apk` to the phone (USB file transfer, Google Drive, etc.), tap it
in the Files app, and allow "install unknown apps" for that file manager.

### OEM gates that block a debug install

| Phone | Symptom | Fix |
| --- | --- | --- |
| Xiaomi / Redmi / POCO (MIUI, HyperOS) | `INSTALL_FAILED_USER_RESTRICTED` | Developer options → enable **Install via USB**; turn off **MIUI optimisation**; be logged into a Mi account (MIUI requires it for USB installs) |
| Realme / OPPO / OnePlus (ColorOS) | "Installation blocked" | allow **Install via USB** + disable **Verify apps over USB** (Developer options) |
| Samsung | fine | if not, disable **Auto Blocker** in Settings → Security and privacy |
| Anything with "Verify apps" / Play Protect | `INSTALL_FAILED_VERIFICATION_FAILURE` | temporarily disable Play Protect scanning for the install |

The debug APK is signed with the local debug keystore. That is normal for
personal use; it is not a Play-Store-ready build and does not need to be.

### First launch

* Allow the notification permission (Android 13+) — without it the bridge still
  runs, but you will not see its persistent notification.
* The dashboard shows **device id**, **listening address** (`0.0.0.0:8765`), a
  **Bridge** switch, the pairing card when a PC asks, and a **log**.

---

## 5. Run the protocol self-test

1. Open the app and press **Run protocol self-test**.
2. Expect **8 checks** to pass:

| Check | Proves |
| --- | --- |
| identity key load + public encoding | P-256 key format matches the PC's |
| ECDH shared secret (X coordinate, 32-byte BE) | key agreement is byte-identical |
| HKDF-SHA256 expand (c2s / s2c) | keys, salt and info strings match |
| pairing SAS derivation | the 6 digits the user compares are derived identically |
| AES-256-GCM frame seal | nonce + AAD layout matches |
| AES-256-GCM frame open + replay rejection | decryption works and replays are refused |
| framing round-trip | envelope codec matches |
| audio routing probe | the platform's current Bluetooth audio state (informational) |

**Passing means the phone's crypto is interchangeable with the PC's.** If it
passes and a connection still fails, the problem is networking or permissions —
never cryptography. If it fails, stop: no amount of connection debugging helps.

### If a check fails

```powershell
adb logcat -s BridgeServer:V BridgeService:V
```

Send me: which check(s) failed, the `detail` string the app printed, and the
logcat lines. The self-test is a known-answer test against
`ProtocolVectors.kt`, which is generated from the Python reference by
`tools/vectorgen/gen_vectors.py` and verified in CI-style by
`windows/tests/test_android_conformance.py`.

---

## 6. Get the phone's local IP address

### From the phone UI

**Settings → Network & internet → Wi-Fi → (the connected network) → IP address**
→ e.g. `192.168.1.42`.

The app's own dashboard also prints the address it is listening on, and its log
card shows the active interface.

### From adb (exact, no guessing)

```powershell
adb shell ip -4 addr show wlan0          # Wi-Fi
adb shell ip route                        # default route -> gateway
```

Filter for just the address:

```powershell
adb shell "ip -4 addr show wlan0 | grep inet"
#    inet 192.168.1.42/24 brd 192.168.1.255 scope global wlan0
```

### Which address, depending on how you connect

| Connection | Phone's address | How to find it |
| --- | --- | --- |
| **Wi-Fi (same LAN)** | `192.168.x.y` on your subnet | §6 above; must match your PC's first three octets |
| **USB + adb forward** | not needed — use `127.0.0.1` | `adb forward tcp:8765 tcp:8765`, then `--host 127.0.0.1` |
| **USB tethering** | gateway of the RNDIS link, usually `192.168.42.129` | `ipconfig` on the PC → new adapter's *Default Gateway* |
| **Phone hotspot** | the PC's default gateway on the hotspot adapter | `Get-NetRoute -DestinationPrefix 0.0.0.0/0`; stock Android uses `192.168.43.1` |

Sanity check from the PC before blaming the app:

```powershell
Test-NetConnection 192.168.1.42 -Port 8765
# TcpTestSucceeded : True
```

If that is `False` while the app shows *listening*, the network or firewall is
blocking you, not PhoneBridge (§8).

---

## 7. Connect the Windows CLI to the phone

### One-time Python setup

```powershell
cd phone-bridge\windows
python -m venv .venv
.venv\Scripts\activate
pip install -e .
python -m pytest -q                 # 122 tests, should pass
```

Python **3.10+** is required. `phonebridge` puts a `phonebridge` command on PATH;
if that is missing, use `python -m phonebridge` — identical.

### Connect

```powershell
# 1. find the phone automatically (needs the app running; beacon is UDP 47823)
phonebridge discover --timeout 10

# 2. or connect directly
phonebridge info --host 192.168.1.42

# 3. verify the link
phonebridge ping  --host 192.168.1.42 --count 20
phonebridge bench --host 192.168.1.42 --frames 3000 --size 1024
```

### First connection: pairing

```
  ┌─────────────────────────────────────────────────────────┐
  │  PAIRING CODE:  123 456                                 │
  └─────────────────────────────────────────────────────────┘
  Codes match? [y/N]
```

Compare it with the card on the phone screen and press **"Codes match —
allow"** there as well. Both sides must confirm; the digits must be identical.
Afterwards both sides pin each other's key and later connections ask for
nothing.

Use `--auto-pair` **only** with the simulator, or when you are on a network you
fully trust and are re-pairing with your own device.

### Expected result

```json
{
  "protocol_version": 1,
  "device_id": "13000791ea89b7e7",
  "name": "Pixel 7",
  "sdk_int": 34,
  "android_version": "14",
  "capabilities": { "audio.capture": true, "camera.back": true, "...": "..." }
}
```

`capabilities.audio.bluetooth_sco` is `true` only while a Bluetooth headset is
connected to the phone — that flag is the Phase 2 go/no-go signal.

### Windows Firewall

The first time `python.exe` opens a socket, Windows asks. Allow it on **Private
networks** only. If you dismissed it:

```powershell
# as administrator - only needed for discovery (inbound UDP 47823)
New-NetFirewallRule -DisplayName "PhoneBridge discovery" -Direction Inbound `
  -Program "$(Get-Command python).Source" -Protocol UDP -LocalPort 47823 -Profile Private
```

Direct `--host` connections are outbound and usually need no rule.

---

## 8. Diagnose connection failures

Work down this list — each step isolates one layer.

```powershell
python tools\verify.py            # repo sound? tests + lint + vectors + CLI smoke
phonebridge doctor                # this PC: python, deps, network interfaces
adb logcat -s BridgeServer:V DiscoveryBeacon:W   # phone side, live
```

| # | Check | Command | If it fails |
| --- | --- | --- | --- |
| 1 | App running and bridge on? | look at the phone | flip **Bridge** on; the notification must be present |
| 2 | Is the port actually bound? | `Test-NetConnection <ip> -Port 8765` | firewall, wrong IP, or the bind failed (the app's log shows it) |
| 3 | Right IP? | `adb shell "ip -4 addr show wlan0 \| grep inet"` | re-read §6; USB tethering and hotspot use different addresses |
| 4 | Same subnet? | `ipconfig` | `192.168.1.x` vs `192.168.43.x` cannot talk — fix the network choice |
| 5 | Wi-Fi client isolation? | try the phone hotspot instead | guest/hotel/corporate APs often isolate clients; tethering bypasses it |
| 6 | adb path healthy? | `adb forward --list` | re-create the forward: `adb forward tcp:8765 tcp:8765` |
| 7 | Firewall? | `New-NetFirewallRule ...` above | allow python.exe on Private |
| 8 | Identity mismatch? | `phonebridge identity` | **app reinstalled?** the phone regenerated its key → `phonebridge forget <old-id>` then re-pair |
| 9 | Phone refuses this PC? | the app's log card | press **Forget all paired PCs** on the phone, then re-pair |

### CLI messages and what they mean

| Message | Meaning |
| --- | --- |
| `Connection refused` | nothing is listening: app stopped, wrong port, or bind failed |
| `timed out connecting` | packets dropped: firewall, wrong IP, or client isolation |
| `phone identity key does not match the pinned key` | the phone's identity changed (reinstall / data clear). Forget and re-pair |
| `phone refused authentication: identity key changed` | *your PC's* identity changed (new config dir or new machine). Forget the PC on the phone |
| `pairing did not complete` | nobody answered the code within ~90 s |
| `error: port already in use` | two PC instances, or a stale forward. Change the port on both ends (`--port`) |
| `pairing declined` | the phone pressed **Refuse**, or the codes differed |
| `bench: payload corruption detected` | a real bug — stop and send me the output |

### Collect this if you need help

1. `python tools\verify.py` output.
2. The self-test result (`8/8` or the failing check names + details).
3. `phonebridge -v info --host <ip>` output.
4. `adb logcat -s BridgeServer:V DiscoveryBeacon:W` (last ~50 lines around the attempt).
5. How you are connected (Wi-Fi / USB+adb / tethering / hotspot) and the two IP addresses.

---

## 9. Done checklist

The first real device test is complete when all of these are true:

- [ ] `python tools\verify.py` → all checks pass
- [ ] `.\gradlew.bat :app:assembleDebug` → `BUILD SUCCESSFUL`, APK present
- [ ] APK installs and launches
- [ ] Foreground service starts with no `SecurityException` in logcat
- [ ] **Self-test: 8/8**
- [ ] `phonebridge info` returns JSON over **Wi-Fi**
- [ ] …and over **USB + adb forward** (`--host 127.0.0.1`)
- [ ] SAS matched on the first pairing; the second connection asked for nothing
- [ ] `phonebridge bench` → zero payload corruption
- [ ] `phonebridge watch` reconnects unattended after you break the link
- [ ] `capabilities.audio.bluetooth_sco` flips to `true` with your headset connected

Items 1–6 are Phase 1's acceptance criteria. The last item is the Phase 2 entry
condition — and it is the single most useful thing this test run tells us about
Bluetooth audio, because it decides whether duplex HFP is available on your
handset before a line of audio code is written.
