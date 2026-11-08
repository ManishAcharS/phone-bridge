import os

import pytest

from phonebridge.protocol.crypto import (
    HandshakeError,
    HandshakeInitiator,
    HandshakeResponder,
    Identity,
    ReplayError,
    SecureCodec,
    hkdf_sha256,
    sas_matches,
)
from phonebridge.protocol.framing import (
    KIND_CLOSE,
    KIND_SECURE,
    Envelope,
    decode_envelope,
    encode_envelope,
    encode_json_envelope,
)


def _establish(expected_peer_static=None):
    pc = Identity.generate("pc")
    phone = Identity.generate("phone")
    initiator = HandshakeInitiator(pc, expected_peer_static=expected_peer_static)
    responder = HandshakeResponder(phone)

    hello = decode_envelope(initiator.hello())
    ack = responder.on_hello(hello, known_peer=True)
    keys_c = initiator.on_hello_ack(decode_envelope(ack))
    keys_s = responder.keys

    assert keys_c.client_to_server == keys_s.client_to_server
    assert keys_c.server_to_client == keys_s.server_to_client
    assert keys_c.sas == keys_s.sas
    return pc, phone, keys_c, keys_s, responder


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
def test_identity_is_deterministic_and_pem_roundtrips():
    ident = Identity.generate("pc")
    again = Identity.from_pem(ident.to_pem(), "pc")
    assert again.public_raw == ident.public_raw
    assert again.device_id == ident.device_id
    assert len(ident.device_id) == 16
    assert len(ident.public_raw) == 65 and ident.public_raw[0] == 0x04


def test_hkdf_matches_rfc5869_style_vectors():
    # HKDF-SHA256 with a short input; verifies our wrapper wiring.
    out = hkdf_sha256(b"\x0b" * 22, bytes.fromhex("000102030405060708090a0b0c"), b"test", 32)
    assert len(out) == 32
    assert hkdf_sha256(b"x", b"y", b"z") == hkdf_sha256(b"x", b"y", b"z")


# --------------------------------------------------------------------------- #
# handshake
# --------------------------------------------------------------------------- #
def test_both_sides_derive_identical_keys_and_sas():
    _, _, keys_c, keys_s, _ = _establish()
    assert keys_c.client_to_server != keys_c.server_to_client
    assert len(keys_c.client_to_server) == 32
    assert len(keys_c.sas) == 6 and keys_c.sas.isdigit()
    assert keys_c.sas_digits == f"{keys_c.sas[:3]} {keys_c.sas[3:]}"


def test_each_session_has_a_fresh_sas():
    sas = {_establish()[2].sas for _ in range(6)}
    assert len(sas) == 6  # ephemeral keys must make every SAS unique


def test_handshake_rejects_wrong_pinned_peer_key():
    pc = Identity.generate("pc")
    phone = Identity.generate("phone")
    impostor = Identity.generate("impostor")
    initiator = HandshakeInitiator(pc, expected_peer_static=impostor.public_raw)
    responder = HandshakeResponder(phone)
    ack = responder.on_hello(decode_envelope(initiator.hello()), known_peer=True)
    with pytest.raises(HandshakeError, match="does not match the pinned key"):
        initiator.on_hello_ack(decode_envelope(ack))


def test_handshake_rejects_version_mismatch():
    pc = Identity.generate("pc")
    phone = Identity.generate("phone")
    initiator = HandshakeInitiator(pc)
    bad_ack = encode_json_envelope(
        0x01,
        {
            "t": "hello_ack",
            "v": 99,
            "id": phone.device_id,
            "name": "phone",
            "static": phone.public_raw.hex(),
            "eph": phone.public_raw.hex(),
            "nonce": "AAAAAAAAAAAAAAAAAAAAAA==",
        },
    )
    with pytest.raises(HandshakeError, match="version"):
        initiator.on_hello_ack(decode_envelope(bad_ack))


def test_responder_rejects_wrong_first_message():
    phone = Identity.generate("phone")
    responder = HandshakeResponder(phone)
    not_hello = encode_json_envelope(0x01, {"t": "nope"})
    with pytest.raises(HandshakeError):
        responder.on_hello(decode_envelope(not_hello), known_peer=False)


def test_mitm_gets_different_sas_on_each_side():
    """A relay attacker produces mismatched codes, which the user can detect."""
    pc = Identity.generate("pc")
    phone = Identity.generate("phone")
    mitm_phone_side = Identity.generate("mitm")

    # MITM pretends to be the phone when talking to the PC...
    initiator = HandshakeInitiator(pc)
    fake_responder = HandshakeResponder(mitm_phone_side)
    ack = fake_responder.on_hello(decode_envelope(initiator.hello()), known_peer=True)
    pc_sas = initiator.on_hello_ack(decode_envelope(ack)).sas

    # ...and pretends to be the PC when talking to the real phone.
    mitm_as_pc = Identity.generate("mitm-as-pc")
    real_initiator = HandshakeInitiator(mitm_as_pc)
    real_responder = HandshakeResponder(phone)
    real_ack = real_responder.on_hello(
        decode_envelope(real_initiator.hello()), known_peer=False
    )
    phone_sas = real_initiator.on_hello_ack(decode_envelope(real_ack)).sas

    assert not sas_matches(pc_sas, phone_sas)


def test_sas_matches_ignores_spacing():
    assert sas_matches("123 456", "123456")
    assert not sas_matches("123 456", "123 457")


# --------------------------------------------------------------------------- #
# secure channel
# --------------------------------------------------------------------------- #
def test_sealed_frames_roundtrip_both_directions():
    _, _, keys_c, keys_s, _ = _establish()
    client = SecureCodec.for_client(keys_c)
    server = SecureCodec.for_server(keys_s)

    env = client.seal(0x10, 0, b'{"hello":"phone"}')
    msg_type, stream, payload = server.unseal(decode_envelope(env))
    assert (msg_type, stream, payload) == (0x10, 0, b'{"hello":"phone"}')

    reply = server.seal(0x10, 7, b"reply")
    assert client.unseal(decode_envelope(reply)) == (0x10, 7, b"reply")


def test_ciphertext_differs_each_frame_even_with_identical_plaintext():
    _, _, keys_c, keys_s, _ = _establish()
    client = SecureCodec.for_client(keys_c)
    a = client.seal(0x10, 0, b"same")
    b = client.seal(0x10, 0, b"same")
    assert a != b


def test_plaintext_frame_is_refused_mid_session():
    _, _, keys_c, keys_s, _ = _establish()
    server = SecureCodec.for_server(keys_s)
    with pytest.raises(HandshakeError, match="plaintext"):
        server.unseal(decode_envelope(encode_envelope(KIND_CLOSE, b"bye")))


def test_tampered_payload_fails_authentication():
    _, _, keys_c, keys_s, _ = _establish()
    client = SecureCodec.for_client(keys_c)
    server = SecureCodec.for_server(keys_s)
    env = bytearray(client.seal(0x10, 0, b"important"))
    env[-1] ^= 0x01
    with pytest.raises(HandshakeError, match="authentication"):
        server.unseal(decode_envelope(bytes(env)))


def test_tampered_counter_fails_authentication():
    _, _, keys_c, keys_s, _ = _establish()
    client = SecureCodec.for_client(keys_c)
    server = SecureCodec.for_server(keys_s)
    env = bytearray(client.seal(0x10, 0, b"payload"))
    env[5] ^= 0xFF  # counter byte inside the envelope body
    with pytest.raises(HandshakeError):
        server.unseal(decode_envelope(bytes(env)))


def test_replayed_frame_is_rejected():
    _, _, keys_c, keys_s, _ = _establish()
    client = SecureCodec.for_client(keys_c)
    server = SecureCodec.for_server(keys_s)
    env = client.seal(0x10, 0, b"one")
    server.unseal(decode_envelope(env))
    with pytest.raises(ReplayError):
        server.unseal(decode_envelope(env))


def test_out_of_order_frame_is_rejected():
    _, _, keys_c, keys_s, _ = _establish()
    client = SecureCodec.for_client(keys_c)
    server = SecureCodec.for_server(keys_s)
    first = client.seal(0x10, 0, b"1")
    second = client.seal(0x10, 0, b"2")
    server.unseal(decode_envelope(second))
    with pytest.raises(ReplayError):
        server.unseal(decode_envelope(first))


def test_wrong_key_cannot_open_frames():
    _, _, keys_c, _, _ = _establish()
    _, _, _, other_keys, _ = _establish()
    client = SecureCodec.for_client(keys_c)
    stranger = SecureCodec.for_server(other_keys)
    with pytest.raises(HandshakeError):
        stranger.unseal(decode_envelope(client.seal(0x10, 0, b"secret")))


def test_counter_increments_and_is_unique():
    _, _, keys_c, _, _ = _establish()
    client = SecureCodec.for_client(keys_c)
    counters = set()
    for _ in range(50):
        env = client.seal(0x10, 0, b"x")
        counters.add(env[5:13])
    assert len(counters) == 50
    assert client.sent_frames == 50


def test_short_key_rejected():
    with pytest.raises(HandshakeError, match="32 bytes"):
        SecureCodec(send_key=b"short", recv_key=os.urandom(32))


def test_envelope_with_garbage_kind_rejected():
    _, _, keys_c, keys_s, _ = _establish()
    server = SecureCodec.for_server(keys_s)
    with pytest.raises(HandshakeError):
        server.unseal(Envelope(kind=KIND_SECURE + 99, body=b"\x00" * 40))


def test_truncated_secure_envelope_rejected():
    _, _, keys_c, keys_s, _ = _establish()
    server = SecureCodec.for_server(keys_s)
    with pytest.raises(HandshakeError, match="too short"):
        server.unseal(Envelope(kind=KIND_SECURE, body=b"\x00" * 10))
