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

import ast
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
BLE_ENGINE = KOTLIN / "net/BleEngine.kt"
SENSOR_ENGINE = KOTLIN / "net/SensorEngine.kt"
FILE_ENGINE = KOTLIN / "net/FileTransferEngine.kt"
DEVICE_FACTS = KOTLIN / "net/DeviceFacts.kt"
MESSAGES = KOTLIN / "protocol/Messages.kt"
BLE = KOTLIN / "net/BleEngine.kt"
SENSOR_ENGINE = KOTLIN / "net/SensorEngine.kt"
FILE_ENGINE = KOTLIN / "net/FileTransferEngine.kt"
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
    "audio.playback.start",
    "audio.playback.stop",
    "audio.playback.mute",
    "audio.playback.volume",
    "audio.mic.start",
    "audio.mic.stop",
    "audio.status",
    "camera.start",
    "camera.stop",
    "camera.switch",
    "camera.status",
    # Phase 5: Bluetooth, sensors and file transfer.
    "bluetooth.adapter.status",
    "bluetooth.scan.start",
    "bluetooth.scan.stop",
    "bluetooth.scan.results",
    "bluetooth.gatt.connect",
    "bluetooth.gatt.services",
    "bluetooth.gatt.read",
    "bluetooth.gatt.write",
    "bluetooth.gatt.subscribe",
    "bluetooth.gatt.disconnect",
    "bluetooth.gatt.status",
    "sensor.list",
    "sensor.start",
    "sensor.stop",
    "sensor.status",
    "file.offer",
    "file.request",
    "file.complete",
    "file.progress",
    "file.cancel",
    "file.pause",
    "file.resume",
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
    "audio.playback.start": {"ok", "format"},
    "audio.playback.stop": {"ok"},
    "audio.playback.mute": {"ok", "muted"},
    "audio.playback.volume": {"ok", "percent"},
    "audio.mic.start": {"ok", "format"},
    "audio.mic.stop": {"ok"},
    # The playback block: what the phone's own AudioTrack is doing, so the PC can
    # tell "the phone accepted a stream" from "the phone is playing it".
    "audio.status": {
        "playback",
        "mic",
        "volume",
        "muted",
        "permitted",
        "route",
        "device_name",
        "track_state",
        "buffered_ms",
        "buffer_ms",
        "underruns",
        "played_ms",
        "frames_in",
        "frames_written",
        "dropped",
    },
    "camera.start": {"ok", "config"},
    "camera.stop": {"ok", "streaming"},
    "camera.switch": {"ok", "facing", "streaming"},
    "camera.status": {"streaming", "facing", "width", "height", "fps", "permitted"},
    "bluetooth.adapter.status": {"supported", "enabled", "le_supported", "permitted", "detail"},
    "bluetooth.scan.start": {"ok", "scanning", "started_at_ms", "duration_ms"},
    "bluetooth.scan.stop": {"ok", "scanning", "devices"},
    "bluetooth.scan.results": {"scanning", "state", "devices", "detail"},
    "bluetooth.gatt.connect": {"address", "state", "services", "mtu"},
    "bluetooth.gatt.services": {"address", "mtu", "services"},
    "bluetooth.gatt.read": {"value_b64", "bytes"},

    "bluetooth.gatt.write": {"ok", "bytes"},
    "bluetooth.gatt.subscribe": {"ok", "subscribed"},
    "bluetooth.gatt.disconnect": {"ok", "state"},
    "bluetooth.gatt.status": {"connections", "scanning"},
    "sensor.list": {"sensors", "permitted", "interval_us", "detail"},
    "sensor.start": {"ok", "active", "interval_us"},
    "sensor.stop": {"ok", "stopped", "active"},
    "sensor.status": {"active", "interval_us", "frames_sent", "permitted"},
    "file.offer": {"transfer_id", "chunk_size", "window", "resume_from"},
    "file.request": {"transfer_id", "name", "size", "sha256", "chunk_size"},
    "file.complete": {"ok", "verified", "sha256", "bytes"},
    "file.progress": {"transfer_id", "state", "bytes", "chunks"},
    "file.cancel": {"ok", "state"},
    "file.pause": {"ok", "state"},
    "file.resume": {"ok", "from_bytes", "state"},
}

DOCUMENTED_ERROR_CODES = {
    "method_not_found",
    "bad_params",
    "internal",
    "bad_request",
    "unsupported_by_device",
    "probe_failed",
    "busy",
    # Phase 4 camera.
    "camera_denied",
    "no_camera",
    "camera_busy",
    "bad_config",
    # Phase 5 Bluetooth, sensors and files.
    "no_bluetooth",
    "bluetooth_off",
    "bluetooth_denied",
    "gatt_failed",
    "not_connected",
    "sensor_denied",
    "sensor_failed",
    "unknown_transfer",
    "no_file",
    "hash_mismatch",
    "storage_denied",
    "cancelled",
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


#: Phase 5 methods whose result map is built inside an engine rather than in the
#: dispatch branch. The contract test follows the delegation instead of demanding
#: that every key be spelled twice: the keys live next to the code that produces
#: them, and this table says where to look.
DELEGATED_KEYS: dict[str, tuple[Path, str]] = {
    "bluetooth.adapter.status": (BLE, "fun status("),
    "bluetooth.scan.start": (BLE, "fun startScan("),
    "bluetooth.scan.stop": (BLE, "fun stopScan("),
    "bluetooth.scan.results": (BLE, "fun results("),
    "bluetooth.gatt.connect": (BLE, "fun connect("),
    "bluetooth.gatt.services": (BLE, "fun services("),
    "bluetooth.gatt.read": (BLE, "fun read("),
    "bluetooth.gatt.write": (BLE, "fun write("),
    "bluetooth.gatt.subscribe": (BLE, "fun subscribe("),
    "bluetooth.gatt.disconnect": (BLE, "fun disconnect("),
    "bluetooth.gatt.status": (BLE, "fun connections("),
    "sensor.status": (SENSOR_ENGINE, "fun status("),
    "file.offer": (FILE_ENGINE, "fun offer("),
    "file.request": (FILE_ENGINE, "fun request("),
    "file.complete": (FILE_ENGINE, "fun complete("),
    "file.progress": (FILE_ENGINE, "fun progress("),
    "file.cancel": (FILE_ENGINE, "fun cancel("),
    "file.pause": (FILE_ENGINE, "fun pause("),
    "file.resume": (FILE_ENGINE, "fun resume("),
}


def delegated_keys(method: str) -> set[str]:
    """Keys returned by an engine function the dispatch branch delegates to."""
    entry = DELEGATED_KEYS.get(method)
    if entry is None:
        return set()
    path, signature = entry
    # Every engine accessor is expression-bodied (`fun f(): Map<..> = linkedMapOf(`),
    # so the map call itself is the extent — no braces to balance.
    return map_keys(paren_block_after(read(path), signature, "linkedMapOf("))


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
    """Every method string the Python client passes to ``session.call()``.

    Parsed, not pattern-matched: the old regex only matched `.call("name")` on one
    line, so a call split across lines — or one using a keyword argument — was
    invisible to the contract, and a method that does not exist could slip
    through. `ast` has no opinion about formatting; a call whose method is not a
    literal string (a computed name) is reported rather than silently skipped.
    """
    found: set[str] = set()
    dynamic: list[tuple[str, str]] = []
    for path in sorted(CLI_PACKAGE.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        tree = ast.parse(read(path), filename=str(path))
        # `_call`-style private helpers forward a method name they were handed.
        # That is fine: their callers pass literals, which this scan collects. A
        # *public* method that computes an RPC name would be a hole in the
        # contract, so that is what the assertion below refuses.
        enclosing = {
            id(child): node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for child in ast.walk(node)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "call":
                continue
            name = None
            if node.args and isinstance(node.args[0], ast.Constant):
                name = node.args[0].value
            for keyword in node.keywords:
                if keyword.arg == "method" and isinstance(keyword.value, ast.Constant):
                    name = keyword.value.value
            if isinstance(name, str):
                found.add(name)
            else:
                dynamic.append((enclosing.get(id(node), "<module>"), f"{path.name}:{node.lineno}"))
    public = [site for site in dynamic if not site[0].startswith("_")]
    assert not public, (
        "a public call site computes its RPC method name, so the contract cannot "
        f"check it: {public}"
    )
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
    """The matrix may only disclaim an area that has no handler on the phone.

    The rule survives Phase 5 even though nothing is disclaimed any more: if a
    future phase removed a handler, a NOT IMPLEMENTED row would be the honest
    answer, and one appearing while the phone *does* dispatch the methods would
    mean the matrix had started lying.
    """
    assert selftest_module.UNSUPPORTED_WORKFLOWS == (), (
        "Phase 5 implemented Bluetooth, sensors and file transfer, so the matrix has "
        "nothing left to disclaim"
    )
    dispatch = set(kotlin_dispatch())
    for prefix in ("audio.", "camera.", "bluetooth.", "sensor.", "file."):
        assert any(method.startswith(prefix) for method in dispatch), (
            f"the phone dispatches no {prefix}* method, yet the feature is implemented"
        )
    for name, _reason in selftest_module.UNSUPPORTED_WORKFLOWS:
        lowered = name.lower()
        for word in ("audio", "camera", "bluetooth", "sensor", "file"):
            assert word not in lowered, (
                f"the matrix disclaims {name!r} while the phone implements {word}"
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
    keys |= delegated_keys(method)
    missing = EXPECTED_KEYS[method] - keys
    assert not missing, f"{method} does not return {sorted(missing)}"


def test_every_documented_phase_five_method_is_dispatched():
    """The docs and the phone must agree on the whole Phase 5 surface."""
    dispatch = kotlin_dispatch()
    for method in (
        "bluetooth.adapter.status", "bluetooth.scan.start", "bluetooth.scan.stop",
        "bluetooth.scan.results", "bluetooth.gatt.connect", "bluetooth.gatt.services",
        "bluetooth.gatt.read", "bluetooth.gatt.write", "bluetooth.gatt.subscribe",
        "bluetooth.gatt.disconnect", "bluetooth.gatt.status", "sensor.list",
        "sensor.start", "sensor.stop", "sensor.status", "file.offer", "file.request",
        "file.complete", "file.progress", "file.cancel", "file.pause", "file.resume",
    ):
        assert method in dispatch, f"the phone does not dispatch {method}"


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


def test_the_docs_say_every_operation_is_implemented():
    """The Phase 1 disclaimer is gone, and the doc says so explicitly.

    It must be replaced by the stronger claim, not just deleted: a reader has to be
    able to tell that `0x50`/`0x60` stopped being reserved numbers.
    """
    text = read(PROTOCOL_DOC)
    assert "There is no other operation" not in text, (
        "the Phase 1 disclaimer survived Phase 5"
    )
    assert "Every operation this document lists is implemented on both sides" in text
    for kind in ("AUDIO", "VIDEO", "FILE", "SENSOR"):
        assert kind in text
    for body in ("`MSG_FILE` body", "`MSG_SENSOR` body"):
        assert body in text, f"{body} is not documented"


# --------------------------------------------------------------------------- #
# 3. error contract
# --------------------------------------------------------------------------- #
def test_kotlin_only_returns_documented_error_codes():
    """Every refusal the phone can send is a documented code.

    Four classes raise them (``responseErr`` for the dispatcher,
    ``BleFailure``/``SensorFailure``/``FileFailure`` for the engines), so all
    four are scanned: checking only the dispatcher is how the file-transfer codes
    drifted away from docs/23 in the first place.
    """
    patterns = {
        "BridgeServer": (SERVER, r'responseErr\(\s*[^,()]+,\s*"([a-z_]+)"'),
        "BleEngine": (BLE_ENGINE, r'BleFailure\(\s*"([a-z_]+)"'),
        "SensorEngine": (SENSOR_ENGINE, r'SensorFailure\(\s*"([a-z_]+)"'),
        "FileTransferEngine": (FILE_ENGINE, r'FileFailure\(\s*"([a-z_]+)"'),
    }
    codes: set[str] = set()
    for name, (path, pattern) in patterns.items():
        found = set(re.findall(pattern, read(path)))
        assert found, f"no error codes found in {name}; the pattern or the source changed"
        codes |= found
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


def test_every_implemented_media_kind_has_a_handler():
    """All four media kinds are implemented now, and each has a real handler.

    The inverse of the Phase 1-4 test: a kind whose capability row says
    "implemented" must be handled by the phone, or the matrix would promise a
    feature that nothing answers.
    """
    server_code = mask_source(read(SERVER))
    assert "Messages.MSG_AUDIO" in server_code, "Phase 2 audio handler disappeared"
    assert "Messages.MSG_VIDEO" in server_code, "Phase 4 camera handler disappeared"
    # 0x50/0x60 need a handler *and* a body format defined in docs/03.
    doc = read(PROTOCOL_DOC)
    for kind in ("MSG_FILE", "MSG_SENSOR"):
        assert kind in doc, f"{kind} is not documented in docs/03"
    assert "Messages.MSG_FILE" in server_code, "BridgeServer does not handle MSG_FILE"
    assert "Messages.MSG_SENSOR" in server_code, "BridgeServer does not handle MSG_SENSOR"
    assert "files.onChunk(opened.payload)" in server_code, (
        "MSG_FILE must reach the transfer engine"
    )
    for name in ("MSG_AUDIO", "MSG_VIDEO", "MSG_FILE", "MSG_SENSOR"):
        assert f"const val {name}" in read(MESSAGES), f"{name} constant disappeared"
    python_code = mask_source(read(REPO / "windows/phonebridge/protocol/messages.py"))
    for name in ("MSG_AUDIO", "MSG_VIDEO", "MSG_FILE", "MSG_SENSOR"):
        assert name in python_code
    sim_code = mask_source(read(REPO / "tools/simulator/phone_sim.py"))
    for kind in ("MSG_AUDIO", "MSG_VIDEO", "MSG_FILE", "MSG_SENSOR"):
        assert kind in sim_code, f"the reference phone must implement {kind}"


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
    for name in ("RECORD_AUDIO", "POST_NOTIFICATIONS", "BLUETOOTH_CONNECT", "CAMERA"):
        assert name in permissions, f"{name} missing from the permission model"
        assert name in manifest, f"{name} missing from AndroidManifest.xml"
    # Phase 5 added the Bluetooth pair. Both are declared, modelled and requested
    # at the moment of use; the location permission is the Android 11 spelling of
    # the same power and is capped at SDK 30 in the manifest.
    for name in ("BLUETOOTH_SCAN", "ACCESS_FINE_LOCATION"):
        assert f"Manifest.permission.{name}" in permissions, (
            f"{name} is not modelled in Permissions.kt"
        )
        assert name in manifest, f"{name} is not declared in AndroidManifest.xml"
    assert 'android:maxSdkVersion="30"' in manifest, (
        "ACCESS_FINE_LOCATION must not be requested on Android 12+"
    )
    assert "usesPermissionFlags=\"neverForLocation\"" in manifest, (
        "BLUETOOTH_SCAN must state that scan results are not used for location"
    )



def test_the_capability_permission_mapping_matches_the_documentation():
    text = read(PROTOCOL_DOC)
    assert "RECORD_AUDIO" in text
    assert "permanently_denied" in text
    facts = read(DEVICE_FACTS)
    assert "RECORD_AUDIO" in facts, "the phone must gate audio.capture on RECORD_AUDIO"
