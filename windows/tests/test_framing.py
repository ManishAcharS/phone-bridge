import pytest

from phonebridge.protocol.framing import (
    KIND_CLOSE,
    KIND_HANDSHAKE,
    KIND_SECURE,
    MAX_ENVELOPE_BODY,
    Envelope,
    EnvelopeStreamParser,
    FramingError,
    decode_envelope,
    encode_envelope,
    encode_json_envelope,
)


def test_roundtrip_binary():
    env = encode_envelope(KIND_SECURE, b"\x00\x01\x02payload")
    parsed = decode_envelope(env)
    assert parsed.kind == KIND_SECURE
    assert parsed.body == b"\x00\x01\x02payload"


def test_roundtrip_empty_body():
    env = encode_envelope(KIND_CLOSE, b"")
    assert len(env) == 5
    parsed = decode_envelope(env)
    assert parsed.kind == KIND_CLOSE
    assert parsed.body == b""


def test_json_envelope_is_sorted_and_compact():
    env = encode_json_envelope(KIND_HANDSHAKE, {"b": 1, "a": 2})
    parsed = decode_envelope(env)
    assert parsed.as_json() == {"a": 2, "b": 1}
    assert b" " not in parsed.body


def test_rejects_truncated():
    with pytest.raises(FramingError):
        decode_envelope(b"\x00\x00")
    env = encode_envelope(KIND_HANDSHAKE, b"abcd")
    with pytest.raises(FramingError):
        decode_envelope(env[:-1])


def test_rejects_trailing_bytes():
    env = encode_envelope(KIND_HANDSHAKE, b"abcd") + b"x"
    with pytest.raises(FramingError):
        decode_envelope(env)


def test_rejects_oversized_body():
    with pytest.raises(FramingError):
        encode_envelope(0x01, b"x" * MAX_ENVELOPE_BODY)


def test_rejects_zero_length_declared():
    with pytest.raises(FramingError):
        decode_envelope(b"\x00\x00\x00\x00" + b"\x01")


def test_stream_parser_handles_split_and_coalesced_chunks():
    frames = [
        encode_envelope(KIND_SECURE, b"a" * 10),
        encode_envelope(KIND_SECURE, b"b" * 3),
        encode_envelope(KIND_CLOSE, b""),
    ]
    blob = b"".join(frames)

    parser = EnvelopeStreamParser()
    # one byte at a time
    out = []
    for i in range(len(blob)):
        out.extend(parser.feed(blob[i : i + 1]))
    assert [e.body for e in out] == [b"a" * 10, b"b" * 3, b""]

    parser = EnvelopeStreamParser()
    out = list(parser.feed(blob))
    assert len(out) == 3

    # split exactly on a header boundary
    parser = EnvelopeStreamParser()
    out = list(parser.feed(blob[:7]))
    out.extend(parser.feed(blob[7:]))
    assert len(out) == 3


def test_stream_parser_rejects_absurd_length():
    parser = EnvelopeStreamParser()
    with pytest.raises(FramingError):
        list(parser.feed(b"\xff\xff\xff\xff"))


def test_envelope_as_json_rejects_non_object():
    env = Envelope(kind=KIND_HANDSHAKE, body=b'[1,2,3]')
    with pytest.raises(FramingError):
        env.as_json()
