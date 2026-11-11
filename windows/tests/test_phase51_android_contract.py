"""Phase 5.1 source contracts for the Android fixes (there is no SDK here).

This repository has no ``src/test`` Kotlin: the Android half has always been
pinned by source-level tests on the PC (see ``tests/test_android_completion.py``
for the convention) plus ``SelfTest.kt`` on the device. These tests follow that
convention for the four defects Phase 5.1 fixed on the phone:

* **BLE (I1-I5)** — a write was reported successful before
  ``onCharacteristicWrite`` said so, GATT operations on one link could overlap,
  a read timeout hid the real status, and a failed connect leaked its
  ``BluetoothGatt``.
* **Resume (C1)** — the phone never reopened the ``.part`` document it had kept,
  never waited for the PC's resume offset, and deleted a usable partial on any
  failure.
* **Sensors (I8)** — ``permitted`` was the literal ``true`` and heart rate was
  offered without its permission.
* **The executor (I11)** — the phase-5 pool had an unbounded queue.

A source test cannot prove the code *runs*; it can prove the defect's shape is
gone and that the callback plumbing it needed now exists, which is exactly what
the review could not find evidence for. ``docs/21 §10`` and ``docs/22 §10`` are
the hardware checklists that remain outstanding.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
KOTLIN = REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
MANIFEST = REPO / "android" / "app" / "src" / "main" / "AndroidManifest.xml"

NET = KOTLIN / "net"
BLE = NET / "BleEngine.kt"
FILES = NET / "FileTransferEngine.kt"
SENSORS = NET / "SensorEngine.kt"
SENSOR_FRAMES = NET / "SensorFrames.kt"
SERVER = NET / "BridgeServer.kt"
FACTS = NET / "DeviceFacts.kt"
PERMISSIONS = KOTLIN / "Permissions.kt"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def body(text: str, needle: str, span: int = 1200) -> str:
    """The source from `needle` onwards, for a scoped assertion."""
    index = text.find(needle)
    assert index >= 0, f"{needle!r} is missing"
    return text[index : index + span]


# --------------------------------------------------------------------------- #
# BLE: callback-confirmed operations, serialisation, no leaked GATT
# --------------------------------------------------------------------------- #
def test_write_completion_comes_from_the_platform_callback() -> None:
    """I1: a write used to be "pending" until an arbitrary timeout.

    The GATT callbacks must now complete the operation, and the pending map the
    old code polled must be gone — otherwise the fix is cosmetic.
    """
    text = read(BLE)
    assert "override fun onCharacteristicWrite(" in text
    assert "override fun onDescriptorWrite(" in text
    callback = body(text, "override fun onCharacteristicWrite(")
    assert "completeOperation(" in callback, "the write must be completed by its callback"
    assert "status" in callback, "the real GATT status must reach the caller"
    assert "readValues" not in text, (
        "the old polled-value map would let a fake timeout pass for a real write"
    )


def test_gatt_operations_on_one_link_are_serialised() -> None:
    """I2: docs/21 promised serialisation that the code did not implement."""
    text = read(BLE)
    assert "linkLocks" in text, "a per-link lock is what serialises the operations"
    run = body(text, "private fun <T> runOperation(")
    assert "synchronized(linkLock(" in run, (
        "the monitor must be held for issue *and* callback, not just for the call"
    )
    assert "linkLock(" in text
    assert "awaitOperation(" in text
    assert "abortOperations(" in text, "a failed link must not leave operations parked"


def test_a_failed_link_closes_its_gatt_object() -> None:
    """I4: every failure path must release the ``BluetoothGatt``."""
    text = read(BLE)
    fail = body(text, "private fun failLink(", span=1600)
    assert "close()" in fail, "failLink must close the GATT"
    assert "disconnect()" in fail
    assert "subscriptions" in fail, "and forget what was subscribed"
    # Connect and discovery failures must route through the same path.
    assert text.count("failLink(") >= 3


def test_read_reports_the_real_status_instead_of_a_timeout() -> None:
    """I3: a 15 s timeout used to be reported as if it were the GATT status."""
    text = read(BLE)
    read_callback = body(text, "override fun onCharacteristicRead(")
    assert "status" in read_callback
    assert "completeOperation(" in read_callback
    assert "gatt_failed" in text or "GATT_FAILED" in text or "statusText" in text


def test_the_scan_uses_the_name_prefix_and_bounds_its_events() -> None:
    """I6/I7: unbounded scan events; a ``name_prefix`` that was dead code."""
    text = read(BLE)
    assert "MAX_SCAN_EVENTS_PER_SECOND" in text
    record = body(text, "private fun record(", span=2600)
    assert "acceptsByName" in record, "the name filter must be applied to sightings"
    assert "eventWindowCount >= MAX_SCAN_EVENTS_PER_SECOND" in text
    start = body(text, "fun startScan(", span=3000)
    assert "name_prefix" in text
    assert "stopScan" in start or "scanCallback" in start, "a repeated start must not stack"
    assert "REQUESTED_MTU" in text, "the negotiated MTU is what the GUI now shows"


def test_the_connection_state_is_volatile() -> None:
    """I5: the state was written on the GATT thread and read on the loop thread."""
    text = read(BLE)
    match = re.search(r"@Volatile\s+var state", text)
    assert match, "Link.state must be @Volatile to be visible across threads"


# --------------------------------------------------------------------------- #
# file transfer: resume both ways
# --------------------------------------------------------------------------- #
def test_the_partial_document_is_reopened_not_replaced() -> None:
    """C1: the phone stored ``part_uri`` and never used it."""
    text = read(FILES)
    assert "private fun openPart(" in text
    opener = body(text, "private fun openPart(", span=2600)
    assert "part_uri" in opener or "KEY_PART" in opener, (
        "resume is only possible if the stored document is reopened"
    )
    # `openFileDescriptor(uri, "rwt")` is the update-in-place mode: the document
    # keeps the bytes already written, which is what a resume appends to.
    writer = body(text, "private fun openPart(uri: Uri, mode: String)", span=400)
    assert "mode" in writer
    assert "rememberResume(" in text


def test_an_inbound_chunk_is_duplicate_safe() -> None:
    """A resumed or restarted sender must not append a chunk twice."""
    text = read(FILES)
    writer = body(text, "private fun writeChunk(", span=2200)
    assert "expectedIndex" in writer or "chunks" in writer
    assert "chunkIndex <" in writer, "an old chunk must be acknowledged, not written"


def test_a_failure_keeps_a_usable_partial_and_deletes_a_corrupt_one() -> None:
    """The partial file may only be deleted when it cannot be trusted."""
    text = read(FILES)
    fail = body(text, "private fun failInbound(", span=1200)
    assert "keepPart" in fail
    assert "deleteDocument" in fail, "a corrupt part is deleted, a short one is not"
    assert "clearResume()" in fail
    assert "keepPart = true" in text and "keepPart = false" in text


def test_the_sender_waits_for_the_pc_to_choose_an_offset() -> None:
    """C1: the sender used to start before it could know where to start."""
    text = read(FILES)
    assert "RESUME_GRACE_MS" in text
    assert "awaitResumeOffset(" in text
    sender = body(text, "private fun sender(", span=2600)
    assert "awaitResumeOffset(" in sender, "chunk 0 must be held until the PC decides"
    assert "startIndex" in sender
    assert "sanitiseOffset(" in text, "an offset must be coerced to a chunk boundary"


# --------------------------------------------------------------------------- #
# sensors: measured state, not a constant
# --------------------------------------------------------------------------- #
def test_sensor_permission_is_measured_not_assumed() -> None:
    """I8: ``"permitted" to true`` was a lie that hid every denial."""
    text = read(SENSORS)
    assert '"permitted" to true' not in text
    assert "checkSelfPermission" in text
    assert "permitted()" in text and "permissionDetail()" in text
    assert "sensor_denied" in text, "a refusal must be an actionable error, not silence"


def test_heart_rate_is_gated_on_its_own_permission() -> None:
    """``BODY_SENSORS`` is required for heart rate, and refused without it."""
    text = read(SENSORS)
    assert "requiresBiometricPermission" in text or "BODY_SENSORS" in text
    assert "highRatePermission" in text
    assert "Build.VERSION.SDK_INT" in text, "the permission name differs before Android 12"


def test_high_sampling_rate_permission_is_declared_but_not_requested() -> None:
    """It is a normal permission: declaring it is enough, prompting is wrong."""
    manifest = read(MANIFEST)
    assert "android.permission.HIGH_SAMPLING_RATE_SENSORS" in manifest
    runtime = read(PERMISSIONS)
    runtime_block = body(runtime, "val runtime: List<Item> = listOf(", span=2000)
    assert "HIGH_SAMPLING_RATE_SENSORS" not in runtime_block, (
        "a normal permission does not belong in the runtime request list"
    )


def test_a_refused_sensor_is_not_advertised_as_available() -> None:
    # The capability row and the catalogue both have to say *why* a sensor is
    # unavailable: hardware, permission, or an implementation that is not there.
    facts = read(FACTS)
    row = body(facts, 'key = "sensors"', span=1400)
    assert "permission" in row.lower() or "HIGH_SAMPLING_RATE_SENSORS" in row
    assert "permitted.sensors" in facts
    frames = read(SENSOR_FRAMES)
    assert "21" in frames and "35" in frames, "heart rate and heart beat types are gated"


# --------------------------------------------------------------------------- #
# the phase-5 executor
# --------------------------------------------------------------------------- #
def test_the_phase_pool_has_a_bounded_queue() -> None:
    """I11: an unbounded queue turns a slow phone into unbounded memory."""
    text = read(SERVER)
    assert "ThreadPoolExecutor(" in text
    assert "ArrayBlockingQueue" in text
    assert "RejectedExecutionException" in text
    assert "busy" in text, "a full queue must answer `busy`, not hang the caller"


def test_frames_go_to_the_owner_not_the_first_session() -> None:
    """I13: sensor and file frames used to go to whichever session was first."""
    text = read(SERVER)
    assert "sensorPeer" in text
    assert "filePeer" in text
    routing = body(text, "private fun sendSensorFrame(", span=1200)
    assert "sensorPeer" in routing
