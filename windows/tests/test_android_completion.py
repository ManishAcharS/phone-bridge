"""Static regression tests for the Android app-completion work.

What this phase had to make true, and what these tests pin down:

1. **Capabilities are measured, not asserted.** The old `capabilities()` map
   hard-coded `camera.back`, `camera.front`, `sensors` and `file_transfer` to
   `true`. Every capability must now come from a hardware probe and must be
   `false` until the feature is implemented.
2. **Permissions are requested when needed**, with the denied / permanently
   denied / not-applicable cases handled differently, and a revocation while the
   app is open must be noticed.
3. **The UI and `system.info` agree**, because both read `DeviceFacts`.
4. **Nothing regressed**: the audio probe keeps its permission guards, the
   protocol layer is untouched (v1), and the service still owns its lifecycle.

As usual in this repo, there is no Android SDK here, so these are source-level
checks plus a kotlinc declaration check reported separately. They are not a
substitute for `gradlew assembleDebug` or a device run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
KOTLIN = REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
MANIFEST = REPO / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
DOCS_PROTOCOL = REPO / "docs" / "03-protocol.md"

DEVICE_FACTS = KOTLIN / "net" / "DeviceFacts.kt"
BRIDGE_SERVER = KOTLIN / "net" / "BridgeServer.kt"
PERMISSIONS = KOTLIN / "Permissions.kt"
PERMISSIONS_CARD = KOTLIN / "PermissionsCard.kt"
CAPABILITIES_CARD = KOTLIN / "CapabilitiesCard.kt"
UI = KOTLIN / "Ui.kt"
ACTIVITY = KOTLIN / "MainActivity.kt"
PROBE_CARD = KOTLIN / "ProbeCard.kt"
CAPABILITY_PROBE = KOTLIN / "net" / "CapabilityProbe.kt"
SERVICE = KOTLIN / "service" / "BridgeService.kt"
JSON = KOTLIN / "protocol" / "Json.kt"
MESSAGES = KOTLIN / "protocol" / "Messages.kt"


def read(path: Path) -> str:
    return path.read_text("utf-8")


def segment_after(text: str, signature: str, end: str) -> str:
    """Text from ``signature`` up to the next ``end`` marker."""
    start = text.index(signature)
    stop = text.index(end, start)
    return text[start:stop]


def body_after(text: str, signature: str) -> str:
    start = text.index(signature)
    open_index = text.index("{", start)
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index]
    raise AssertionError(f"unbalanced braces after {signature!r}")


# --------------------------------------------------------------------------- #
# 1. capabilities are measured, not asserted
# --------------------------------------------------------------------------- #
CAPABILITY_KEYS = {
    "audio.playback",
    "audio.capture",
    "audio.bluetooth_sco",
    "camera.back",
    "camera.front",
    "sensors",
    "file_transfer",
    "bluetooth_scan",
}


def declared_capability_keys() -> set[str]:
    return set(re.findall(r'key\s*=\s*"([a-z_.]+)"', read(DEVICE_FACTS)))


def test_every_capability_is_declared_once_in_device_facts() -> None:
    assert declared_capability_keys() == CAPABILITY_KEYS


def test_no_capability_is_hard_coded_as_supported() -> None:
    """The specific lie: `"camera.back" to true` and friends in the RPC layer."""
    offenders: list[str] = []
    for path in sorted(KOTLIN.rglob("*.kt")):
        text = read(path)
        for key in CAPABILITY_KEYS:
            for match in re.finditer(rf'"{re.escape(key)}"\s+to\s+true', text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(REPO)}:{line} claims {key}=true")
    assert not offenders, "\n".join(offenders)


def test_only_shipped_phases_are_marked_implemented() -> None:
    """`implemented` may only be true for phases that actually shipped.

    Through Phase 1 every row was false, and the test said "nothing". Phases 2 and
    4 shipped audio and the camera, so the honest assertion is now per key: those
    two are implemented, and everything still waiting (sensors, file transfer,
    Bluetooth scanning) must stay false or absent.
    """
    rows = read(DEVICE_FACTS)
    assert "implemented = false" in rows, "unshipped rows must still say false"
    implemented = set(re.findall(r'key = "([a-z_.]+)"[^)]*?implemented = true', rows, re.S))
    assert implemented == {"camera.back", "camera.front"}, (
        f"unexpected implemented rows: {sorted(implemented)}"
    )
    for later in ("sensors", "file_transfer", "bluetooth_scan"):
        start = rows.index(f'key = "{later}"')
        block = rows[start : start + 400]
        assert "implemented = false" in block, (
            f"{later} is not implemented and must say so"
        )


def test_capabilities_are_gated_on_hardware_and_permission() -> None:
    text = read(DEVICE_FACTS)
    body = segment_after(text, "fun available(", "fun summary(")
    assert "hardware == true" in body
    assert "implemented" in body
    assert "permission != null || permissionGranted" in body or "permissionGranted" in body


def test_the_wire_map_carries_hardware_and_reasons() -> None:
    text = read(DEVICE_FACTS)
    body = body_after(text, "fun capabilities(context: Context)")
    assert 'out["hardware.${row.key}"]' in body, (
        "a PC must be able to tell hardware presence from availability"
    )
    assert 'out["detail.${row.key}"]' in body, "false must come with a reason"
    assert 'out["protocol_version"]' in body


def test_bridge_server_delegates_capabilities_to_device_facts() -> None:
    text = read(BRIDGE_SERVER)
    assert "private fun capabilities(): Map<String, Any?> = DeviceFacts.capabilities(context)" in text


# --------------------------------------------------------------------------- #
# 2. device information is complete and shared
# --------------------------------------------------------------------------- #
REQUIRED_DEVICE_KEYS = {
    "model",
    "manufacturer",
    "brand",
    "device",
    "product",
    "android_release",
    "android_version",
    "sdk_int",
    "target_sdk",
    "app_version",
    "app_version_code",
    "protocol_version",
    "rom",
}


def device_info_keys() -> set[str]:
    body = segment_after(read(DEVICE_FACTS), "fun deviceInfo(", "/** e.g.")
    return set(re.findall(r'"([a-z_]+)"\s+to\s', body))


def test_the_device_block_reports_everything_the_ui_claims() -> None:
    missing = REQUIRED_DEVICE_KEYS - device_info_keys()
    assert not missing, f"DeviceFacts.deviceInfo is missing {sorted(missing)}"


def test_system_info_serves_the_same_device_block_as_the_ui() -> None:
    server = read(BRIDGE_SERVER)
    info = body_after(server, '"system.info" ->')
    assert "DeviceFacts.deviceInfo(context)" in info, (
        "two sources for the device block would drift"
    )
    # The capability map itself is built once, in capabilities(); the block must
    # use it rather than assembling a second copy.
    assert 'info["capabilities"] = capabilities()' in info
    assert 'info["permissions"] = Permissions.wireSnapshot(context)' in info
    assert 'info["capabilities"] = capabilities()' in info
    # The pre-existing fields the PC already read must survive.
    for legacy in ("device_id", "name"):
        assert f'"{legacy}" to' in info


def test_ui_reads_the_device_block_rather_than_rebuilding_it() -> None:
    activity = read(ACTIVITY)
    assert "DeviceFacts.deviceInfo(context)" in activity
    for key in ('facts["model"]', 'facts["android_release"]', 'facts["sdk_int"]',
                'facts["app_version"]', 'facts["app_version_code"]'):
        assert key in activity, f"the device card does not show {key}"


# ----------------------------------------------------------------- ---------- #
# 3. permissions
# --------------------------------------------------------------------------- #
RUNTIME_PERMISSIONS = {
    "POST_NOTIFICATIONS",
    "RECORD_AUDIO",
    "BLUETOOTH_CONNECT",
    "CAMERA",
}


def manifest_permissions() -> set[str]:
    return set(
        re.findall(r'android:name="android\.permission\.([A-Z_]+)"', read(MANIFEST))
    )


def modelled_permissions() -> set[str]:
    text = read(PERMISSIONS)
    body = segment_after(text, "val runtime: List<Item> = listOf(", "fun applicable(")
    return set(re.findall(r"Manifest\.permission\.([A-Z_]+)", body))


def test_every_runtime_permission_the_app_declares_is_modelled() -> None:
    declared = manifest_permissions() & RUNTIME_PERMISSIONS
    assert declared == modelled_permissions(), (
        "the permission panel and the manifest disagree about runtime permissions"
    )


def test_no_permission_is_requested_that_the_manifest_does_not_declare() -> None:
    # Every Manifest.permission.* the app mentions must be declared, so a request
    # can never silently do nothing.
    mentioned: set[str] = set()
    for path in KOTLIN.rglob("*.kt"):
        mentioned |= set(re.findall(r"Manifest\.permission\.([A-Z_]+)", read(path)))
    missing = mentioned - manifest_permissions()
    assert not missing, f"declared nowhere in the manifest: {sorted(missing)}"


def test_permissions_that_nothing_uses_are_not_declared() -> None:
    """Holding a permission the app cannot use is a false capability claim."""
    declared = manifest_permissions()
    for unused in ("BLUETOOTH_SCAN", "ACCESS_FINE_LOCATION"):
        assert unused not in declared, (
            f"{unused} is declared but nothing uses it; add it with the feature"
        )
    # ...and the UI says so, instead of leaving the user guessing.
    future = read(PERMISSIONS)
    for name in ("BLUETOOTH_SCAN", "ACCESS_FINE_LOCATION"):
        assert name in future, f"{name} should appear in futurePermissions with a reason"


def test_denied_and_permanently_denied_are_distinguished() -> None:
    text = read(PERMISSIONS)
    assert "shouldShowRequestPermissionRationale" in text
    assert "PERMANENTLY_DENIED" in text
    assert "wasAsked" in text and "markAsked" in text, (
        "without a record of having asked, a first denial is indistinguishable "
        "from a permanent one"
    )


def test_the_permanently_denied_case_opens_app_settings() -> None:
    perms = read(PERMISSIONS)
    assert "Settings.ACTION_APPLICATION_DETAILS_SETTINGS" in perms
    card = read(PERMISSIONS_CARD)
    assert "Permissions.openAppSettings" in card, (
        "a blocked permission needs the settings route, not another request"
    )
    assert "Permissions.Status.PERMANENTLY_DENIED -> OutlinedButton" in card.replace("\n", " ")
    assert "Settings\")" in card, "the blocked row needs a button that opens settings"


def test_each_permission_explains_why_it_is_needed() -> None:
    text = read(PERMISSIONS)
    # One per runtime permission: notifications, microphone, Bluetooth, camera.
    assert text.count("why = ") == 4
    assert text.count("withoutIt = ") == 4
    # The panel shows the reason for a denial, not just a red label.
    assert "item.why" in read(PERMISSIONS_CARD)
    assert "item.withoutIt" in read(PERMISSIONS_CARD)


def test_revocation_while_running_is_noticed_and_logged() -> None:
    card = read(PERMISSIONS_CARD)
    assert "LaunchedEffect(Unit)" in card
    assert "Permissions.snapshot(context)" in card
    assert "BridgeState.note(" in card, (
        "a revocation must reach the bridge log, because it changes what the PC "
        "can ask for"
    )
    assert "revoked" in card.lower()


def test_the_blocked_verdict_reaches_the_service_too() -> None:
    """`system.info` is answered from a Service, which has no Activity and so
    cannot call shouldShowRequestPermissionRationale itself."""
    text = read(PERMISSIONS)
    assert "BLOCKED_PREFIX" in text
    assert "fun blocked(context: Context, permission: String): Boolean" in text
    status = segment_after(text, "fun status(context: Context, item: Item): Status", "fun snapshot(")
    assert "setFlag(context, BLOCKED_PREFIX" in status
    assert "context as? Activity" in status


def test_permission_wire_keys_are_the_real_permission_names() -> None:
    text = read(PERMISSIONS)
    assert "item.shortName to status.name.lowercase()" in text
    doc = read(DOCS_PROTOCOL)
    assert "RECORD_AUDIO" in doc and "permanently_denied" in doc


def test_permissions_are_requested_at_the_moment_of_use_not_at_launch() -> None:
    """Every request is triggered by a user action, never by composition.

    Two launchers exist since Phase 4: notifications from `startBridge()`, and the
    camera from the camera card's Allow button. Both are inside a callback, which
    is the whole point — nothing asks on launch.
    """
    activity = read(ACTIVITY)
    launch_sites = re.findall(r"\.launch\(", activity)
    assert len(launch_sites) == 2, (
        "MainActivity should launch exactly two permission requests: "
        "notifications from startBridge(), camera from the camera card"
    )
    start = body_after(activity, "fun startBridge()")
    assert "notificationPermission.launch(" in start
    # No eager request anywhere in the launch path.
    for path in (ACTIVITY,):
        text = read(path)
        for match in re.finditer(r"LaunchedEffect\([^)]*\)\s*\{", text):
            block = body_after(text[match.start() :], text[match.start() : match.end()])
            assert ".launch(" not in block, "a LaunchedEffect requests a permission"


def test_the_probe_waits_for_the_permission_answer() -> None:
    """The old flow launched the request and started the probe immediately."""
    card = read(PROBE_CARD)
    assert "requestPermissionsThenRun()" in card
    assert "pendingRun" in card
    callback = body_after(card, "ActivityResultContracts.RequestMultiplePermissions()")
    assert "runProbe()" in callback, "the probe must start from the permission result"
    launcher = card[card.index("fun requestPermissionsThenRun()") :]
    assert "permissionLauncher.launch(" in launcher
    # ...and the probe itself is only started from that callback or when nothing
    # is missing.
    assert card.count("runProbe()") >= 2


def test_the_probe_still_refuses_to_touch_audio_without_permission() -> None:
    """No safety regression: Phase 1's guards in the probe stay in place."""
    probe = read(CAPABILITY_PROBE)
    assert probe.count("hasPermission(context, Manifest.permission.RECORD_AUDIO)") >= 3
    assert "RECORD_AUDIO was not granted" in probe


# --------------------------------------------------------------------------- #
# 4. UI: status, errors, and the cards are actually wired in
# --------------------------------------------------------------------------- #
def test_status_comes_from_one_place() -> None:
    ui = read(UI)
    assert re.search(r"enum class BridgeStatus \{ STOPPED, LISTENING, CONNECTED, ERROR \}", ui)
    status = body_after(ui, "internal fun statusOf(")
    for state in ("BridgeStatus.ERROR", "BridgeStatus.CONNECTED",
                  "BridgeStatus.LISTENING", "BridgeStatus.STOPPED"):
        assert state in status
    activity = read(ACTIVITY)
    assert "statusOf(running, peers.size, lastError)" in activity
    assert "StatusPill(status, peers.size)" in activity


def test_the_dashboard_shows_every_section_this_phase_promised() -> None:
    activity = read(ACTIVITY)
    for section in (
        "Section(\"Bridge\")",
        "PermissionsCard()",
        "Section(\"This device\")",
        "CapabilitiesCard()",
        "Section(\"Connected PCs\")",
    ):
        assert section in activity, f"the dashboard no longer shows {section}"
    assert "BridgeService.start(context)" in activity
    assert "BridgeService.stop(context)" in activity


def test_the_connect_address_is_shown() -> None:
    activity = read(ACTIVITY)
    assert "DeviceFacts.lanIpv4()" in activity
    assert "ws://$lanIp:$port" in activity
    assert "adb forward tcp:$port tcp:$port" in activity


def test_failures_come_with_an_explanation_not_just_a_string() -> None:
    activity = read(ACTIVITY)
    assert "explainBridgeError(lastError, port)" in activity
    ui = read(UI)
    body = body_after(ui, "internal fun explainBridgeError(")
    assert "eaddrinuse" in body.lower()
    assert "port $port" in body


def test_the_capability_card_is_fed_by_the_same_object_as_the_pc() -> None:
    card = read(CAPABILITIES_CARD)
    assert "DeviceFacts.capabilityRows(context)" in card
    assert "row.summary(" in card
    assert "row.available(" in card


def test_shared_layout_helpers_live_in_one_file() -> None:
    ui = read(UI)
    assert "internal fun Section(" in ui
    assert "internal fun KeyValue(" in ui
    activity = read(ACTIVITY)
    assert "private fun Section(" not in activity, (
        "two copies of Section would drift; the shared one is in Ui.kt"
    )
    assert "private fun KeyValue(" not in activity


# --------------------------------------------------------------------------- #
# 5. lifecycle and protocol must not regress
# --------------------------------------------------------------------------- #
def test_service_cancels_its_state_collectors_on_stop() -> None:
    """Collectors were launched per start and never cancelled: N notifiers after
    N restarts."""
    text = read(SERVICE)
    assert "private var stateJobs: List<Job>" in text
    stop = body_after(text, "private fun stopBridge()")
    assert "stateJobs.forEach { it.cancel() }" in stop


def test_duplicate_start_is_a_no_op() -> None:
    text = read(SERVICE)
    start = body_after(text, "private fun startBridge()")
    assert "if (server != null) return" in start
    command = body_after(text, "override fun onStartCommand(")
    assert "if (server == null) startBridge()" in command


def test_protocol_version_is_untouched() -> None:
    assert "const val PROTOCOL_VERSION = 1" in read(KOTLIN / "protocol" / "Framing.kt")


def test_nested_maps_are_converted_before_encoding() -> None:
    """org.json stores a nested Map as an object and prints it as `{a=1}`."""
    text = read(JSON)
    assert "fun mapToJson(map: Map<String, Any?>): JSONObject" in text
    assert "private fun wrap(value: Any?): Any" in text
    assert "is Map<*, *>" in text and "is Iterable<*>" in text
    assert "encodeToBytes(mapToJson(map))" in text
    messages = read(MESSAGES)
    assert '"result" to result' in messages, (
        "wrapping the result in JSONObject only converts the top level"
    )


# --------------------------------------------------------------------------- #
# 6. the protocol document and the code agree on the capability keys
# --------------------------------------------------------------------------- #
def test_documented_capability_keys_match_the_code() -> None:
    doc = read(DOCS_PROTOCOL)
    documented = set(re.findall(r"`((?:audio|camera|file_transfer|sensors|bluetooth_scan)[a-z_.]*)`", doc))
    missing = CAPABILITY_KEYS - documented
    assert not missing, f"documented nowhere in docs/03: {sorted(missing)}"
    assert "hardware." in doc and "detail." in doc, (
        "docs/03 must explain the three-part capability contract"
    )


def test_compose_delegates_import_their_operators() -> None:
    """`var x by remember { ... }` needs androidx.compose.runtime.setValue.

    kotlinc in this environment cannot check it: `remember`/`mutableStateOf` are
    themselves unresolved without the Compose artifacts, so the delegate
    resolution never happens and no diagnostic is produced. A real build would
    fail, which is exactly the class of error this test is here to prevent.
    """
    problems: list[str] = []
    for path in sorted(KOTLIN.rglob("*.kt")):
        text = read(path)
        if " by remember" not in text and "collectAsState()" not in text:
            continue
        if "import androidx.compose.runtime.getValue" not in text:
            problems.append(f"{path.name}: uses a Compose delegate without getValue")
        if re.search(r"\bvar\s+\w+\s+by\s+", text) and (
            "import androidx.compose.runtime.setValue" not in text
        ):
            problems.append(f"{path.name}: declares `var ... by` without setValue")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "path",
    [DEVICE_FACTS, PERMISSIONS, PERMISSIONS_CARD, CAPABILITIES_CARD, UI, ACTIVITY],
    ids=lambda p: p.name,
)
def test_new_files_have_no_empty_catch_blocks(path: Path) -> None:
    from kotlin_scan import mask_source

    masked = mask_source(read(path))
    for match in re.finditer(r"\bcatch\s*\([^)]*\)\s*\{", masked):
        open_index = masked.index("{", match.start())
        depth = 0
        for index in range(open_index, len(masked)):
            if masked[index] == "{":
                depth += 1
            elif masked[index] == "}":
                depth -= 1
                if depth == 0:
                    assert masked[open_index + 1 : index].strip(), (
                        f"{path.name}: empty catch at line "
                        f"{read(path).count(chr(10), 0, open_index) + 1}"
                    )
                    break
