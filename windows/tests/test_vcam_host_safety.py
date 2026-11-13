"""Safety contract of the virtual-camera host, pinned without a compiler.

`windows-native/vcam-poc` cannot be built or run in this environment (no MSVC, no
Windows SDK), so the only thing that can be checked here is the *shape* of the
source. That is still worth doing, because the dangerous mistakes in a host like
this are structural, not typographical:

* deleting a registry key it did not create (destroys another vendor's camera),
* registering over an existing CLSID (same, one step earlier),
* leaving a half-initialised COM/Media Foundation state behind on a failure path,
* discarding the results of `RegSetValueExW` / `Stop` / `Remove` so a failure
  looks like success,
* unloading the DLL while the frame server still holds an object,
* declaring the CLSID twice and letting registration and activation drift apart.

Each test below fails if one of those reappears. They assert behaviour visible in
the source, not formatting; none of them claims the program *works* - for that the
host has to be built and run on Windows.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
POC = REPO / "windows-native" / "vcam-poc"
HOST = POC / "src" / "vcam_host.cpp"
ACTIVATE_CPP = POC / "src" / "media_source_activate.cpp"
ACTIVATE_H = POC / "src" / "media_source_activate.h"

CLSID_TEXT = "{7F1B3C1E-9D62-4C2B-9E7A-5B4C0D2A6F31}"


def host() -> str:
    return HOST.read_text(encoding="utf-8")


def body(text: str, start_marker: str, end_marker: str) -> str:
    """The slice of `text` between two markers, for 'this must precede that' checks."""
    start = text.index(start_marker)
    return text[start : text.index(end_marker, start)]


def test_the_clsid_is_declared_exactly_once() -> None:
    """Registration, the class factory and MFCreateVirtualCamera must share one value.

    Documentation may quote the CLSID; compiled code may not, because two copies
    can drift and the host would then register a class nobody activates.
    """
    code_files = sorted(POC.joinpath("src").rglob("*")) + [POC / "CMakeLists.txt"]
    occurrences: list[Path] = []
    for path in code_files:
        if not path.is_file():
            continue
        if CLSID_TEXT in path.read_text(encoding="utf-8", errors="replace"):
            occurrences.append(path.relative_to(POC))
    assert occurrences == [Path("src/media_source_activate.h")], (
        "the CLSID string must live in one place; found in "
        f"{[str(p) for p in occurrences]}"
    )


def test_the_host_derives_the_key_path_instead_of_repeating_the_clsid() -> None:
    text = host()
    assert CLSID_TEXT not in text, "the host must not hardcode the CLSID"
    assert "phonebridge::kSourceClsidString" in text
    assert 'L"Software\\\\Classes\\\\CLSID\\\\"' in text


def test_an_existing_registration_is_detected_and_refused() -> None:
    text = host()
    registration = body(text, "RegisterResult RegisterComClass", "// Files")
    assert "RegOpenKeyExW(HKEY_CURRENT_USER" in registration
    assert "kConflict" in registration
    # A key that appears during the create must not be adopted as ours.
    assert "REG_CREATED_NEW_KEY" in registration
    assert "disposition != REG_CREATED_NEW_KEY" in registration


def test_only_self_created_keys_are_deleted() -> None:
    text = host()
    removal = body(text, "bool RemoveRegistration", "RegisterResult RegisterComClass")
    # The guard comes first, and the delete happens only after it.
    guard = removal.index("if (!registration->createdKey)")
    delete = removal.index("RegDeleteTreeW")
    assert guard < delete, "RegDeleteTreeW must sit behind the created-by-us guard"
    # One call site (the error label mentioning the API is inside the same function).
    assert text.count("RegDeleteTreeW(HKEY_CURRENT_USER") == 1, "exactly one deletion site"


def test_registry_writes_are_all_result_checked() -> None:
    text = host()
    unchecked = [
        line.strip()
        for line in text.splitlines()
        if "RegSetValueExW(" in line and not line.strip().startswith("status = ")
    ]
    assert not unchecked, f"RegSetValueExW result discarded: {unchecked}"
    # Creation and cleanup are checked too.
    assert "ReportStatus(L\"RegCreateKeyExW\"" in text
    assert "ReportStatus(L\"RegDeleteTreeW\"" in text
    assert "ReportStatus(L\"RegCloseKey\"" in text


def test_the_media_source_dll_is_verified_before_registration() -> None:
    text = host()
    verify = text.index("VerifyMediaSourceDll(")
    register = text.index("RegisterComClass(dllPath, reuseExisting")
    assert verify < register, "the DLL must be checked before anything is registered"
    assert "GetFileAttributesExW" in text
    assert "FILE_ATTRIBUTE_DIRECTORY" in text, "a directory must not pass for a DLL"


def test_camera_and_media_foundation_teardown_is_checked() -> None:
    text = host()
    assert "camera->Stop()" in text and "ReportHresult(L\"IMFVirtualCamera::Stop\"" in text
    assert "camera->Remove()" in text and "ReportHresult(L\"IMFVirtualCamera::Remove\"" in text
    # Release's remaining count is meaningful: zero means nobody else holds it.
    assert "remaining != 0" in text
    # One owner of the MF/COM lifetime, unwound in reverse order of acquisition.
    runtime = body(text, "struct Runtime {", "// Console lifetime")
    assert runtime.index("MFShutdown") < runtime.index("CoUninitialize")


def test_check_mode_stays_read_only() -> None:
    text = host()
    main = text[text.index("int wmain(") :]
    check_return = main.index("return RunApiCheck();")
    first_registry = main.index("RegisterComClass(")
    first_com = main.index("CoInitializeEx(")
    assert check_return < first_registry, "--check must return before any registration"
    assert check_return < first_com, "--check must return before COM is initialised"
    check_body = body(text, "int RunApiCheck()", "void PrintUsage()")
    for forbidden in ("RegCreateKeyExW", "RegSetValueExW", "RegDeleteTreeW", "MFCreateVirtualCamera("):
        assert forbidden not in check_body, f"--check must not call {forbidden}"


def test_machine_mode_is_refused_before_side_effects() -> None:
    text = host()
    main = text[text.index("int wmain(") :]
    refuse = main.index("kExitMachineUnsupported")
    assert refuse < main.index("RegisterComClass(")
    assert refuse < main.index("CoInitializeEx(")


def test_exit_through_enter_or_control_event_always_cleans_up() -> None:
    text = host()
    assert "SetConsoleCtrlHandler(OnConsoleEvent, TRUE)" in text
    assert "WaitForMultipleObjects(2, handles, FALSE, INFINITE)" in text
    assert "VK_RETURN" in text, "Enter must end the wait"
    # The stop event is created before the camera exists, so Ctrl+C during Start
    # still reaches cleanup rather than killing the process mid-initialisation.
    main = text[text.index("int wmain(") :]
    assert main.index("CreateEventW") < main.index("MFCreateVirtualCamera(")


def test_the_activator_reports_a_broken_attribute_store() -> None:
    header = ACTIVATE_H.read_text(encoding="utf-8")
    source = ACTIVATE_CPP.read_text(encoding="utf-8")
    assert "m_attributeInitHr" in header and "m_attributeInitHr" in source
    assert "m_attributeInitHr = MFCreateAttributes" in source
    activate = body(source, "MediaSourceActivate::ActivateObject", "MediaSourceActivate::DetachObject")
    assert "FAILED(m_attributeInitHr)" in activate


def test_the_dll_refuses_to_unload_while_objects_are_alive() -> None:
    source = ACTIVATE_CPP.read_text(encoding="utf-8")
    unload = body(source, "STDAPI DllCanUnloadNow()", "BOOL WINAPI DllMain")
    assert "GetObjectCount()" in unload
    assert "g_serverLocks" in unload
    # Media Foundation is started outside the loader lock, and DllMain stays empty.
    assert "DllMain" in source
    dllmain = source[source.index("BOOL WINAPI DllMain") :]
    assert "MFStartup" not in dllmain and "MFShutdown" not in dllmain


def test_media_foundation_startup_failure_is_propagated() -> None:
    source = ACTIVATE_CPP.read_text(encoding="utf-8")
    get_class_object = body(source, "STDAPI DllGetClassObject", "STDAPI DllCanUnloadNow")
    assert re.search(r"HRESULT mf = EnsureMediaFoundation\(\)", get_class_object)
    assert "if (FAILED(mf)) return mf;" in get_class_object
