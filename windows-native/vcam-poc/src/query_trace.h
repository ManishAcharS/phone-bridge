// PhoneBridge virtual camera — diagnostic: QueryInterface tracing.
//
// Why this exists. `IMFVirtualCamera::Start` fails with E_NOINTERFACE
// (0x80004002) even though every call we already log — activation, the
// requested-interface QI inside ActivateObject, source creation and
// GetSourceAttributes — returns S_OK. Something asked one of our objects for an
// interface it does not expose, and neither the frame server nor Media
// Foundation reports which one. This header records every QueryInterface that
// lands on the three objects we own (media source, activator, class factory),
// resolving the IID to a name where we know one, so the next run on Windows
// names the missing interface instead of leaving us to guess.
//
// It changes no behaviour. Each call is forwarded to WRL's implementation and
// that HRESULT is returned untouched — the log line is the only difference.
// Reference counts and interface identity are unaffected.
//
// It is scaffolding, not part of the design, and is compiled out entirely by
// configuring with -DPB_TRACE_QUERY_INTERFACE=0: both the override declarations
// and their definitions disappear, leaving the objects exactly as they were.
//
// Capture with DebugView running as administrator with "Capture Global Win32"
// enabled, filtered on "[PhoneBridge]". Lines that matter:
//
//   [PhoneBridge] QI MediaSource: MFMediaSourceEx -> 0x00000000
//   [PhoneBridge] QI MISS MediaSource: <guid> -> 0x80004002
//
// A "QI MISS" immediately before Start returns E_NOINTERFACE names the
// interface the frame server wanted and the object it asked.

#pragma once

#include <windows.h>

#include <mfapi.h>
#include <mfobjects.h>
#include <mfidl.h>
//: ks.h declares the KS structures ksproxy.h expects; IKsControl itself lives in
//: ksproxy.h. Both are needed here because this header is included by files that
//: do not otherwise pull in the Windows SDK's streaming headers.
#include <ks.h>
#include <ksproxy.h>

#include <stdio.h>

//: 1 = trace QueryInterface on our COM objects (default), 0 = compile it out.
#ifndef PB_TRACE_QUERY_INTERFACE
#define PB_TRACE_QUERY_INTERFACE 1
#endif

namespace phonebridge {
namespace trace {

//: Diagnostics go to the debugger, never to stderr: this DLL runs inside the
//: frame server's process, which has no console and whose stdout nobody reads.
inline void Line(const wchar_t* text) {
    ::OutputDebugStringW(L"[PhoneBridge] ");
    ::OutputDebugStringW(text);
    ::OutputDebugStringW(L"\n");
}

//: Names for the interfaces a virtual-camera source is realistically asked for.
//: Anything else is printed as a GUID — an unresolved IID is still useful
//: evidence, so an unknown one is never treated as an error.
inline const wchar_t* IidName(REFIID iid) {
    if (iid == __uuidof(IUnknown)) return L"IUnknown";
    if (iid == __uuidof(IClassFactory)) return L"IClassFactory";
    if (iid == __uuidof(IMFAttributes)) return L"IMFAttributes";
    if (iid == __uuidof(IMFActivate)) return L"IMFActivate";
    if (iid == __uuidof(IMFMediaSource)) return L"IMFMediaSource";
    if (iid == __uuidof(IMFMediaSourceEx)) return L"IMFMediaSourceEx";
    if (iid == __uuidof(IMFMediaStream)) return L"IMFMediaStream";
    if (iid == __uuidof(IMFMediaStream2)) return L"IMFMediaStream2";
    if (iid == __uuidof(IMFMediaEventGenerator)) return L"IMFMediaEventGenerator";
    if (iid == __uuidof(IMFGetService)) return L"IMFGetService";
    if (iid == __uuidof(IKsControl)) return L"IKsControl";
    if (iid == __uuidof(IMFSampleAllocatorControl)) return L"IMFSampleAllocatorControl";
    if (iid == __uuidof(IMFPresentationDescriptor)) return L"IMFPresentationDescriptor";
    if (iid == __uuidof(IMFStreamDescriptor)) return L"IMFStreamDescriptor";
    if (iid == __uuidof(IMFMediaTypeHandler)) return L"IMFMediaTypeHandler";
    if (iid == __uuidof(IMFMediaType)) return L"IMFMediaType";
    if (iid == __uuidof(IMFMediaEvent)) return L"IMFMediaEvent";
    if (iid == __uuidof(IMFSample)) return L"IMFSample";
    if (iid == __uuidof(IMFMediaBuffer)) return L"IMFMediaBuffer";
    if (iid == __uuidof(IMF2DBuffer)) return L"IMF2DBuffer";
    if (iid == __uuidof(IMF2DBuffer2)) return L"IMF2DBuffer2";
    if (iid == __uuidof(IMFShutdown)) return L"IMFShutdown";
    return nullptr;
}

//: Formats a GUID by hand rather than with StringFromGUID2: that call is in
//: ole32 and this header is included by objects that must not acquire a new
//: import just because a diagnostic was added.
inline void FormatIid(REFIID iid, wchar_t* buffer, size_t capacity) {
    swprintf_s(buffer, capacity, L"{ %08lX-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X }",
               static_cast<unsigned long>(iid.Data1),
               static_cast<unsigned>(iid.Data2), static_cast<unsigned>(iid.Data3),
               static_cast<unsigned>(iid.Data4[0]), static_cast<unsigned>(iid.Data4[1]),
               static_cast<unsigned>(iid.Data4[2]), static_cast<unsigned>(iid.Data4[3]),
               static_cast<unsigned>(iid.Data4[4]), static_cast<unsigned>(iid.Data4[5]),
               static_cast<unsigned>(iid.Data4[6]), static_cast<unsigned>(iid.Data4[7]));
}

//: One QueryInterface result. `object` names the object that was asked —
//: "MediaSource", "MediaSourceActivate", "ClassFactory" — because the same IID
//: succeeding on one object and failing on another is exactly the distinction
//: that resolves this class of bug.
inline void Query(const wchar_t* object, REFIID iid, HRESULT hr) {
    wchar_t iidText[64] = {};
    const wchar_t* name = IidName(iid);
    if (!name) {
        FormatIid(iid, iidText, 64);
        name = iidText;
    }

    wchar_t line[192] = {};
    swprintf_s(line, FAILED(hr) ? L"QI MISS %ls: %ls -> 0x%08lX" : L"QI %ls: %ls -> 0x%08lX",
               object, name, static_cast<unsigned long>(hr));
    Line(line);
}

}  // namespace trace
}  // namespace phonebridge
