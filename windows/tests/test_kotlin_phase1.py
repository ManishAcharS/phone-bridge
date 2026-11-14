"""Static regression tests for the Phase 1 Kotlin changes.

There is no Android SDK in this environment, so these tests cannot *run* the
bridge. What they can do is pin the parts of the source that a compiler would
not: the ordering that makes the foreground-service contract hold, the fact
that a refusal is sent before the peer is told the session is up, that no
`catch` block silently swallows an error, and that the old one-argument
`answerPairing(boolean)` call site cannot come back.

Compilation itself is a separate, honest step, and it cannot be done here: there
is no Android SDK. `kotlinc 1.9.24` over the pre-Phase-1 tree reports 869
diagnostics and over this one 892; the difference is entirely "unresolved
reference" lines for platform APIs the new code calls (`ConnectivityManager`,
`Network`, `registerDefaultNetworkCallback`, `stopForeground`, ...) plus the
`MutableStateFlow.value` cascade, none of which the compiler can resolve without
the SDK. No diagnostic mentions a symbol this phase introduced.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from kotlin_scan import mask_source

REPO = Path(__file__).resolve().parents[2]
KOTLIN = REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
SERVER = KOTLIN / "net" / "BridgeServer.kt"
BEACON = KOTLIN / "net" / "DiscoveryBeacon.kt"
SERVICE = KOTLIN / "service" / "BridgeService.kt"
STATE = KOTLIN / "service" / "BridgeState.kt"
ACTIVITY = KOTLIN / "MainActivity.kt"
MANIFEST = REPO / "android" / "app" / "src" / "main" / "AndroidManifest.xml"

PHASE1_KOTLIN = [SERVER, BEACON, SERVICE, STATE, ACTIVITY]


def read(path: Path) -> str:
    return path.read_text("utf-8")


def body_after(text: str, signature: str) -> str:
    """The `{...}` body of the first declaration whose signature contains ``signature``."""
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
# 1. pairing: one decision, one request
# --------------------------------------------------------------------------- #
def test_answer_pairing_requires_the_token_the_user_saw() -> None:
    """The UI used to send a bare boolean, which answered whichever request
    happened to be first - possibly a different PC than the SAS on screen."""
    text = read(SERVER)
    assert re.search(r"fun answerPairing\(token: Long, approve: Boolean\)", text), (
        "answerPairing must take the token of the request shown to the user"
    )
    assert "pendingPairing.firstOrNull { it.token == token }" in text


def test_unknown_pairing_token_is_refused_loudly() -> None:
    body = body_after(read(SERVER), "fun answerPairing(token: Long")
    missing = body[body.index("if (target == null)") :]
    assert 'addLog("WARN"' in missing, "a stale token must be logged, not applied"
    assert "onUserPairingDecision" not in missing.split("return")[0], (
        "an unknown token must not answer a session"
    )


def test_no_call_site_uses_the_old_one_argument_pairing() -> None:
    offenders = [
        str(path.relative_to(REPO))
        for path in KOTLIN.rglob("*.kt")
        if re.search(r"answerPairing\(\s*(true|false)\s*\)", read(path))
    ]
    assert not offenders, offenders


def test_pairing_is_denied_when_the_server_stops() -> None:
    body = body_after(read(SERVER), "fun stop()")
    assert "denyAllPairings(" in body, "pending pairings would outlive the server"


# --------------------------------------------------------------------------- #
# 2. session limits
# --------------------------------------------------------------------------- #
def test_session_limits_exist_and_are_enforced_on_the_ready_path() -> None:
    text = read(SERVER)
    assert "MAX_SESSIONS_PER_DEVICE = 4" in text
    assert "MAX_SESSIONS = 8" in text
    ready = body_after(text, "private fun markReady(")
    assert "admitSession(" in ready, "markReady no longer enforces the ceiling"
    assert not re.search(r"\bfun registerSession\b", text), (
        "the unchecked registration helper must stay gone: two registration "
        "paths is how the ceiling became dead code"
    )


def test_ready_messages_are_only_sent_to_an_admitted_session() -> None:
    text = read(SERVER)
    for marker in (
        'if (!markReady()) return',
        "if (!markReady()) {",
    ):
        assert marker in text, (
            "auth_ok / pair_ok must be gated on admission, otherwise the phone "
            "tells a peer the session is up and then drops it"
        )


def test_half_open_sockets_are_capped_before_a_session_exists() -> None:
    body = body_after(read(SERVER), "override fun onOpen(")
    assert "byConnection.size >= MAX_SESSIONS" in body
    assert "1008" in body, "refusals must carry a close code the PC can show"


# --------------------------------------------------------------------------- #
# 3. timeouts
# --------------------------------------------------------------------------- #
def test_setup_timeout_is_armed_and_cancelled() -> None:
    text = read(SERVER)
    assert "SETUP_TIMEOUT_S = 20" in text
    init = body_after(text, "init {")
    assert "setupTimer?.schedule(" in init, "a silent socket can hold setup forever"
    for cancel_site in ("private fun markReady(", "fun close(", "fun onClosed("):
        assert "cancelSetupTimeout()" in body_after(text, cancel_site), cancel_site


# --------------------------------------------------------------------------- #
# 4. BYE and honest error reporting
# --------------------------------------------------------------------------- #
def test_graceful_stop_sends_bye_first() -> None:
    text = read(SERVER)
    assert re.search(r'close\("server stopping", bye = true\)', text), (
        "the PC must be able to tell an intentional stop from a dropped link"
    )
    secure = body_after(text, "private fun onSecure(")
    assert "Messages.MSG_BYE ->" in secure, "an inbound BYE must close the session"


def test_send_failures_close_the_session_instead_of_being_swallowed() -> None:
    text = read(SERVER)
    send_raw = body_after(text, "private fun sendRaw(")
    catch = send_raw[send_raw.index("catch") :]
    assert 'close("send failed")' in catch, "a dead socket must not stay 'open'"
    assert 'addLog("ERROR"' in catch, "the failure must be visible in the log"
    send_secure = body_after(text, "fun sendSecure(")
    assert "no session keys yet" in send_secure, "a dropped frame must be logged"


def test_malformed_hello_is_refused_before_pairing() -> None:
    text = read(SERVER)
    assert re.search(r"DEVICE_ID = Regex\(", text)
    hello = body_after(text, "private fun onHello(")
    assert "DEVICE_ID.matches(claimedId)" in hello, (
        "an empty or bogus device id must not reach the pairing store"
    )


# --------------------------------------------------------------------------- #
# 5. no silent swallowing in the files Phase 1 touched
# --------------------------------------------------------------------------- #
def _empty_catch_blocks(text: str) -> list[int]:
    """Line numbers of `catch (...) { }` blocks with no statements."""
    masked = mask_source(text)
    offences: list[int] = []
    for match in re.finditer(r"\bcatch\s*\([^)]*\)\s*\{", masked):
        open_index = masked.index("{", match.start())
        depth = 0
        for index in range(open_index, len(masked)):
            if masked[index] == "{":
                depth += 1
            elif masked[index] == "}":
                depth -= 1
                if depth == 0:
                    inner = masked[open_index + 1 : index].strip()
                    if not inner:
                        offences.append(text.count("\n", 0, open_index) + 1)
                    break
    return offences


@pytest.mark.parametrize("path", PHASE1_KOTLIN, ids=lambda p: p.name)
def test_no_empty_catch_blocks(path: Path) -> None:
    offences = _empty_catch_blocks(read(path))
    assert not offences, f"{path.name}: empty catch block(s) at line(s) {offences}"


def test_the_catch_scanner_detects_an_empty_handler() -> None:
    """Without this, the test above passes when the scanner stops working."""
    sample = "fun f() {\n    try { g() } catch (e: Exception) { }\n}\n"
    assert _empty_catch_blocks(sample) == [2]


# --------------------------------------------------------------------------- #
# 6. Android background / foreground lifecycle
# --------------------------------------------------------------------------- #
def test_foreground_contract_is_satisfied_before_any_action() -> None:
    """Android 12+ kills the process if startForegroundService() is not followed
    by startForeground(), including on the stop and restart paths."""
    text = read(SERVICE)
    body = body_after(text, "override fun onStartCommand(")
    foreground = body.index("startForeground(")
    action = body.index("when (intent?.action)")
    assert foreground < action, "startForeground() must come before the action switch"


def test_a_failed_bind_is_retried_before_giving_up() -> None:
    text = read(SERVICE)
    assert "START_RETRY_DELAYS_MS" in text
    assert "retryStart(" in body_after(text, "private fun startBridge()")
    assert "stopSelf()" in body_after(text, "private fun retryStart("), (
        "giving up must still shut the service down honestly"
    )


def test_network_changes_rebind_the_beacon() -> None:
    text = read(SERVICE)
    assert "registerDefaultNetworkCallback" in text
    assert "unregisterNetworkCallback" in body_after(text, "private fun stopBridge()")
    assert "b.restart()" in body_after(text, "private fun watchNetwork(")


def test_notification_is_removed_with_the_service() -> None:
    assert "stopForeground" in body_after(read(SERVICE), "override fun onDestroy()")


def test_service_owns_the_restart_order() -> None:
    text = read(SERVICE)
    assert "fun restart(context: Context)" in text
    activity = mask_source(read(ACTIVITY))
    restart_body = body_after(activity, "private fun restartService(")
    assert "postDelayed" not in restart_body, (
        "the UI must not race the old socket's teardown"
    )
    assert "BridgeService.restart(context)" in restart_body


def test_beacon_restart_cannot_leak_the_previous_worker() -> None:
    """Two daemons per interval used to keep broadcasting after a network change."""
    text = read(BEACON)
    assert "fun restart()" in text
    assert "while (!myCancel.get())" in text, "the loop must use the per-run flag"
    assert "while (running.get())" not in text, (
        "a shared flag cannot cancel a worker that is sleeping"
    )


def test_manifest_still_declares_the_connected_device_service() -> None:
    manifest = read(MANIFEST)
    assert 'android:foregroundServiceType="connectedDevice|camera|microphone|mediaPlayback"' in manifest
    assert "FOREGROUND_SERVICE_CONNECTED_DEVICE" in manifest
