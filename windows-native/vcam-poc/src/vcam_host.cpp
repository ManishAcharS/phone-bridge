// PhoneBridge virtual camera - proof of concept, part 3: the host.
//
// What this does, in order, and nothing else:
//   1. registers the media-source CLSID for the current user (HKCU - no admin),
//   2. asks Media Foundation to create and start a software virtual camera by that
//      CLSID, per user, for this session,
//   3. prints the device's symbolic link and waits,
//   4. on exit: stops the camera, removes it from the system, and deletes only the
//      registry keys this process created.
//
// Safety contract (each point has a matching assertion in
// windows/tests/test_vcam_host_safety.py):
//   * An existing CLSID registration is never overwritten. If one is present this
//     program reports what it points at and exits without writing anything.
//   * Only keys this process created are deleted. A pre-existing key, or one that
//     appeared in a race with another process, is left exactly as found.
//   * Every registry result is checked; a failure rolls back what was written.
//   * The media-source DLL is verified to exist before anything is registered.
//   * Every failure path runs one teardown that releases COM objects and shuts
//     Media Foundation down in the right order.
//   * Stop / Remove / Release results are reported rather than discarded.
//   * Enter, Ctrl+C and stdin EOF all reach the same cleanup.
//   * --check writes nothing at all; --machine refuses before touching anything.
//
// NOT COMPILED: this file was written and reviewed where no MSVC and no Windows
// SDK exist. See README.md.

#include <windows.h>

#include <mfapi.h>
#include <mfidl.h>          // MFCreateVirtualCamera's attribute keys, CoTaskMemFree
#include <mfvirtualcamera.h>
#include <objbase.h>        // CoInitializeEx / CoUninitialize / CoTaskMemFree

#include <cstdio>
#include <cwchar>
#include <string>

#include "media_source_activate.h"

namespace {

constexpr wchar_t kFriendlyName[] = L"PhoneBridge Camera";
constexpr wchar_t kDllName[] = L"PhoneBridgeVcamSource.dll";

// Exit codes, so a script or a person can tell outcomes apart without parsing text.
constexpr int kExitOk = 0;
constexpr int kExitFatal = 1;               // COM / MF / registry setup failed
constexpr int kExitNoApi = 2;               // MFCreateVirtualCamera is unavailable
constexpr int kExitMachineUnsupported = 3;  // --machine is deliberately unimplemented
constexpr int kExitAlreadyRegistered = 4;   // a CLSID registration already exists
constexpr int kExitDllMissing = 5;          // media-source DLL is absent or empty
constexpr int kExitCameraFailed = 6;        // create/start/orientation failed
constexpr int kExitCleanupFailed = 7;       // Stop/Remove/registry cleanup reported failure

// Set by the console control handler; the main thread waits on it.
HANDLE g_stopEvent = nullptr;

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------
void ReportStatus(const wchar_t* what, LSTATUS status) {
    std::fwprintf(stderr, L"  %ls failed: %ld (0x%08lX)\n", what,
                  static_cast<long>(status), static_cast<unsigned long>(status));
}

void ReportHresult(const wchar_t* what, HRESULT hr) {
    std::fwprintf(stderr, L"  %ls failed: 0x%08lX\n", what, static_cast<unsigned long>(hr));
    switch (hr) {
        case E_ACCESSDENIED:
            std::fwprintf(stderr,
                          L"    Windows' camera privacy setting is denying this app, or the\n"
                          L"    operation needs administrator rights. Check Settings >\n"
                          L"    Privacy & security > Camera.\n");
            break;
        case REGDB_E_CLASSNOTREG:
            std::fwprintf(stderr,
                          L"    The media-source CLSID is not visible where the frame server\n"
                          L"    looks. Registration was per user (HKCU).\n");
            break;
        case HRESULT_FROM_WIN32(ERROR_NOT_FOUND):
            std::fwprintf(stderr,
                          L"    Media Foundation could not find the virtual camera it was asked\n"
                          L"    to create.\n");
            break;
        default:
            break;
    }
}

// GetVersionEx lies unless the binary is manifested for the tested Windows
// versions; RtlGetVersion does not, so the number printed is the real build.
unsigned long OsBuild() {
    using RtlGetVersionFn = LONG(WINAPI*)(PRTL_OSVERSIONINFOW);
    const HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    if (!ntdll) return 0;
    const auto rtlGetVersion =
        reinterpret_cast<RtlGetVersionFn>(GetProcAddress(ntdll, "RtlGetVersion"));
    if (!rtlGetVersion) return 0;
    RTL_OSVERSIONINFOW info = {};
    info.dwOSVersionInfoSize = sizeof(info);
    if (rtlGetVersion(&info) != 0) return 0;
    return info.dwBuildNumber;
}

std::wstring ExecutableDirectory() {
    wchar_t path[MAX_PATH] = {};
    const DWORD length = GetModuleFileNameW(nullptr, path, MAX_PATH);
    if (length == 0 || length == MAX_PATH) return L"";
    std::wstring full(path, length);
    const size_t slash = full.find_last_of(L'\\');
    return slash == std::wstring::npos ? L"" : full.substr(0, slash);
}

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------
// Built from the single CLSID definition in media_source_activate.h, so the
// registry key, the GUID the class factory answers to and the string handed to
// MFCreateVirtualCamera cannot drift apart.
std::wstring ClsidKeyPath() {
    return std::wstring(L"Software\\Classes\\CLSID\\") + phonebridge::kSourceClsidString;
}

bool ReadStringValue(HKEY key, const wchar_t* name, std::wstring* out) {
    DWORD type = 0;
    DWORD bytes = 0;
    LSTATUS status = RegQueryValueExW(key, name, nullptr, &type, nullptr, &bytes);
    if (status != ERROR_SUCCESS) return false;
    if (type != REG_SZ && type != REG_EXPAND_SZ) return false;
    if (bytes < sizeof(wchar_t)) return false;
    std::wstring buffer(bytes / sizeof(wchar_t) + 1, L'\0');
    status = RegQueryValueExW(key, name, nullptr, &type,
                              reinterpret_cast<LPBYTE>(&buffer[0]), &bytes);
    if (status != ERROR_SUCCESS) return false;
    buffer.resize(bytes / sizeof(wchar_t));
    while (!buffer.empty() && buffer.back() == L'\0') buffer.pop_back();
    *out = buffer;
    return true;
}

// What this process put in the registry. Nothing outside these flags is deleted:
// `createdKey == false` means the key belongs to somebody else.
struct Registration {
    std::wstring keyPath;
    bool createdKey = false;     // we created the CLSID key itself
    bool createdInproc = false;  // we created its InprocServer32 subkey
    bool wroteValues = false;    // friendly name / server path / ThreadingModel landed
};

enum class RegisterResult {
    kCreated,         // key written by us, and will be removed by us
    kReusedExisting,  // --reuse-existing: points at this DLL already; left untouched
    kConflict,        // an unrelated registration exists; nothing was written
    kFailed,          // a registry call failed; anything written was rolled back
};

// Deletes only what this process created. Returns false if it could not, having
// printed the command that would finish the job by hand.
bool RemoveRegistration(Registration* registration) {
    if (!registration->createdKey) {
        // Deliberately does not touch a pre-existing key. This is the whole point
        // of tracking creation rather than assuming ownership of the path.
        return true;
    }
    const LSTATUS status =
        RegDeleteTreeW(HKEY_CURRENT_USER, registration->keyPath.c_str());
    if (status == ERROR_SUCCESS || status == ERROR_FILE_NOT_FOUND) {
        registration->createdKey = false;
        registration->createdInproc = false;
        registration->wroteValues = false;
        return true;
    }
    ReportStatus(L"RegDeleteTreeW", status);
    std::fwprintf(stderr, L"    remove it by hand: reg delete \"HKCU\\%ls\" /f\n",
                  registration->keyPath.c_str());
    return false;
}

RegisterResult RegisterComClass(const std::wstring& dllPath, bool reuseIfOurs,
                                Registration* registration) {
    registration->keyPath = ClsidKeyPath();

    // 1. Refuse to overwrite anything that is already there.
    HKEY existing = nullptr;
    LSTATUS status = RegOpenKeyExW(HKEY_CURRENT_USER, registration->keyPath.c_str(), 0,
                                   KEY_READ, &existing);
    if (status == ERROR_SUCCESS) {
        std::wstring server;
        std::wstring friendly;
        HKEY inprocExisting = nullptr;
        if (RegOpenKeyExW(existing, L"InprocServer32", 0, KEY_READ, &inprocExisting) ==
            ERROR_SUCCESS) {
            ReadStringValue(inprocExisting, nullptr, &server);
            RegCloseKey(inprocExisting);
        }
        ReadStringValue(existing, nullptr, &friendly);
        RegCloseKey(existing);

        std::wprintf(L"  an existing CLSID registration is present:\n");
        std::wprintf(L"    friendly name : %ls\n",
                     friendly.empty() ? L"(not set)" : friendly.c_str());
        std::wprintf(L"    InprocServer32: %ls\n",
                     server.empty() ? L"(not set)" : server.c_str());

        // Registry paths are case-insensitive on Windows, and a leftover entry may
        // carry different casing, so compare ordinally and ignore case.
        const bool isOurs =
            !server.empty() &&
            CompareStringOrdinal(server.c_str(), -1, dllPath.c_str(), -1, TRUE) == CSTR_EQUAL;
        if (reuseIfOurs && isOurs) {
            std::wprintf(L"    --reuse-existing: it already points at this DLL, so it is used\n"
                         L"    as found and will NOT be deleted on exit.\n");
            return RegisterResult::kReusedExisting;
        }
        std::wprintf(L"    refusing to overwrite it: nothing was changed.\n");
        if (isOurs) {
            std::wprintf(L"    It points at this DLL already - a leftover from an interrupted\n"
                         L"    run. Either pass --reuse-existing, or remove it with:\n");
        } else {
            std::wprintf(L"    It belongs to something else. Remove it with:\n");
        }
        std::wprintf(L"      reg delete \"HKCU\\%ls\" /f\n", registration->keyPath.c_str());
        return RegisterResult::kConflict;
    }
    if (status != ERROR_FILE_NOT_FOUND) {
        ReportStatus(L"RegOpenKeyExW", status);
        return RegisterResult::kFailed;
    }

    // 2. Create the key, and confirm the system says we created it. If another
    //    process won the race the disposition says "opened existing", and we must
    //    not treat it as ours.
    HKEY key = nullptr;
    DWORD disposition = 0;
    status = RegCreateKeyExW(HKEY_CURRENT_USER, registration->keyPath.c_str(), 0, nullptr,
                             REG_OPTION_NON_VOLATILE, KEY_WRITE | KEY_READ, nullptr, &key,
                             &disposition);
    if (status != ERROR_SUCCESS) {
        ReportStatus(L"RegCreateKeyExW", status);
        return RegisterResult::kFailed;
    }
    if (disposition != REG_CREATED_NEW_KEY) {
        RegCloseKey(key);
        std::fwprintf(stderr,
                      L"  the CLSID key appeared while we were creating it; refusing to adopt "
                      L"a key we did not create.\n");
        return RegisterResult::kConflict;
    }
    registration->createdKey = true;

    bool ok = true;

    status = RegSetValueExW(key, nullptr, 0, REG_SZ,
                            reinterpret_cast<const BYTE*>(kFriendlyName),
                            static_cast<DWORD>(sizeof(kFriendlyName)));
    if (status != ERROR_SUCCESS) {
        ReportStatus(L"RegSetValueExW (friendly name)", status);
        ok = false;
    }

    if (ok) {
        HKEY inproc = nullptr;
        DWORD inprocDisposition = 0;
        status = RegCreateKeyExW(key, L"InprocServer32", 0, nullptr, REG_OPTION_NON_VOLATILE,
                                 KEY_WRITE | KEY_READ, nullptr, &inproc, &inprocDisposition);
        if (status != ERROR_SUCCESS) {
            ReportStatus(L"RegCreateKeyExW (InprocServer32)", status);
            ok = false;
        } else if (inprocDisposition != REG_CREATED_NEW_KEY) {
            RegCloseKey(inproc);
            std::fwprintf(stderr,
                          L"  InprocServer32 already existed under a key we had just created; "
                          L"refusing.\n");
            ok = false;
        } else {
            registration->createdInproc = true;

            status = RegSetValueExW(inproc, nullptr, 0, REG_SZ,
                                    reinterpret_cast<const BYTE*>(dllPath.c_str()),
                                    static_cast<DWORD>((dllPath.size() + 1) * sizeof(wchar_t)));
            if (status != ERROR_SUCCESS) {
                ReportStatus(L"RegSetValueExW (InprocServer32)", status);
                ok = false;
            }

            if (ok) {
                constexpr wchar_t kThreadingModel[] = L"Both";
                status = RegSetValueExW(inproc, L"ThreadingModel", 0, REG_SZ,
                                        reinterpret_cast<const BYTE*>(kThreadingModel),
                                        static_cast<DWORD>(sizeof(kThreadingModel)));
                if (status != ERROR_SUCCESS) {
                    ReportStatus(L"RegSetValueExW (ThreadingModel)", status);
                    ok = false;
                }
            }

            const LSTATUS closeInproc = RegCloseKey(inproc);
            if (closeInproc != ERROR_SUCCESS) {
                ReportStatus(L"RegCloseKey (InprocServer32)", closeInproc);
                ok = false;
            }
        }
    }

    const LSTATUS closeKey = RegCloseKey(key);
    if (closeKey != ERROR_SUCCESS) {
        ReportStatus(L"RegCloseKey", closeKey);
        ok = false;
    }

    registration->wroteValues = ok;
    if (!ok) {
        // Roll back only the part that is ours; this is why the flags exist.
        RemoveRegistration(registration);
        return RegisterResult::kFailed;
    }
    return RegisterResult::kCreated;
}

// ---------------------------------------------------------------------------
// Files
// ---------------------------------------------------------------------------
bool VerifyMediaSourceDll(const std::wstring& dllPath) {
    if (dllPath.empty()) {
        std::fwprintf(stderr, L"  could not determine this executable's directory.\n");
        return false;
    }
    WIN32_FILE_ATTRIBUTE_DATA data = {};
    if (!GetFileAttributesExW(dllPath.c_str(), GetFileExInfoStandard, &data)) {
        const DWORD error = GetLastError();
        std::fwprintf(stderr, L"  media source DLL not found: %ls\n", dllPath.c_str());
        ReportStatus(L"GetFileAttributesExW", static_cast<LSTATUS>(error));
        std::fwprintf(stderr,
                      L"    the DLL must sit beside vcam_host.exe; build the CMake target\n"
                      L"    PhoneBridgeVcamSource before running this.\n");
        return false;
    }
    if ((data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0) {
        std::fwprintf(stderr, L"  %ls is a directory, not a DLL.\n", dllPath.c_str());
        return false;
    }
    const ULONGLONG size =
        (static_cast<ULONGLONG>(data.nFileSizeHigh) << 32) | data.nFileSizeLow;
    if (size == 0) {
        std::fwprintf(stderr, L"  %ls is empty.\n", dllPath.c_str());
        return false;
    }
    std::wprintf(L"  media source DLL: %ls (%llu bytes)\n", dllPath.c_str(), size);
    return true;
}

// ---------------------------------------------------------------------------
// COM / Media Foundation lifetime
// ---------------------------------------------------------------------------
// One object, one destructor: whatever partial state a failure path leaves behind
// is unwound in the reverse order it was acquired.
struct Runtime {
    bool comInitialised = false;
    bool mfStarted = false;

    ~Runtime() {
        if (mfStarted) {
            const HRESULT hr = MFShutdown();
            if (FAILED(hr)) ReportHresult(L"MFShutdown", hr);
        }
        if (comInitialised) CoUninitialize();
    }
};

// ---------------------------------------------------------------------------
// Console lifetime: Enter, Ctrl+C and stdin EOF all end here
// ---------------------------------------------------------------------------
BOOL WINAPI OnConsoleEvent(DWORD eventType) {
    switch (eventType) {
        case CTRL_C_EVENT:
        case CTRL_BREAK_EVENT:
        case CTRL_CLOSE_EVENT:
        case CTRL_LOGOFF_EVENT:
        case CTRL_SHUTDOWN_EVENT:
            // Signal the main thread, which owns the COM cleanup. Returning TRUE
            // keeps this process alive for Ctrl+C/Ctrl+Break so the teardown runs;
            // for close/logoff/shutdown Windows terminates us shortly afterwards,
            // which is why the removal command is printed up front.
            if (g_stopEvent != nullptr) SetEvent(g_stopEvent);
            return TRUE;
        default:
            return FALSE;
    }
}

// Blocks until the user presses Enter on the console, stdin reaches EOF, or a
// console control event asks us to stop.
void WaitForExit() {
    const HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
    DWORD mode = 0;
    const bool consoleInput =
        input != nullptr && input != INVALID_HANDLE_VALUE && GetConsoleMode(input, &mode) != 0;

    if (!consoleInput) {
        std::wprintf(L"  stdin is not a console: press Ctrl+C to stop and clean up.\n");
        WaitForSingleObject(g_stopEvent, INFINITE);
        return;
    }

    HANDLE handles[2] = {input, g_stopEvent};
    for (;;) {
        const DWORD wait = WaitForMultipleObjects(2, handles, FALSE, INFINITE);
        if (wait == WAIT_OBJECT_0 + 1) return;  // stop event
        if (wait != WAIT_OBJECT_0) {
            std::fwprintf(stderr, L"  WaitForMultipleObjects returned %lu; stopping.\n",
                          static_cast<unsigned long>(wait));
            return;
        }
        INPUT_RECORD record = {};
        DWORD consumed = 0;
        if (!ReadConsoleInputW(input, &record, 1, &consumed) || consumed == 0) {
            return;  // input handle closed
        }
        if (record.EventType == KEY_EVENT && record.Event.KeyEvent.bKeyDown != 0 &&
            record.Event.KeyEvent.wVirtualKeyCode == VK_RETURN) {
            return;
        }
    }
}

// ---------------------------------------------------------------------------
// Teardown
// ---------------------------------------------------------------------------
bool StopAndRemoveCamera(IMFVirtualCamera* camera) {
    bool ok = true;

    HRESULT hr = camera->Stop();
    if (FAILED(hr)) {
        ReportHresult(L"IMFVirtualCamera::Stop", hr);
        ok = false;
    }

    // Attempted even if Stop failed: leaving a half-live device registered is
    // worse than a second error message.
    hr = camera->Remove();
    if (FAILED(hr)) {
        ReportHresult(L"IMFVirtualCamera::Remove", hr);
        ok = false;
    }

    const ULONG remaining = camera->Release();
    if (remaining != 0) {
        std::fwprintf(stderr,
                      L"  IMFVirtualCamera::Release left %lu reference(s): the camera object is "
                      L"still held\n  somewhere, which usually means a callback is still alive.\n",
                      static_cast<unsigned long>(remaining));
        ok = false;
    }
    return ok;
}

// ---------------------------------------------------------------------------
// --check: read only
// ---------------------------------------------------------------------------
int RunApiCheck() {
    std::wprintf(L"  Windows build: %lu (MFCreateVirtualCamera needs 22000 or later)\n",
                 OsBuild());

    HMODULE module = GetModuleHandleW(L"mfsensorgroup.dll");
    bool loadedHere = false;
    if (module == nullptr) {
        module = LoadLibraryW(L"mfsensorgroup.dll");
        loadedHere = true;
    }
    if (module == nullptr) {
        const DWORD error = GetLastError();
        std::fwprintf(stderr, L"  mfsensorgroup.dll is not present (error %lu):\n"
                              L"  this system cannot host a Media Foundation software camera.\n",
                      static_cast<unsigned long>(error));
        return kExitNoApi;
    }

    FARPROC proc = GetProcAddress(module, "MFCreateVirtualCamera");
    std::wprintf(L"  MFCreateVirtualCamera: %ls\n",
                 proc != nullptr ? L"present" : L"MISSING (needs Windows 11 build 22000+)");
    if (loadedHere) FreeLibrary(module);

    // Read-only side check: the host cannot work without the media source beside it.
    const std::wstring dllPath = ExecutableDirectory() + L"\\" + kDllName;
    WIN32_FILE_ATTRIBUTE_DATA data = {};
    if (GetFileAttributesExW(dllPath.c_str(), GetFileExInfoStandard, &data)) {
        const ULONGLONG size =
            (static_cast<ULONGLONG>(data.nFileSizeHigh) << 32) | data.nFileSizeLow;
        std::wprintf(L"  media source DLL: present (%llu bytes)\n", size);
    } else {
        std::wprintf(L"  media source DLL: not beside the executable yet (%ls)\n",
                     dllPath.c_str());
    }

    std::wprintf(L"  read only: no registry key written, no camera created.\n");
    return proc != nullptr ? kExitOk : kExitNoApi;
}

void PrintUsage() {
    std::wprintf(
        L"\n"
        L"  usage: vcam_host.exe [--check] [--machine] [--reuse-existing]\n"
        L"\n"
        L"    (no switches)     register, start the camera, wait, then clean up\n"
        L"    --check           report the OS build and API availability; writes nothing\n"
        L"    --machine         refused: all-users mode needs an elevated HKLM install\n"
        L"    --reuse-existing  accept a leftover HKCU registration that points at this\n"
        L"                      DLL, and leave it in place on exit\n"
        L"\n"
        L"  exit codes: 0 ok, 1 setup failed, 2 API unavailable, 3 --machine refused,\n"
        L"              4 registration already present, 5 media source DLL missing,\n"
        L"              6 camera failed, 7 cleanup reported failure\n");
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    bool machineWide = false;
    bool checkOnly = false;
    bool reuseExisting = false;
    for (int i = 1; i < argc; ++i) {
        const std::wstring argument(argv[i]);
        if (argument == L"--check") {
            checkOnly = true;
        } else if (argument == L"--machine") {
            machineWide = true;
        } else if (argument == L"--reuse-existing") {
            reuseExisting = true;
        } else if (argument == L"--help" || argument == L"-h" || argument == L"/?") {
            PrintUsage();
            return kExitOk;
        } else {
            std::fwprintf(stderr, L"  unknown argument: %ls\n", argument.c_str());
            PrintUsage();
            return kExitFatal;
        }
    }

    std::wprintf(L"PhoneBridge virtual camera - proof of concept\n");

    if (checkOnly) {
        std::wprintf(L"  mode: check (read only)\n");
        return RunApiCheck();
    }

    std::wprintf(L"  Windows build: %lu\n", OsBuild());

    // Refused before COM is initialised and before anything is written.
    if (machineWide) {
        std::fwprintf(stderr,
                      L"  --machine is not implemented: all-users mode needs an elevated HKLM\n"
                      L"  registration and a system-lifetime camera, neither of which this\n"
                      L"  proof of concept sets up. Nothing was changed.\n");
        return kExitMachineUnsupported;
    }

    const std::wstring dllPath = ExecutableDirectory() + L"\\" + kDllName;
    if (!VerifyMediaSourceDll(dllPath)) {
        return kExitDllMissing;
    }

    Runtime runtime;
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) {
        ReportHresult(L"CoInitializeEx", hr);
        return kExitFatal;
    }
    runtime.comInitialised = true;

    hr = MFStartup(MF_VERSION);
    if (FAILED(hr)) {
        ReportHresult(L"MFStartup", hr);
        return kExitFatal;
    }
    runtime.mfStarted = true;

    Registration registration;
    switch (RegisterComClass(dllPath, reuseExisting, &registration)) {
        case RegisterResult::kCreated:
            std::wprintf(L"  registered %ls (per user, HKCU)\n", dllPath.c_str());
            break;
        case RegisterResult::kReusedExisting:
            break;
        case RegisterResult::kConflict:
            return kExitAlreadyRegistered;
        case RegisterResult::kFailed:
        default:
            return kExitFatal;
    }

    // The console handler needs the event before the camera exists, so a Ctrl+C
    // during Start still reaches the cleanup below.
    g_stopEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (g_stopEvent == nullptr) {
        ReportStatus(L"CreateEventW", static_cast<LSTATUS>(GetLastError()));
        RemoveRegistration(&registration);
        return kExitFatal;
    }
    if (SetConsoleCtrlHandler(OnConsoleEvent, TRUE) == FALSE) {
        ReportStatus(L"SetConsoleCtrlHandler", static_cast<LSTATUS>(GetLastError()));
        CloseHandle(g_stopEvent);
        g_stopEvent = nullptr;
        RemoveRegistration(&registration);
        return kExitFatal;
    }

    IMFVirtualCamera* camera = nullptr;
    hr = MFCreateVirtualCamera(MFVirtualCameraType_SoftwareCameraSource,
                               MFVirtualCameraLifetime_Session,    // dies with this process
                               MFVirtualCameraAccess_CurrentUser,  // no administrator rights
                               kFriendlyName, phonebridge::kSourceClsidString,
                               nullptr,  // default categories: VIDEO_CAMERA, VIDEO, CAPTURE
                               0, &camera);
    if (FAILED(hr)) {
        ReportHresult(L"MFCreateVirtualCamera", hr);
        if (camera != nullptr) {
            // A failing API is still allowed to hand back an object; drop it rather
            // than leaking a camera that was never started.
            const ULONG remaining = camera->Release();
            if (remaining != 0) {
                std::fwprintf(stderr, L"  the failed camera object still has %lu reference(s).\n",
                              static_cast<unsigned long>(remaining));
            }
        }
        SetConsoleCtrlHandler(OnConsoleEvent, FALSE);
        CloseHandle(g_stopEvent);
        g_stopEvent = nullptr;
        RemoveRegistration(&registration);
        return kExitCameraFailed;
    }

    hr = camera->Start(nullptr);
    if (FAILED(hr)) {
        ReportHresult(L"IMFVirtualCamera::Start", hr);
        const ULONG remaining = camera->Release();
        if (remaining != 0) {
            std::fwprintf(stderr, L"  the failed camera object still has %lu reference(s).\n",
                          static_cast<unsigned long>(remaining));
        }
        SetConsoleCtrlHandler(OnConsoleEvent, FALSE);
        CloseHandle(g_stopEvent);
        g_stopEvent = nullptr;
        RemoveRegistration(&registration);
        return kExitCameraFailed;
    }

    WCHAR* link = nullptr;
    UINT32 linkLength = 0;
    if (SUCCEEDED(camera->GetAllocatedString(
            MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK, &link, &linkLength))) {
        std::wprintf(L"  symbolic link: %ls\n", link);
        CoTaskMemFree(link);
    } else {
        std::wprintf(L"  symbolic link: (not reported by the system)\n");
    }

    std::wprintf(
        L"\n"
        L"  The camera is live as \"%ls Windows Virtual Camera\".\n"
        L"  Open the Windows Camera app and pick it; you should see a grey frame with a\n"
        L"  bright bar moving left to right.\n"
        L"\n"
        L"  Press Enter (or Ctrl+C) here to stop it. If this process is killed instead,\n"
        L"  the camera disappears with it and the registry key is left behind; remove it with\n"
        L"    reg delete \"HKCU\\%ls\" /f\n",
        kFriendlyName, registration.keyPath.c_str());

    WaitForExit();

    std::wprintf(L"\n  stopping and removing...\n");
    // Captured before removal: RemoveRegistration clears the flag on success, and
    // the summary below must say whether anything was ours in the first place.
    const bool registrationWasOurs = registration.createdKey;
    const bool cameraOk = StopAndRemoveCamera(camera);
    const bool registrationOk = RemoveRegistration(&registration);

    if (SetConsoleCtrlHandler(OnConsoleEvent, FALSE) == FALSE) {
        ReportStatus(L"SetConsoleCtrlHandler (remove)", static_cast<LSTATUS>(GetLastError()));
    }
    if (g_stopEvent != nullptr) {
        CloseHandle(g_stopEvent);
        g_stopEvent = nullptr;
    }

    if (!registrationWasOurs) {
        std::wprintf(L"  registry entry left in place (it was not created by this process).\n");
    } else if (registrationOk) {
        std::wprintf(L"  registry entry removed (only what this process created).\n");
    } else {
        std::wprintf(L"  registry entry could not be removed; the command above finishes it.\n");
    }
    std::wprintf(L"  %ls\n", (cameraOk && registrationOk) ? L"done." : L"finished with errors.");
    return (cameraOk && registrationOk) ? kExitOk : kExitCleanupFailed;
}
