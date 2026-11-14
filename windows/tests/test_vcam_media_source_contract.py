"""Source-level guards for the native Media Foundation virtual camera.

These checks do not replace an MSVC build or a real Windows camera test. They
prevent known interface and activation regressions from silently returning.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VCAM = ROOT / "windows-native" / "vcam-poc"


def test_activator_uses_the_sdk_imfattributes_index_method() -> None:
    header = (VCAM / "src" / "media_source_activate.h").read_text(encoding="utf-8")
    source = (VCAM / "src" / "media_source_activate.cpp").read_text(encoding="utf-8")

    assert "GetItemByIndex(UINT32 index, GUID* key, PROPVARIANT* value)" in header
    assert "MediaSourceActivate::GetItemByIndex(UINT32 index, GUID* key, PROPVARIANT* value)" in source
    assert "GetAllItems" not in header
    assert "GetAllItems" not in source


def test_activation_retains_source_without_replacing_the_caller_interface() -> None:
    source = (VCAM / "src" / "media_source_activate.cpp").read_text(encoding="utf-8")

    assert "source->QueryInterface(iid, object)" in source
    assert "source.As(&m_activated)" in source
    assert "m_activated = source" not in source
    assert "ActivateObject: retain source as IUnknown" in source


def test_activation_diagnostics_cover_the_com_entry_path() -> None:
    source = (VCAM / "src" / "media_source_activate.cpp").read_text(encoding="utf-8")

    for marker in (
        "DllGetClassObject: requested IID",
        "ClassFactory::CreateInstance: requested IID",
        "ActivateObject: requested IID",
        "ActivateObject: requested-interface QueryInterface",
        "ActivateObject: succeeded",
    ):
        assert marker in source


def test_virtual_camera_dll_links_runtimeobject() -> None:
    cmake = (VCAM / "CMakeLists.txt").read_text(encoding="utf-8")
    dll_link_block = cmake.split("target_link_libraries(PhoneBridgeVcamSource PRIVATE", 1)[1].split(")", 1)[0]

    assert "runtimeobject.lib" in dll_link_block


def test_query_interface_trace_is_on_by_default_and_switchable() -> None:
    """0x80004002 is only diagnosable if the trace is there in the default build."""
    header = (VCAM / "src" / "query_trace.h").read_text(encoding="utf-8")
    cmake = (VCAM / "CMakeLists.txt").read_text(encoding="utf-8")

    assert "#ifndef PB_TRACE_QUERY_INTERFACE" in header
    assert "#define PB_TRACE_QUERY_INTERFACE 1" in header
    assert "option(PB_TRACE_QUERY_INTERFACE" in cmake
    assert cmake.count("PB_TRACE_QUERY_INTERFACE=$<BOOL:${PB_TRACE_QUERY_INTERFACE}>") == 2


def test_every_owned_com_object_traces_query_interface() -> None:
    """The three objects the frame server can QI must all report their answer."""
    override = "STDMETHOD(QueryInterface)(REFIID riid, void** object) noexcept override;"

    source_header = (VCAM / "src" / "media_source.h").read_text(encoding="utf-8")
    activate_header = (VCAM / "src" / "media_source_activate.h").read_text(encoding="utf-8")

    assert "#if PB_TRACE_QUERY_INTERFACE" in source_header
    assert override in source_header
    assert source_header.count(override) == 1
    # MediaSourceActivate and ClassFactory, in that order.
    assert activate_header.count(override) == 2
    assert activate_header.count("#if PB_TRACE_QUERY_INTERFACE") == 2


def test_query_interface_trace_forwards_to_wrl_and_returns_its_answer() -> None:
    """The trace may log; it may not reimplement dispatch or change an HRESULT."""
    source = (VCAM / "src" / "media_source.cpp").read_text(encoding="utf-8")
    activate = (VCAM / "src" / "media_source_activate.cpp").read_text(encoding="utf-8")

    for text, base, label in (
        (source, "MediaSourceRuntime", "MediaSource"),
        (activate, "MediaSourceActivateRuntime", "MediaSourceActivate"),
        (activate, "ClassFactoryRuntime", "ClassFactory"),
    ):
        assert f"const HRESULT hr = {base}::QueryInterface(riid, object);" in text
        assert f'trace::Query(L"{label}", riid, hr);' in text

    # Logged first, forwarded result returned unmodified.
    for text in (source, activate):
        assert "    return hr;\n}" in text


def test_query_interface_trace_does_not_change_the_interface_list() -> None:
    """A diagnostic must not be a way to quietly drop an interface."""
    header = (VCAM / "src" / "media_source.h").read_text(encoding="utf-8")
    runtime = header.split("using MediaSourceRuntime = Microsoft::WRL::RuntimeClass<", 1)[1]

    for interface in (
        "IMFMediaSourceEx",
        "IMFMediaStream2",
        "IMFGetService",
        "IKsControl",
        "IMFSampleAllocatorControl",
    ):
        assert interface in runtime.split(">;", 1)[0]
