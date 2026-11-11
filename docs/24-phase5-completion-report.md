# 24 — Phase 5 completion report

Bluetooth LE, sensors and file transfer, end to end. Branch
`arena/935b6dca-phone-bridge`, based on `main` `99d9ac7` (Phase 4 camera, PR #11,
merged). Pull request: **[#12](https://github.com/ManishAcharS/phone-bridge/pull/12)**
— open, not merged.

**Read this first:** the software is complete and tested; nothing here has run on
a physical handset. The Android app was never compiled (this environment has no
JDK, Android SDK or Gradle), no BLE peripheral was connected, no real sensor
streamed and no real file crossed the wire. §9 lists what is still required.

---

## 1. Bluetooth

**Android** — `android/app/src/main/java/dev/phonebridge/net/BleEngine.kt` (~820
lines, one engine, one owner of the radio).

* adapter status: `supported` / `enabled` / `le_supported` / `name` / `address` /
  `permitted` / `detail` — a radio that is off is reported as *off*, not missing;
* scan: `duration_ms` (0 = until stopped, capped at 120 s), `filter_uuids`,
  `name_prefix`; results deduplicated by address with the newest RSSI, name and
  service list preserved across advertisements, `sightings` counted; ≤200 devices;
* GATT: connect, discover services/characteristics, read, write (with or without
  response), subscribe/unsubscribe, disconnect, `gatt.status`. Operations are
  queued per link with a 15 s deadline; ≤32 subscriptions, ≤200 buffered
  notifications, ≤512-byte values;
* permissions: scan and connect are gated through `Permissions.effective()`, so
  Android ≤30 asks for location/`BLUETOOTH` and 31+ for
  `BLUETOOTH_SCAN`/`BLUETOOTH_CONNECT`. Denial, radio off and no-LE hardware each
  produce a specific error code and sentence;
* **never silently connects**: `bluetooth.gatt.connect` resolves the address in
  the *current* scan results (else `ble_not_found`) and only runs because the PC
  asked for that address. No bonding, no pairing, no retry around a bond.

**PC** — `windows/phonebridge/bluetooth/{frames,link}.py`.

* strict parsing of device objects, service trees and notifications; malformed
  entries raise `BleError` and are counted, never half-rendered;
* `BluetoothLink`: scan control, `stop_scan`, `refresh_scan`, `connect`,
  `services`, `read`, `write`, `subscribe`, `disconnect`, `refresh_status`
  (reconciles with `gatt.status` so a dropped link disappears),
  `on_event` (device/scan/gatt/notification), `snapshot()`;
* UUIDs normalised so `180f`, `0x180F` and the 128-bit form compare equal.

**GUI** — Bluetooth tab: scan/stop with a duration picker, device table (name,
address, RSSI, advertised services, connectable, sightings), Connect / Disconnect
/ Browse for the selected row, a service/characteristic tree with properties and
`[notifying]`, Read / Subscribe / Unsubscribe / Write (hex-validated dialog), a
value line (printable + hex) and a notification viewer with timestamps and
sequence numbers.

## 2. Sensors

**Android** — `net/SensorEngine.kt`.

* discovery from `getSensorList(TYPE_ALL)`: name, vendor, min/max delay, range,
  resolution, power; ids 1..N stable for the process; text sensors and unknown
  types are listed with a `reason` instead of being hidden or faked;
* per-sensor identity table (`IdentityHashMap<Sensor, Int>`) because `Sensor` has
  no stable equality across enumeration; `SensorEvent.sensor` is one of the
  registered objects, so identity is the only correct lookup, with the sensor
  *type* as a fallback;
* registration at the requested interval, the platform's floor reported back;
  drop-oldest queue of 64 frames; a sender thread with daemon + interrupt;
* `stop()` unregisters, wakes the sender and clears state; `onSessionClosed` and
  `release()` do the same for a dropped session and service destruction.

**PC** — `windows/phonebridge/sensors/{frames,link}.py`.

* `MSG_SENSOR` = `>HHIBBH` (12 bytes) + `count` float32 big-endian values;
  13 kinds named, unknown kinds still carried with their Android type;
* per-sensor `seq` (wraps at 65535), `lost`, `duplicates`, `out_of_order`,
  `invalid`, smoothed `hz`, `age_ms`; a NaN/Inf value or a bad count is dropped
  and counted;
* `SensorLink`: `refresh`, `start(ids)`, `stop`, `set_interval`, `reading`,
  `snapshot` (keys are strings so the JSON output is serialisable);
* **no simulated readings anywhere**: readings are only ever what a frame
  contained. The reference phone labels its own data `simulated` and the e2e
  test asserts that label.

**GUI** — Sensors tab: table of every sensor (state, measured rate, frames, lost,
newest reading with unit, age), rate picker (200/100/50/15/5/1 Hz, "takes effect
on the next start"), Start selected / Stop, status line with applied interval and
the phone's reason when it refuses. Readings are shown numerically; a drawn
orientation indicator was deliberately not built (untested canvas is worse than
none).

## 3. File transfer

**Android** — `net/FileTransferEngine.kt` + `net/SafStore.kt`.

* receive: `file.offer` → `DocumentsContract.createDocument` inside the granted
  tree, streamed chunks to `<name>.part`, SHA-256 while writing, rename on match;
* send: `file.request` → hashes the picked URI, streams it, waits for the PC's
  `file.complete`;
* resume metadata persisted per transfer and reported as `resume_from`;
* a revoked grant produces
  `the phone could not open <name> (the folder permission may have been revoked)`;
* **no storage permission is declared** — SAF only (pinned by
  `test_android_build.py`).

**PC** — `windows/phonebridge/files/{frames,transfer}.py`.

* `MSG_FILE` = `>IIIBB` (14 bytes) + payload; 64 KiB chunks (1–256 KiB allowed),
  8-chunk window, ack every 4, 4 GiB cap; flags START/END/ACK/CANCEL/PAUSE/RESUME;
* `TransferManager`: `send_file`, `request_file` (subscribes before asking, with a
  bounded orphan buffer for chunks that beat the reply), `pause`, `resume`,
  `cancel`, `progress`, `history`, `set_destination`;
* resume: `<name>.part` + `<name>.part.json` (name, size, digest); a matching
  triple offers `resume_from`, the part is re-hashed from disk and truncated to a
  chunk boundary — a partial digest is never trusted;
* **never reports success before verification**: the destination file exists only
  after the receiver's SHA-256 matched;
* path safety: no separators, `..`, `:`, control characters, Windows reserved
  names (`NUL.txt` included), trailing dot, >200 chars; the resolved parent must
  be exactly the chosen directory.

**GUI** — Files tab: Send file… (multi-select), Receive… (optional name hint),
Save incoming to…, and a table with file, direction, progress, `done / total`,
speed, state and **integrity** ("hash verified" / "hash NOT confirmed" / the
failure reason / "pending"), with Pause / Resume / Cancel on the selection.

## 4. Protocol changes

Additive; `PROTOCOL_VERSION` stays **1**.

* 21 RPC methods: `sensor.list|start|stop|status`; `file.offer|request|complete|progress|cancel|pause|resume`;
  `bluetooth.adapter.status`, `bluetooth.scan.start|stop|results`,
  `bluetooth.gatt.connect|services|read|write|subscribe|disconnect|status`.
* 5 events: `bluetooth.device`, `bluetooth.scan`, `bluetooth.gatt`,
  `bluetooth.notification` (plus the existing `audio.state`/`camera.state`).
* 2 message kinds with documented bodies: `MSG_FILE` (0x50) on streams 5/6,
  `MSG_SENSOR` (0x60) on stream 4.
* Error codes: 15 new documented values (`bluetooth_off`, `ble_denied`,
  `sensor_denied`, `file_hash_failed`, …) — enforced by
  `test_kotlin_only_returns_documented_error_codes`.
* Logical isolation: each plane has its own stream and its own bounded queue, so
  a stalled transfer cannot starve control traffic.

## 5. GUI changes

One window, one new `ttk.Notebook` with three tabs, placed above the capability
table. Window default size grew from 960×720 to 1000×840 (minimum 880×720) to fit
it without hiding the table. Everything is submitted to the runtime's asyncio
loop; the Tk thread only reads state, and the panels redraw at most once a second
updating rows in place so a scan filling up does not steal the selection.

## 6. Android changes

New: `net/BleEngine.kt`, `net/SensorEngine.kt`, `net/SensorFrames.kt`,
`net/FileFrames.kt`, `net/FileTransferEngine.kt`, `net/SafStore.kt`,
`BluetoothCard.kt`, `SensorsCard.kt`, `FilesCard.kt`.
Modified: `AndroidManifest.xml` (two permissions, `uses-feature` bluetooth_le),
`Permissions.kt` (two runtime items, `effective()`, `effectiveGranted()`),
`DeviceFacts.kt` (four capabilities implemented, gate switch),
`BridgeServer.kt` (three engines, `phase5Executor`, 22 branches, `answerLater`,
MSG_FILE/MSG_SENSOR routing, release on close/stop),
`service/BridgeState.kt`, `service/BridgeService.kt`, `MainActivity.kt`.

## 7. Files created and modified

* **PC, new:** `windows/phonebridge/bluetooth/{__init__,frames,link}.py`,
  `windows/phonebridge/sensors/{__init__,frames,link}.py`,
  `windows/phonebridge/files/{__init__,frames,transfer}.py`,
  `windows/phonebridge/cli/planes.py`,
  `windows/phonebridge/controller/planeview.py`,
  `windows/phonebridge/controller/ui/planes.py`;
* **PC, modified:** `core/session.py`, `cli/{main,format,selftest}.py`,
  `controller/{manager,runtime,capabilities}.py`, `controller/ui/app.py`;
* **Tests, new:** `test_bluetooth.py`, `test_sensors.py`, `test_files.py`,
  `test_phase5_e2e.py`, `test_controller_planeview.py`;
* **Tests, modified:** `test_protocol_contract.py`, `test_android_completion.py`,
  `test_android_build.py`, `test_cli.py`, `test_e2e_workflows.py`,
  `test_controller_{capabilities,e2e,view}.py`;
* **Docs:** new `21-bluetooth.md`, `22-sensors.md`, `23-file-transfer.md`, this
  report; updated `03-protocol.md`, `02-architecture.md`, `04-roadmap.md`,
  `10-android-app-completion.md`, `12-end-to-end-verification.md`,
  `14-release-readiness.md`, `16-known-limitations.md`, `README.md`;
* `tools/simulator/phone_sim.py` — all 21 methods, capability keys, permission
  defaults, sensor emitter, `simulated` labels.

61 files, +14 341 / −246 lines (Android 3 793, PC 5 668, tests 2 945, docs 1 935).

## 8. Automated test results

| Gate | Command | Result |
| --- | --- | --- |
| Python suite | `cd windows && python -m pytest -q` | **1132 passed, 1 skipped** (was 927/1 before this phase) |
| Lint | `ruff check .` | clean |
| Types | `mypy` | clean, 53 files |
| Full gate | `python tools/verify.py` | **8/8** |
| Self-test vs reference phone | `python -m phonebridge selftest` | **17 rows, 17 PASS** |
| Android unit tests | `gradlew test` | **not run** — no Gradle/JDK |
| Debug APK | `gradlew assembleDebug` | **not built** — no SDK |

New coverage: bluetooth 36, sensors 31, files 51, end-to-end 18, GUI text 31 —
**167 new tests**, plus the contract/completion tests rewritten to the Phase-5
truth (they were strengthened, not weakened: the deviation from the pre-existing
expectations is documented in the PR).

## 9. Known limitations

* **Android is uncompiled.** Kotlin is source-audited and text-tested; a compiler
  may still find what a person did not.
* **No hardware was exercised.** No BLE peripheral, sensor or real storage.
* **No bonding/pairing by the app.** A peripheral that requires a bond fails with
  the platform's reason; bypassing it is out of scope by design.
* **No location/GPS.** `ACCESS_FINE_LOCATION` exists only as the Android ≤11
  spelling of Bluetooth scanning, capped at `maxSdkVersion="30"`.
* **No graphical sensor visualisation** — numeric only, deliberately.
* **One transfer at a time per direction** in this release; multiple files queue.
* **4 GiB per file**, no cross-session resume of a partially different file.
* **No virtual camera or virtual microphone** (unchanged from Phases 2 and 4).

## 10. Hardware tests still required

`docs/21-bluetooth.md` §10, `docs/22-sensors.md` §10, `docs/23-file-transfer.md`
§11. In short: real scan with a real peripheral and permission prompts on
Android 12 and 11; GATT read/subscribe/notify against a real device; a real
sensor stream at a few rates with the phone in hand and locked; and a real file
in both directions including pause/resume, cancel, a 0-byte and a 65 536-byte
file, and a revoked SAF grant.

## 11. Pull request

<https://github.com/ManishAcharS/phone-bridge/pull/12> — open against `main`,
seven commits, **not merged**.
