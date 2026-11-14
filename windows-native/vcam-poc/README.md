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

## 2b. What the host guarantees (and what it deliberately refuses to do)

The host touches three things that outlive a process - a registry key, a device, and
a COM object - so its failure behaviour is part of the design rather than an
afterthought. Every clause below is asserted against the source in
`windows/tests/test_vcam_host_safety.py`.

| Situation | Behaviour |
| --- | --- |
| A registration for the CLSID already exists | It is printed (friendly name + `InprocServer32` path) and the host exits **4** without writing anything. Nothing is overwritten, ever. |
| The existing registration points at *this* DLL, left over from an interrupted run | Still refused by default; `--reuse-existing` uses it as found and does **not** delete it on exit. |
| The CLSID key appears between the check and the create | `RegCreateKeyExW` reports `REG_OPENED_EXISTING_KEY`, so the key is treated as somebody else's: exit **4**, key untouched. |
| Any `RegSetValueExW` fails | Everything written so far is rolled back (the key is ours, so removing it is exact). Exit **1**. |
| `PhoneBridgeVcamSource.dll` is missing, empty, or a directory | Exit **5** *before* any registry write. |
| `MFCreateVirtualCamera` / `Start` fails | The camera object is released, the key is removed, `CoUninitialize`/`MFShutdown` run in order. Exit **6**. |
| `Stop`, `Remove` or the final `Release` fails | Reported individually; the other calls are still attempted; exit **7**. A `Release` that returns a non-zero count means something still holds the object, which is reported rather than ignored. |
| Press Enter | Normal path; cleanup runs. |
| Press Ctrl+C / Ctrl+Break | Console handler signals the main thread, which owns the COM teardown, so cleanup still runs instead of being skipped. |
| Close the console window, log off, shut down | The system terminates the process after the handler returns, so the device dies with the session but the registry key can survive. That is why the host prints the exact `reg delete` command up front. |
| `--check` | Loads `mfsensorgroup.dll`, reads one export, frees it, inspects two file paths. No registry access at all, no camera created. |
| `--machine` | Refused before COM is initialised and before anything is written. Exit **3**. |

Exit codes: `0` ok, `1` setup failed, `2` API unavailable, `3` `--machine` refused,
`4` registration already present, `5` media source DLL missing, `6` camera failed,
`7` cleanup reported failure.

The host never calls `RegDeleteKey`/`RegDeleteTreeW` on a path it did not create.
That is enforced by tracking creation, not by assuming ownership of the path: the
`Registration` struct carries `createdKey`/`createdInproc`/`wroteValues`, and
`RemoveRegistration` is a no-op when `createdKey` is false.

## 3. Run

```bat
:: 1. Does this machine have the API at all? Registers nothing.
build\vcam\Release\vcam_host.exe --check

:: 2. Start the camera and hold it alive (Enter or Ctrl+C stops it and cleans up).
build\vcam\Release\vcam_host.exe
```

Further switches: `--reuse-existing` (accept a leftover HKCU registration that
already points at this DLL, and leave it in place), `--machine` (refused, exit 3),
`--help` (usage and exit codes). If a previous run was killed and now exits **4**,
check whether the registration is yours and either pass `--reuse-existing` or run
the `reg delete` line the host prints.

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
| Host exits `0x80004002` (`E_NOINTERFACE`) from `IMFVirtualCamera::Start` | A caller asked one of our COM objects for an interface it does not expose | Read the `QI MISS` line in DebugView — see *Diagnosing 0x80004002* below. |

### Diagnosing `0x80004002` (E_NOINTERFACE)

`Start` reports `E_NOINTERFACE` without saying which interface, on which object.
The objects this DLL owns — `MediaSource`, `MediaSourceActivate` and
`ClassFactory` — therefore log every `QueryInterface` that reaches them:

```
[PhoneBridge] QI MediaSource: IMFMediaSourceEx -> 0x00000000
[PhoneBridge] QI MISS MediaSource: { A987FBC9-4BED-3078-CF07-9141BA07C9F3 } -> 0x80004002
```

The `QI MISS` line names the interface a caller wanted and the object it asked,
which is the fact `Start`'s return code withholds. Unknown IIDs are printed as
GUIDs, so a miss on an interface this code has never heard of is still recorded.

This tracing is additive: every call is forwarded to WRL's own implementation and
that `HRESULT` is returned untouched, so reference counts and interface identity
are unchanged. It can be compiled out entirely if it ever gets in the way:

```
cmake -S windows-native/vcam-poc -B build/vcam -A x64 -DPB_TRACE_QUERY_INTERFACE=0
```

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

The COM surface is not invented. It is copied from Microsoft's own virtual-camera
sample, because every interface the frame server cannot find comes back from
`IMFVirtualCamera::Start` as the *same* error - `0x80004002 (E_NOINTERFACE)`, with
nothing to say which query failed:

> `microsoft/Windows-Camera` → `Samples/VirtualCamera/VirtualCameraMediaSource`
>
> | Reference sample | This source |
> |---|---|
> | `SimpleMediaSource : IMFMediaSourceEx, IMFGetService, IKsControl, IMFSampleAllocatorControl` | same four, one class |
> | `SimpleMediaStream : IMFMediaStream2` | `IMFMediaStream2` on the same object |

Each of the three "control" interfaces does something real here; none is a stub
added to silence a query:

| Interface | What this source answers | Why |
|---|---|---|
| `IMFGetService` | `MF_E_UNSUPPORTED_SERVICE` | The QI has to succeed even though there is no service to give: callers reach this through `MFGetService`, which queries first, and a failed query is indistinguishable from a fatal error. |
| `IKsControl` | `ERROR_SET_NOT_FOUND` from all three methods, after validating the header length | What an AVStream miniport returns for a control it never registered. No custom property set is claimed, so nothing can wait for a reply that will not come. |
| `IMFSampleAllocatorControl` | `UsesProvidedAllocator`, and `RequestSample` writes into that allocator's buffers | The frame server hands the source the allocator it wants frames in; filling it directly is what makes a camera shared between two applications cost no copy. With no allocator supplied (a source reader, a plain pull consumer) it falls back to its own system-memory sample rather than refusing to produce. |

Other decisions worth keeping:

* **`ActivateObject` returns `MediaSource`, and the activator owns an attribute
  store.** The frame server writes configuration into the activator's
  `IMFAttributes`; a stub that ignores them yields a source that starts and then
  produces nothing.
* **`Start` queues `MENewStream`, then `MEStreamStarted`, then `MESourceStarted`,
  in that order,** and `RequestSample` builds one sample per call with a
  monotonically increasing timestamp. `MESourceStarted` is the one a consumer
  actually waits for: leave it out and `Start` returns `S_OK` while the pipeline
  sits there. Reordering these is the usual reason a camera shows one frame and
  freezes.
* **The stream pointer in `MENewStream` comes from `QueryInterface`, never
  `static_cast<IMFMediaStream*>(this)`.** With four interface roots, converting
  a `this` pointer to the `IUnknown*` the event queue wants is ambiguous - it
  does not compile, and it is the mistake the merged source/stream design invites.
* **Source and stream attribute stores are created in the constructor**, before
  `Start`, because that is when the frame server reads them, and released in
  `Shutdown()`. `GetSourceAttributes` hands back a reference to the live store
  (the same interface pointer, add-ref'd), not a clone - a copy would silently
  diverge from what the pipeline configured. `SetD3DManager` returns `E_NOTIMPL`
  on purpose: frames are CPU NV12, so the system-memory allocator is correct, and
  claiming a D3D manager that is never honoured would hang the pipeline instead.
* **`ks.h` for the structures, `ksproxy.h` for `IKsControl`.** The two halves of
  the kernel-streaming API live in different headers in the Windows SDK: `ks.h`
  declares `KSPROPERTY`/`KSMETHOD`/`KSEVENT`, and the user-mode `IKsControl`
  interface is declared in `ksproxy.h`. Including only `ks.h` is the mistake that
  produced `IKsControl: undeclared identifier` on the first real build. Neither
  header needs a library here: the interface is used through its vtable and its
  IID comes from `__uuidof`, so nothing references `IID_IKsControl` at link time.
* **Two SDK spellings that look right and are not:** the colour frame-source type
  is `MFFrameSourceTypes_Color` (not `MF_FRAMESOURCE_TYPES_COLOR`), and the
  allocator-usage enum in the shipping SDK has a single member,
  `MFSampleAllocatorUsage_UsesProvidedAllocator` - there is no
  `_AllocatesSamples` to fall back to, which is why "we allocate our own" is
  expressed by the fallback in `RequestSample` rather than by an enum value.
* **`FillSample` locks through `Lock2DSize` and passes the real pitch to
  `FillTestPattern`.** An allocator-provided surface is usually padded beyond the
  1280-pixel width; writing width-strided into it shears the picture. A stride
  shorter than one row is rejected rather than written past the end.
* **`FillTestPattern` writes into a buffer someone else has already locked.**
  Locking inside the helper while the caller also holds the lock is a deadlock
  that only shows under a second stream — which the full version has.

Deliberately *not* copied from the reference:

* **Sensor profile collection** (`MF_DEVICEMFT_SENSORPROFILE_COLLECTION` with a
  Legacy and a High-Frame-Rate profile). It matters for profile-aware applications
  negotiating 60 fps; this source advertises one 30 fps format, so there is nothing
  to negotiate, and adding it drags in the `ksmedia.h` GUIDs. This is the first
  thing to add when profiles enter scope.
* **The `PROPSETID_SIMPLEMEDIASOURCE_CUSTOMCONTROL` KS property set** the sample
  uses to demo `IKsControl`. There is no user-facing control on a test pattern, so
  the honest answer is the "unregistered control" error above.
* **A separate stream object and a second RGB32 media type.** One class, one
  format: both are correct for a proof of concept and both are the wrong shape for
  the real source, which will need a stream per camera direction and the format
  list the phone can actually produce.
