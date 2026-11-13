// PhoneBridge virtual camera — proof of concept, part 1 (implementation).
//
// See media_source.h for what this is and why it is a test pattern rather than
// the phone. NOT COMPILED: no MSVC and no Windows SDK in the authoring
// environment.

#include "media_source.h"

#include <algorithm>
#include <cstddef>
#include <cstring>

namespace phonebridge {

MediaSource::MediaSource()
    : m_frameDuration(10'000'000LL / kFps) {
    // Created eagerly: the pipeline is allowed to ask for events before Start,
    // and a null queue there would be a crash rather than an error code.
    if (FAILED(MFCreateEventQueue(&m_events))) m_events.Reset();
    // Same reason for both attribute stores: IMFMediaSourceEx is queried and read
    // while the frame server configures the camera, before Start is ever called.
    if (FAILED(MFCreateAttributes(&m_sourceAttributes, 2))) m_sourceAttributes.Reset();
    if (FAILED(CreateStreamAttributes())) m_streamAttributes.Reset();
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
                                  const GUID* timeFormat,
                                  const PROPVARIANT* /*startPosition*/) {
    if (!descriptor) return E_POINTER;
    // A live source only understands the default time format. Saying so is what
    // the reference sample does; silently ignoring a format we cannot honour
    // would leave the caller with a running source and the wrong timeline.
    if (timeFormat && *timeFormat != GUID_NULL) return MF_E_UNSUPPORTED_TIME_FORMAT;

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

    // One stream: a presentation descriptor describing any other topology is not
    // ours, and starting it would be a promise we cannot keep.
    DWORD streamCount = 0;
    HRESULT hr = descriptor->GetStreamDescriptorCount(&streamCount);
    if (FAILED(hr)) return hr;
    if (streamCount != 1) return E_INVALIDARG;

    BOOL selected = FALSE;
    ComPtr<IMFStreamDescriptor> streamDescriptor;
    hr = descriptor->GetStreamDescriptorByIndex(0, &selected, &streamDescriptor);
    if (FAILED(hr)) return hr;
    if (!selected) return MF_E_UNEXPECTED;

    // The pipeline supplies the allocator frames should be written into. It has to
    // be initialised for our media type before the first AllocateSample, which is
    // what the reference sample does in StartInternal.
    if (m_sampleAllocator && !m_allocatorInitialized) {
        ComPtr<IMFMediaType> mediaType;
        hr = CreateVideoType(&mediaType);
        if (FAILED(hr)) return hr;
        hr = m_sampleAllocator->InitializeSampleAllocator(kSampleAllocatorBuffers,
                                                          mediaType.Get());
        if (FAILED(hr)) return hr;
        m_allocatorInitialized = true;
    }

    m_streamState = MF_STREAM_STATE_RUNNING;
    m_frameIndex = 0;

    // The order is the one the pipeline expects: the new stream, then the stream
    // started, then the source started. The stream is this object, so it comes
    // back through QueryInterface - converting `this` to the IUnknown* the event
    // queue wants would be ambiguous now that the class has four interface roots.
    ComPtr<IUnknown> stream;
    hr = this->QueryInterface(IID_PPV_ARGS(stream.GetAddressOf()));
    if (FAILED(hr)) return hr;
    hr = m_events->QueueEventParamUnk(MENewStream, GUID_NULL, S_OK, stream.Get());
    if (FAILED(hr)) return hr;
    hr = m_events->QueueEventParamVar(MEStreamStarted, GUID_NULL, S_OK, nullptr);
    if (FAILED(hr)) return hr;
    // MESourceStarted is the event a Media Foundation consumer actually waits
    // for; without it Start returns S_OK and the pipeline waits for ever.
    return m_events->QueueEventParamVar(MESourceStarted, GUID_NULL, S_OK, nullptr);
}

IFACEMETHODIMP MediaSource::Stop() {
    std::lock_guard<std::mutex> guard(m_lock);
    if (m_shutdown) return MF_E_SHUTDOWN;
    m_streamState = MF_STREAM_STATE_STOPPED;
    if (!m_events) return S_OK;
    // Both, in the order the reference sample sends them: the stream stops, then
    // the source. Our single queue carries both halves.
    HRESULT hr = m_events->QueueEventParamVar(MEStreamStopped, GUID_NULL, S_OK, nullptr);
    if (FAILED(hr)) return hr;
    return m_events->QueueEventParamVar(MESourceStopped, GUID_NULL, S_OK, nullptr);
}

IFACEMETHODIMP MediaSource::Pause() {
    // Live capture has nothing to pause, and GetCharacteristics deliberately does
    // not advertise MFMEDIASOURCE_CAN_PAUSE. The reference sample answers this
    // way; queueing a pause event would tell a consumer a frame is frozen when
    // the phone would still be sending.
    return MF_E_INVALID_STATE_TRANSITION;
}

IFACEMETHODIMP MediaSource::Shutdown() {
    ComPtr<IMFMediaEventQueue> events;
    {
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return S_OK;
        m_shutdown = true;
        m_streamState = MF_STREAM_STATE_STOPPED;
        events = m_events;
        // A shut-down source must not keep the pipeline's configuration - or a
        // reference cycle through it - alive. Every method that could hand these
        // back checks m_shutdown first, so clearing them here is safe.
        m_sourceAttributes.Reset();
        m_streamAttributes.Reset();
        m_streamDescriptor.Reset();
        // Drop the pipeline's allocator too: it holds the shared surfaces, and a
        // shut-down source has no business keeping them mapped.
        m_sampleAllocator.Reset();
        m_allocatorInitialized = false;
    }
    if (events) events->Shutdown();
    return S_OK;
}

// ---------------------------------------------------------------------------
// IMFMediaSourceEx
// ---------------------------------------------------------------------------
IFACEMETHODIMP MediaSource::GetSourceAttributes(IMFAttributes** attributes) {
    if (!attributes) return E_POINTER;
    *attributes = nullptr;

    std::lock_guard<std::mutex> guard(m_lock);
    if (m_shutdown) return MF_E_SHUTDOWN;

    // Built in the constructor. If Media Foundation was not ready then, the store
    // is null; build it now instead of reporting a source whose attributes cannot
    // be handed out - that would surface as a different Start failure later.
    if (!m_sourceAttributes && FAILED(MFCreateAttributes(&m_sourceAttributes, 2))) {
        return E_OUTOFMEMORY;
    }
    return m_sourceAttributes.CopyTo(attributes);
}

IFACEMETHODIMP MediaSource::GetStreamAttributes(DWORD streamId, IMFAttributes** attributes) {
    if (!attributes) return E_POINTER;
    *attributes = nullptr;

    std::lock_guard<std::mutex> guard(m_lock);
    if (m_shutdown) return MF_E_SHUTDOWN;
    // Exactly one stream exists. Answering stream 0 for any id would let a caller
    // configure a stream that does not exist and then wonder why frames never come.
    if (streamId != kStreamId) return MF_E_INVALIDSTREAMNUMBER;

    // Same reasoning as GetSourceAttributes: normally already built, but the
    // frame server reads these attributes at Start, so build on demand rather
    // than fail there. CreateStreamAttributes does not take the lock itself.
    if (!m_streamAttributes) {
        const HRESULT created = CreateStreamAttributes();
        if (FAILED(created)) return created;
    }
    return m_streamAttributes.CopyTo(attributes);
}

IFACEMETHODIMP MediaSource::SetD3DManager(IUnknown* manager) {
    (void)manager;
    // There is no D3D path: FillTestPattern writes CPU NV12 into a system-memory
    // buffer. E_NOTIMPL is the documented way to say so, and it makes the pipeline
    // use its default allocator instead of waiting for shared surfaces that never
    // arrive. Returning S_OK here without honouring the manager would hang Start.
    return E_NOTIMPL;
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

IFACEMETHODIMP MediaSource::RequestSample(IUnknown* token) {
    ComPtr<IMFMediaEventQueue> events;
    ComPtr<IMFVideoSampleAllocator> allocator;
    LONGLONG index = 0;
    {
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        if (!m_events) return MF_E_UNEXPECTED;
        // Same check the reference stream makes: a stopped or paused stream has no
        // frame to give, and handing one out would advance a timeline that is not
        // running.
        if (m_streamState != MF_STREAM_STATE_RUNNING) return MF_E_INVALIDREQUEST;
        events = m_events;
        allocator = m_sampleAllocator;
        index = m_frameIndex++;
    }

    ComPtr<IMFSample> sample;
    HRESULT hr = S_OK;
    if (allocator) {
        // The path the frame server uses: the frame is written straight into the
        // buffer the pipeline allocated, so a camera shared between applications
        // costs no copy per frame.
        hr = allocator->AllocateSample(&sample);
    } else {
        // No allocator was supplied - a source reader in a test, or a consumer
        // that only ever got IMFMediaStream. Fall back to our own system-memory
        // sample rather than refusing to produce a frame.
        hr = MFCreateSample(&sample);
        if (SUCCEEDED(hr)) {
            ComPtr<IMFMediaBuffer> buffer;
            hr = MFCreateMemoryBuffer(kFrameBytes, &buffer);
            if (SUCCEEDED(hr)) hr = buffer->SetCurrentLength(kFrameBytes);
            if (SUCCEEDED(hr)) hr = sample->AddBuffer(buffer.Get());
        }
    }
    if (FAILED(hr)) return hr;

    hr = FillSample(sample.Get(), index);
    if (FAILED(hr)) return hr;

    hr = sample->SetSampleTime(index * m_frameDuration);
    if (FAILED(hr)) return hr;
    hr = sample->SetSampleDuration(m_frameDuration);
    if (FAILED(hr)) return hr;
    // Echo the caller's token so it can match sample to request; the reference
    // stream does the same.
    if (token) {
        hr = sample->SetUnknown(MFSampleExtension_Token, token);
        if (FAILED(hr)) return hr;
    }

    return events->QueueEventParamUnk(MEMediaSample, GUID_NULL, S_OK, sample.Get());
}

// ---------------------------------------------------------------------------
// IMFMediaStream2
// ---------------------------------------------------------------------------
IFACEMETHODIMP MediaSource::SetStreamState(MF_STREAM_STATE state) {
    std::lock_guard<std::mutex> guard(m_lock);
    if (m_shutdown) return MF_E_SHUTDOWN;
    if (m_streamState == state) return S_OK;

    switch (state) {
        case MF_STREAM_STATE_RUNNING:
            m_streamState = MF_STREAM_STATE_RUNNING;
            return S_OK;
        case MF_STREAM_STATE_STOPPED:
            m_streamState = MF_STREAM_STATE_STOPPED;
            return S_OK;
        case MF_STREAM_STATE_PAUSED:
            // Only a running stream can be held; a stopped one has nothing to
            // freeze, and the frame server treats a bogus transition as a
            // pipeline error rather than trying again.
            if (m_streamState != MF_STREAM_STATE_RUNNING) return MF_E_INVALID_STATE_TRANSITION;
            m_streamState = MF_STREAM_STATE_PAUSED;
            return S_OK;
        default:
            return MF_E_INVALID_STATE_TRANSITION;
    }
}

IFACEMETHODIMP MediaSource::GetStreamState(MF_STREAM_STATE* state) {
    if (!state) return E_POINTER;
    std::lock_guard<std::mutex> guard(m_lock);
    if (m_shutdown) return MF_E_SHUTDOWN;
    *state = m_streamState;
    return S_OK;
}

// ---------------------------------------------------------------------------
// IMFGetService
// ---------------------------------------------------------------------------
IFACEMETHODIMP MediaSource::GetService(REFGUID /*service*/, REFIID /*riid*/, LPVOID* object) {
    std::lock_guard<std::mutex> guard(m_lock);
    if (m_shutdown) return MF_E_SHUTDOWN;
    if (!object) return E_POINTER;
    *object = nullptr;
    // There is no accelerator, EVR or rate-control service behind a synthetic
    // NV12 source, and inventing one would be worse than saying so. The reference
    // sample answers exactly this code for the same reason.
    return MF_E_UNSUPPORTED_SERVICE;
}

// ---------------------------------------------------------------------------
// IKsControl
//
// The source exposes no custom KS property set, so every call is "this driver
// does not handle that control": ERROR_SET_NOT_FOUND, which is what the AVStream
// framework returns for an unregistered control. Only the argument sanity check
// is real work - it stops a caller passing a truncated header from being told
// "not supported" when the truth is "malformed".
// ---------------------------------------------------------------------------
IFACEMETHODIMP MediaSource::KsProperty(PKSPROPERTY property, ULONG propertyLength,
                                       LPVOID /*propertyData*/, ULONG /*dataLength*/,
                                       ULONG* /*bytesReturned*/) {
    if (!property || propertyLength < sizeof(KSPROPERTY)) return E_INVALIDARG;
    return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
}

IFACEMETHODIMP MediaSource::KsMethod(PKSMETHOD method, ULONG methodLength,
                                     LPVOID /*methodData*/, ULONG /*dataLength*/,
                                     ULONG* /*bytesReturned*/) {
    if (!method || methodLength < sizeof(KSMETHOD)) return E_INVALIDARG;
    return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
}

IFACEMETHODIMP MediaSource::KsEvent(PKSEVENT event, ULONG eventLength,
                                    LPVOID /*eventData*/, ULONG /*dataLength*/,
                                    ULONG* /*bytesReturned*/) {
    if (!event || eventLength < sizeof(KSEVENT)) return E_INVALIDARG;
    return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
}

// ---------------------------------------------------------------------------
// IMFSampleAllocatorControl
// ---------------------------------------------------------------------------
IFACEMETHODIMP MediaSource::SetDefaultAllocator(DWORD outputStreamId, IUnknown* allocator) {
    std::lock_guard<std::mutex> guard(m_lock);
    if (m_shutdown) return MF_E_SHUTDOWN;
    if (outputStreamId != kStreamId) return MF_E_INVALIDSTREAMNUMBER;
    if (!allocator) return E_POINTER;
    // Swapping the allocator mid-stream would leave half the queued samples in
    // the old surfaces; the reference stream refuses the same way.
    if (m_streamState == MF_STREAM_STATE_RUNNING) return MF_E_INVALIDREQUEST;

    ComPtr<IMFVideoSampleAllocator> provided;
    HRESULT hr = allocator->QueryInterface(IID_PPV_ARGS(provided.GetAddressOf()));
    if (FAILED(hr)) return hr;

    m_sampleAllocator = provided;
    m_allocatorInitialized = false;
    return S_OK;
}

IFACEMETHODIMP MediaSource::GetAllocatorUsage(DWORD outputStreamId, DWORD* inputStreamId,
                                              MFSampleAllocatorUsage* usage) {
    if (!inputStreamId || !usage) return E_POINTER;
    // Defined outputs even on the failure paths, so a caller that ignores the
    // result cannot read an uninitialised enum. UsesProvidedAllocator is not just
    // a default here: it is the only usage this source reports, because the
    // pipeline's allocator is what RequestSample fills. "We allocate our own" is
    // the fallback path inside RequestSample for consumers that supply nothing;
    // the SDK's MFSampleAllocatorUsage offers no second member to say it with.
    *inputStreamId = 0;
    *usage = MFSampleAllocatorUsage_UsesProvidedAllocator;

    std::lock_guard<std::mutex> guard(m_lock);
    if (m_shutdown) return MF_E_SHUTDOWN;
    if (outputStreamId != kStreamId) return MF_E_INVALIDSTREAMNUMBER;

    *inputStreamId = outputStreamId;
    return S_OK;
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

HRESULT MediaSource::CreateStreamAttributes() {
    if (m_streamAttributes) return S_OK;

    ComPtr<IMFAttributes> attributes;
    HRESULT hr = MFCreateAttributes(&attributes, 8);
    if (FAILED(hr)) return hr;

    // What the frame server needs in order to treat this as a colour capture
    // stream. Without the category the stream is not offered to applications at
    // all; without the frameserver flag the device is treated as exclusive, so a
    // second app gets "camera in use" instead of the same frames.
    hr = attributes->SetUINT32(MF_DEVICESTREAM_STREAM_ID, kStreamId);
    if (FAILED(hr)) return hr;
    hr = attributes->SetGUID(MF_DEVICESTREAM_STREAM_CATEGORY, kPinNameVideoCapture);
    if (FAILED(hr)) return hr;
    hr = attributes->SetUINT32(MF_DEVICESTREAM_FRAMESERVER_SHARED, 1);
    if (FAILED(hr)) return hr;
    hr = attributes->SetUINT32(MF_DEVICESTREAM_ATTRIBUTE_FRAMESOURCE_TYPES,
                               static_cast<UINT32>(MFFrameSourceTypes_Color));
    if (FAILED(hr)) return hr;

    m_streamAttributes = attributes;
    return S_OK;
}

HRESULT MediaSource::CreateStreamDescriptor() {
    HRESULT hr = CreateStreamAttributes();
    if (FAILED(hr)) return hr;

    ComPtr<IMFMediaType> mediaType;
    hr = CreateVideoType(&mediaType);
    if (FAILED(hr)) return hr;

    IMFMediaType* types[1] = {mediaType.Get()};
    ComPtr<IMFStreamDescriptor> descriptor;
    hr = MFCreateStreamDescriptor(kStreamId, 1, types, &descriptor);
    if (FAILED(hr)) return hr;

    ComPtr<IMFMediaTypeHandler> handler;
    hr = descriptor->GetMediaTypeHandler(&handler);
    if (FAILED(hr)) return hr;
    hr = handler->SetCurrentMediaType(mediaType.Get());
    if (FAILED(hr)) return hr;

    // Mirror the four stream attributes onto the descriptor as well: a consumer
    // that reads IMFStreamDescriptor::GetAttributes instead of asking the source
    // through IMFMediaSourceEx must see the same values.
    hr = m_streamAttributes->CopyAllItems(descriptor.Get());
    if (FAILED(hr)) return hr;

    m_streamDescriptor = descriptor;
    return S_OK;
}

HRESULT MediaSource::FillSample(IMFSample* sample, LONGLONG frameIndex) const {
    ComPtr<IMFMediaBuffer> buffer;
    HRESULT hr = sample->GetBufferByIndex(0, &buffer);
    if (FAILED(hr)) return hr;

    // Lock2DSize first: an allocator-provided surface usually has a pitch wider
    // than the frame, and writing width-strided into it would shear the picture.
    ComPtr<IMF2DBuffer2> buffer2d;
    if (SUCCEEDED(buffer.As(&buffer2d))) {
        BYTE* scanline = nullptr;
        LONG pitch = 0;
        BYTE* start = nullptr;
        DWORD length = 0;
        hr = buffer2d->Lock2DSize(MF2DBuffer_LockFlags_Write, &scanline, &pitch, &start, &length);
        if (FAILED(hr)) return hr;
        hr = FillTestPattern(scanline, pitch, frameIndex);
        // Unlock even when writing failed, and report the write error in
        // preference to the unlock error: the first one is the cause.
        const HRESULT unlocked = buffer2d->Unlock2D();
        return FAILED(hr) ? hr : unlocked;
    }

    BYTE* data = nullptr;
    hr = buffer->Lock(&data, nullptr, nullptr);
    if (FAILED(hr)) return hr;
    hr = FillTestPattern(data, static_cast<LONG>(kWidth), frameIndex);
    const HRESULT unlocked = buffer->Unlock();
    return FAILED(hr) ? hr : unlocked;
}

HRESULT MediaSource::FillTestPattern(BYTE* data, LONG stride, LONGLONG frameIndex) const {
    if (!data) return E_POINTER;
    // A stride shorter than one row cannot hold the frame; writing anyway would
    // run past the end of a surface this source does not own. Negative strides
    // are legal (bottom-up surfaces) and are walked by row arithmetic below.
    if ((stride < 0 ? -stride : stride) < static_cast<LONG>(kWidth)) return E_INVALIDARG;

    const UINT32 width = kWidth;
    const UINT32 height = kHeight;
    const UINT32 barWidth = std::max<UINT32>(8, width / 16);
    const UINT32 barStart = static_cast<UINT32>(
        (frameIndex * 16) % (static_cast<LONGLONG>(width) + barWidth));

    // Y plane: vertical gradient with a moving light bar - motion is the whole
    // point, because a still picture is indistinguishable from a frozen one.
    for (UINT32 y = 0; y < height; ++y) {
        BYTE* row = data + static_cast<ptrdiff_t>(y) * stride;
        const BYTE base = static_cast<BYTE>(32 + (y * 180) / height);
        std::memset(row, base, width);
        for (UINT32 x = 0; x < barWidth; ++x) {
            const UINT32 px = (barStart + x) % width;
            row[px] = 235;
        }
    }

    // Chroma plane: neutral grey (128), so any colour cast is the consumer's.
    // Half as many rows as luma, each still `width` bytes wide, at the same pitch.
    BYTE* chroma = data + static_cast<ptrdiff_t>(height) * stride;
    for (UINT32 y = 0; y < height / 2; ++y) {
        std::memset(chroma + static_cast<ptrdiff_t>(y) * stride, 128, width);
    }
    return S_OK;
}

}  // namespace phonebridge
