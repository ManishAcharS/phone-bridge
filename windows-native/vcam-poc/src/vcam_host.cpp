// PhoneBridge virtual camera — proof of concept, part 3: the host.
//
// What this does, in order, and nothing else:
//   1. registers the media-source CLSID for the current user (HKCU — no admin),
//   2. asks Media Foundation to create and start a software virtual camera by that
//      CLSID, per user, for this session,
//   3. prints the device's symbolic link (the identifier `adb`-style tools and
//      `Get-PnpDevice` show) and waits,
//   4. on exit: stops the camera, removes it from the system, deletes the
//      registry key it created.
//
// Step 4 is not politeness: `MFVirtualCameraLifetime_Session` means the device
// vanishes when this process ends, so a crash leaves nothing behind. The
// registry key is removed on every exit path except a hard kill.
//
// NOT COMPILED. See README.md.

#include <windows.h>
#include <mfapi.h>
#include <mfvirtualcamera.h>

#include <cstdio>
#include <cwchar>
#include <string>

#include "media_source_activate.h"

namespace {

constexpr wchar_t kFriendlyName[] = L"PhoneBridge Camera";
constexpr wchar_t kClsidKeyPath[] = L"Software\\Classes\\CLSID\\{7F1B3C1E-9D62-4C2B-9E7A-5B4C0D2A6F31}";

// GetVersionEx lies unless the app has a manifest entry for the tested Windows
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

// The COM loader reads InprocServer32 from the class key. Writing it under HKCU
// registers the class for this user only, which is what the per-user virtual
// camera needs and why no elevation is involved.
bool RegisterComClass(const std::wstring& dllPath) {
    HKEY key = nullptr;
    LSTATUS status = RegCreateKeyExW(HKEY_CURRENT_USER, kClsidKeyPath, 0, nullptr, 0,
                                     KEY_WRITE, nullptr, &key, nullptr);
    if (status != ERROR_SUCCESS) {
        std::fwprintf(stderr, L"  RegCreateKeyExW failed: %ld\n", status);
        return false;
    }
    HKEY inprocKey = nullptr;
    status = RegCreateKeyExW(key, L"InprocServer32", 0, nullptr, 0, KEY_WRITE, nullptr,
                             &inprocKey, nullptr);
    if (status == ERROR_SUCCESS) {
        RegSetValueExW(inprocKey, nullptr, 0, REG_SZ,
                       reinterpret_cast<const BYTE*>(dllPath.c_str()),
                       static_cast<DWORD>((dllPath.size() + 1) * sizeof(wchar_t)));
        const wchar_t threading[] = L"Both";
        RegSetValueExW(inprocKey, L"ThreadingModel", 0, REG_SZ,
                       reinterpret_cast<const BYTE*>(threading), sizeof(threading));
        RegCloseKey(inprocKey);
    }
    RegSetValueExW(key, nullptr, 0, REG_SZ,
                   reinterpret_cast<const BYTE*>(kFriendlyName),
                   static_cast<DWORD>((std::wcslen(kFriendlyName) + 1) * sizeof(wchar_t)));
    RegCloseKey(key);
    return status == ERROR_SUCCESS;
}

void UnregisterComClass() {
    // `RegDeleteTreeW` removes the key and its subkeys in one call; a missing key
    // is success, because the point is "after this, nothing is registered".
    RegDeleteTreeW(HKEY_CURRENT_USER, kClsidKeyPath);
}

void ExplainHresult(HRESULT hr) {
    switch (hr) {
        case E_ACCESSDENIED:
            std::fwprintf(stderr,
                          L"  E_ACCESSDENIED: Windows' camera privacy setting is denying this "
                          L"app, or the parameters need administrator rights.\n"
                          L"  Check Settings > Privacy & security > Camera.\n");
            break;
        case REGDB_E_CLASSNOTREG:
        case CO_E_CLASSSTRING:
            std::fwprintf(stderr,
                          L"  The media-source CLSID is not registered where the frame server "
                          L"can see it.\n"
                          L"  This POC registers per user (HKCU); try the elevated build with "
                          L"--machine.\n");
            break;
        case HRESULT_FROM_WIN32(ERROR_NOT_FOUND):
            std::fwprintf(stderr, L"  Media Foundation could not find the virtual camera it "
                                  L"just created.\n");
            break;
        default:
            break;
    }
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    bool machineWide = false;
    bool checkOnly = false;
    for (int i = 1; i < argc; ++i) {
        const std::wstring argument(argv[i]);
        if (argument == L"--machine") machineWide = true;
        if (argument == L"--check") checkOnly = true;
    }

    std::wprintf(L"PhoneBridge virtual camera — proof of concept\n");
    std::wprintf(L"  Windows build: %lu (needs 22000 or later)\n", OsBuild());
    std::wprintf(L"  mode: %ls\n", machineWide ? L"all users (needs elevation)" : L"current user");

    if (checkOnly) {
        // A --check run answers the only question this POC exists for: does this
        // machine have the API at all? It does not register anything.
        HMODULE handle = GetModuleHandleW(L"mfsensorgroup.dll");
        if (!handle) handle = LoadLibraryW(L"mfsensorgroup.dll");
        if (!handle) {
            std::fwprintf(stderr, L"  mfsensorgroup.dll is missing: this is not Windows 11 "
                                  L"build 22000 or later.\n");
            return 2;
        }
        const auto create = reinterpret_cast<decltype(&MFCreateVirtualCamera)>(
            GetProcAddress(handle, "MFCreateVirtualCamera"));
        std::wprintf(L"  MFCreateVirtualCamera: %ls\n",
                     create ? L"present" : L"MISSING (needs Windows 11 build 22000+)");
        return create ? 0 : 2;
    }

    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) {
        std::fwprintf(stderr, L"CoInitializeEx failed: 0x%08lX\n", hr);
        return 1;
    }
    hr = MFStartup(MF_VERSION);
    if (FAILED(hr)) {
        std::fwprintf(stderr, L"MFStartup failed: 0x%08lX\n", hr);
        CoUninitialize();
        return 1;
    }

    const std::wstring directory = ExecutableDirectory();
    const std::wstring dllPath = directory + L"\\PhoneBridgeVcamSource.dll";
    if (machineWide) {
        std::fwprintf(stderr,
                      L"  --machine is not implemented in the POC: it needs an elevated HKLM "
                      L"registration and a system-lifetime camera.\n");
        MFShutdown();
        CoUninitialize();
        return 3;
    }

    if (!RegisterComClass(dllPath)) {
        MFShutdown();
        CoUninitialize();
        return 1;
    }
    std::wprintf(L"  registered %ls\n", dllPath.c_str());

    IMFVirtualCamera* camera = nullptr;
    hr = MFCreateVirtualCamera(
        MFVirtualCameraType_SoftwareCameraSource,
        MFVirtualCameraLifetime_Session,   // gone when this process ends
        MFVirtualCameraAccess_CurrentUser, // no administrator rights needed
        kFriendlyName,
        phonebridge::kSourceClsidString,
        nullptr,  // default categories: VIDEO_CAMERA, VIDEO, CAPTURE
        0,
        &camera);
    if (FAILED(hr)) {
        std::fwprintf(stderr, L"MFCreateVirtualCamera failed: 0x%08lX\n", hr);
        ExplainHresult(hr);
        UnregisterComClass();
        MFShutdown();
        CoUninitialize();
        return 1;
    }

    hr = camera->Start(nullptr);
    if (FAILED(hr)) {
        std::fwprintf(stderr, L"IMFVirtualCamera::Start failed: 0x%08lX\n", hr);
        ExplainHresult(hr);
        camera->Release();
        UnregisterComClass();
        MFShutdown();
        CoUninitialize();
        return 1;
    }

    WCHAR* link = nullptr;
    UINT32 linkLength = 0;
    if (SUCCEEDED(camera->GetAllocatedString(
            MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK, &link, &linkLength))) {
        std::wprintf(L"  symbolic link: %ls\n", link);
        CoTaskMemFree(link);
    }
    std::wprintf(
        L"\n  The camera is live as \"%ls Windows Virtual Camera\".\n"
        L"  Open the Windows Camera app and pick it; you should see a grey frame with a\n"
        L"  bright bar moving left to right. Press Enter here to stop and remove it.\n",
        kFriendlyName);
    std::getwchar();

    camera->Stop();
    camera->Remove();
    camera->Release();
    UnregisterComClass();
    MFShutdown();
    CoUninitialize();
    std::wprintf(L"  stopped and removed.\n");
    return 0;
}
