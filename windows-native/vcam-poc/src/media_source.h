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

using Microsoft::WRL::ComPtr;

// ---------------------------------------------------------------------------
// The media source and its single stream are one object on purpose: a
// one-stream source that answers `GetMediaSource()` with itself is legal, and it
// halves the amount of COM boilerplate in a proof of concept.
// ---------------------------------------------------------------------------
class MediaSource
    : public Microsoft::WRL::RuntimeClass<
          Microsoft::WRL::RuntimeClassFlags<Microsoft::WRL::ClassicCom>,
          IMFMediaSource,
          IMFMediaEventGenerator,
          IMFMediaStream> {
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

    // IMFMediaStream
    IFACEMETHODIMP GetMediaSource(IMFMediaSource** source) override;
    IFACEMETHODIMP GetStreamDescriptor(IMFStreamDescriptor** descriptor) override;
    IFACEMETHODIMP RequestSample(IUnknown* token) override;

private:
    ~MediaSource() override = default;

    HRESULT CreateVideoType(IMFMediaType** type) const;
    HRESULT CreateStreamDescriptor();
    //: Fills an already-locked NV12 buffer. The caller owns the Lock/Unlock
    //: pair: locking the same buffer twice and unlocking once is a leak at best.
    void FillTestPattern(BYTE* data, LONGLONG frameIndex) const;

    std::mutex m_lock;
    ComPtr<IMFMediaEventQueue> m_events;
    ComPtr<IMFStreamDescriptor> m_streamDescriptor;
    std::atomic<bool> m_shutdown{false};
    bool m_started = false;
    LONGLONG m_frameIndex = 0;
    LONGLONG m_frameDuration = 0;  // 100 ns units
};

}  // namespace phonebridge
