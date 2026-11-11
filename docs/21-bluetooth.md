# 21 — Bluetooth LE: scanning and GATT (Phase 5)

The phone is the radio; the PC is the remote control. Scanning, connecting,
reading, writing and subscribing all happen on the phone's Bluetooth stack, and
every result comes back over the session that is already authenticated and
encrypted. No second socket, no pairing bypass, and nothing is ever connected to
without the user picking it.

Status: **implemented on both sides; exercised end-to-end against the reference
phone (`tools/simulator`) in software; not yet run against a real handset or a
real peripheral** (see §10). Pull request: [#12](https://github.com/ManishAcharS/phone-bridge/pull/12),
based on `main` `99d9ac7`.

---

## 1. What this phase adds

| Piece | Where | What it does |
| --- | --- | --- |
| BLE engine | `android/.../net/BleEngine.kt` | adapter status, scan lifecycle, GATT connect/discover/read/write/subscribe/disconnect, notification buffer — one file, one owner |
| Wire helpers | `windows/phonebridge/bluetooth/frames.py` | device list, service tree, notification, scan state, adapter status — strict parsers, no trusting the peer |
| PC client | `windows/phonebridge/bluetooth/link.py` | `BluetoothLink`: scan control, GATT calls with timeouts, event handling, `snapshot()` for the GUI |
| RPCs | `BridgeServer.dispatchRpc`, `docs/03` §6 | 11 methods, listed in §3 |
| Events | `docs/03` §6.1 | `bluetooth.device`, `bluetooth.scan`, `bluetooth.gatt`, `bluetooth.notification` |
| PC GUI | `controller/ui/planes.py` | **Bluetooth** tab: scan controls, device table with RSSI + services, service/characteristic browser, read/write/subscribe, notification viewer, status line |
| PC manager | `controller/manager.py` | one `BluetoothLink` per authenticated session, closed with it |
| Capabilities | `DeviceFacts.kt`, `controller/capabilities.py` | `bluetooth_scan` and `bluetooth_gatt` are separate rows — a radio does not imply GATT works |
| CLI | `phonebridge/cli/planes.py` | `phonebridge bluetooth scan | devices | services | read | write | subscribe | status` |
| Self-test | `cli/selftest.py` | scan lifecycle and GATT lifecycle rows against the live phone |

Nothing in Phases 1–4 changed: pairing, AES-GCM, the connection manager, USB,
Wi-Fi, hotspot, audio and the camera are untouched. Bluetooth adds message
*kinds* and *events*, never a transport.

---

## 2. Architecture

```
  peripheral                phone (Android)                        PC (Windows)
  ┌─────────┐        ┌──────────────────────────┐            ┌─────────────────────┐
  │ BLE     │◄──────►│ BleEngine                │            │ BluetoothLink       │
  │ device  │  GATT  │  ScanCallback (dedupe)   │  RPC       │  scan state         │
  └─────────┘        │  BluetoothGatt per addr  │◄──────────►│  GattConnection[]   │
                     │  serialised operations   │  events    │  notifications      │
                     │  bounded notification buf│            └──────────┬──────────┘
                     └──────────┬───────────────┘                       │ snapshot()
                                │ MSG_CONTROL inside AES-256-GCM         ▼
                                └──────────────── encrypted session ─────┘
                                                       GUI: Bluetooth tab (Tk)
```

Five properties are structural:

1. **One radio, one owner.** `BleEngine` is created by `BridgeServer` with the
   application context and is the only object that touches `BluetoothAdapter`,
   `BluetoothLeScanner` or `BluetoothGatt`. The PC never sees a radio.
2. **Explicit connect only.** `bluetooth.gatt.connect` takes an address that came
   from a scan *the user started*. There is no code path that connects because a
   device was seen, and no auto-reconnect after a drop: a peripheral that comes
   back on its own is a peripheral the user did not ask for.
3. **Operations are serialised per connection, and confirmed by the platform.**
   `BluetoothGatt` allows one outstanding operation, so `BleEngine` holds a
   per-address monitor for the whole exchange — issue *and* callback — and a
   write is only reported as done when `onCharacteristicWrite` arrives with
   `status == GATT_SUCCESS`. A timeout is reported as a timeout, with the real
   GATT status when there is one; it is never dressed up as success.
4. **Bounded buffers.** ≤200 devices per scan, 32 subscriptions per connection,
   200 notifications per connection, 512-byte values. Overflow drops the oldest
   notification and is counted, never silently grows.
5. **Bounded events, applied filters.** Scan sightings are coalesced to at most
   20 published events a second (the newest state per address is kept, so the
   device list stays complete), and `name_prefix` is applied on the phone *and*
   on the PC — a phone that predates the parameter cannot turn `--name` into a
   no-op.
6. **Nothing for location.** `BLUETOOTH_SCAN` is declared with
   `android:usesPermissionFlags="neverForLocation"`; scan results are never used
   to infer position and the app does not request location on Android 12+.

---

### 2.1 Phase 5.1: the review findings, and what changed

The Phase 5 review found that §2 above described intentions the code did not
implement. What changed, and what now pins it:

| finding | before Phase 5.1 | now | pinned by |
| --- | --- | --- | --- |
| writes were "pending" until a timeout | `bluetooth.gatt.write` succeeded without waiting for `onCharacteristicWrite` | the write completes only on the callback, and the reply carries the real GATT status | `test_write_completion_comes_from_the_platform_callback` |
| no serialisation | two callers could interleave callbacks on one link | one monitor per address, held for issue and callback; `failLink` aborts waiting operations | `test_gatt_operations_on_one_link_are_serialised` |
| a read timeout was reported as the result | 15 s of silence looked like a refusal with no status | the callback's status is reported; a timeout says it timed out | `test_read_reports_the_real_status_instead_of_a_timeout` |
| a failed connect leaked its `BluetoothGatt` | connect/discovery failures left the object open | every failure, timeout, disconnect and cancel goes through `failLink`, which disconnects, closes and forgets the link | `test_a_failed_link_closes_its_gatt_object` |
| `Link.state` was read across threads unsafely | a stale state could outlive a connect | `@Volatile` | `test_the_connection_state_is_volatile` |
| scan events were unbounded | a crowded room meant one publish per sighting | 20 published events a second, newest state per address kept | `test_a_burst_of_sightings_publishes_at_a_bounded_rate` |
| `name_prefix` was dead | the parameter was sent and ignored | applied on the phone and on the PC, case-insensitively | `test_the_name_filter_drops_events_that_do_not_match` |

A GATT connect also requests an MTU of 517 now, and `bluetooth.gatt.status`
reports the negotiated value rather than leaving the GUI to guess. A scan that is
already running is stopped before a new one starts, so repeated start/stop cannot
stack callbacks.

None of this has been run against a peripheral: there is no radio in this
environment. §10 is the checklist that remains.

---

## 3. Protocol

Normative reference: `docs/03-protocol.md` §6. All of this is **additive**: an
older phone answers `method_not_found` and the PC shows that sentence.

**RPCs**

| method | params | result |
| --- | --- | --- |
| `bluetooth.adapter.status` | — | `{supported, enabled, le_supported, name, address, permitted, detail}` |
| `bluetooth.scan.start` | `duration_ms` (0 = until stopped), `filter_uuids`, `name_prefix` | `{ok, scanning, started_at_ms, duration_ms}` |
| `bluetooth.scan.stop` | — | `{ok, scanning:false}` |
| `bluetooth.scan.results` | — | `{scanning, started_at_ms, duration_ms, filter_uuids, detail, error, devices:[…]}` |
| `bluetooth.gatt.connect` | `address` | `{ok, address, state, services, mtu}` |
| `bluetooth.gatt.services` | `address` | `{address, mtu, services:[{uuid, characteristics:[…]}]}` |
| `bluetooth.gatt.read` | `address`, `service`, `characteristic` | `{value_b64, bytes}` |
| `bluetooth.gatt.write` | `address`, `service`, `characteristic`, `value_b64`, `with_response` | `{ok, bytes}` |
| `bluetooth.gatt.subscribe` | `address`, `service`, `characteristic`, `enable` | `{ok, subscribed}` |
| `bluetooth.gatt.disconnect` | `address` | `{ok, state}` |
| `bluetooth.gatt.status` | — | `{connections:[{address, state, mtu, subscriptions}], scanning}` |

A device object (scan result) is:

| field | meaning |
| --- | --- |
| `address` | MAC as the phone reports it; `AA:BB:CC:DD:EE:FF` |
| `name` | advertised name, possibly `""` when the peripheral does not advertise one |
| `rssi` | dBm of the **newest** sighting |
| `service_uuids` | advertised service UUIDs, normalised to 128-bit lowercase |
| `connectable` | whether the advertisement said connectable |
| `first_seen_ms`, `last_seen_ms`, `sightings` | lifecycle of this entry in the scan |
| `address_type` | `public` / `random` / `unknown` — reserved: this build's phone does not send it, so the PC shows `""` rather than guessing |

**Events** (`MSG_CONTROL`, `event` name + data):

| event | when | shape |
| --- | --- | --- |
| `bluetooth.scan` | scan started / stopped / errored | `{state, scanning, duration_ms, error}` |
| `bluetooth.device` | a scan result (deduplicated by address) | device object |
| `bluetooth.gatt` | connection state change or disconnect | `{address, state, error}` |
| `bluetooth.notification` | a subscribed characteristic changed | `{address, service, characteristic, value_b64, seq, timestamp_ms}` |

**Error codes** (docs/03 §7): `bluetooth_off` (the radio is off — no call opens
it), `bluetooth_unsupported` (no LE radio), `ble_denied` (scan or connect
permission not granted), `ble_scan_failed`, `ble_not_found` (address not in the
current results), `ble_connect_failed`, `ble_timeout` (no answer inside 15 s),
`ble_not_connected` (GATT call on a dead link), `ble_no_service` /
`ble_no_characteristic`, `ble_read_failed`, `ble_write_failed`,
`ble_subscribe_failed`, `bad_params`.

---

## 4. Permissions and Android version differences

| Android | Scan needs | Connect/GATT needs | Manifest |
| --- | --- | --- | --- |
| 12+ (API 31+) | `BLUETOOTH_SCAN` | `BLUETOOTH_CONNECT` | both, scan marked `neverForLocation` |
| 6–11 (API 23–30) | `ACCESS_FINE_LOCATION` | `BLUETOOTH` | location capped `maxSdkVersion="30"`, `BLUETOOTH` capped the same |
| ≤5 (API ≤22) | granted at install | granted at install | nothing extra |

`Permissions.kt` models this with `effective(context, permission)`, so the panel,
the capability map and the request dialog all say the same thing on every
version:
`BLUETOOTH_SCAN` maps to `ACCESS_FINE_LOCATION` below API 31, and
`BLUETOOTH_CONNECT` maps to `BLUETOOTH` there. Nothing is requested at launch: the
scan permission is asked from the **Grant** button in the Bluetooth card, and
`BLUETOOTH_CONNECT` is asked the first time the user presses Connect.

Three failure modes are handled rather than assumed away:

* **Denied** — `ble_denied` with the phone's own sentence; `bluetooth_scan`
  reports `permitted = false` while `hardware.bluetooth_scan` stays `true`.
* **Bluetooth off** — `bluetooth_off`. The app does not enable the radio itself
  (that would need `BLUETOOTH_CONNECT` plus a system dialog the user did not ask
  for); the status line says "Bluetooth is off on the phone".
* **No LE hardware** — `bluetooth_unsupported`; the manifest's
  `<uses-feature android:hardware.bluetooth_le android:required="false" />` means
  the APK still installs there.

**Security stance.** Scanning and GATT need no pairing, and PhoneBridge never
attempts to bond, pair, bypass a bond or write to a characteristic it cannot
legitimately see. `neverForLocation` is a promise the manifest makes and the code
keeps: `BleEngine` never derives a position from a scan result. A peripheral that
requires bonding will simply fail `ble_connect_failed` / `ble_read_failed` with
the platform's reason — the app does not try to work around it.

---

## 5. Scan and connection behaviour

**Scan.** `duration_ms` is honoured by the phone (capped at 120 s; `0` means until
`bluetooth.scan.stop`). Duplicates are merged by address: the newest RSSI wins,
the name and service list are kept if a later advertisement omits them, and
`sightings` counts how often the device was seen. Results survive the end of a
scan so the list does not vanish under the user's cursor; `bluetooth.scan.results`
returns them with `scanning:false`. A second `scan.start` while scanning is a
no-op that answers `ok` (rather than an error the GUI would have to special-case).

**Connect.** `bluetooth.gatt.connect` resolves the address against the *current*
scan results (`ble_not_found` otherwise — this is what stops a stale window from
connecting to a device that is no longer there), then connects and discovers
services before answering. The PC polls `bluetooth.gatt.status` until the link is
`connected` or a 20 s deadline expires, so a phone that goes quiet produces
`ble_timeout` instead of a hung GUI.

**Operations.** Read, write and subscribe are queued per connection with a 15 s
deadline each. Values are capped at 512 bytes on the way in and out; a longer
write is `bad_params`, not a truncated one.

**Notifications.** Subscribing is explicit and per characteristic; the phone
buffers up to 200 notifications per connection and the PC keeps the newest 200.
Each one carries `seq` (per characteristic, wrapping) and the phone's
`timestamp_ms`, so the viewer can show a rate and the user can tell a 1 Hz battery
report from a 50 Hz IMU stream.

**Disconnect.** `bluetooth.gatt.disconnect` closes the `BluetoothGatt` (no
`close()`-and-hope), drops the subscriptions and answers with the final state. A
session close, a service stop or a peripheral walking out of range does the same
thing and produces a `bluetooth.gatt` event, so the GUI empties the service tree
instead of showing a link that no longer exists.

---

## 6. Windows side

* **Parsing** — `bluetooth/frames.py`. Every field is validated; a malformed
  device or service tree raises `BleError` and is dropped with a count, never
  half-rendered. UUIDs are normalised so `180f`, `0x180F` and the full 128-bit form
  compare equal.
* **`BluetoothLink`** — scan state, `GattConnection` objects with service trees,
  subscriptions and notifications; `snapshot()` is what the GUI reads.
  `refresh_status()` reconciles with `bluetooth.gatt.status` once a second so a
  link the phone dropped disappears here too.
* **GUI (Bluetooth tab)** — scan / stop with a duration picker, a device table
  (name, address, RSSI, advertised services, connectable, sightings), Connect /
  Disconnect / Browse for the *selected* row, a service/characteristic tree with
  properties and a `[notifying]` marker, Read / Subscribe / Unsubscribe / Write
  buttons, a value line (printable text plus hex) and a notification viewer with
  timestamps and sequence numbers.
* **Blocking work** — every RPC is submitted to the runtime's asyncio loop; the Tk
  thread only reads state and redraws. The tables redraw at most once a second
  and update rows in place, so a scan filling up does not fight the selection.
* **Write dialog validates hex** — an odd digit count or a non-hex character is
  refused before anything is sent. Writing is a real side effect on real hardware.
* **CLI** — the same operations, scriptable:

  ```
  phonebridge bluetooth scan --duration 10 [--uuid 180f]
  phonebridge bluetooth gatt services C4:7F:51:00:00:01
  phonebridge bluetooth gatt read  C4:7F:51:00:00:01 180f:2a19
  phonebridge bluetooth gatt write C4:7F:51:00:00:01 180f:2a19 hex:01ff [--mode with|without]
  phonebridge bluetooth gatt watch C4:7F:51:00:00:01 180f:2a19 --seconds 20
  ```

  All of them take the shared target arguments (`--host`, `--device`, `--last`,
  `--port`, `--transport`, `--format json`) and the same pairing path as every
  other command. `--auto-pair` exists for tests and prints a warning when used.

---

## 7. Capabilities

Two rows, because two powers fail independently:

| key | hardware | permission | implemented |
| --- | --- | --- | --- |
| `bluetooth_scan` | `BluetoothAdapter.isEnabled` + `isLeSupported` | `BLUETOOTH_SCAN` (or location ≤30) | yes |
| `bluetooth_gatt` | as above, plus a discovered service tree in this session | `BLUETOOTH_CONNECT` (or `BLUETOOTH` ≤30) | yes |

`available` = hardware **and** implemented **and** permitted. `active` is true
only while a scan is running (`bluetooth_scan`) or a link is up
(`bluetooth_gatt`) — a radio that scanned a minute ago is not active. The
capability table in the GUI, `phonebridge capabilities`, `system.info` and the
self-test all read the same five facts, so a phone with a radio and no permission
reports *unavailable with a reason*, not *missing*.

---

## 8. Tests

| Area | Where | What is covered |
| --- | --- | --- |
| Frames | `windows/tests/test_bluetooth.py` | device/service/notification parsing, UUID normalisation, malformed input, caps |
| Link | `windows/tests/test_bluetooth.py` | scan lifecycle, stop, results after stop, deny, radio off, unsupported, duplicates/RSSI merge, GATT connect/discover/read/write/subscribe/disconnect, timeouts, notification ordering |
| End to end | `windows/tests/test_phase5_e2e.py` | real handshake + AES-GCM against the simulator: scan, connect, read battery, subscribe and receive notifications, disconnect |
| Contract | `windows/tests/test_protocol_contract.py` | every method and result key the docs promise exists in the Kotlin dispatch and the engines |
| Self-test | `cli/selftest.py` | "bluetooth scan lifecycle" and "bluetooth GATT lifecycle" rows, run against the connected phone |
| Android | — | **not compiled**; no JDK/SDK in this environment (§10) |

`phonebridge selftest` reports 17 of 17 supported workflows against a connected
phone or the simulator; `windows/tests/test_cli.py::test_selftest_runs_every_supported_workflow`
pins that count, and `tools/verify.py` runs the whole matrix as its check 8.

---

## 9. Troubleshooting

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| "adapter not supported" | phone has no BLE radio (rare on modern hardware) | nothing to scan; GATT is impossible there |
| "Bluetooth is off on the phone" | radio off | turn Bluetooth on in the phone's settings; the app will not enable it for you |
| "permission denied" | scan or connect permission refused | press **Grant** in the Bluetooth card; if permanently denied, allow it in Settings |
| scan finds nothing | the peripheral is off, out of range, or already connected to another phone | move closer; a device connected elsewhere stops advertising |
| `ble_not_found` on connect | the results are stale | scan again, then connect from the fresh list |
| `ble_timeout` | the peripheral stopped answering mid-connect | power-cycle the peripheral; 15 s is the deadline, not a retry loop |
| `ble_not_connected` | the link dropped (range, peripheral reset) | Browse/Connect again; the GUI marks the link gone |
| read fails but subscribe works | the characteristic has no `read` property | use what the properties column allows |
| names are empty, addresses are not | the peripheral does not advertise a name | connect and read `0x2a00` (GAP Device Name) if it has it |

---

## 10. Manual hardware checklist (not yet done)

This environment has **no Android SDK, no JDK, no phone and no BLE peripheral**.
Nothing below has been executed; §8 is what *was* verified.

1. Build and install: `cd android && .\gradlew.bat assembleDebug`,
   `adb install -r app\build\outputs\apk\debug\app-debug.apk`.
2. Android 12+ phone: press **Scan** with the permission not yet granted →
   the request appears *then*; deny → the status line says so and no scan starts.
3. Grant, scan 10 s: your fitness band / thermometer appears with an RSSI that
   moves when you walk away.
4. Connect to it. Service tree appears with properties; a device that requires
   bonding fails with the platform's reason (this is expected and correct).
5. Read a readable characteristic twice; the value and hex update.
6. Subscribe to a notifying characteristic (a heart-rate strap is ideal): rows
   appear with timestamps and sequence numbers, and stop when you unsubscribe.
7. Walk out of range: the link is marked disconnected within a few seconds and
   the service tree clears.
8. Turn Bluetooth off with a scan running: `bluetooth_off`, no crash, and the
   capability row turns unavailable.
9. Android 11 and older: repeat 2–3 and confirm the permission dialog names
   location, not Bluetooth.
10. Confirm nothing connects by itself: leave the app on the Bluetooth tab for
    ten minutes after a scan and check no peripheral shows a link.
