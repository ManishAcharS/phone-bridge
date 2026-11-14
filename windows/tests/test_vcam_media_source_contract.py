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
