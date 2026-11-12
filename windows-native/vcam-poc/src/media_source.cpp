// PhoneBridge virtual camera — proof of concept, part 1 (implementation).
//
// See media_source.h for what this is and why it is a test pattern rather than
// the phone. NOT COMPILED: no MSVC and no Windows SDK in the authoring
// environment.

#include "media_source.h"

#include <algorithm>
#include <cstring>

namespace phonebridge {

MediaSource::MediaSource()
    : m_frameDuration(10'000'000LL / kFps) {
    // Created eagerly: the pipeline is allowed to ask for events before Start,
    // and a null queue there would be a crash rather than an error code.
    if (FAILED(MFCreateEventQueue(&m_events))) m_events.Reset();
}

// ---------------------------------------------------------------------------
// IMFMediaEventGenerator
// ---------------------------------------------------------------------------
IFACEMETHODIMP MediaSource::GetEvent(DWORD flags, IMFMediaEvent** event) {
    ComPtr<IMFMediaEventQueue> events;
    {
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        events = m_events;
    }
    if (!events) return MF_E_NOT_INITIALIZED;
    return events->GetEvent(flags, event);
}

IFACEMETHODIMP MediaSource::BeginGetEvent(IMFAsyncCallback* callback, IUnknown* state) {
    ComPtr<IMFMediaEventQueue> events;
    {
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        events = m_events;
    }
    if (!events) return MF_E_NOT_INITIALIZED;
    return events->BeginGetEvent(callback, state);
}

IFACEMETHODIMP MediaSource::EndGetEvent(IMFAsyncResult* result, IMFMediaEvent** event) {
    ComPtr<IMFMediaEventQueue> events;
    {
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        events = m_events;
    }
    if (!events) return MF_E_NOT_INITIALIZED;
    return events->EndGetEvent(result, event);
}

IFACEMETHODIMP MediaSource::QueueEvent(MediaEventType type, REFGUID extendedType,
                                       HRESULT status, const PROPVARIANT* value) {
    ComPtr<IMFMediaEventQueue> events;
    {
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        events = m_events;
    }
    if (!events) return MF_E_NOT_INITIALIZED;
    return events->QueueEventParamVar(type, extendedType, status, value);
}

// ---------------------------------------------------------------------------
// IMFMediaSource
// ---------------------------------------------------------------------------
IFACEMETHODIMP MediaSource::GetCharacteristics(DWORD* characteristics) {
    if (!characteristics) return E_POINTER;
    std::lock_guard<std::mutex> guard(m_lock);
    if (m_shutdown) return MF_E_SHUTDOWN;
    // Live and not seekable. Remote sources will need MFMEDIASOURCE_CAN_PAUSE
    // as well once the phone can actually be paused.
    *characteristics = MFMEDIASOURCE_IS_LIVE;
    return S_OK;
}

IFACEMETHODIMP MediaSource::CreatePresentationDescriptor(IMFPresentationDescriptor** descriptor) {
    if (!descriptor) return E_POINTER;
    *descriptor = nullptr;

    std::lock_guard<std::mutex> guard(m_lock);
    if (m_shutdown) return MF_E_SHUTDOWN;
    if (!m_streamDescriptor) {
        const HRESULT created = CreateStreamDescriptor();
        if (FAILED(created)) return created;
    }

    IMFStreamDescriptor* streams[1] = {m_streamDescriptor.Get()};
    ComPtr<IMFPresentationDescriptor> presentation;
    HRESULT hr = MFCreatePresentationDescriptor(1, streams, &presentation);
    if (FAILED(hr)) return hr;

    // One stream, selected: the pipeline expects the descriptor to say which
    // streams it is about to receive.
    hr = presentation->SelectStream(0);
    if (FAILED(hr)) return hr;
    return presentation.CopyTo(descriptor);
}

IFACEMETHODIMP MediaSource::Start(IMFPresentationDescriptor* descriptor,
                                  const GUID* /*timeFormat*/,
                                  const PROPVARIANT* /*startPosition*/) {
    if (!descriptor) return E_POINTER;

    std::lock_guard<std::mutex> guard(m_lock);
    if (m_shutdown) return MF_E_SHUTDOWN;
    if (!m_events) {
        const HRESULT created = MFCreateEventQueue(&m_events);
        if (FAILED(created)) return created;
    }
    if (!m_streamDescriptor) {
        const HRESULT created = CreateStreamDescriptor();
        if (FAILED(created)) return created;
    }

    // Exactly one stream: take its descriptor from the presentation descriptor
    // rather than trusting our own copy, because that is what the caller will
    // start.
    BOOL selected = FALSE;
    ComPtr<IMFStreamDescriptor> streamDescriptor;
    HRESULT hr = descriptor->GetStreamDescriptorByIndex(0, &selected, &streamDescriptor);
    if (FAILED(hr)) return hr;
    if (!selected) return MF_E_UNEXPECTED;

    m_started = true;
    m_frameIndex = 0;

    // The order matters: new stream, then started, then the first frame.
    // MENewStream carries the stream itself; one object can be both.
    hr = m_events->QueueEventParamUnk(MENewStream, GUID_NULL, S_OK,
                                      static_cast<IMFMediaStream*>(this));
    if (FAILED(hr)) return hr;
    hr = m_events->QueueEventParamVar(MEStreamStarted, GUID_NULL, S_OK, nullptr);
    if (FAILED(hr)) return hr;
    return S_OK;
}

IFACEMETHODIMP MediaSource::Stop() {
    std::lock_guard<std::mutex> guard(m_lock);
    if (m_shutdown) return MF_E_SHUTDOWN;
    m_started = false;
    if (!m_events) return S_OK;
    return m_events->QueueEventParamVar(MEStreamStopped, GUID_NULL, S_OK, nullptr);
}

IFACEMETHODIMP MediaSource::Pause() {
    std::lock_guard<std::mutex> guard(m_lock);
    if (m_shutdown) return MF_E_SHUTDOWN;
    if (!m_events) return S_OK;
    return m_events->QueueEventParamVar(MEStreamPaused, GUID_NULL, S_OK, nullptr);
}

IFACEMETHODIMP MediaSource::Shutdown() {
    ComPtr<IMFMediaEventQueue> events;
    {
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return S_OK;
        m_shutdown = true;
        m_started = false;
        events = m_events;
    }
    if (events) events->Shutdown();
    return S_OK;
}

// ---------------------------------------------------------------------------
// IMFMediaStream
// ---------------------------------------------------------------------------
IFACEMETHODIMP MediaSource::GetMediaSource(IMFMediaSource** source) {
    if (!source) return E_POINTER;
    return QueryInterface(IID_PPV_ARGS(source));
}

IFACEMETHODIMP MediaSource::GetStreamDescriptor(IMFStreamDescriptor** descriptor) {
    if (!descriptor) return E_POINTER;
    *descriptor = nullptr;

    std::lock_guard<std::mutex> guard(m_lock);
    if (m_shutdown) return MF_E_SHUTDOWN;
    if (!m_streamDescriptor) {
        const HRESULT created = CreateStreamDescriptor();
        if (FAILED(created)) return created;
    }
    return m_streamDescriptor.CopyTo(descriptor);
}

IFACEMETHODIMP MediaSource::RequestSample(IUnknown* /*token*/) {
    ComPtr<IMFMediaEventQueue> events;
    LONGLONG index = 0;
    {
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        if (!m_started) return MF_E_MEDIA_SOURCE_NOT_STARTED;
        events = m_events;
        index = m_frameIndex++;
    }
    if (!events) return MF_E_UNEXPECTED;

    ComPtr<IMFSample> sample;
    HRESULT hr = MFCreateSample(&sample);
    if (FAILED(hr)) return hr;

    ComPtr<IMFMediaBuffer> buffer;
    hr = MFCreateMemoryBuffer(kFrameBytes, &buffer);
    if (FAILED(hr)) return hr;

    BYTE* data = nullptr;
    hr = buffer->Lock(&data, nullptr, nullptr);
    if (FAILED(hr)) return hr;
    FillTestPattern(data, index);
    hr = buffer->Unlock();
    if (FAILED(hr)) return hr;

    hr = buffer->SetCurrentLength(kFrameBytes);
    if (FAILED(hr)) return hr;
    hr = sample->AddBuffer(buffer.Get());
    if (FAILED(hr)) return hr;
    hr = sample->SetSampleTime(index * m_frameDuration);
    if (FAILED(hr)) return hr;
    hr = sample->SetSampleDuration(m_frameDuration);
    if (FAILED(hr)) return hr;

    return events->QueueEventParamUnk(MEMediaSample, GUID_NULL, S_OK, sample.Get());
}

// ---------------------------------------------------------------------------
// internals
// ---------------------------------------------------------------------------
HRESULT MediaSource::CreateVideoType(IMFMediaType** type) const {
    ComPtr<IMFMediaType> mediaType;
    HRESULT hr = MFCreateMediaType(&mediaType);
    if (FAILED(hr)) return hr;

    hr = mediaType->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    if (FAILED(hr)) return hr;
    // NV12 is what the Frame Server pipeline prefers; a consumer that wants RGB
    // gets an in-box converter rather than a second copy here.
    hr = mediaType->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
    if (FAILED(hr)) return hr;
    hr = MFSetAttributeSize(mediaType.Get(), MF_MT_FRAME_SIZE, kWidth, kHeight);
    if (FAILED(hr)) return hr;
    hr = MFSetAttributeRatio(mediaType.Get(), MF_MT_FRAME_RATE, kFps, 1);
    if (FAILED(hr)) return hr;
    hr = MFSetAttributeRatio(mediaType.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
    if (FAILED(hr)) return hr;
    hr = mediaType->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    if (FAILED(hr)) return hr;
    // The producer writes 8 bits per channel, 4:2:0, BT.709 limited range; saying
    // so stops the pipeline from guessing wrong about colour.
    hr = mediaType->SetUINT32(MF_MT_YUV_MATRIX, MFVideoTransferMatrix_BT709);
    if (FAILED(hr)) return hr;
    hr = mediaType->SetUINT32(MF_MT_VIDEO_PRIMARIES, MFVideoPrimaries_BT709);
    if (FAILED(hr)) return hr;
    hr = mediaType->SetUINT32(MF_MT_TRANSFER_FUNCTION, MFVideoTransFunc_709);
    if (FAILED(hr)) return hr;
    hr = mediaType->SetUINT32(MF_MT_VIDEO_NOMINAL_RANGE, MFNominalRange_16_235);
    if (FAILED(hr)) return hr;

    return mediaType.CopyTo(type);
}

HRESULT MediaSource::CreateStreamDescriptor() {
    ComPtr<IMFMediaType> mediaType;
    HRESULT hr = CreateVideoType(&mediaType);
    if (FAILED(hr)) return hr;

    IMFMediaType* types[1] = {mediaType.Get()};
    ComPtr<IMFStreamDescriptor> descriptor;
    hr = MFCreateStreamDescriptor(0, 1, types, &descriptor);
    if (FAILED(hr)) return hr;

    ComPtr<IMFMediaTypeHandler> handler;
    hr = descriptor->GetMediaTypeHandler(&handler);
    if (FAILED(hr)) return hr;
    hr = handler->SetCurrentMediaType(mediaType.Get());
    if (FAILED(hr)) return hr;

    m_streamDescriptor = descriptor;
    return S_OK;
}

void MediaSource::FillTestPattern(BYTE* data, LONGLONG frameIndex) const {
    const UINT32 width = kWidth;
    const UINT32 height = kHeight;
    const UINT32 barWidth = std::max<UINT32>(8, width / 16);
    const UINT32 barStart = static_cast<UINT32>(
        (frameIndex * 16) % (static_cast<LONGLONG>(width) + barWidth));

    // Y plane: vertical gradient with a moving light bar — motion is the whole
    // point, because a still picture is indistinguishable from a frozen one.
    for (UINT32 y = 0; y < height; ++y) {
        BYTE* row = data + static_cast<size_t>(y) * width;
        const BYTE base = static_cast<BYTE>(32 + (y * 180) / height);
        std::memset(row, base, width);
        for (UINT32 x = 0; x < barWidth; ++x) {
            const UINT32 px = (barStart + x) % width;
            row[px] = 235;
        }
    }
    // Chroma plane: neutral grey (128), so any colour cast is the consumer's.
    std::memset(data + static_cast<size_t>(width) * height, 128,
                static_cast<size_t>(width) * height / 2);
}

}  // namespace phonebridge
