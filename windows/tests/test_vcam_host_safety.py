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
SOURCE_H = POC / "src" / "media_source.h"
SOURCE_CPP = POC / "src" / "media_source.cpp"

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


# ---------------------------------------------------------------------------
# The media source must be an IMFMediaSourceEx
#
# A real run failed with `IMFVirtualCamera::Start` returning 0x80004002
# (E_NOINTERFACE) because the object only implemented IMFMediaSource. The frame
# server queries for the Ex interface while configuring the pipeline. These tests
# pin the interface surface and the attribute stores that interface exposes.
# ---------------------------------------------------------------------------


def test_the_media_source_is_an_IMFMediaSourceEx() -> None:
    header = SOURCE_H.read_text(encoding="utf-8")
    base_list = body(header, "class MediaSource", "public:")
    assert "IMFMediaSourceEx" in base_list
    assert "IMFMediaStream" in base_list
    # IMFMediaSourceEx inherits IMFMediaEventGenerator; listing it as well would
    # only hide the fact that one interface answers all three queries.
    assert "IMFMediaEventGenerator" not in base_list, (
        "IMFMediaEventGenerator is inherited from IMFMediaSourceEx and must not be "
        "listed separately"
    )


def test_the_source_implements_every_IMFMediaSourceEx_method() -> None:
    header = SOURCE_H.read_text(encoding="utf-8")
    source = SOURCE_CPP.read_text(encoding="utf-8")
    for method in ("GetSourceAttributes", "GetStreamAttributes", "SetD3DManager"):
        assert f"IFACEMETHODIMP {method}(" in header, f"{method} missing from the header"
        assert f"IFACEMETHODIMP MediaSource::{method}(" in source, (
            f"{method} declared but not implemented"
        )
    # Plain IMFMediaSource behaviour has to survive the change.
    for method in ("GetEvent", "QueueEvent", "CreatePresentationDescriptor", "Start",
                   "Stop", "Pause", "Shutdown", "RequestSample", "GetStreamDescriptor"):
        assert f"IFACEMETHODIMP {method}(" in header


def test_set_d3d_manager_declines_support() -> None:
    source = SOURCE_CPP.read_text(encoding="utf-8")
    implementation = body(source, "MediaSource::SetD3DManager", "// ------")
    assert "return E_NOTIMPL;" in implementation, (
        "claiming D3D support without honouring the manager would hang the pipeline"
    )


def test_stream_attributes_describe_a_colour_capture_stream() -> None:
    source = SOURCE_CPP.read_text(encoding="utf-8")
    create = body(source, "HRESULT MediaSource::CreateStreamAttributes()",
                  "HRESULT MediaSource::CreateStreamDescriptor()")
    for key in (
        "MF_DEVICESTREAM_STREAM_ID",
        "MF_DEVICESTREAM_STREAM_CATEGORY",
        "MF_DEVICESTREAM_FRAMESERVER_SHARED",
        "MF_DEVICESTREAM_ATTRIBUTE_FRAMESOURCE_TYPES",
    ):
        assert key in create, f"{key} must be set on the stream attribute store"
    assert "MFFrameSourceTypes_Color" in create
    assert "kPinNameVideoCapture" in create, "PINNAME_VIDEO_CAPTURE is the capture category"
    # Every Set call is result-checked, so a failed configuration cannot look like
    # a working source.
    unchecked = [
        line.strip()
        for line in create.splitlines()
        if "attributes->Set" in line and not line.strip().startswith("hr = ")
    ]
    assert not unchecked, f"attribute result discarded: {unchecked}"


def test_an_invalid_stream_id_is_rejected() -> None:
    source = SOURCE_CPP.read_text(encoding="utf-8")
    implementation = body(source, "MediaSource::GetStreamAttributes", "MediaSource::SetD3DManager")
    assert "streamId != kStreamId" in implementation
    assert "MF_E_INVALIDSTREAMNUMBER" in implementation
    header = SOURCE_H.read_text(encoding="utf-8")
    assert "constexpr DWORD kStreamId = 0;" in header


def test_attribute_stores_are_released_on_shutdown() -> None:
    source = SOURCE_CPP.read_text(encoding="utf-8")
    shutdown = body(source, "MediaSource::Shutdown()", "// IMFMediaSourceEx")
    assert "m_sourceAttributes.Reset()" in shutdown
    assert "m_streamAttributes.Reset()" in shutdown
    # ...and are never handed out after shutdown.
    for method in ("MediaSource::GetSourceAttributes", "MediaSource::GetStreamAttributes"):
        implementation = body(source, method, "// ------")
        assert "MF_E_SHUTDOWN" in implementation


def test_attribute_stores_are_built_on_demand_if_media_foundation_was_late() -> None:
    # The stores are normally built in the constructor, but the frame server reads
    # them at Start. A getter that merely failed would turn a late MFStartup into a
    # Start failure with no hint about the cause, so both getters rebuild instead.
    source = SOURCE_CPP.read_text(encoding="utf-8")
    getter = body(source, "MediaSource::GetSourceAttributes", "// ------")
    assert "MFCreateAttributes(&m_sourceAttributes" in getter
    stream_getter = body(source, "MediaSource::GetStreamAttributes", "MediaSource::SetD3DManager")
    assert "CreateStreamAttributes()" in stream_getter


# ---------------------------------------------------------------------------
# Interface parity with Microsoft's reference implementation
#
# Even after IMFMediaSourceEx was added, `IMFVirtualCamera::Start` still failed
# with 0x80004002. The reference sample (microsoft/Windows-Camera,
# Samples/VirtualCamera/VirtualCameraMediaSource) implements a wider set:
#
#   SimpleMediaSource : IMFMediaSourceEx, IMFGetService, IKsControl,
#                       IMFSampleAllocatorControl
#   SimpleMediaStream : IMFMediaStream2
#
# and its own tests QI every one of them. The tests below pin the set and the
# behaviour behind each, so none of them can be dropped back to "close enough".
# ---------------------------------------------------------------------------

REFERENCE_SOURCE_INTERFACES = (
    "IMFMediaSourceEx",
    "IMFGetService",
    "IKsControl",
    "IMFSampleAllocatorControl",
)


def source_base_list(header: str) -> str:
    return body(header, "class MediaSource", "public:")


def method_body(name: str, end_marker: str = "// ---") -> str:
    return body(SOURCE_CPP.read_text(encoding="utf-8"), name, end_marker)


def test_the_source_exposes_the_reference_interface_set() -> None:
    base_list = source_base_list(SOURCE_H.read_text(encoding="utf-8"))
    for interface in REFERENCE_SOURCE_INTERFACES:
        assert interface in base_list, (
            f"{interface} is in Microsoft's SimpleMediaSource; leaving it out of a "
            "virtual-camera source is what Start reports as E_NOINTERFACE"
        )
    # The reference stream is an IMFMediaStream2, not a bare IMFMediaStream.
    assert "IMFMediaStream2" in base_list
    assert "IMFMediaEventGenerator" not in base_list, "inherited, not listed separately"


def test_every_declared_interface_method_is_implemented() -> None:
    header = SOURCE_H.read_text(encoding="utf-8")
    source = SOURCE_CPP.read_text(encoding="utf-8")
    declared = set(re.findall(r"IFACEMETHODIMP (\w+)\(", header))
    defined = set(re.findall(r"IFACEMETHODIMP MediaSource::(\w+)\(", source))
    assert declared == defined, f"declared but not defined: {sorted(declared - defined)}"
    # 4 event generator + 6 IMFMediaSource + 3 Ex + 5 stream + 1 GetService +
    # 3 IKsControl + 2 allocator control.
    assert len(declared) == 24, "the interface surface changed: update this test with it"


def test_get_service_declines_without_failing_the_query() -> None:
    implementation = method_body("MediaSource::GetService")
    assert "MF_E_UNSUPPORTED_SERVICE" in implementation
    # The QI has to succeed - that is the whole reason the interface is there - so
    # the method itself must answer "no such service", not "no such interface".
    assert "E_NOINTERFACE" not in implementation
    assert "E_NOTIMPL" not in implementation


def test_ks_control_reports_an_unregistered_control() -> None:
    for method in ("KsProperty", "KsMethod", "KsEvent"):
        implementation = method_body(f"MediaSource::{method}")
        assert "HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND)" in implementation, (
            f"{method} must answer the way an AVStream miniport does for a control "
            "it does not register"
        )
        assert "E_INVALIDARG" in implementation, f"{method} must validate its header"
    # No custom property set: the source would be lying if it named one.
    assert "PROPSETID_SIMPLEMEDIASOURCE_CUSTOMCONTROL" not in SOURCE_CPP.read_text(
        encoding="utf-8"
    )


def test_allocator_usage_matches_what_request_sample_does() -> None:
    usage = method_body("MediaSource::GetAllocatorUsage")
    assert "MFSampleAllocatorUsage_UsesProvidedAllocator" in usage
    assert "*inputStreamId = outputStreamId;" in usage
    assert "MF_E_INVALIDSTREAMNUMBER" in usage

    # ...and the promise is kept: RequestSample allocates from that allocator, and
    # only falls back to its own memory when none was supplied.
    request = method_body("MediaSource::RequestSample")
    assert "AllocateSample" in request
    assert "MFCreateMemoryBuffer" in request
    assert request.index("AllocateSample") < request.index("MFCreateMemoryBuffer")
    assert "MF_E_INVALIDREQUEST" in request, "a stopped stream must not produce frames"


def test_set_default_allocator_stores_the_allocator_it_is_given() -> None:
    implementation = method_body("MediaSource::SetDefaultAllocator")
    assert "IMFVideoSampleAllocator" in implementation
    assert "m_sampleAllocator = provided;" in implementation
    assert "m_allocatorInitialized = false;" in implementation, "must be initialised again"
    assert "MF_E_INVALIDREQUEST" in implementation, "no swapping mid-stream"

    start = body(
        SOURCE_CPP.read_text(encoding="utf-8"),
        "IFACEMETHODIMP MediaSource::Start(",
        "IFACEMETHODIMP MediaSource::Stop()",
    )
    assert "InitializeSampleAllocator(" in start, (
        "AllocateSample fails until the allocator is initialised for our media type"
    )
    assert "kSampleAllocatorBuffers" in start


def test_shutdown_releases_the_pipeline_allocator() -> None:
    shutdown = method_body("MediaSource::Shutdown()", "// IMFMediaSourceEx")
    assert "m_sampleAllocator.Reset()" in shutdown
    assert "m_allocatorInitialized = false" in shutdown


def test_start_sends_the_events_the_pipeline_waits_for() -> None:
    start = body(
        SOURCE_CPP.read_text(encoding="utf-8"),
        "IFACEMETHODIMP MediaSource::Start(",
        "IFACEMETHODIMP MediaSource::Stop()",
    )
    # Assert the call that queues the event, not the name: a comment mentioning it
    # is exactly the kind of evidence that lets a regression through.
    for queued in (
        "QueueEventParamUnk(MENewStream",
        "QueueEventParamVar(MEStreamStarted",
        "QueueEventParamVar(MESourceStarted",
    ):
        assert queued in start, f"{queued}...) missing: Start succeeds and the pipeline waits"
    assert start.index("MENewStream") < start.index("QueueEventParamVar(MEStreamStarted")
    assert start.index("MEStreamStarted") < start.index("QueueEventParamVar(MESourceStarted")
    # The stream pointer must come from QueryInterface: converting `this` to
    # IUnknown* is ambiguous with four interface roots and does not compile.
    assert "static_cast<IMFMediaStream*>(this)" not in start
    assert "QueryInterface" in start
    assert "MF_E_UNSUPPORTED_TIME_FORMAT" in start
    assert "E_INVALIDARG" in start, "a presentation descriptor of the wrong shape"


def test_request_sample_echoes_the_callers_token() -> None:
    request = method_body("MediaSource::RequestSample")
    assert "MFSampleExtension_Token" in request


def test_every_sample_buffer_is_filled_through_its_real_pitch() -> None:
    source = SOURCE_CPP.read_text(encoding="utf-8")
    fill = body(source, "HRESULT MediaSource::FillSample(", "HRESULT MediaSource::FillTestPattern(")
    assert "Lock2DSize" in fill, "allocator surfaces are padded; width-striding shears them"
    assert "Unlock2D" in fill, "every Lock2DSize needs its Unlock2D, error path included"
    assert "buffer->Lock(" in fill, "plain system-memory buffers still have to work"

    pattern = body(source, "HRESULT MediaSource::FillTestPattern(", "}  // namespace")
    assert "if (!data) return E_POINTER;" in pattern
    assert "< static_cast<LONG>(kWidth)" in pattern, "a short stride would write past the end"
    assert "stride" in pattern


# ---------------------------------------------------------------------------
# Spellings that the Windows SDK build rejected
#
# The first real compile of this source failed on three identifiers that read
# plausibly but do not exist. They are pinned here so the *documented-looking*
# spelling cannot come back: each one is a compile error, not a style choice.
# ---------------------------------------------------------------------------


def test_ks_control_comes_from_the_user_mode_header() -> None:
    header = SOURCE_H.read_text(encoding="utf-8")
    includes = body(header, "#include <windows.h>", "using Microsoft::WRL::ComPtr")
    assert "#include <ks.h>" in includes, "the KS structures come from ks.h"
    assert "#include <ksproxy.h>" in includes, (
        "IKsControl is declared in ksproxy.h; ks.h alone leaves it undeclared"
    )
    assert includes.index("<ks.h>") < includes.index("<ksproxy.h>"), (
        "ksproxy.h expects the KS structures to be defined first"
    )


def test_frame_source_type_uses_the_sdk_spelling() -> None:
    source = SOURCE_CPP.read_text(encoding="utf-8")
    assert "MFFrameSourceTypes_Color" in source
    assert "MF_FRAMESOURCE_TYPES_COLOR" not in source, (
        "MF_FRAMESOURCE_TYPES_COLOR does not exist in the SDK"
    )


def test_allocator_usage_uses_a_documented_enum_member() -> None:
    source = SOURCE_CPP.read_text(encoding="utf-8")
    assert "MFSampleAllocatorUsage_AllocatesSamples" not in source, (
        "the SDK's MFSampleAllocatorUsage has no such member"
    )
    usage = method_body("MediaSource::GetAllocatorUsage")
    members = re.findall(r"MFSampleAllocatorUsage_\w+", usage)
    assert members == ["MFSampleAllocatorUsage_UsesProvidedAllocator"], (
        f"unexpected allocator usage members: {sorted(set(members))}"
    )
