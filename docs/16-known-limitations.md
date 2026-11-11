# 16. Known limitations

Everything PhoneBridge does not do, cannot do, or has not proven. Grouped by
kind, with the reason and the workaround where one exists. If a line here can be
removed, it should be removed *with evidence*, not because it became
inconvenient.

---

## 1. Features that do not exist yet

| Limitation | Why | When |
| --- | --- | --- |
| **No audio relay.** The phone measures its audio routing and reports it; it does not carry sound between PC and headset. | Phase 2, and it is gated on measurement: whether off-call SCO works is a ROM policy per handset, not a radio feature. Building it before measuring would be a guess. | Phase 2, after the primary handset is measured (`docs/09`) |
| **No camera, sensors, file transfer or Bluetooth scanning.** | Protocol v1 reserves message kinds `0x40`, `0x60`, `0x50`, `0x30` and implements none of them. The capability report says `false` for all of them instead of shipping dead UI. | Phases 4–5 |
| **No desktop GUI.** | The PC client is a CLI. A GUI is Phase 3. | Phase 3 |
| **No Windows service / autostart.** | The CLI runs when you run it. | not planned for 1.x |

## 2. Hardware and OS realities (not bugs)

| Limitation | Detail | Workaround |
| --- | --- | --- |
| **PC audio and headset mic cannot both be high quality** | A Bluetooth headset renders one profile at a time: A2DP (stereo output, no mic) or HFP/SCO (mono, bidirectional, call quality). | Choose the mode you need; both will be labelled explicitly once the relay exists. |
| **Windows will never list your headphones as a PhoneBridge device** | Android's A2DP *sink* is not in the public SDK; PhoneBridge is a relay, not an adapter emulator. | Virtual audio cable if the PC must see a device. |
| **Latency of 70–150 ms one-way** (expected, once the relay exists) | Bluetooth radio + Android audio path. | Fine for calls, Discord, OBS monitoring; not for musical monitoring. |
| **Some ROMs declare off-call SCO and deliver silence** | OEM policy. The capability probe exists specifically to catch this. | Run the probe; the report records `SCO_DECLARED_BUT_UNUSABLE`. |
| **Phone must be on the same network as the PC** | No relay server, by design. | USB tethering or the phone's hotspot creates that network. |
| **AP client isolation breaks Wi-Fi connections** | The access point drops peer-to-peer traffic. | Hotspot or USB tethering. |
| **OEM battery optimisation can kill the bridge service** | Android Doze / vendor "cleaners". | Allow PhoneBridge to run unrestricted in the app's battery settings. |

## 3. Trust and security

| Limitation | Detail | Mitigation |
| --- | --- | --- |
| **Trust on first use** | The six-digit code is the only defence against an active attacker on the *first* connection. `--auto-pair` skips even that and is for scripts only. | Compare the code; never approve a code you did not expect. |
| **Identity keys are stored unencrypted** | Mode 0600 on the PC, app-private storage on the phone. No Keystore, no passphrase. | Full-disk encryption; do not run on a shared account. |
| **Any paired PC can call every method** | Authorization is binary: paired ⇒ everything, including the microphone-touching capability probe. No per-method scopes. | Pair only machines you control; "Forget all paired PCs" revokes everything at once. |
| **No audit trail** | The phone keeps a 200-line in-memory log; nothing is persisted. | Watch the app's Log card while connected. |
| **The LAN is trusted for reachability, not confidentiality** | The listener binds `0.0.0.0` and the beacon advertises the phone. Content is protected by the P-256/AES-GCM layer inside the WebSocket; the socket itself is plaintext (no TLS). | Firewall the machine; the app-layer crypto is what protects content. |
| **Pairing prompts are a spam target** | A LAN attacker can provoke up to 4 concurrent prompts (they expire, and browser origins are refused). | Do not approve a prompt you did not initiate. |

Full analysis with severities: [`13-security-review.md`](13-security-review.md).

## 4. Platform and build

| Limitation | Detail | Next step |
| --- | --- | --- |
| **`targetSdk` is 34; Google Play requires 36** for new uploads from Aug 2026 | Sideloading works; Play submission is blocked. | Blocker B3 in [`14-release-readiness.md`](14-release-readiness.md) |
| **Android app not verified on a physical handset by this project** | No device was available to the automated checks. | Blocker B1; the 20-minute checklist is `docs/12` §5 |
| **Release APK must be built by the user** | No SDK in this environment. | `docs/14` §3 |
| **Minification (R8) is disabled** | Cannot be validated without installing and exercising the APK. | Re-enable with a device test |
| **Windows is the supported PC target but was not the build environment** | The release was prepared on Linux. | Blocker B4 |
| **No MSI/installer** | `pip install .` from a checkout, or a published wheel. | Optional packaging work |
| **No Python package signing / reproducibility guarantees** | A wheel built from a checkout is unsigned. | Publish with `twine` + hashes if distributing |
| **`pwsh` 7 recommended; PowerShell 5.1 works with the documented quoting** | Some commands need backtick line continuations quoted differently. | `docs/11` §2 |
| **UI strings are hardcoded in Compose, not in `strings.xml`** | English only. | Translation pass, not a correctness issue |
| Unsaved text typed into the port field is dropped on rotation | The field re-reads the saved port when the activity is recreated. Saved settings are never lost. |
| **Launcher icon is a single vector drawable** | No adaptive icon set (masking/shape on modern launchers) | Design pass |
| **Port 8765 by default, configurable 1024–65535** | Ports below 1024 are refused by Android. | Change it in the app's Settings; remember to update `adb forward` |

## 5. Operational

| Limitation | Detail |
| --- | --- |
| One phone at a time from the CLI | The reference phone and the app support up to 8 sessions (4 per PC), but the CLI holds one session per command. |
| No multi-device dashboard | `capabilities`/`compat` are per-connection. |
| `watch` reconnects forever by default | `--max-attempts` bounds it; terminal errors (changed key, version skew, refusal) stop immediately. |
| The pairing prompt needs an interactive terminal | By design: a human must compare the code. Scripts should use `--auto-pair` knowingly. |
| The bridge does not start itself after a reboot | No `BOOT_COMPLETED` receiver: nothing runs in the background until the app is opened. This is deliberate, and it means a rebooted phone needs one tap. |
| Config directory is per-user, not portable | `%LOCALAPPDATA%\PhoneBridge` (or `--config-dir`). Copying it copies your identity key. |

## 6. Verification gaps

| Gap | Consequence |
| --- | --- |
| No physical Android device | Everything about the app on a real phone: lifecycle, Doze, permissions UX, recovery after a network change, dark mode, rotation, TalkBack. |
| No Windows host | PowerShell scripts, Windows Firewall prompts, `%LOCALAPPDATA%` behaviour, `adb` on Windows. |
| No Bluetooth headset attached | Audio measurements on all three handsets (`docs/09`). |
| No fuzzing | The parsers are size- and shape-limited and well tested, but not fuzzed. |
| Single-machine end-to-end | Every end-to-end test uses loopback; real Wi-Fi loss, MTU issues and roaming are untested. |

## USB diagnosis (milestone 3)

| Limitation | Why, and what would remove it |
| --- | --- |
| The USB verdict is a Windows-only conclusion | it reads `Get-PnpDevice`; on Linux/macOS the row says *not inspected* rather than inventing an answer |
| Problem codes are mapped, not fixed | `Code 28` means no driver is installed; the app will not install one silently, and installing a kernel driver without the user's knowledge is out of scope by instruction |
| Android Open Accessory was investigated, not implemented | AOA needs the phone to expose accessory mode and the PC to claim the interface with a USB driver; it would only help where adb cannot, and it cannot be validated without the handset. It is not wired in, and the app does not claim it |
| `adb devices -l` can be stale | a phone that was unplugged may linger as `offline` until `adb kill-server`; the window says so instead of reporting a healthy cable |
| The descriptor names are shown, not interpreted | a cable that only charges usually produces *no Android row at all*; the window can say Windows never saw the phone, but it cannot distinguish a dead cable from a dead port |
