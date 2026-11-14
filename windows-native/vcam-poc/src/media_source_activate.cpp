// PhoneBridge virtual camera - proof of concept, part 2 (implementation).
//
// Lifetime rules that the rest of this file depends on:
//   * Media Foundation is started lazily from DllGetClassObject, never from
//     DllMain (MFStartup loads DLLs and creates threads: both unsafe inside the
//     loader lock), and a failure there is returned rather than swallowed.
//   * DllCanUnloadNow refuses to unload while any object this DLL created is
//     still alive. The frame server asks this question, and answering "yes" too
//     early means it calls into code that no longer exists.
//
// NOT COMPILED: no MSVC and no Windows SDK in the authoring environment.

#include "media_source_activate.h"

#include "media_source.h"

#include <wrl/module.h>

#include <atomic>
#include <mutex>

namespace {

// Media Foundation has to be started in the process that calls MFCreateSample,
// MFCreateEventQueue and friends - that is this DLL's host (the frame server),
// not the host program. Doing it in DllMain is the obvious place and the wrong
// one: MFStartup loads DLLs and creates threads, both of which are unsafe under
// the loader lock. DllGetClassObject runs on a COM thread outside that lock, so
// the startup happens there, once.
std::atomic<bool> g_mfStarted{false};
std::mutex g_mfLock;

// Returns the result of starting Media Foundation in this process. Idempotent: the
// first call starts it, later calls report the same outcome, including failure.
HRESULT EnsureMediaFoundation() {
    std::lock_guard<std::mutex> guard(g_mfLock);
    if (g_mfStarted) return S_OK;
    const HRESULT hr = MFStartup(MF_VERSION, MFSTARTUP_LITE);
    if (SUCCEEDED(hr)) g_mfStarted = true;
    return hr;
}

}  // namespace

namespace phonebridge {

// kSourceClsid / kSourceClsidString are defined once, in the header, so that the
// host executable and this DLL cannot disagree about the CLSID.

MediaSourceActivate::MediaSourceActivate() {
    // Checked, not dropped: this is the store the frame server writes its
    // configuration into, and every forwarded method below depends on it.
    m_attributeInitHr = MFCreateAttributes(&m_attributes, 8);
}

IFACEMETHODIMP MediaSourceActivate::ActivateObject(REFIID iid, void** object) {
    if (!object) return E_POINTER;
    *object = nullptr;
    if (FAILED(m_attributeInitHr) || !m_attributes) {
        // Better a clear failure here than a source that starts and then cannot be
        // configured, which would look like a pipeline fault.
        return FAILED(m_attributeInitHr) ? m_attributeInitHr : E_UNEXPECTED;
    }
    if (m_activated) {
        // Activated twice with a different interface: hand back the same object,
        // which is what a real device enumerator expects.
        return m_activated->QueryInterface(iid, object);
    }
    auto source = Microsoft::WRL::Make<MediaSource>();
    if (!source) return E_OUTOFMEMORY;
    HRESULT hr = source->QueryInterface(iid, object);
    if (FAILED(hr)) return hr;
    m_activated = source;
    return S_OK;
}

IFACEMETHODIMP MediaSourceActivate::DetachObject() {
    m_activated.Reset();
    return S_OK;
}

IFACEMETHODIMP MediaSourceActivate::ShutdownObject() {
    if (!m_activated) return S_OK;
    Microsoft::WRL::ComPtr<IMFMediaSource> source;
    if (SUCCEEDED(m_activated.As(&source))) source->Shutdown();
    m_activated.Reset();
    return S_OK;
}

// ---------------------------------------------------------------------------
// IMFAttributes, forwarded.
// ---------------------------------------------------------------------------
#define PB_FORWARD_WITH(ret_null, call)                       \
    if (!m_attributes) return ret_null;                       \
    return m_attributes->call

IFACEMETHODIMP MediaSourceActivate::GetItem(REFGUID key, PROPVARIANT* value) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, GetItem(key, value));
}
IFACEMETHODIMP MediaSourceActivate::GetItemType(REFGUID key, MF_ATTRIBUTE_TYPE* type) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, GetItemType(key, type));
}
IFACEMETHODIMP MediaSourceActivate::CompareItem(REFGUID key, REFPROPVARIANT value, BOOL* result) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, CompareItem(key, value, result));
}
IFACEMETHODIMP MediaSourceActivate::Compare(IMFAttributes* theirs,
                                            MF_ATTRIBUTES_MATCH_TYPE match, BOOL* result) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, Compare(theirs, match, result));
}
IFACEMETHODIMP MediaSourceActivate::GetUINT32(REFGUID key, UINT32* value) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, GetUINT32(key, value));
}
IFACEMETHODIMP MediaSourceActivate::GetUINT64(REFGUID key, UINT64* value) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, GetUINT64(key, value));
}
IFACEMETHODIMP MediaSourceActivate::GetDouble(REFGUID key, double* value) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, GetDouble(key, value));
}
IFACEMETHODIMP MediaSourceActivate::GetGUID(REFGUID key, GUID* value) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, GetGUID(key, value));
}
IFACEMETHODIMP MediaSourceActivate::GetStringLength(REFGUID key, UINT32* length) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, GetStringLength(key, length));
}
IFACEMETHODIMP MediaSourceActivate::GetString(REFGUID key, LPWSTR value, UINT32 size,
                                              UINT32* length) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, GetString(key, value, size, length));
}
IFACEMETHODIMP MediaSourceActivate::GetAllocatedString(REFGUID key, LPWSTR* value, UINT32* length) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, GetAllocatedString(key, value, length));
}
IFACEMETHODIMP MediaSourceActivate::GetBlobSize(REFGUID key, UINT32* size) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, GetBlobSize(key, size));
}
IFACEMETHODIMP MediaSourceActivate::GetBlob(REFGUID key, UINT8* buffer, UINT32 size,
                                            UINT32* blobSize) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, GetBlob(key, buffer, size, blobSize));
}
IFACEMETHODIMP MediaSourceActivate::GetAllocatedBlob(REFGUID key, UINT8** buffer, UINT32* size) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, GetAllocatedBlob(key, buffer, size));
}
IFACEMETHODIMP MediaSourceActivate::GetUnknown(REFGUID key, REFIID iid, LPVOID* value) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, GetUnknown(key, iid, value));
}
IFACEMETHODIMP MediaSourceActivate::SetItem(REFGUID key, REFPROPVARIANT value) {
    PB_FORWARD_WITH(E_OUTOFMEMORY, SetItem(key, value));
}
IFACEMETHODIMP MediaSourceActivate::DeleteItem(REFGUID key) {
    PB_FORWARD_WITH(E_OUTOFMEMORY, DeleteItem(key));
}
IFACEMETHODIMP MediaSourceActivate::DeleteAllItems() {
    PB_FORWARD_WITH(E_OUTOFMEMORY, DeleteAllItems());
}
IFACEMETHODIMP MediaSourceActivate::SetUINT32(REFGUID key, UINT32 value) {
    PB_FORWARD_WITH(E_OUTOFMEMORY, SetUINT32(key, value));
}
IFACEMETHODIMP MediaSourceActivate::SetUINT64(REFGUID key, UINT64 value) {
    PB_FORWARD_WITH(E_OUTOFMEMORY, SetUINT64(key, value));
}
IFACEMETHODIMP MediaSourceActivate::SetDouble(REFGUID key, double value) {
    PB_FORWARD_WITH(E_OUTOFMEMORY, SetDouble(key, value));
}
IFACEMETHODIMP MediaSourceActivate::SetGUID(REFGUID key, REFGUID value) {
    PB_FORWARD_WITH(E_OUTOFMEMORY, SetGUID(key, value));
}
IFACEMETHODIMP MediaSourceActivate::SetString(REFGUID key, LPCWSTR value) {
    PB_FORWARD_WITH(E_OUTOFMEMORY, SetString(key, value));
}
IFACEMETHODIMP MediaSourceActivate::SetBlob(REFGUID key, const UINT8* buffer, UINT32 size) {
    PB_FORWARD_WITH(E_OUTOFMEMORY, SetBlob(key, buffer, size));
}
IFACEMETHODIMP MediaSourceActivate::SetUnknown(REFGUID key, IUnknown* value) {
    PB_FORWARD_WITH(E_OUTOFMEMORY, SetUnknown(key, value));
}
IFACEMETHODIMP MediaSourceActivate::LockStore() {
    PB_FORWARD_WITH(E_OUTOFMEMORY, LockStore());
}
IFACEMETHODIMP MediaSourceActivate::UnlockStore() {
    PB_FORWARD_WITH(E_OUTOFMEMORY, UnlockStore());
}
IFACEMETHODIMP MediaSourceActivate::GetCount(UINT32* count) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, GetCount(count));
}
IFACEMETHODIMP MediaSourceActivate::GetAllItems(REFGUID* keys, PROPVARIANT* values, UINT32 size,
                                                UINT32* count) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, GetAllItems(keys, values, size, count));
}
IFACEMETHODIMP MediaSourceActivate::CopyAllItems(IMFAttributes* destination) {
    PB_FORWARD_WITH(MF_E_ATTRIBUTENOTFOUND, CopyAllItems(destination));
}

#undef PB_FORWARD_WITH

// ---------------------------------------------------------------------------
// IClassFactory
// ---------------------------------------------------------------------------
IFACEMETHODIMP ClassFactory::CreateInstance(IUnknown* outer, REFIID iid, void** object) {
    if (!object) return E_POINTER;
    *object = nullptr;
    if (outer) return CLASS_E_NOAGGREGATION;
    auto activate = Microsoft::WRL::Make<MediaSourceActivate>();
    if (!activate) return E_OUTOFMEMORY;
    return activate->QueryInterface(iid, object);
}

IFACEMETHODIMP ClassFactory::LockServer(BOOL lock) {
    if (lock) {
        ++g_serverLocks;
    } else {
        --g_serverLocks;
    }
    return S_OK;
}

}  // namespace phonebridge

// ---------------------------------------------------------------------------
// DLL exports — the frame server loads this DLL by CLSID and calls these.
// ---------------------------------------------------------------------------
STDAPI DllGetClassObject(REFCLSID clsid, REFIID iid, void** object) {
    if (!object) return E_POINTER;
    *object = nullptr;
    if (!IsEqualGUID(clsid, phonebridge::kSourceClsid)) return CLASS_E_CLASSNOTAVAILABLE;
    const HRESULT mf = EnsureMediaFoundation();
    if (FAILED(mf)) return mf;  // the frame server gets a real error, not a dead source
    auto factory = Microsoft::WRL::Make<phonebridge::ClassFactory>();
    if (!factory) return E_OUTOFMEMORY;
    return factory->QueryInterface(iid, object);
}

STDAPI DllCanUnloadNow() {
    // Two independent reasons to stay loaded: a factory outstanding from
    // IClassFactory::LockServer, and any RuntimeClass object this DLL created
    // (WRL keeps that count). Unloading with either non-zero lets the frame server
    // call into freed code.
    if (phonebridge::g_serverLocks.load() > 0) return S_FALSE;
    if (Microsoft::WRL::Module<Microsoft::WRL::InProc>::GetModule().GetObjectCount() > 0) {
        return S_FALSE;
    }
    // Nothing of ours is alive, so the platform we started can be shut down.
    if (g_mfStarted.exchange(false)) MFShutdown();
    return S_OK;
}

// Nothing but the bare minimum here: see EnsureMediaFoundation above for why
// Media Foundation is not started in the loader lock.
BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID /*reserved*/) {
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(instance);
    return TRUE;
}
