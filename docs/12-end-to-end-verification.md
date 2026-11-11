# End-to-end workflow verification

What PhoneBridge can actually do today, workflow by workflow, with the evidence
for each claim and the parts that are still unproven.

**Read this first:** no workflow is marked `COMPLETE`. On this machine the phone
side is the reference implementation (`tools/simulator/phone_sim.py`), not a
handset. Anything that says "device run outstanding" has been verified on the PC
side, against the protocol, and by static review of the Kotlin - never by
watching it work on Android. §5 is the checklist that would close that gap.

---

## 1. What "verified" means here

Three different questions, kept apart because conflating them is how a mock ends
up described as a working feature:

| Level | Question | How it is answered here |
| --- | --- | --- |
| **Specified** | does `docs/03` define it? | read the normative tables (§5 message types, §6 methods and response schemas) |
| **Implemented** | do both sides have code? | Python is imported and exercised; Kotlin is parsed (`tests/test_protocol_contract.py`) and reviewed |
| **Verified** | was it observed working, and by whom? | **PC ↔ reference phone**: real WebSocket, real P-256 handshake, real AES-GCM (`tests/test_e2e_workflows.py`). **Android**: static contract only. **On hardware**: not done here |

A row is `PASS` only for level "PC ↔ reference phone". The Android column says
what the source says, not what a device did.

---

## 2. Feature verification matrix

Legend - **Android**: `impl` = handler and response present in `BridgeServer.kt`,
checked by parsing it; `device` = run on a handset (never claimed here).

| # | Workflow | Specified | Android | Python | Automated evidence | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Authenticated phone ↔ PC connection (handshake, pairing, pinning, refusal) | yes, §2–4 | impl, device **outstanding** | verified | `test_e2e_workflows.py::test_first_connection_pairs_and_pins`, `test_second_connection_authenticates_without_pairing`, `test_protocol_version_refusal_is_terminal`; Phase-1 tests for pin changes and refusals | **PASS** (PC side), device run outstanding |
| 2 | Phone information (`system.info`) | yes, §6 + device block | impl, device **outstanding** | verified | `test_system_info_schema`, `test_system_info_device_block_is_complete`, `test_handshake_and_rpc_report_the_same_capabilities`, `test_protocol_contract.py::test_kotlin_result_has_every_key_the_docs_require` | **PASS** (PC side), device run outstanding |
| 3 | Capability inspection (`hardware.*` / usable / `detail.*`) | yes, §6 | impl, device **outstanding** | verified | `test_capability_contract_shape`, `test_capability_usability_follows_the_permission`, `test_selftest_catches_a_capability_that_contradicts_the_hardware` | **PASS** (PC side), device run outstanding |
| 4 | Permission reporting and gating (`RECORD_AUDIO`, `BLUETOOTH_CONNECT`, `BLUETOOTH_SCAN`, `ACCESS_FINE_LOCATION` ≤30, `CAMERA`, `POST_NOTIFICATIONS`) | yes, §6 | impl, device **outstanding** | verified | `test_permissions_report_only_declared_runtime_permissions`, `test_permission_change_is_visible_without_reconnecting`, `test_android_completion.py` (manifest ↔ model ↔ request sites) | **PASS** (PC side), device run outstanding |
| 5 | Session statistics (`session.stats`) | yes, §6 | impl, device **outstanding** | verified | `test_session_stats_schema_and_live_counters` | **PASS** (PC side), device run outstanding |
| 6 | Bluetooth headset status (`bluetooth.headset.status`) | yes, §6 | impl, device **outstanding** | verified | `test_headset_status_schema` | **PASS** (PC side), device run outstanding |
| 7 | Audio relay *state* (`audio.route.bluetooth`, honest placeholder) | yes, §6 | impl (placeholder), device **outstanding** | verified | `test_relay_state_is_an_honest_placeholder` | **PASS as a placeholder**; audio itself is #12 |
| 8 | Audio capability probe (`audio.capabilities.run` / `.last`) | yes, §6, docs/08 §2 | impl, device **outstanding** | verified | `test_probe_report_absent_then_stored_intact`, `test_compat.py` (report schema, comparison) | **PASS** (PC side), device run outstanding |
| 9 | Ping / RTT (`MSG_PING`/`MSG_PONG`) | yes, §5 | impl, device **outstanding** | verified | `test_selftest_passes_every_supported_workflow` (ping row), `test_regressions.py::test_concurrent_pings_all_resolve` | **PASS** (PC side), device run outstanding |
| 10 | Binary data integrity (BLOB echo, 1 B … 1 MiB, stream isolation) | yes, §5 | impl (`MSG_BLOB` echo), device **outstanding** | verified | `test_blob_echo_is_byte_exact`, `test_zero_length_blob_round_trips`, `test_stream_ids_are_isolated`, `test_interleaved_streams_arrive_on_their_own_stream` | **PASS** (PC side), device run outstanding |
| 11 | Error contract (unknown method, bad params, internal, timeout, drop) | yes, §6 | impl, device **outstanding** | verified | `test_unknown_method_returns_method_not_found_and_keeps_the_session`, `test_bad_parameters_are_reported_as_bad_params`, `test_a_crashing_handler_becomes_an_internal_error`, `test_a_client_timeout_fails_the_call_not_the_session`, `test_a_dropped_link_fails_quickly_instead_of_hanging` | **PASS** (PC side), device run outstanding |
| 12 | Audio relay (playback + microphone) | yes, §6 (`MSG_AUDIO`, stream 1/2) | impl, device **outstanding** | verified | `test_audio.py` (62), `test_phase2_e2e.py`, self-test row "audio relay state" | **PASS** (PC side), device run outstanding |
| 13 | File transfer, PC → phone | yes, §6 (`MSG_FILE`, stream 5) | impl, device **outstanding** | verified | `test_files.py`, `test_phase5_e2e.py::test_send_file_to_the_phone_verifies`, self-test row "file transfer PC -> phone" | **PASS** (PC side), device run outstanding |
| 14 | File transfer, phone → PC | yes, §6 (`MSG_FILE`, stream 6) | impl, device **outstanding** | verified | `test_files.py` (resume, cancel, wrong hash), `test_phase5_e2e.py::test_receive_file_from_the_phone_verifies`, self-test row "file transfer phone -> PC" | **PASS** (PC side), device run outstanding |
| 15 | Camera capture / retrieval | yes, §6 (`MSG_VIDEO`, stream 3) | impl, device **outstanding** | verified | `test_camera.py` (66), `test_phase4_e2e.py`, self-test has no camera row (a stream, not an RPC workflow) | **PASS** (PC side), device run outstanding |
| 16 | Sensor data retrieval | yes, §6 (`MSG_SENSOR`, stream 4) | impl, device **outstanding** | verified | `test_sensors.py` (31), `test_phase5_e2e.py::test_sensor_stream_*`, self-test row "sensor discovery and stream" | **PASS** (PC side), device run outstanding |
| 17 | Bluetooth LE scan lifecycle | yes, §6 | impl, device **outstanding** | verified | `test_bluetooth.py` (scan lifecycle, denial, radio off, duplicates, RSSI), self-test row "bluetooth scan lifecycle" | **PASS** (PC side), device run outstanding |
| 18 | Bluetooth GATT lifecycle | yes, §6 | impl, device **outstanding** | verified | `test_bluetooth.py` (connect, discover, read, write, subscribe, disconnect, timeout), `test_phase5_e2e.py`, self-test row "bluetooth GATT lifecycle" | **PASS** (PC side), device run outstanding |

Every row the self-test prints is now a `PASS` against a compliant phone: the
`NOT IMPLEMENTED` state is empty (`selftest.UNSUPPORTED_WORKFLOWS == ()`), and
`test_the_self_test_does_not_invent_workflows` fails if a row ever reappears
while the phone still dispatches the methods it disclaims.

---

## 3. Per-workflow verification detail

### 3.1 Authenticated connection

* **Request/response**: `hello` → `hello_ack` → `auth` → (`auth_ok` |
  `pair_required` → `pair_confirm` → `pair_ok` | `auth_fail`), all inside the
  encrypted channel after the ECDH exchange. Schema checked by
  `tests/test_framing.py`, `tests/test_crypto.py` and the handshake tests.
* **Permissions**: none (this is transport-level).
* **Timeout/failure**: `--connect-timeout` bounds the TCP/WebSocket step, the
  handshake ack is bounded by `request_timeout`, and a refusal (`auth_fail`,
  changed identity key, protocol skew) is a `TerminalSessionError` that the
  supervisor does not retry. Verified end to end by
  `test_protocol_version_refusal_is_terminal`.
* **Integrity**: every later frame is AES-256-GCM with a counter bound into the
  tag; replays and reordering are rejected (Phase 1 tests).
* **Unproven**: that the Kotlin handshake accepts the same clients on a device.

### 3.2 Phone information

* **Response schema**: the 17-key device block plus `device_id`, `name`,
  `capabilities`, `permissions` (docs/03 §6). Enforced three ways: the docs table,
  the Kotlin map (`test_kotlin_result_has_every_key_the_docs_require`), and the
  live response (`test_system_info_device_block_is_complete`).
* **Consistency**: the capability map sent during the handshake and the one in
  `system.info` must be identical - `test_handshake_and_rpc_report_the_same_capabilities`.
* **Permissions**: the block includes `permissions`, so a PC can explain *why* a
  capability is unavailable without another call.

### 3.3 Capabilities

* **The contract**: for each of the eight keys, `hardware.<key>` (measured),
  `<key>` (usable today), `detail.<key>` (why not).
* **Honesty invariant**: `usable` may never be true while `hardware` is false -
  asserted live by the self-test and in `test_capability_contract_shape`.
* **Permission term**: with a phone that implements `audio.capture`, denying
  `RECORD_AUDIO` makes it unusable and granting makes it usable, while the
  hardware row never moves (`test_capability_usability_follows_the_permission`).
  On today's real app every bare key is `false`, which is the honest answer.

### 3.4 Permissions

* **Reported states**: `granted`, `denied`, `permanently_denied`,
  `not_applicable`; only for declared runtime permissions
  (`RECORD_AUDIO`, `BLUETOOTH_CONNECT` on 12+, `POST_NOTIFICATIONS` on 13+).
* **No invention**: the Android test suite proves the manifest and the model
  agree in both directions (nothing requested that is undeclared, nothing
  declared that is unused).
* **Revocation while connected**: the next `system.info` reflects the change
  without reconnecting (`test_permission_change_is_visible_without_reconnecting`);
  on the phone the same values come from a live query each call.

### 3.5 Binary integrity

Byte-exact echo at 1 B, 17 B, 1 KiB, 4 KiB, 64 KiB and 1 MiB, a zero-length
frame, and two streams interleaved - each asserted element by element. This is
the strongest evidence available without hardware that the framing, the cipher
and the stream multiplexing are not quietly corrupting payloads.

### 3.6 Failure handling

| Situation | Expected | Verified by |
| --- | --- | --- |
| unknown method | `method_not_found`, session survives | `test_unknown_method_returns_method_not_found_and_keeps_the_session` |
| bad params | `bad_params` with the message | `test_bad_parameters_are_reported_as_bad_params` |
| handler crash | `internal: …`, session survives | `test_a_crashing_handler_becomes_an_internal_error` |
| client timeout | call fails, session stays usable | `test_a_client_timeout_fails_the_call_not_the_session`, self-test row |
| link drop | call fails at once, session closed | `test_a_dropped_link_fails_quickly_instead_of_hanging` |
| protocol skew | terminal, not retried | `test_protocol_version_refusal_is_terminal` |
| unreachable phone | clean CLI error + hint, exit 2 | `test_cli.py::test_unreachable_phone_is_a_clean_error_not_a_traceback` |

---

## 4. Defects found by this verification pass

1. **`session.stats` and `bluetooth.headset.status` had no agreed schema.** The
   Kotlin app returned `frames_out`/`bytes_out`/`connections` and
   `connected`/`profile`/`devices`; the reference phone returned a different set
   (`blobs_echoed`, `supported`, …). docs/03 now states the required keys, the
   reference phone was aligned, and a test compares all three copies.
2. **The device block was missing `hardware` in the reference phone** even
   though `DeviceFacts.deviceInfo()` sends it - a client parsing the documented
   block would have seen a hole.
3. **`probe_failed` was an undocumented error code** emitted by the phone's
   asynchronous probe worker. Now documented, so a client can handle it.
4. **The reference phone's capability map was static**, so the permission term of
   `implemented AND hardware AND permitted` was never exercised. It is now
   derived from the reported permissions, which is what made
   `test_capability_usability_follows_the_permission` possible.
5. **The self-test's own first version could not detect a timeout** (a 1 ms limit
   was beaten by loopback). It now uses a sub-microsecond limit that no round trip
   can meet, and says so in the evidence.
6. (During the same work, the existing suite caught a regression I introduced in
   the reference phone: RPC replies were sent unsealed. Fixed before commit -
   recorded here because "the tests caught it" is the point of the suite.)

---

## 5. Device run checklist (the part that is still open)

Run these on the Windows PC with the app installed and the bridge switched on.
Each line maps to a matrix row; paste the output back and the "device
outstanding" notes can be removed.

```powershell
# 0. wire it up
adb install -r android\app\build\outputs\apk\debug\app-debug.apk
adb forward tcp:8765 tcp:8765

# 1. on the phone: flip Bridge on, press "Run protocol self-test" -> expect 8/8

# 2-4. connection, info, capabilities, permissions
phonebridge info --host 127.0.0.1 --format text
phonebridge capabilities --host 127.0.0.1
phonebridge capabilities --host 127.0.0.1 --json | Out-File capabilities.json

# 5-7. stats, headset, relay
phonebridge audio status --host 127.0.0.1

# 8. audio probe (30 s, real audio stack)
phonebridge compat run --host 127.0.0.1 --label "Pixel 7" --seconds 30
phonebridge audio last --host 127.0.0.1

# 9-11. ping, integrity, errors, timeouts - all in one run
phonebridge selftest --host 127.0.0.1

# 12-16. confirm the phone really cannot do them yet
phonebridge capabilities --host 127.0.0.1 --require file_transfer   # expect exit 4
```

Expected today: every supported row `PASS`, every unsupported row
`NOT IMPLEMENTED`, and `capabilities` showing `USABLE no` for all eight keys on
the real app (the simulator is the only phone that claims `audio.bluetooth_sco`).

What a device run can still fail on, and why these tests cannot catch it:
`AudioProbe` reading a real `AudioManager`, `CameraManager` enumeration on
vendors that report odd lens lists, permission prompts on OEM ROMs, and the
foreground service surviving Doze. Those are hardware behaviours, not protocol
logic.

---

## 6. Media planes: no workflow was invented, and nothing is left out

`docs/03` §6 now defines all four media planes with a body format and a handler:
`0x30 AUDIO` (streams 1/2), `0x40 VIDEO` (3), `0x50 FILE` (5/6) and
`0x60 SENSOR` (4). `PROTOCOL_VERSION` stayed at **1** — a new message kind and a
new RPC are additive, and an old client never sends them; the version is bumped
only when an existing frame changes shape.

Three checks keep the matrix honest in the other direction, so a row cannot claim
a feature that has no handler:

* `test_every_implemented_media_kind_has_a_handler` — every kind the capability
  table calls implemented is referenced by `BridgeServer.kt`, by the Python
  constants and by the reference phone.
* `test_the_self_test_does_not_invent_workflows` — the disclaimer list must be
  empty while the phone dispatches audio, camera, Bluetooth, sensor and file
  methods; a stale "not implemented" row would fail it.
* `test_the_cli_calls_nothing_the_protocol_does_not_define` — every method string
  the CLI sends is one of the documented ones (plus one deliberate bad name that
  proves the error path).

The inverse rule still holds too: no device run is claimed anywhere in this
document. Every Phase 4 and Phase 5 row is `PASS` for *the PC against a compliant
phone*, and the Android column stays `device outstanding` until the checklists in
`docs/20` §9, `docs/21` §10, `docs/22` §10 and `docs/23` §11 are executed.

---

## 7. Reproducing everything in this document

```powershell
cd windows
python -m pytest -q tests\test_e2e_workflows.py tests\test_protocol_contract.py  # this pass
python -m pytest -q                                                             # whole suite
python tools\verify.py                                                          # + lint, vectors, CLI smoke
# and against a phone or the simulator:
python -m phonebridge selftest --host <ip>
```

## 8. Trust model behind the matrix

What "authenticated connection" means is spelled out in
[`13-security-review.md`](13-security-review.md): mutual pinned ECDH, a
human-compared six-digit code on first use, and browser-originated handshakes
refused at the socket. The matrix verifies the *protocol* behaviour of that
model; §6 of that review lists the risks the design accepts.
