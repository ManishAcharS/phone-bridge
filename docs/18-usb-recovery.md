# 18. USB recovery: why the phone never shows the RSA prompt

The reported symptom, in full:

> USB debugging is enabled on the Redmi Note 14 Pro+ 5G. The PhoneBridge app is
> open. `adb devices -l` prints an empty list. The phone never shows *"Allow USB
> debugging?"*, and PhoneBridge says no device is connected over USB.

Every part of that is consistent with one thing: **the phone was never asked.**
The RSA prompt is the *last* link in a four-link chain, and nothing in the
earlier three links was happening.

---

## 1. The chain, and what each link looks like on the wire

| Stage | Link | Who has to do it | What proves it happened |
| --- | --- | --- | --- |
| **A** | Windows enumerates the physical phone | the cable, the port, the phone's USB stack | a USB device node appears (any vendor id) |
| **B** | the phone exposes its **ADB interface** | *Developer options → USB debugging* being ON, and a data-capable USB mode | a second interface node (or a composite parent with an ADB child) exists |
| **C** | Windows binds a driver to that interface | the Windows driver | the node is healthy: no Code 28/10/43/52 |
| **D** | the adb server opens the device | `adb` | `adb devices -l` lists a serial with state `device`/`unauthorized` |
| **E** | the phone authorises this PC | the user, on the phone | the *"Allow USB debugging?"* dialog |

The prompt lives in **E**. It cannot appear before **B** and **C** exist, because
the request that triggers it travels over the ADB interface. A phone that shows
MTP (file transfer) but no ADB interface will never display the prompt, no matter
how many times the cable is replugged — and that is the most common HyperOS case.

This is why the old message ("plug in the cable and accept the debugging prompt")
was useless here: it named step E while the failure was at step B.

---

## 2. What PhoneBridge now reports

`controller/usbcheck.py` builds a `UsbReport` from two independent sources —
`adb` and Windows' own device tree — and prints where the chain stopped:

```
PhoneBridge USB report
======================
  system           : Windows
  adb              : C:\Users\2026\AppData\Local\Android\Sdk\platform-tools\adb.exe
  adb version      : Android Debug Bridge version 1.0.41
  adb server       : running
  adb devices      : none
  phone in Windows : Redmi Note 14 Pro+
  MTP interface    : yes
  ADB interface    : NO
  driver status    : OK
  stops at         : stage B
  decision         : Windows sees the phone as a media device (MTP) but exposes no
                     ADB interface - this is why the RSA prompt never appears
  next action      : Windows sees the phone but not its ADB interface, so no
                     debugging request can reach it: ...
  evidence:
    - Redmi Note 14 Pro+ (Windows: OK)
```

Three ways to get it:

| Where | How |
| --- | --- |
| GUI | the **Diagnose USB** button (re-runs everything, bypassing the 30 s cache) |
| GUI rows | **USB stops at**, **USB diagnosis**, **USB next step** |
| Terminal | `phonebridge usb`, or `phonebridge usb --json` for scripts |

`phonebridge usb` is read-only. It does not kill the adb server, delete
`adbkey`, install a driver, or change a Windows setting.

### The stages, one by one

| Stage | Kind | Meaning | The one action |
| --- | --- | --- | --- |
| A | `no-device` | nothing Android-shaped reached Windows | cable that shipped with the phone, a PC port rather than a hub/monitor, unlock the phone |
| B | `no-adb-interface` | the phone is enumerated as MTP, no ADB interface | *Developer options → USB debugging ON*, USB configuration *File transfer*, replug |
| C | `driver` | the interface exists, Windows cannot drive it | `Code 28` → install the phone's USB driver; `Code 43`/`10` → another cable/port first |
| D | `adb-server` | adb cannot reach its own daemon | `adb kill-server` then `adb start-server` (keys are kept, so authorisation survives) |
| E | `unauthorized` | adb has it, this PC is not approved | accept *"Allow USB debugging?"* on the phone, then Reconnect |
| F | `offline` | adb has it, it does not answer | replug, or `adb kill-server` |
| G | `wireless-only` | only a wireless-debugging device is attached | that is not a cable — plug one in |
| H | `debugging-off` | enumerated and healthy, adb blind | check USB debugging and the USB mode; if no prompt ever appears the blocker is B, not authorisation |

Two honesty rules, enforced by tests: the device tree is only read on **Windows**
(elsewhere the report says *not inspected*), and `driver status` is *not
inspected* rather than *OK* when nothing was read.

---

## 3. Xiaomi / HyperOS specifics, with the reason for each

Only settings that change USB enumeration are listed. Nothing here needs root, a
security bypass, or an unsigned driver.

| Setting | Where | What it changes | Why it matters here |
| --- | --- | --- | --- |
| **Developer options** | Settings → About phone → tap *OS version* 7 times | unlocks the menu | without it there is no USB debugging switch at all |
| **USB debugging** | Settings → Additional settings → Developer options | the phone advertises its **ADB interface** | this is stage B; with it off the phone is MTP-only and the prompt cannot appear |
| **USB debugging (Security settings)** | same screen (HyperOS/MIUI only) | allows adb to inject input and change some secure settings | not required for PhoneBridge's transport; it is only relevant if a command fails with a security error |
| **Default USB configuration** | same screen | what the port does before you choose | set it to *File transfer* so a replug lands in a mode that exposes interfaces |
| **USB mode (notification)** | tap the USB notification after plugging | MTP / PTP / charging only | *Charging only* exposes **no interface at all** except power — a classic stage B |
| **"Install via USB"** (HyperOS) | Developer options | permits adb install | only needed for `adb install`; it does not affect the transport |
| **MIUI/HyperOS optimisation / "USB debugging (security)"** toggles being off | — | nothing | do not change these for a transport problem; they are unrelated |

**The single most common cause on this hardware**: the USB mode is *Charging
only* (or the cable is charge-only), so Windows sees power and no interfaces.
Second most common: a vendor driver never bound to the ADB interface (`Code 28`),
which the report names explicitly.

Windows drivers: the phone works with the generic Microsoft drivers when it
presents a WinUSB-class ADB interface. Xiaomi's own Mi USB driver is only needed
for old ADB/`fastboot`-style interfaces, and installing it is a user decision —
PhoneBridge never installs a driver.

---

## 4. What was verified, and what was not

| Statement | Status |
| --- | --- |
| Every stage above is produced by unit tests with injected Windows/adb data | **verified** — `windows/tests/test_controller_usbdiag.py` (29 tests) |
| Malformed and localized PowerShell output cannot crash the report | **verified** — `test_junk_from_powershell_is_an_empty_tree_not_a_crash`, `test_localized_status_text_still_yields_the_problem_code` |
| The GUI button and the CLI command exist and run | **verified** — CLI smoke in `tools/verify.py`; the button calls a tested path, but the window itself cannot be rendered in this environment |
| The Redmi reaches `ready` over USB on your PC | **not verified** — needs the hardware. Run `phonebridge usb` and read the stage |

Until that last row is run on the real phone, this document is a diagnosis tool,
not a claim that USB works.

---

## 5. Exact commands

```powershell
cd C:\projects\phone-bridge
git pull

# the full chain, read-only
.\.venv\Scripts\python.exe -m phonebridge usb

# or the GUI: double-click PhoneBridge-Start.bat, then press "Diagnose USB"
```

Repeat after each change; the cache is bypassed on every press, so the stage
letter moving from B to E is exactly the progress you are looking for. B → E is a
success: the phone has now been asked and is waiting for your answer.
