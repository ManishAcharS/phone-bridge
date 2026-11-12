# PhoneBridge virtual camera — Media Foundation proof of concept

**Status: written, reviewed, never compiled.** The authoring environment is Linux
with no MSVC, no Windows SDK and no Media Foundation headers, so nothing in this
directory has been built, loaded or enumerated. Its purpose is to answer one
question on *your* machine before any of PhoneBridge's own code is committed to
this design:

> Does a Media Foundation software camera, backed by our own COM media source,
> appear in the Windows Camera app and deliver frames?

There is **no phone, no Python, no protocol and no network** in this directory. It
draws a 1280×720 test pattern. If the picture appears and the bar moves, the
device plumbing works and the remaining work is *where the pixels come from* —
which is the part based on code that already exists. If it does not appear, the
failure is in this ~700 lines, not in the bridge.

Nothing here installs a driver, writes to `Program Files`, changes a security
setting, or needs an administrator password. See *Removal* and *What this does
not do*.

---

## 1. What it is, mechanically

Windows 11 can host a "software camera": Media Foundation will enumerate and
serve a camera whose frames come from an ordinary COM object, as long as the
object is registered and the camera host is told about it.

```
  Windows Camera app / OBS / browser
            │  (standard Media Foundation camera path — the apps need no changes)
            ▼
  Frame Server  ──loads by CLSID──►  PhoneBridgeVcamSource.dll
            │                          │  DllGetClassObject → ClassFactory
            │                          │      → MediaSourceActivate (IMFActivate)
            │                          │      → MediaSource (IMFMediaSource
            │                          │        + IMFMediaEventGenerator
            │                          │        + IMFMediaStream)
            ▼                          ▼
     your screen  ◄── 1280×720@30 NV12 frames, generated per RequestSample
```

`vcam_host.exe` is the other half: it registers the CLSID for the current user,
calls `MFCreateVirtualCamera`, starts the camera, waits, and on exit stops it,
removes it and deletes the registry key.

### Files

| File | Role |
| --- | --- |
| `src/media_source.h` / `.cpp` | The media source: one stream, pull-driven, NV12 test pattern with a moving bar. |
| `src/media_source_activate.h` / `.cpp` | `IMFActivate` + `IMFAttributes` implementation, `IClassFactory`, `DllGetClassObject`, `DllCanUnloadNow`, `DllMain`. |
| `src/vcam.def` | Exports. |
| `src/vcam_host.cpp` | Registrar + lifetime harness (the part PhoneBridge's service eventually takes over). |
| `CMakeLists.txt` | Build. |

The CLSID is `{7F1B3C1E-9D62-4C2B-9E7A-5B4C0D2A6F31}`. It is a POC value; the
real implementation gets a permanent one and must never reuse this one, because
two DLLs answering the same CLSID is a debugging trap.

---

## 2. Check the machine, then build

### Step 0 — what is already installed (read only, no admin, changes nothing)

```bat
windows-native\vcam-poc\tools\run-check.bat
```

It reports the OS build, whether `mfsensorgroup.dll` actually exports
`MFCreateVirtualCamera`, the installed Visual Studio C++ toolset, every installed
Windows SDK and which of them ships `mfvirtualcamera.h`, and the CMake version —
then lists exactly what is missing. It registers nothing,
writes nothing and needs no elevation. The `-ExecutionPolicy Bypass` in the
wrapper applies to that one PowerShell process, not to the machine's policy.

Run this *before* the build, and send back its output if anything is `[missing]`:
it distinguishes "the SDK is there but the header is not" from "no C++ tools",
which are different fixes.

### Toolchain, exactly

* **Windows 11, build 22000 or later.** `MFCreateVirtualCamera` documents
  Windows Build 22000 as its minimum. (Windows 10 is not a target: the API simply
  is not there, and `--check` says so.) 22621 is only required for the *physical
  camera source* API, which this does not use.
* **Visual Studio 2022** with the **"Desktop development with C++"** workload —
  MSVC v143 x64 compiler, Windows SDK, CMake.
* **Windows 11 SDK 10.0.22000.0 or later** — this is what provides
  `mfvirtualcamera.h`. The VS 2022 installer may install 10.0.22621 by default;
  any 22000+ SDK includes the header.
* **CMake 3.20 or later** (the VS installer ships one).
* No Python, no driver, no admin rights, no Windows Developer Mode, no test
  signing, no disabled security feature.

### Commands

From the repository root, in a **x64 Native Tools Command Prompt for VS 2022**
(or any shell where `cmake` is on `PATH`):

```bat
cmake -S windows-native\vcam-poc -B build\vcam -G "Visual Studio 17 2022" -A x64
cmake --build build\vcam --config Release
```

Outputs land together in one directory:

```
build\vcam\Release\PhoneBridgeVcamSource.dll
build\vcam\Release\vcam_host.exe
```

Both must stay in that directory: the host registers the DLL by its own path, so
moving the DLL after registering it breaks the camera.

The build defines `WINVER=_WIN32_WINNT=0x0A00`, which is what the SDK needs to
expose the Media Foundation headers used here; if a future SDK guards
`mfvirtualcamera.h` behind a higher value, raise it in `CMakeLists.txt` and note
why, rather than lowering the SDK requirement.

Expected warnings: none. If `mfvirtualcamera.h` is not found, the SDK step above
was skipped — the CMake configure error says exactly that.

---

## 3. Run

```bat
:: 1. Does this machine have the API at all? Registers nothing.
build\vcam\Release\vcam_host.exe --check

:: 2. Start the camera and hold it alive.
build\vcam\Release\vcam_host.exe
```

`--check` prints the real OS build (via `RtlGetVersion`, not the manifest-lied
`GetVersionEx`) and whether `mfsensorgroup.dll` exports `MFCreateVirtualCamera`.
Expected on Windows 11 22H2 or later:

```
PhoneBridge virtual camera — proof of concept
  Windows build: 22621 (needs 22000 or later)
  mode: current user
  MFCreateVirtualCamera: present
```

A normal run prints the symbolic link and then waits for Enter:

```
  registered C:\...\build\vcam\Release\PhoneBridgeVcamSource.dll
  symbolic link: \\?\DISPLAY#PBVCAM00001#5&2a3b...
  The camera is live as "PhoneBridge Camera Windows Virtual Camera".
```

Media Foundation appends `"Windows Virtual Camera"` to the friendly name, hence
the doubled wording; that is the documented behaviour, not a bug.

### Acceptance test (in this order)

1. Leave `vcam_host.exe` running.
2. **Windows Camera app** → the camera-switch button → pick
   *PhoneBridge Camera Windows Virtual Camera*. Expect a grey frame with a bright
   vertical bar sweeping left to right at about 1 Hz, and the app's own frame
   counter moving.
3. **OBS** → *Video Capture Device* → same name. 1280×720, 30 fps, no green or
   black frames.
4. **Browser test page** (`https://webcamtests.com/check`) in Chrome or Edge →
   same name. Browsers route through the same frame server, so this is the check
   that matters for Meet/Teams-in-browser.
5. **Zoom or Teams** → *Video* settings → same name. Only after 2–4 pass; meeting
   apps add permission prompts that obscure device problems.
6. Press Enter in the host window. All four apps must report the camera
   disappearing, and Windows Settings → Bluetooth & devices → Cameras must no
   longer list it.

Record: Windows build, which apps showed it, which did not, whether the bar
moved, and any `0x…` error code. Those four facts decide the next step.

### If it fails

| Symptom | Cause | What to do |
| --- | --- | --- |
| `MFCreateVirtualCamera: MISSING` | Windows 10, or Windows 11 < 22000 | Stop: the MF path cannot work on this machine. |
| Host exits `0x80070005` (`E_ACCESSDENIED`) | Camera privacy is off for desktop apps | Settings → Privacy & security → Camera → *Let desktop apps access your camera*. |
| Host exits `0x80040154` (`REGDB_E_CLASSNOTREG`) | DLL not where the CLSID points, or registration was removed | Re-run from the output directory; check `HKCU\Software\Classes\CLSID\{7F1B3C1E-…}\InprocServer32`. |
| Camera exists, app shows a black or frozen frame | Media source is not delivering samples | Report it with the Windows `Event Viewer` entry; this is the case the moving bar is designed to distinguish. |
| Camera does not appear in an app but appears in Settings | App caches its device list | Restart the app. |
| Camera appears, then vanishes | Host process ended | Keep the console window open; a session-lifetime camera dies with its process by design. |

---

## 4. Removal

The camera cannot outlive its process (`MFVirtualCameraLifetime_Session`) and the
host calls `Stop`, `Remove` and `RegDeleteTreeW` on every exit path except a hard
kill. So:

* **Normal:** press Enter. Nothing is left.
* **Killed the console window:** the camera disappears with the process. The
  registry key survives; delete it with

  ```bat
  reg delete "HKCU\Software\Classes\CLSID\{7F1B3C1E-9D62-4C2B-9E7A-5B4C0D2A6F31}" /f
  ```

* **Nothing was installed system-wide:** no service, no driver, no `Program
  Files` entry, no scheduled task. Deleting `build\vcam` completes the removal.

There is no uninstaller because there is nothing installed.

---

## 5. What this does not do (and what that implies for the plan)

* **It does not carry phone pixels.** The next step replaces `FillTestPattern`
  with a frame producer fed from PhoneBridge's existing authenticated session —
  a named shared-memory ring with an explicit DACL, written by the Python
  service, read here. The device plumbing proven above is unchanged by that
  swap, which is the entire point of doing this first.
* **It advertises one format.** 1280×720@30 NV12. Real camera apps negotiate;
  the full implementation advertises a list (and will need 640×360 and
  1920×1080 at least, plus the phone's native aspect).
* **It is per-user and per-session.** `MFVirtualCameraAccess_CurrentUser` plus the
  `HKCU` registration is why no admin rights are needed. A camera that survives
  the host process, or is visible to other users, needs `Lifetime_System` +
  `Access_AllUsers` + an elevated `HKLM` registration; `--machine` is present but
  returns "not implemented" on purpose, so nobody assumes it works.
* **It has not been tested against any real application.** The compatibility
  claim in `docs/25-system-integration-audit.md` — Camera, OBS, one browser, then
  a meeting app — is the test plan, not a result.
* **It is not endian- or HDR-aware.** NV12 BT.709 limited range, SDR. HDR
  output is out of scope until an app is shown to need it.
* **`Microsoft::WRL::RuntimeClass` with `ClassicCom`** is the COM flavour used
  here; the DLL declares `ThreadingModel=Both`, and all state is guarded by the
  media source's own mutex. Verify with the *Application Verifier* basic COM
  checks once it builds — that is the cheapest way to find a missing
  `AddRef`.

---

## 6. Why the media source looks the way it does

Three choices that are easy to get wrong, recorded so they are not "simplified"
away later:

* **`ActivateObject` returns `MediaSource`, and the activator owns an attribute
  store.** The frame server writes configuration into the activator's
  `IMFAttributes`; a stub that ignores them yields a source that starts and then
  produces nothing.
* **`Start` pushes `MENewStream` *then* `MEStreamStarted`,** in that order, and
  `RequestSample` builds one sample per call with a monotonically increasing
  timestamp. Reordering these is the usual reason a camera shows one frame and
  freezes.
* **`FillTestPattern` writes into a buffer the caller has already locked.**
  Locking inside the helper while the caller also holds the lock is a deadlock
  that only shows under a second stream — which the full version has.
