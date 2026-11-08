"""Cross-language conformance: Android (Kotlin) vs PC (Python) vs the spec.

These tests exist because the Kotlin implementation cannot be compiled or run in
the environment where most of this work happens. Instead of trusting a manual
read-through, the *constants and generated artifacts* that define the wire
protocol are parsed out of the Kotlin sources and compared with the canonical
Python values.

What this catches:
  * a protocol version or message type drifting on one side only
  * HKDF info strings reordered/renamed in Kotlin
  * the curve changing on one side
  * a hand-edited ``ProtocolVectors.kt`` (it must equal the generated output)
  * the AES vector key/frame pair falling out of sync (a real bug this caught:
    the frame was sealed with the derived c2s key while the self-test used the
    standalone AES key, so a *correct* Kotlin build reported FAIL)

What this cannot catch: Kotlin syntax errors, type errors, Android API misuse.
Those need ``gradlew assembleDebug`` on a machine with a JDK and the SDK.
"""

from __future__ import annotations

import re
from pathlib import Path

import gen_vectors
import pytest

from phonebridge.protocol import crypto as py_crypto
from phonebridge.protocol import framing as py_framing
from phonebridge.protocol import messages as py_messages

REPO = Path(__file__).resolve().parents[2]
KOTLIN_DIR = REPO / "android/app/src/main/java/dev/phonebridge"
FRAMING_KT = KOTLIN_DIR / "protocol/Framing.kt"
CRYPTO_KT = KOTLIN_DIR / "protocol/Crypto.kt"
MESSAGES_KT = KOTLIN_DIR / "protocol/Messages.kt"
VECTORS_KT = KOTLIN_DIR / "protocol/ProtocolVectors.kt"
BRIDGE_SERVER_KT = KOTLIN_DIR / "net/BridgeServer.kt"
PHONE_SIM_PY = REPO / "tools/simulator/phone_sim.py"
SESSION_PY = REPO / "windows/phonebridge/core/session.py"

# `const val NAME = "string"` / `const val NAME = 123` / `= 0x10`
_CONST_RE = re.compile(r'const val ([A-Z0-9_]+)\s*=\s*("([^"]*)"|([^/\n]+))')


def _eval_numeric(expr: str) -> int:
    """Evaluate a Kotlin constant expression like `8 * 1024 * 1024` or `0x10`."""
    expr = expr.strip().removesuffix("L")
    allowed = set("0123456789abcdefABCDEFxX*+()- ")
    if not set(expr) <= allowed:
        raise ValueError(f"refusing to evaluate suspicious constant: {expr!r}")
    return int(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 - whitelisted above


def kotlin_constants(path: Path) -> dict[str, str | int]:
    """Extract `const val` declarations from a Kotlin file."""
    text = path.read_text("utf-8")
    out: dict[str, str | int] = {}
    for match in _CONST_RE.finditer(text):
        name, _raw, string_val, numeric = match.groups()
        if string_val is not None:
            out[name] = string_val
        else:
            out[name] = _eval_numeric(numeric)
    return out


# --------------------------------------------------------------------------- #
# generated vectors
# --------------------------------------------------------------------------- #
def test_kotlin_vectors_are_exactly_the_generated_output():
    """ProtocolVectors.kt must be byte-identical to what the generator emits.

    This is the anti-drift guard for the self-test: if someone hand-edits a
    vector on the phone side, the PC stops being the reference implementation.
    """
    vectors = gen_vectors.build_vectors()
    generated = gen_vectors.render_kotlin(vectors)
    actual = VECTORS_KT.read_text("utf-8")
    assert actual == generated, (
        "ProtocolVectors.kt is out of date; run "
        "`python tools/vectorgen/gen_vectors.py`"
    )


def test_vectors_json_matches_generator_output():
    import json

    vectors = gen_vectors.build_vectors()
    on_disk = json.loads(gen_vectors.JSON_PATH.read_text("utf-8"))
    assert on_disk == {
        k: (v if isinstance(v, (int, str)) else v) for k, v in vectors.items()
    }


def test_kotlin_vector_constants_match_json():
    kt = kotlin_constants(VECTORS_KT)
    vectors = gen_vectors.build_vectors()
    for key, value in vectors.items():
        assert key.upper() in kt, f"{key.upper()} missing from ProtocolVectors.kt"
        assert kt[key.upper()] == value, f"{key} differs between Kotlin and Python"


def test_aes_vector_opens_with_the_key_the_self_test_uses():
    """The self-test does SecureCodec(AES_KEY_HEX, AES_KEY_HEX).seal(...).

    Therefore the embedded frame MUST be openable with that same key. It was not
    (it used the HKDF c2s key), which would have made a correct Kotlin build
    report FAIL. This test pins the invariant.
    """
    from phonebridge.protocol.crypto import SecureCodec
    from phonebridge.protocol.framing import decode_envelope

    kt = kotlin_constants(VECTORS_KT)
    key = bytes.fromhex(str(kt["AES_KEY_HEX"]))
    frame = bytes.fromhex(str(kt["AES_FRAME_HEX"]))

    opened = SecureCodec(key, key).unseal(decode_envelope(frame))
    assert opened[0] == kt["AES_MSG_TYPE"]
    assert opened[1] == kt["AES_STREAM_ID"]
    assert opened[2].decode("ascii") == kt["AES_PAYLOAD"]

    # and resealing the same plaintext must reproduce the same bytes
    assert SecureCodec(key, key).seal(opened[0], opened[1], opened[2]) == frame


def test_sas_vector_derives_from_the_hkdf_vector():
    """SAS_EXPECTED must be the SAS of HKDF_C2S_HEX, not an unrelated number."""
    from phonebridge.protocol.crypto import sas_from_key

    kt = kotlin_constants(VECTORS_KT)
    c2s = bytes.fromhex(str(kt["HKDF_C2S_HEX"]))
    assert sas_from_key(c2s) == kt["SAS_EXPECTED"]


def test_ecdh_vector_is_symmetric():
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    kt = kotlin_constants(VECTORS_KT)
    a = serialization.load_der_private_key(
        base64.b64decode(str(kt["SCALAR_A_PKCS8_B64"])), password=None
    )
    b = serialization.load_der_private_key(
        base64.b64decode(str(kt["SCALAR_B_PKCS8_B64"])), password=None
    )
    expected = str(kt["ECDH_SHARED_HEX"])
    assert a.exchange(ec.ECDH(), b.public_key()).hex() == expected
    assert b.exchange(ec.ECDH(), a.public_key()).hex() == expected


# --------------------------------------------------------------------------- #
# protocol constants
# --------------------------------------------------------------------------- #
def test_framing_constants_match():
    kt = kotlin_constants(FRAMING_KT)
    assert kt["PROTOCOL_VERSION"] == py_framing.PROTOCOL_VERSION
    assert kt["KIND_HANDSHAKE"] == py_framing.KIND_HANDSHAKE
    assert kt["KIND_SECURE"] == py_framing.KIND_SECURE
    assert kt["KIND_CLOSE"] == py_framing.KIND_CLOSE
    assert kt["MAX_ENVELOPE_BODY"] == py_framing.MAX_ENVELOPE_BODY


def test_message_type_constants_match():
    kt = kotlin_constants(MESSAGES_KT)
    expected = {
        "MSG_CONTROL": py_messages.MSG_CONTROL,
        "MSG_PING": py_messages.MSG_PING,
        "MSG_PONG": py_messages.MSG_PONG,
        "MSG_BYE": py_messages.MSG_BYE,
        "MSG_BLOB": py_messages.MSG_BLOB,
        "MSG_AUDIO": py_messages.MSG_AUDIO,
        "MSG_VIDEO": py_messages.MSG_VIDEO,
        "MSG_FILE": py_messages.MSG_FILE,
        "MSG_SENSOR": py_messages.MSG_SENSOR,
        "STREAM_CONTROL": py_messages.STREAM_CONTROL,
    }
    for name, value in expected.items():
        assert kt[name] == value, f"{name}: Kotlin {kt.get(name)} != Python {value}"


def test_crypto_parameters_match():
    kt = kotlin_constants(CRYPTO_KT)
    text = CRYPTO_KT.read_text("utf-8")

    assert kt["CURVE"] == "secp256r1"
    assert '"EC"' in text  # JCA algorithm name for the curve parameters
    assert kt["GCM_TAG_BITS"] == 128
    assert 'Cipher.getInstance("AES/GCM/NoPadding")' in text
    assert '"HmacSHA256"' in text
    assert '"SHA-256"' in text  # device id derivation

    # The three HKDF info strings are the single most likely thing to be
    # mistyped, and a mistake is undetectable until pairing fails.
    for info in ("phonebridge/1 c2s", "phonebridge/1 s2c", "phonebridge/1 sas"):
        assert f'"{info}"' in text, f"Kotlin is missing info string {info!r}"
        assert info.encode() in (
            py_crypto._INFO_C2S,
            py_crypto._INFO_S2C,
            py_crypto._INFO_SAS,
        )

    # key sizes: 32-byte session keys, 16-byte nonces, 65-byte public points
    assert "32" in text and "AES-256" in text
    assert re.search(r"ByteArray\(4\)", text), "nonce must start with 4 zero bytes"
    assert re.search(r"putLong\(counter\)", text), "counter must be a big-endian u64"


def test_python_public_key_encoding_is_x962_uncompressed():
    """Both sides must agree on 0x04 || X || Y, 65 bytes."""
    assert py_crypto.public_key_to_raw(
        py_crypto.Identity.generate().private_key.public_key()
    )[0] == 0x04
    assert "raw.size != 65" in CRYPTO_KT.read_text("utf-8")


# --------------------------------------------------------------------------- #
# pairing / lifecycle handshake names
# --------------------------------------------------------------------------- #
SETUP_MESSAGES = [
    "auth",
    "auth_ok",
    "auth_fail",
    "pair_required",
    "pair_confirm",
    "pair_cancel",
    "pair_ok",
    "pair_fail",
]


@pytest.mark.parametrize("name", SETUP_MESSAGES)
def test_setup_message_understood_by_both_sides(name: str):
    """Every setup message the PC can send must be recognised by the phone.

    The Kotlin server and the Python reference phone must both handle the same
    names, otherwise pairing works against the simulator and fails on hardware.
    """
    kotlin = BRIDGE_SERVER_KT.read_text("utf-8")
    python = SESSION_PY.read_text("utf-8") + PHONE_SIM_PY.read_text("utf-8")
    assert f'"{name}"' in kotlin, f"Android never handles {name!r}"
    assert f'"{name}"' in python, f"PC/simulator never handles {name!r}"


@pytest.mark.parametrize("field", ["hello", "hello_ack"])
def test_handshake_plaintext_types_match(field: str):
    """`t` values of the two plaintext handshake envelopes."""
    assert f'"{field}"' in CRYPTO_KT.read_text("utf-8")
    assert f'"{field}"' in Path(py_crypto.__file__).read_text("utf-8")


def test_hello_field_names_match():
    """The JSON field names in the plaintext handshake are part of the wire format."""
    kotlin = CRYPTO_KT.read_text("utf-8")
    python = Path(py_crypto.__file__).read_text("utf-8")
    for field in ("t", "v", "id", "name", "static", "eph", "nonce", "paired"):
        assert f'"{field}"' in kotlin, f"Kotlin handshake missing field {field!r}"
        assert f'"{field}"' in python, f"Python handshake missing field {field!r}"


def test_kotlin_server_handles_every_message_type_the_pc_sends():
    """The PC only ever sends CONTROL, PING and BLOB in Phase 1."""
    kotlin = BRIDGE_SERVER_KT.read_text("utf-8")
    for msg in ("MSG_CONTROL", "MSG_PING", "MSG_BLOB"):
        assert f"Messages.{msg}" in kotlin, f"Android does not handle {msg}"


def test_kotlin_server_exposes_every_rpc_the_cli_uses():
    kotlin = BRIDGE_SERVER_KT.read_text("utf-8")
    cli = (REPO / "windows/phonebridge/cli/main.py").read_text("utf-8")
    called = set(re.findall(r'call\(\s*"([a-z_.]+)"', cli))
    for method in sorted(called):
        assert f'"{method}"' in kotlin, (
            f"CLI calls {method!r} but the Android server has no handler for it"
        )


# --------------------------------------------------------------------------- #
# structural sanity (a weak but cheap guard against truncated/botched edits)
# --------------------------------------------------------------------------- #
def _strip_kotlin_noise(text: str) -> str:
    """Remove comments and string/char literals so brackets can be counted."""
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        elif text.startswith('"""', i):
            i += 3
            while i + 2 < n and not text.startswith('"""', i):
                i += 1
            i += 3
        elif ch in "\"'":
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


@pytest.mark.parametrize(
    "path", sorted(str(p.relative_to(KOTLIN_DIR)) for p in KOTLIN_DIR.rglob("*.kt"))
)
def test_kotlin_file_has_balanced_brackets_and_matching_package(path: str):
    source = KOTLIN_DIR / path
    text = source.read_text("utf-8")

    expected_package = "dev.phonebridge" + (
        "." + str(source.parent.relative_to(KOTLIN_DIR)).replace("/", ".")
        if source.parent != KOTLIN_DIR
        else ""
    )
    assert text.startswith(f"package {expected_package}\n"), (
        f"{path}: package declaration does not match its directory "
        f"(expected {expected_package})"
    )

    stripped = _strip_kotlin_noise(text)
    for opening, closing in (("{", "}"), ("(", ")"), ("[", "]")):
        balance = 0
        for ch in stripped:
            if ch == opening:
                balance += 1
            elif ch == closing:
                balance -= 1
            assert balance >= 0, f"{path}: unbalanced {closing!r} at some point"
        assert balance == 0, f"{path}: {opening}{closing} balance is {balance}"


def test_every_protocol_vector_reference_exists():
    declared = set(kotlin_constants(VECTORS_KT))
    referenced: set[str] = set()
    for kt in KOTLIN_DIR.rglob("*.kt"):
        if kt == VECTORS_KT:
            continue
        referenced.update(re.findall(r"ProtocolVectors\.([A-Z0-9_]+)", kt.read_text("utf-8")))
    missing = referenced - declared
    assert not missing, f"Kotlin references undefined vectors: {sorted(missing)}"


def test_no_kotlin_message_type_is_invented():
    """Every Messages.MSG_* the Kotlin code uses must exist in Messages.kt."""
    declared = set(kotlin_constants(MESSAGES_KT))
    used: set[str] = set()
    for kt in KOTLIN_DIR.rglob("*.kt"):
        used.update(re.findall(r"Messages\.(MSG_[A-Z0-9_]+)", kt.read_text("utf-8")))
    missing = used - declared
    assert not missing, f"Kotlin uses undeclared message types: {sorted(missing)}"
