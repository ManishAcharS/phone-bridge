# 22 — Sensors (Phase 5)

The phone's sensors, streamed to the PC as numbers with the phone's own
timestamps. The PC does not simulate, interpolate or invent a reading: a sensor
with no frame shows "no frame yet", and a sensor that does not exist is listed as
missing rather than hidden.

Status: **implemented on both sides; exercised end-to-end against the reference
phone (`tools/simulator`) in software; not yet run on a real handset** (see §10).
The simulator's readings are labelled `simulated` everywhere they appear — see
§7. Pull request: [#12](https://github.com/ManishAcharS/phone-bridge/pull/12),
based on `main` `99d9ac7`.

---

## 1. What this phase adds

| Piece | Where | What it does |
| --- | --- | --- |
| Sensor engine | `android/.../net/SensorEngine.kt` | dynamic discovery, per-sensor id table, listener registration, bounded queue, sender thread, `stop()` that unregisters |
| Frame format | `windows/phonebridge/sensors/frames.py` | 12-byte header + float32 values, strict decode, per-sensor statistics |
| PC client | `windows/phonebridge/sensors/link.py` | `SensorLink`: list/start/stop, interval, per-sensor rate and loss counts, readings |
| RPCs | `BridgeServer.dispatchRpc`, `docs/03` §6 | `sensor.list`, `sensor.start`, `sensor.stop`, `sensor.status` |
| Stream | `MSG_SENSOR` on stream 4 | one reading per message, phone → PC |
| PC GUI | `controller/ui/planes.py` | **Sensors** tab: sensor table (state, measured rate, frames, lost, newest reading, age), rate picker, Start selected / Stop, status line |
| PC manager | `controller/manager.py` | one `SensorLink` per session, closed with it |
| Capabilities | `DeviceFacts.kt`, `controller/capabilities.py` | `sensors` plus one row per *sensor* (`sensor.<id>`), each with its own five facts |
| CLI | `phonebridge/cli/planes.py` | `phonebridge sensors list | stream | status` |
| Self-test | `cli/selftest.py` | "sensor discovery and stream": list, start two sensors, check rate and sequence numbers, stop |

---

## 2. Architecture

```
  phone (Android)                                     PC (Windows)
  ┌────────────────────────┐                        ┌──────────────────────┐
  │ SensorManager          │  MSG_SENSOR (12 B + n) │ SensorLink           │
  │  getSensorList(TYPE_ALL)│──► per-sensor seq ────►│  SensorRegistry      │
  │  registerListener      │   phone's ts_ms        │  SensorStats (hz,    │
  │  (interval, 0)         │   inside AES-256-GCM   │   lost, dup, order)  │
  │  bounded queue (64)    │                        │  readings per id     │
  │  sender thread         │◄── sensor.start/stop ──┤  bounded queue (64)  │
  └────────────────────────┘                        └──────────┬───────────┘
                                                               │ snapshot()
                                                               ▼
                                                    GUI Sensors tab (Tk)
```

1. **Discovery is dynamic.** Nothing is hardcoded: `sensor.list` returns whatever
   `getSensorList(TYPE_ALL)` reports, with name, vendor, min/max delay, range,
   resolution and power. The phone numbers them 1..N in that order and keeps the
   table, so an id is stable for the life of the process.
2. **Selection is explicit.** `sensor.start` takes the ids the user ticked. There
   is no "start everything": a phone with 40 sensors would open 40 listeners for
   a user who wanted the accelerometer.
3. **One message, one sample.** A lost message loses a sample. Nothing is
   reassembled, so a reconnect costs nothing and a slow link degrades into a lower
   rate rather than a growing backlog.
4. **Bounded on both ends.** The phone's queue holds 64 frames and the PC's holds
   64; overflow drops the **oldest** and counts it. A stale reading is worth less
   than a current one.
5. **Stop means stop.** `sensor.stop`, a closed session, a phone-side Stop and
   service destruction all unregister the listener and clear the sender thread;
   `sensor.status` then reports `active: []` and `frames_sent` stops moving.

---

## 3. Protocol

Normative reference: `docs/03-protocol.md` §6. Additive only.

| method | params | result |
| --- | --- | --- |
| `sensor.list` | — | `{sensors:[sensor…], permitted, interval_us, detail}` |
| `sensor.start` | `ids` int[] (omit = all), `interval_us` | `{ok, active:[ids started], interval_us}` |
| `sensor.stop` | — | `{ok, stopped:[ids], active:[]}` |
| `sensor.status` | — | `{active:[ids], interval_us, frames_sent, permitted}` |

A sensor object:

| field | meaning |
| --- | --- |
| `id` | 1..N, stable while the process lives |
| `kind` | Android `Sensor.TYPE_*` |
| `name`, `vendor` | exactly what the platform reported |
| `min_delay_us`, `max_delay_us` | the sensor's own limits (0 = not reported) |
| `max_range`, `resolution`, `power_ma` | metadata for the GUI |
| `available` | present **and** usable (a text sensor is present but not streamable) |
| `reason` | why it is not streamable, when it is not |

**Frame** (`MSG_SENSOR`, stream 4, phone → PC):

```
offset  size  field
0       2     sensor_id   the `id` from sensor.list
2       2     seq         per sensor, wraps at 65535
4       4     ts_ms       phone's monotonic ms since the stream started
8       1     kind        Android Sensor.TYPE_*
9       1     count       1..16 values
10      2     reserved    must be zero
12      4×n   values      float32 big-endian
```

Rules both sides enforce: `seq` is **per sensor**; `ts_ms` is the phone's clock
and never a wall-clock date; a frame with an unknown `kind`, `count` outside
1..16, a truncated body or a NaN/Inf value is dropped and counted, never fatal;
a sequence gap is counted as `lost` and a *backwards* jump is counted as
`out_of_order` rather than treated as a rewind.

Interval: microseconds, 1 000..10 000 000. The PC offers 200, 100, 50, 15, 5 and
1 Hz. The phone's reply is authoritative because a sensor may floor the interval
at its own `min_delay_us` — the PC shows the applied value, not the requested
one.

**Error codes**: `sensor_denied` (permission missing, or a restricted sensor),
`sensor_failed` (registration refused, nothing could be started, or the phone
reported no sensors at all — the message says which), `bad_params` (unknown id,
interval outside the range). There is no separate `sensor_unavailable` code: "no
sensors at all" is a `sensor_failed` with a sentence, which is what the phone
sends and what `docs/03` documents.

---

## 4. Permissions

Two different permissions matter here, and Phase 5.1 stopped pretending neither
did.

**`HIGH_SAMPLING_RATE_SENSORS` (declared, normal).** Android 12+ gates sampling
faster than 200 Hz behind this permission, and some vendors gate lower rates as
well. It is a *normal* permission: granted at install, never prompted, no user
friction. The app declares it in `AndroidManifest.xml` and *reads* it with
`checkSelfPermission` — that is the only way `sensor.list` can report a measured
answer instead of a constant. It is deliberately **not** in `Permissions.kt`'s
runtime request list, because asking for it would be a dialog that cannot appear.

**`BODY_SENSORS` (not declared).** Heart rate, heart beat and the other
`TYPE_*` health sensors need it. This release does not declare it, so those
sensors are refused with `sensor_denied` and a reason that says which permission
a future build would need (`Permissions.kt` keeps it in `futurePermissions`).
Declaring a body-sensor permission to read an accelerometer would be a capability
claim the app cannot back, and asking for it at launch would be worse.

What the app reports, and what it never does:

| field | where | truth |
| --- | --- | --- |
| `permitted` | `sensor.list`, `sensor.status`, `system.info.capabilities` | `checkSelfPermission` for the *specific* sensor's gate, or `true` for a sensor with no gate |
| `detail` | same | the permission name and what to grant, when `permitted` is false |
| `state` | `sensor.list` | `available`, `permission_denied`, `unavailable` (hardware), or `unimplemented` — four different answers, never collapsed |
| `reason` | `sensor.list` | one sentence for a refused sensor, e.g. "needs `BODY_SENSORS`, which this build does not declare" |

The permission is **never** requested automatically: `sensor.start` on an
unpermitted sensor answers `sensor_denied` and the session stays open. A denial
is a fact to report, not a dialog to spring on somebody who was streaming.

`DeviceFacts.kt` reports `permitted.sensors` from the same probe, so the PC's
capability card and the sensors tab cannot disagree.

---

## 5. Windows side

* **Decode and count** — `sensors/frames.py`. `SensorStats` tracks `received`,
  `lost`, `duplicates`, `out_of_order`, `invalid`, a smoothed `hz` and `age_ms`.
  A NaN, an unknown kind or a bad count increments `invalid` and does not poison
  the stream.
* **`SensorLink`** — `refresh()` (list), `start(ids)`, `stop()`, `set_interval()`,
  `reading(id)`, `snapshot()`. The registry keeps the phone's own order for the
  table (accelerometer, gyroscope, magnetometer … then the rest), and a sensor
  that produced a frame but is not in the list is still counted.
* **GUI (Sensors tab)** — a table of every sensor with state (idle/streaming),
  measured rate, frames, lost, newest reading and age; a rate picker that says
  "takes effect on the next start"; **Start selected** (refuses with a sentence if
  nothing is ticked) and **Stop**; a status line with state, applied interval, and
  the phone's reason when it refuses.
* **No simulated readings.** The GUI renders exactly what the phone sent. The
  reference phone (`tools/simulator`) marks its capability map `simulated: true`
  and its detail strings say the data is synthetic (§7), so nobody can mistake a
  simulator run for hardware evidence.
* **Motion visualisation** — the newest reading for every sensor is shown
  numerically with its unit and age; the three-axis sensors also show the
  magnitude in the status line. A graphical orientation indicator is *not* built
  (no display available in this environment to test it), and the numeric view is
  the honest substitute: it never draws a picture the data does not support.
* **CLI** — `phonebridge sensors list` (metadata for every sensor) and
  `phonebridge sensors stream [--sensor 1 --sensor 4] [--limit N] [--seconds 5]
  [--interval 50hz] [--format json]`. The stream prints a line per second with
  per-sensor state, measured rate, frames and the newest values, then a summary.

---

## 6. Capabilities

| key | meaning |
| --- | --- |
| `sensors` | the phone has sensors **and** this build streams them |
| `sensor.<id>` | one row per sensor: `present` from the platform, `permitted`, `implemented`, `available` = all three, `active` while it is streaming |
| `permitted.sensors` | whether the phone would let a listener be registered |

Three states are deliberately not collapsed: a phone can have an accelerometer
(`present`), refuse to stream it (`permitted = false`), or have it present and
permitted while nothing is running (`active = false`). The GUI table and
`phonebridge capabilities` show the same three columns.

---

## 7. Simulated data is labelled

`tools/simulator/phone_sim.py` produces synthetic readings so the PC side can be
tested without hardware. It reports:

* `simulated: true` in the capability map;
* `detail.sensors = "simulated: readings are generated by the simulator, not a phone's sensors"`;
* the same for `detail.file_transfer`, `detail.bluetooth_scan` and
  `detail.bluetooth_gatt`.

`windows/tests/test_controller_e2e.py` asserts that a simulator's `sensors`,
`file_transfer`, `bluetooth_scan` and `bluetooth_gatt` rows are `available` **and**
say `simulated`. A real phone never sends those strings, so a screenshot from a
simulator run cannot be presented as hardware evidence.

---

## 8. Tests

| Area | Where | What is covered |
| --- | --- | --- |
| Frames | `windows/tests/test_sensors.py` | header round-trip, unknown kind, NaN/Inf, count limits, sequence wrap, duplicates, gaps, stats |
| Link | `windows/tests/test_sensors.py` | list, start subset, refuse, stop, interval clamp, missing sensor, transport interruption, queue overflow drops oldest |
| End to end | `windows/tests/test_phase5_e2e.py` | discovery, start two sensors, readings arrive with increasing sequence numbers, stop, session survives |
| Self-test | `cli/selftest.py` | "sensor discovery and stream" |
| GUI text | `windows/tests/test_controller_planeview.py` | table rows, "no frame yet", per-sensor state, headline with interval/permission |
| Contract | `windows/tests/test_protocol_contract.py` | the four methods and their result keys exist in Kotlin |
| Android | — | **not compiled** (§10) |

---

## 9. Troubleshooting

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| the list is empty | the phone has no sensors, or `sensor.list` failed | the status line carries the phone's reason |
| a sensor is listed as unavailable | a text/streaming-only sensor (`reason` says which) | it is not streamable over this protocol |
| the measured rate is lower than requested | the sensor floored the interval at its `min_delay_us`, or the link is slow | the "Measured" column is the truth; pick a slower rate deliberately |
| `lost` climbs | frames were dropped because the PC was behind | expected at very high rates over Wi-Fi; the oldest sample is the one dropped |
| `out_of_order` climbs | the phone's sequence wrapped after a restart | harmless; stop and start to reset the counter |
| readings stop but state is "active" | the phone-side service was killed | Stop then Start; the session must still be up |
| `sensor_denied` | a restricted sensor on that build, or a revoked permission | the `detail` names the permission; motion sensors keep working |

---

## 9.1 Phase 5.1: measured permissions

`tests/test_phase51_android_contract.py` pins the source-level half: the sensor
catalogue no longer contains a hardcoded `permitted`, `checkSelfPermission` is
what feeds `permitted`/`detail`, heart-rate kinds are refused with a reason, and
`HIGH_SAMPLING_RATE_SENSORS` is declared in the manifest but absent from the
runtime request list. `tests/test_sensors.py` covers the PC side (denied,
revoked, malformed frames, the phantom-sensor fix). Neither runs a sensor: the
`SensorManager` half is §10.

---

## 10. Manual hardware checklist (not yet done)

No phone and no SDK in this environment: everything below is still required.

1. Install the debug APK, connect, open **Sensors**, press **Refresh list**.
2. Compare the names and count with a sensor-test app on the same phone; the
   simulator's seven sensors are not evidence.
3. Start the accelerometer at 50 Hz. Measured rate should be 45–52 Hz; tilt the
   phone and confirm the numbers track gravity (z ≈ 9.81 flat).
4. Start the gyroscope too. Confirm both stream at once and each has its own
   sequence numbers.
5. Change the rate to 1 Hz while streaming, stop and start: the applied interval
   changes and the status line says so.
6. Press Stop: the listeners are gone (verify with the sensor-test app that no
   listener is registered) and `frames` stops increasing.
7. Walk out of Wi-Fi range: readings stop, the state says idle/interrupted, and
   nothing resumes by itself after reconnecting.
8. Lock the phone screen with the bridge running: Android may throttle
   background sensors — record what actually happens on your build.
9. Uninstall or force-stop the app while streaming: the PC must show the session
   gone, not a frozen table.
