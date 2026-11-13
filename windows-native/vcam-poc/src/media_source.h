// PhoneBridge virtual camera — proof of concept, part 1: the media source.
//
// This is the smallest Media Foundation media source that a Windows 11 software
// virtual camera can be backed with. It has no dependency on the phone, on
// Python, or on PhoneBridge's protocol: it synthesises an NV12 test pattern so
// the *device plumbing* can be proven on its own. If this appears in the Windows
// Camera app and moves, then `MFCreateVirtualCamera` is wired up correctly and
// the only remaining question is where the pixels come from.
//
// It is deliberately single-stream, pull-driven and stateless between frames:
// every `RequestSample` builds one frame from a counter, which is enough to
// prove enumeration and frame delivery and leaves no threads to get wrong.
//
// NOT COMPILED. This container is Linux with no MSVC and no Windows SDK, so this
// file has been written and reviewed but never built. See README.md.

#pragma once

#include <windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mfobjects.h>
#include <mferror.h>
#include <ks.h>   // IKsControl, KSPROPERTY/KSMETHOD/KSEVENT
#include <wrl/client.h>
#include <wrl/implements.h>

#include <atomic>
#include <mutex>

namespace phonebridge {

//: 1280x720 at 30 fps NV12 — the lowest common denominator every consumer
//: accepts. The full implementation will advertise a list instead.
constexpr UINT32 kWidth = 1280;
constexpr UINT32 kHeight = 720;
constexpr UINT32 kFps = 30;

//: One NV12 frame: full-size Y plane plus half-size interleaved UV.
constexpr DWORD kFrameBytes = kWidth * kHeight * 3 / 2;

//: How many buffers to ask the pipeline's allocator for. Same figure as the
//: reference sample (ten 720p NV12 frames, ~14 MB) which is enough to ride out a
//: slow consumer without unbounded growth - RequestSample allocates exactly one
//: buffer per request and never queues more than the allocator holds.
constexpr ULONG kSampleAllocatorBuffers = 10;

//: The single stream this source exposes. Anything else is a caller bug and is
//: rejected with MF_E_INVALIDSTREAMNUMBER rather than silently served stream 0.
constexpr DWORD kStreamId = 0;

//: PINNAME_VIDEO_CAPTURE, the stream category the frame server expects for a
//: colour capture stream. Written out rather than included from <ks.h> so this
//: file does not depend on the kernel-streaming headers being on the include path.
//: {FB6C4281-0353-11D1-905F-0000C0CC16BA}
constexpr GUID kPinNameVideoCapture = {
    0xfb6c4281, 0x0353, 0x11d1, {0x90, 0x5f, 0x00, 0x00, 0xc0, 0xcc, 0x16, 0xba}};

using Microsoft::WRL::ComPtr;

// ---------------------------------------------------------------------------
// The media source and its single stream are one object on purpose: a
// one-stream source that answers `GetMediaSource()` with itself is legal, and it
// halves the amount of COM boilerplate in a proof of concept.
//
// The interface list is taken from Microsoft's reference implementation rather
// than guessed, because the frame server queries for these and reports every miss
// as the same 0x80004002 E_NOINTERFACE from `IMFVirtualCamera::Start`:
//
//   microsoft/Windows-Camera  Samples/VirtualCamera/VirtualCameraMediaSource
//   SimpleMediaSource : winrt::implements<SimpleMediaSource,
//       IMFMediaSourceEx, IMFGetService, IKsControl, IMFSampleAllocatorControl>
//   SimpleMediaStream : winrt::implements<SimpleMediaStream, IMFMediaStream2>
//
// So: IMFMediaSourceEx (which brings IMFMediaSource and IMFMediaEventGenerator),
// IMFMediaStream2 (which brings IMFMediaStream), plus the three "control"
// interfaces. None of them are decoration here - each one has a real, if small,
// implementation below:
//
//   IMFGetService              no services to offer, so MF_E_UNSUPPORTED_SERVICE.
//                              The QI has to succeed even so: the frame server
//                              goes through MFGetService, which QIs first, and a
//                              failed QI is indistinguishable from a fatal error.
//   IKsControl                 no KS controls, so ERROR_SET_NOT_FOUND - what an
//                              AVStream miniport returns for a control it does
//                              not register. Reporting "not supported" honestly
//                              is what stops a caller waiting for a reply.
//   IMFSampleAllocatorControl  reports that samples come from the allocator the
//                              pipeline provides (UsesProvidedAllocator), which
//                              is what RequestSample does when one was set.
//
// The stream is the same object as the source, so IMFMediaStream2 is listed here
// as well. One object answering both halves is legal (GetMediaSource returns it)
// and keeps the proof of concept to a single class.
// ---------------------------------------------------------------------------
class MediaSource
    : public Microsoft::WRL::RuntimeClass<
          Microsoft::WRL::RuntimeClassFlags<Microsoft::WRL::ClassicCom>,
          IMFMediaSourceEx,
          IMFMediaStream2,
          IMFGetService,
          IKsControl,
          IMFSampleAllocatorControl> {
public:
    MediaSource();

    // IMFMediaEventGenerator
    IFACEMETHODIMP GetEvent(DWORD flags, IMFMediaEvent** event) override;
    IFACEMETHODIMP BeginGetEvent(IMFAsyncCallback* callback, IUnknown* state) override;
    IFACEMETHODIMP EndGetEvent(IMFAsyncResult* result, IMFMediaEvent** event) override;
    IFACEMETHODIMP QueueEvent(MediaEventType type, REFGUID extendedType,
                              HRESULT status, const PROPVARIANT* value) override;

    // IMFMediaSource
    IFACEMETHODIMP GetCharacteristics(DWORD* characteristics) override;
    IFACEMETHODIMP CreatePresentationDescriptor(IMFPresentationDescriptor** descriptor) override;
    IFACEMETHODIMP Start(IMFPresentationDescriptor* descriptor, const GUID* timeFormat,
                         const PROPVARIANT* startPosition) override;
    IFACEMETHODIMP Stop() override;
    IFACEMETHODIMP Pause() override;
    IFACEMETHODIMP Shutdown() override;

    // IMFMediaSourceEx
    //: Source-level attribute store. Must be a live store at any time after
    //: construction, because the pipeline reads it before Start.
    IFACEMETHODIMP GetSourceAttributes(IMFAttributes** attributes) override;
    //: Stream-level attribute store. `streamId` must be kStreamId.
    IFACEMETHODIMP GetStreamAttributes(DWORD streamId, IMFAttributes** attributes) override;
    //: No D3D path here: frames are CPU NV12, so the default system-memory
    //: allocator is the correct answer and E_NOTIMPL requests exactly that.
    IFACEMETHODIMP SetD3DManager(IUnknown* manager) override;

    // IMFMediaStream2 (which inherits IMFMediaStream)
    IFACEMETHODIMP GetMediaSource(IMFMediaSource** source) override;
    IFACEMETHODIMP GetStreamDescriptor(IMFStreamDescriptor** descriptor) override;
    IFACEMETHODIMP RequestSample(IUnknown* token) override;
    //: MF_STREAM_STATE_RUNNING/STOPPED drive whether RequestSample produces a
    //: frame; the frame server uses these instead of source Stop when it is
    //: sharing one source between several clients.
    IFACEMETHODIMP SetStreamState(MF_STREAM_STATE state) override;
    IFACEMETHODIMP GetStreamState(MF_STREAM_STATE* state) override;

    // IMFGetService
    //: Never returns a service: the camera has no evr/accelerator service to
    //: hand out. MF_E_UNSUPPORTED_SERVICE, not E_NOINTERFACE - see the class
    //: comment, this is why the interface is here at all.
    IFACEMETHODIMP GetService(REFGUID service, REFIID riid, LPVOID* object) override;

    // IKsControl
    //: All three report ERROR_SET_NOT_FOUND: the source registers no custom KS
    //: property, method or event set, and that is the code an AVStream driver
    //: returns for a control it does not handle.
    IFACEMETHODIMP KsProperty(PKSPROPERTY property, ULONG propertyLength, LPVOID propertyData,
                              ULONG dataLength, ULONG* bytesReturned) override;
    IFACEMETHODIMP KsMethod(PKSMETHOD method, ULONG methodLength, LPVOID methodData,
                            ULONG dataLength, ULONG* bytesReturned) override;
    IFACEMETHODIMP KsEvent(PKSEVENT event, ULONG eventLength, LPVOID eventData,
                           ULONG dataLength, ULONG* bytesReturned) override;

    // IMFSampleAllocatorControl
    //: The pipeline hands us the allocator it wants frames written into. Storing
    //: it is what makes the shared frame-server path work without a copy.
    IFACEMETHODIMP SetDefaultAllocator(DWORD outputStreamId, IUnknown* allocator) override;
    IFACEMETHODIMP GetAllocatorUsage(DWORD outputStreamId, DWORD* inputStreamId,
                                     MFSampleAllocatorUsage* usage) override;

private:
    ~MediaSource() override = default;

    HRESULT CreateVideoType(IMFMediaType** type) const;
    //: Creates the stream attribute store if it does not exist yet. Idempotent.
    HRESULT CreateStreamAttributes();
    HRESULT CreateStreamDescriptor();
    //: Writes the pattern into a sample's first buffer, whichever way that buffer
    //: wants to be locked: 2D surfaces through Lock2DSize so the pitch is honoured,
    //: plain system memory through Lock. Returns the error from the frame content
    //: or from the unlock, never both silently.
    HRESULT FillSample(IMFSample* sample, LONGLONG frameIndex) const;
    //: Fills an already-locked NV12 buffer. `stride` is the row pitch in bytes and
    //: may be larger than the width; anything shorter than one row is rejected
    //: instead of writing past the end. The caller owns the Lock/Unlock pair.
    HRESULT FillTestPattern(BYTE* data, LONG stride, LONGLONG frameIndex) const;

    std::mutex m_lock;
    ComPtr<IMFMediaEventQueue> m_events;
    ComPtr<IMFStreamDescriptor> m_streamDescriptor;
    //: Attribute stores handed out through IMFMediaSourceEx. Owned here, one
    //: reference each, released in Shutdown() so a shutdown source cannot keep
    //: the pipeline's configuration alive.
    ComPtr<IMFAttributes> m_sourceAttributes;
    ComPtr<IMFAttributes> m_streamAttributes;
    //: Allocator supplied through IMFSampleAllocatorControl.
    ComPtr<IMFVideoSampleAllocator> m_sampleAllocator;
    bool m_allocatorInitialized = false;
    std::atomic<bool> m_shutdown{false};
    MF_STREAM_STATE m_streamState = MF_STREAM_STATE_STOPPED;
    LONGLONG m_frameIndex = 0;
    LONGLONG m_frameDuration = 0;  // 100 ns units
};

}  // namespace phonebridge
