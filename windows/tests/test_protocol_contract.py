"""Three-way protocol contract: docs/03 <-> Kotlin app <-> Python client.

The failure this file exists to catch is drift: a method renamed on the phone, a
response key dropped from the docs, a client calling something the protocol never
defined. All three copies are compared against each other, not against memory.

Kotlin is checked statically (nothing here compiles it), the Python side is
checked by importing it, and the docs are checked by reading them. That ordering
is deliberate: the docs are the specification, the two implementations must match
it, and if they disagree the test says which side moved.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from kotlin_scan import mask_source
from phone_sim import SimulatedPhone, _identity_for

from phonebridge import PROTOCOL_VERSION
from phonebridge.cli import format as fmt
from phonebridge.cli import selftest as selftest_module
from phonebridge.cli.selftest import CAPABILITY_KEYS
from phonebridge.protocol import framing

REPO = Path(__file__).resolve().parents[2]
KOTLIN = REPO / "android/app/src/main/java/dev/phonebridge"
SERVER = KOTLIN / "net/BridgeServer.kt"
DEVICE_FACTS = KOTLIN / "net/DeviceFacts.kt"
MESSAGES = KOTLIN / "protocol/Messages.kt"
FRAMING_KT = KOTLIN / "protocol/Framing.kt"
PERMISSIONS_KT = KOTLIN / "Permissions.kt"
MANIFEST = REPO / "android/app/src/main/AndroidManifest.xml"
PROTOCOL_DOC = REPO / "docs/03-protocol.md"
CLI_PACKAGE = REPO / "windows/phonebridge"

DOCUMENTED_METHODS = (
    "system.info",
    "system.echo",
    "session.stats",
    "bluetooth.headset.status",
    "audio.route.bluetooth",
    "audio.capabilities.run",
    "audio.capabilities.last",
)

DEVICE_BLOCK = {
    "model", "manufacturer", "brand", "device", "product", "hardware",
    "android_release", "android_version", "sdk_int", "target_sdk",
    "app_version", "app_version_code", "package", "rom", "rom_version",
    "is_emulator", "protocol_version",
}

#: Top-level keys every method's `result` must contain (docs/03 §6).
EXPECTED_KEYS: dict[str, set[str]] = {
    "system.info": {"device_id", "name", "capabilities", "permissions"} | DEVICE_BLOCK,
    "system.echo": {"echo", "at_ns"},
    "session.stats": {"frames_in", "frames_out", "bytes_in", "bytes_out", "connections"},
    "bluetooth.headset.status": {"connected", "profile", "devices"},
    "audio.route.bluetooth": {"supported", "detail"},
    "audio.capabilities.last": {"available"},
}

DOCUMENTED_ERROR_CODES = {
    "method_not_found",
    "bad_params",
    "internal",
    "bad_request",
    "unsupported_by_device",
    "probe_failed",
    "busy",
}

#: The one request the CLI sends on purpose to prove the error path works.
DELIBERATE_BAD_METHOD = "workflow.self_test_no_such_method"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def read(path: Path) -> str:
    return path.read_text("utf-8")


def block_after(text: str, marker: str) -> str:
    """The brace-balanced block that follows ``marker``."""
    start = text.index(marker)
    open_brace = text.index("{", start + len(marker) - 1)
    depth = 0
    for index in range(open_brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace : index + 1]
    raise AssertionError(f"unbalanced braces after {marker!r}")


def paren_block_after(text: str, marker: str, opener: str) -> str:
    """The paren-balanced argument list of ``opener`` after ``marker``.

    Kotlin's expression-bodied functions (`fun f(): Map<..> = linkedMapOf(`)
    have no braces to balance, so the map call itself is the extent.
    """
    start = text.index(marker)
    open_paren = text.index(opener, start) + len(opener) - 1
    depth = 0
    for index in range(open_paren, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren : index + 1]
    raise AssertionError(f"unbalanced parentheses after {marker!r}")


def map_keys(block: str) -> set[str]:
    """Keys assigned in a Kotlin map expression: `"k" to` and `m["k"] =`."""
    keys = set(re.findall(r'"([a-z0-9_.]+)"\s+to\s', block))
    keys |= set(re.findall(r'\[\s*"([a-z0-9_.]+)"\s*\]\s*=', block))
    return keys


def kotlin_dispatch() -> dict[str, str]:
    """method name -> text of its branch in dispatchRpc().

    Branches are either block bodies (`-> { ... }`) or single expressions
    (`-> Messages.responseOk(...)`), so the extent is «up to the next branch».
    """
    when_block = block_after(read(SERVER), "when (method)")
    markers = [
        (match.group(1), match.start())
        for match in re.finditer(r'"([a-z][a-z0-9_.]*)"\s*->', when_block)
    ]
    branches: dict[str, str] = {}
    for index, (name, start) in enumerate(markers):
        if index + 1 < len(markers):
            end = markers[index + 1][1]
        else:
            end = when_block.index("else ->", start)
        branches[name] = when_block[start:end]
    return branches


def simulator() -> SimulatedPhone:
    """A phone instance without starting a server: only the handler table."""
    ident = _identity_for(Path("/tmp/protocol-contract-identity.pem"), "contract")
    return SimulatedPhone(identity=ident, state_file=None, port=0)


def call_sites() -> set[str]:
    """Every method string the Python client passes to session.call()."""
    found: set[str] = set()
    pattern = re.compile(r'\.call\(\s*"([^"]+)"')
    for path in sorted(CLI_PACKAGE.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        found |= set(pattern.findall(read(path)))
    return found


# --------------------------------------------------------------------------- #
# 1. method lists agree everywhere
# --------------------------------------------------------------------------- #
def test_the_documented_methods_are_the_ones_the_phone_dispatches():
    assert set(kotlin_dispatch()) == set(DOCUMENTED_METHODS)


def test_the_python_client_knows_only_documented_methods():
    assert set(fmt.PROTOCOL_V1_METHODS) == set(DOCUMENTED_METHODS)


def test_the_reference_phone_implements_every_documented_method():
    handlers = set(simulator().router.handlers)
    assert set(DOCUMENTED_METHODS) <= handlers
    # Anything extra must be declared as simulator-only.
    assert handlers - set(DOCUMENTED_METHODS) == {"system.shutdown"}


def test_the_cli_calls_nothing_the_protocol_does_not_define():
    used = call_sites() - {DELIBERATE_BAD_METHOD}
    assert used <= set(DOCUMENTED_METHODS), (
        f"the CLI calls {sorted(used - set(DOCUMENTED_METHODS))}, which docs/03 "
        "does not define"
    )
    assert DELIBERATE_BAD_METHOD in call_sites(), (
        "the error-contract check needs one request that must fail"
    )


def test_the_self_test_does_not_invent_workflows():
    """Every area marked NOT IMPLEMENTED must have no handler on either side."""
    names = " ".join(name for name, _reason in selftest_module.UNSUPPORTED_WORKFLOWS)
    for area in ("file transfer", "camera", "sensor", "audio"):
        assert area in names
    dispatch = set(kotlin_dispatch())
    for area, word in (("file", "file"), ("camera", "camera"), ("sensor", "sensor")):
        assert not any(word in method for method in dispatch), (
            f"the phone dispatches a {area} method; the matrix must not call it unimplemented"
        )


# --------------------------------------------------------------------------- #
# 2. response schemas
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("method", sorted(EXPECTED_KEYS))
def test_kotlin_result_has_every_key_the_docs_require(method):
    branch = kotlin_dispatch()[method]
    keys = map_keys(branch)
    if method == "system.info":
        # The device block lives in DeviceFacts, not in the dispatch branch.
        keys |= map_keys(
            paren_block_after(read(DEVICE_FACTS), "fun deviceInfo", "linkedMapOf(")
        )
    missing = EXPECTED_KEYS[method] - keys
    assert not missing, f"{method} does not return {sorted(missing)}"


def test_kotlin_reports_the_capability_contract_keys():
    source = read(DEVICE_FACTS)
    block = block_after(source, "fun capabilities")
    # `"hardware.${row.key}"` style interpolation, so check the raw sources.
    assert '"hardware.' in block and '"detail.' in block
    assert "CAPABILITY_KEYS" in source or "CapabilityRow" in source
    for key in CAPABILITY_KEYS:
        assert key in source, f"{key} is missing from the phone's capability list"


def schema_section() -> str:
    """Only the normative table, not the earlier "implemented methods" summary."""
    text = read(PROTOCOL_DOC)
    start = text.index("#### Response schemas (normative)")
    end = text.index("#### `system.info` device block", start)
    return text[start:end]


def test_the_docs_list_every_required_key():
    text = schema_section()
    for method, keys in EXPECTED_KEYS.items():
        row = next(
            (line for line in text.splitlines() if line.startswith(f"| `{method}` |")),
            None,
        )
        assert row is not None, f"docs/03 has no schema row for {method}"
        for key in keys:
            assert f"`{key}`" in row, f"docs/03 schema row for {method} omits `{key}`"


def test_the_docs_say_there_is_no_other_operation():
    text = read(PROTOCOL_DOC)
    assert "There is no other operation" in text
    for kind in ("AUDIO", "VIDEO", "FILE", "SENSOR"):
        assert kind in text


# --------------------------------------------------------------------------- #
# 3. error contract
# --------------------------------------------------------------------------- #
def test_kotlin_only_returns_documented_error_codes():
    codes = set(re.findall(r'responseErr\([^,]+,\s*"([a-z_]+)"', read(SERVER)))
    assert codes, "no responseErr calls found; the regex or the source changed"
    assert codes <= DOCUMENTED_ERROR_CODES, (
        f"undocumented error codes: {sorted(codes - DOCUMENTED_ERROR_CODES)}"
    )
    text = read(PROTOCOL_DOC)
    for code in DOCUMENTED_ERROR_CODES:
        assert f"`{code}`" in text, f"docs/03 does not document error code {code}"


def test_an_unknown_method_is_an_error_not_a_disconnect():
    branch = block_after(read(SERVER), "when (method)")
    assert 'responseErr(id, "method_not_found"' in branch or \
           "method_not_found" in branch.split("else ->")[-1]


# --------------------------------------------------------------------------- #
# 4. version and reserved message kinds
# --------------------------------------------------------------------------- #
def test_protocol_version_is_one_everywhere():
    assert PROTOCOL_VERSION == 1
    assert framing.PROTOCOL_VERSION == 1
    assert "const val PROTOCOL_VERSION = 1" in read(FRAMING_KT)
    assert "# PhoneBridge wire protocol v1" in read(PROTOCOL_DOC)


def test_media_message_kinds_are_reserved_without_a_handler():
    """0x30/0x40/0x50/0x60 exist as numbers; nothing implements them."""
    server_code = mask_source(read(SERVER))
    for kind in ("MSG_AUDIO", "MSG_VIDEO", "MSG_FILE", "MSG_SENSOR"):
        assert f"Messages.{kind}" not in server_code, (
            f"BridgeServer handles {kind}; the feature matrix would be wrong"
        )
        assert f"const val {kind}" in read(MESSAGES), f"{kind} constant disappeared"
    python_code = mask_source(read(REPO / "windows/phonebridge/protocol/messages.py"))
    for name in ("MSG_AUDIO", "MSG_VIDEO", "MSG_FILE", "MSG_SENSOR"):
        assert name in python_code
    sim_code = mask_source(read(REPO / "tools/simulator/phone_sim.py"))
    for name in ("MSG_AUDIO", "MSG_VIDEO", "MSG_FILE", "MSG_SENSOR"):
        assert name not in sim_code, (
            f"the reference phone handles {name}, which has no defined body format"
        )


def test_the_blob_echo_is_the_only_binary_workflow():
    sim_code = read(REPO / "tools/simulator/phone_sim.py")
    assert "MSG_BLOB" in sim_code
    server = read(SERVER)
    assert "Messages.MSG_BLOB" in server


# --------------------------------------------------------------------------- #
# 5. permissions
# --------------------------------------------------------------------------- #
def test_the_reported_permissions_are_declared_and_used():
    permissions = read(PERMISSIONS_KT)
    manifest = read(MANIFEST)
    for name in ("RECORD_AUDIO", "POST_NOTIFICATIONS", "BLUETOOTH_CONNECT"):
        assert name in permissions, f"{name} missing from the permission model"
        assert name in manifest, f"{name} missing from AndroidManifest.xml"
    # Nothing is reported that the app does not declare (no invented states):
    # the code never references a permission outside the runtime list.
    assert "Manifest.permission.CAMERA" not in permissions
    assert "Manifest.permission.BLUETOOTH_SCAN" not in permissions
    assert "Manifest.permission.ACCESS_FINE_LOCATION" not in permissions


def test_the_capability_permission_mapping_matches_the_documentation():
    text = read(PROTOCOL_DOC)
    assert "RECORD_AUDIO" in text
    assert "permanently_denied" in text
    facts = read(DEVICE_FACTS)
    assert "RECORD_AUDIO" in facts, "the phone must gate audio.capture on RECORD_AUDIO"
