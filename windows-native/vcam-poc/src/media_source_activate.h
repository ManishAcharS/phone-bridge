// PhoneBridge virtual camera — proof of concept, part 2: the COM entry point.
//
// The frame server does not create the media source directly: it looks up the
// CLSID it was given and asks *that* object for one. The object must implement
// `IMFActivate` (whose `ActivateObject` returns an `IMFMediaSource`) and, because
// `IMFActivate` inherits from it, `IMFAttributes`. This is that object: a thin
// activator that owns a real attribute store (the pipeline writes configuration
// into it) and creates the source on demand.
//
// NOT COMPILED. See README.md.

#pragma once

#include <windows.h>

#include <atomic>

#include <mfapi.h>
#include <mfidl.h>
#include <mfobjects.h>
#include <wrl/client.h>
#include <wrl/implements.h>

namespace phonebridge {

//: The CLSID of the custom media source, defined here exactly once: the class
//: factory answers to it, the host builds `HKCU\Software\Classes\CLSID\<string>`
//: from it and hands the string to `MFCreateVirtualCamera`. A second copy of this
//: literal anywhere is a defect, because registration and activation would then be
//: able to drift apart silently.
//:
//: These are `inline constexpr` rather than `extern` definitions in the DLL: the
//: host executable links neither the DLL nor its import library, so a symbol defined
//: only in the media source would be an unresolved external at link time.
//:
//: {7F1B3C1E-9D62-4C2B-9E7A-5B4C0D2A6F31}
inline constexpr GUID kSourceClsid = {
    0x7f1b3c1e, 0x9d62, 0x4c2b, {0x9e, 0x7a, 0x5b, 0x4c, 0x0d, 0x2a, 0x6f, 0x31}};
inline constexpr wchar_t kSourceClsidString[] = L"{7F1B3C1E-9D62-4C2B-9E7A-5B4C0D2A6F31}";

//: Non-zero while something holds the class factory, so `DllCanUnloadNow` can
//: refuse to unload a DLL the frame server may still call into.
inline std::atomic<long> g_serverLocks{0};

class MediaSourceActivate
    : public Microsoft::WRL::RuntimeClass<
          Microsoft::WRL::RuntimeClassFlags<Microsoft::WRL::ClassicCom>,
          IMFActivate> {
public:
    MediaSourceActivate();

    // IMFActivate
    IFACEMETHODIMP ActivateObject(REFIID iid, void** object) override;
    IFACEMETHODIMP DetachObject() override;
    IFACEMETHODIMP ShutdownObject() override;

    // IMFAttributes — forwarded to a real store rather than ignored: the frame
    // server writes configuration here and reading it back is part of the
    // contract. Unimplemented methods would make a failure look like ours.
    IFACEMETHODIMP GetItem(REFGUID key, PROPVARIANT* value) override;
    IFACEMETHODIMP GetItemType(REFGUID key, MF_ATTRIBUTE_TYPE* type) override;
    IFACEMETHODIMP CompareItem(REFGUID key, REFPROPVARIANT value, BOOL* result) override;
    IFACEMETHODIMP Compare(IMFAttributes* theirs, MF_ATTRIBUTES_MATCH_TYPE match, BOOL* result)
        override;
    IFACEMETHODIMP GetUINT32(REFGUID key, UINT32* value) override;
    IFACEMETHODIMP GetUINT64(REFGUID key, UINT64* value) override;
    IFACEMETHODIMP GetDouble(REFGUID key, double* value) override;
    IFACEMETHODIMP GetGUID(REFGUID key, GUID* value) override;
    IFACEMETHODIMP GetStringLength(REFGUID key, UINT32* length) override;
    IFACEMETHODIMP GetString(REFGUID key, LPWSTR value, UINT32 size, UINT32* length) override;
    IFACEMETHODIMP GetAllocatedString(REFGUID key, LPWSTR* value, UINT32* length) override;
    IFACEMETHODIMP GetBlobSize(REFGUID key, UINT32* size) override;
    IFACEMETHODIMP GetBlob(REFGUID key, UINT8* buffer, UINT32 size, UINT32* blobSize) override;
    IFACEMETHODIMP GetAllocatedBlob(REFGUID key, UINT8** buffer, UINT32* size) override;
    IFACEMETHODIMP GetUnknown(REFGUID key, REFIID iid, LPVOID* value) override;
    IFACEMETHODIMP SetItem(REFGUID key, REFPROPVARIANT value) override;
    IFACEMETHODIMP DeleteItem(REFGUID key) override;
    IFACEMETHODIMP DeleteAllItems() override;
    IFACEMETHODIMP SetUINT32(REFGUID key, UINT32 value) override;
    IFACEMETHODIMP SetUINT64(REFGUID key, UINT64 value) override;
    IFACEMETHODIMP SetDouble(REFGUID key, double value) override;
    IFACEMETHODIMP SetGUID(REFGUID key, REFGUID value) override;
    IFACEMETHODIMP SetString(REFGUID key, LPCWSTR value) override;
    IFACEMETHODIMP SetBlob(REFGUID key, const UINT8* buffer, UINT32 size) override;
    IFACEMETHODIMP SetUnknown(REFGUID key, IUnknown* value) override;
    IFACEMETHODIMP LockStore() override;
    IFACEMETHODIMP UnlockStore() override;
    IFACEMETHODIMP GetCount(UINT32* count) override;
    IFACEMETHODIMP GetItemByIndex(UINT32 index, GUID* key, PROPVARIANT* value) override;
    IFACEMETHODIMP CopyAllItems(IMFAttributes* destination) override;

private:
    ~MediaSourceActivate() override = default;

    Microsoft::WRL::ComPtr<IMFAttributes> m_attributes;
    Microsoft::WRL::ComPtr<IUnknown> m_activated;
    //: Result of creating the attribute store in the constructor. Recorded rather
    //: than dropped: an activator with no store would silently answer every
    //: configuration query with MF_E_ATTRIBUTENOTFOUND and look like a pipeline bug.
    HRESULT m_attributeInitHr = E_PENDING;
};

//: The class factory `DllGetClassObject` returns.
class ClassFactory
    : public Microsoft::WRL::RuntimeClass<
          Microsoft::WRL::RuntimeClassFlags<Microsoft::WRL::ClassicCom>,
          IClassFactory> {
public:
    IFACEMETHODIMP CreateInstance(IUnknown* outer, REFIID iid, void** object) override;
    IFACEMETHODIMP LockServer(BOOL lock) override;
};

}  // namespace phonebridge
