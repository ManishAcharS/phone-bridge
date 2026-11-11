# PhoneBridge wire protocol v1

Normative reference for both implementations:

* `windows/phonebridge/protocol/` (Python, canonical)
* `android/app/src/main/java/dev/phonebridge/protocol/` (Kotlin)

Any change here needs a version bump in `PROTOCOL_VERSION` on both sides.

## 1. Envelope

Every message, in every phase of the connection, is one envelope:

```
+-----------------+---------+------------------+
| u32 length (BE) | u8 kind | body[length - 1] |
+-----------------+---------+------------------+
```

`length` counts the kind byte, so the minimum envelope is 5 bytes. Maximum body
is 8 MiB (`MAX_ENVELOPE_BODY`); a larger length prefix is a framing error and
closes the connection.

| kind | name | body |
| --- | --- | --- |
| `0x01` | `HANDSHAKE` | UTF-8 JSON. **Plaintext**, legal only before the secure channel exists. |
| `0x02` | `SECURE` | `u64 counter (BE) ‖ ciphertext ‖ GCM tag` |
| `0x03` | `CLOSE` | UTF-8 JSON `{"reason": "..."}`. Best effort. |

Transport: one envelope = one WebSocket **binary** frame. A text frame is a
protocol violation (close 1003). The same envelope can be carried over a raw TCP
stream using a length-prefix reassembler.

**Who may open the socket.** The listener accepts native clients only: a
handshake that carries an `Origin` header is refused with close `1008` before any
session state is created. Browsers always send `Origin` and native clients never
do, so this is what stops a web page -- which is not subject to the same-origin
policy when it opens a WebSocket -- from driving the bridge or provoking pairing
prompts. `docs/13-security-review.md` S1.

## 2. Handshake

All handshake messages are `kind = 0x01`. Field names are snake-free lowercase.

### 2.1 `hello` (PC → phone)

```json
{"t":"hello","v":1,"id":"<16-hex>","name":"desktop",
 "static":"<b64 65-byte P-256 point>",
 "eph":"<b64 65-byte P-256 point>",
 "nonce":"<b64 16 bytes>"}
```

### 2.2 `hello_ack` (phone → PC)

```json
{"t":"hello_ack","v":1,"id":"<16-hex>","name":"Pixel 7",
 "static":"...","eph":"...","nonce":"<b64 16 bytes>",
 "paired":false}
```

`paired` tells the PC whether the phone already knows it. It is advisory; the
authoritative answer arrives in `auth_ok` / `pair_required`.

**Both sides must reject** `v != 1` with a version-mismatch error. If one side
speaks v2 and the other v1, the connection fails loudly rather than
misinterpreting fields.

**Device ids are self-certifying.** `id` is `sha256(uncompressed public point)`
in lowercase hex, first 16 characters, and each side MUST verify that the `id` it
received really is the hash of the `static` key in the same message (malformed or
mismatching ids are refused). Without it an unauthenticated peer could present
itself under any id it liked -- including one the other side has pinned. The id
in `hello_ack`/`auth_ok`/`pair_required` must also match the id from `hello`.
`docs/13-security-review.md` S2.

### 2.3 Key derivation

```
static_shared    = ECDH(my_static_priv, peer_static_pub)
ephemeral_shared = ECDH(my_eph_priv,    peer_eph_pub)
ikm              = static_shared ‖ ephemeral_shared          (64 bytes)
salt             = client_nonce ‖ server_nonce                (32 bytes)

prk  = HKDF-Extract-SHA256(salt, ikm)
c2s  = HKDF-Expand-SHA256(prk, "phonebridge/1 c2s", 32)
s2c  = HKDF-Expand-SHA256(prk, "phonebridge/1 s2c", 32)
sas  = (BE32(HMAC-SHA256(c2s, "phonebridge/1 sas")[0:4]) mod 1_000_000), %06d
```

`static_shared` binds the session to both *identities*; `ephemeral_shared` gives
forward secrecy against a later key compromise. Because the PC must be able to
compute `static_shared` with the phone's static key and vice versa, a
man-in-the-middle who does not hold either private key cannot derive `c2s`/`s2c`.

The SAS is **not** used as key material — only as a value for human comparison.

## 3. Setup messages (inside the secure channel)

Once both sides have derived keys, all further messages are `kind = 0x02` and
carry `msg_type = 0x10` (control) with a JSON payload whose `k` field is one of
the setup names below. Setup messages are deliberately distinct from
`req`/`res`/`evt` so the session layer can tell them apart.

| `k` | direction | fields |
| --- | --- | --- |
| `auth` | PC → phone | `id`, `name`, `proto` |
| `auth_ok` | phone → PC | `id`, `name`, `capabilities` |
| `pair_required` | phone → PC | `id`, `name`, `sas` (formatted `"123 456"`), `capabilities` |
| `auth_fail` | phone → PC | `reason` |
| `pair_confirm` | PC → phone | `sas` |
| `pair_cancel` | PC → phone | *(none)* |
| `pair_ok` | phone → PC | `id`, `name` |
| `pair_fail` | phone → PC | `reason` |

Sequence:

1. PC sends `auth` encrypted. **Successful AEAD decryption is the
   authentication proof** — only a peer that derived the session keys can
   produce a frame that opens.
2. Phone checks: `auth.id` equals the id from the hello, and (if that id is
   already pinned) the presented static key matches the pinned one.
   * pinned → `auth_ok`, session READY.
   * not pinned → `pair_required` with the SAS, session waits.
3. PC shows the SAS, the user compares it with the phone, and confirms.
   PC sends `pair_confirm`. A mismatch aborts on both sides.
4. Phone asks its own user (the phone screen shows the same SAS + Allow/Deny).
   Both the PC's confirmation **and** the user's decision are required.
5. Phone pins the PC's identity key, replies `pair_ok`. Session READY.

Closing rules during setup:

* An id known to the phone with a different static key → close with WebSocket
  code `1008` and reason `identity key changed for known device`. No fallback to
  pairing: that would let a reflashed or substituted device take over a pin.
* On the PC side, the reverse check happens before keys are derived: the claimed
  id is looked up in `peers.json`, and if found, `hello_ack.static` must match
  byte for byte.

### 3.1 Limits, setup timeout and shutdown (added in Phase 1)

None of this changes the wire format; it is behaviour both implementations are
required to have, and each side has tests for it.

| Situation | Behaviour |
| --- | --- |
| Peer connects and sends nothing | Close `1002` with reason `setup timeout` after 20 s. Applied on the phone before the first frame, and on the PC around `hello_ack`. |
| Phone is at a session limit | Close `1008` with reason `too many connections from this device` (4 per device) or `server connection limit reached` (8 total). Sent **before** `auth_ok`/`pair_ok`, so the PC reports a refusal rather than a successful session that immediately vanished. |
| Phone stops on purpose | Send BYE (`0x7F`, UTF-8 reason) inside the secure channel, then close `1000`. A BYE is a clean shutdown: the PC must not treat it as a network failure. |
| PC closes on purpose | Send a CLOSE envelope (§1) and close `1000`. The phone treats it as a normal disconnect, not an error. |
| Identity key of a known device changed | Close `1008`, reason `identity key changed for known device`. **Terminal**: the PC must not retry with the same pin. |
| Protocol version mismatch | `auth_fail` with `reason: protocol version`, then close `1002`. **Terminal**: no retry, no fallback. |
| Authentication refused | `auth_fail` inside the secure channel, then close `1008` with the reason. **Terminal**. |

Both sides distinguish *terminal* failures (key change, version skew, refusal,
declined pairing) from *transient* ones (socket drops, timeouts). Only transient
failures are retried, with backoff.

## 4. Secure frames

```
body = counter(8, BE) ‖ AES-256-GCM(key, nonce, aad=counter, plaintext)
nonce = 00000000 ‖ counter(8, BE)          -- 12 bytes
plaintext = msg_type(1) ‖ stream_id(4, BE) ‖ payload
```

* Separate key per direction: the PC sends with `c2s`, the phone with `s2c`, so
  each direction has an independent counter starting at 0 and nonces can never
  repeat.
* The counter travels in the clear and is authenticated as AAD, so it cannot be
  altered without breaking the tag.
* The receiver rejects `counter <= last_seen` (`ReplayError`) and closes the
  connection. Ordering is strict: this protocol assumes a reliable, ordered
  transport. A future UDP media plane needs its own windowing and is not covered
  by this rule.

## 5. Message types

| type | name | payload |
| --- | --- | --- |
| `0x10` | CONTROL | JSON, see §6 |
| `0x11` | PING | `u64` sender clock (ns) |
| `0x12` | PONG | `u64` echoed value ‖ `u64` responder monotonic clock (ns, informational) |
| `0x21` | BLOB | opaque bytes (throughput test; echo stream) |
| `0x30` | AUDIO | `u8 codec ‖ u8 flags ‖ u16 seq ‖ u32 ts_ms ‖ frame` (Phase 2, implemented) |
| `0x40` | VIDEO | `u8 codec ‖ u8 flags ‖ u16 seq ‖ u32 ts_ms ‖ u16 w ‖ u16 h ‖ u16 rot ‖ jpeg` (Phase 4, implemented) |
| `0x50` | FILE | Phase 5 |
| `0x60` | SENSOR | Phase 5 |
| `0x7F` | BYE | UTF-8 reason |

A request may be answered **later than the read loop**: `audio.capabilities.run`
is dispatched to a worker thread and its `res` is sent from there, so the
connection keeps answering heartbeats for the whole probe. Clients must not
assume the reply arrives within a fixed short window.

`stream_id` multiplexes independent flows; control always uses `0`. RTT is
computed application-side from PING/PONG because that is the number that matters
for audio latency, not TCP's estimate.

The second PONG field is **informational only** and is never used for timing: the
receiver computes RTT solely from the echoed value, so the two devices' clocks
never need to agree. Both implementations use a monotonic clock
(`time.monotonic_ns()` / `System.nanoTime()`) so the field means the same thing
on each side, but its epoch is arbitrary and device-local. A PONG that is not
exactly 16 bytes is dropped rather than treated as an error.

## 6. Control JSON

```json
{"k":"req","id":7,"method":"system.info","params":{}}
{"k":"res","id":7,"ok":true,"result":{...}}
{"k":"res","id":7,"ok":false,"error":{"code":"method_not_found","message":"..."}}
{"k":"evt","event":"bluetooth.headset","data":{"connected":true}}
```

* `id` is a positive integer chosen by the requester; `0` is reserved.
* Error codes in use: `method_not_found`, `bad_params`, `internal`,
  `bad_request`, `unsupported_by_device`, `probe_failed` (the asynchronous
  `audio.capabilities.run` worker could not complete a measurement; the session
  stays open), `busy` (a request that is valid but cannot be started now --
  currently: a second `audio.capabilities.run` while one is already running. The
  phone refuses instead of queueing, because a probe can hold the microphone for
  minutes and a queue of probes is an open recording session. The request may be
  retried later; the session stays open), and — from Phase 4 — `camera_denied`
  (the user has not granted `CAMERA`; the phone never opens the lens to find out),
  `no_camera` (this phone has no such lens), `camera_busy` (another app holds the
  camera, or the camera service refused to bind) and `bad_config` (the requested
  resolution/rate/quality/facing is outside the documented ranges).

### Implemented RPC methods (phone side)

| method | result |
| --- | --- |
| `system.info` | device id, name, device block (see below), `capabilities`, `permissions`, protocol version |
| `system.echo` | echoes `params.value` |
| `session.stats` | frames/bytes counters, active connections |
| `bluetooth.headset.status` | headset connected?, profile name, communication devices |
| `audio.route.bluetooth` | Phase 2 placeholder that reports support honestly |
| `audio.capabilities.run` | run the capability probe on the phone; result is the report object itself (see `docs/08-multi-device-test-strategy.md` §2). Params: `seconds` (5-300, default 30), `acoustic` (bool). **Long-running:** the phone answers from a worker thread and never blocks its read loop, so keep the client timeout generous (the CLI uses 420 s) |
| `audio.capabilities.last` | `{available: bool, report?: object}` — the last probe run, so a reconnecting PC need not re-measure |

#### Response schemas (normative)

The phone answers `{"k":"res","id":<id>,"ok":true,"result":<result>}`; the
implementations must agree on the **required** keys of every `result`. Extra
keys are additive and must be ignored by a client. Types are JSON types.

| method | required keys of `result` |
| --- | --- |
| `system.info` | `device_id` str, `name` str, `protocol_version` int, `capabilities` object, `permissions` object, plus the device block below (`model`, `manufacturer`, `brand`, `device`, `product`, `hardware`, `android_release`, `android_version`, `sdk_int`, `target_sdk`, `app_version`, `app_version_code`, `package`, `rom`, `rom_version`, `is_emulator`) |
| `system.echo` | `echo` (whatever was passed in `params.value`, including absent), `at_ns` int |
| `session.stats` | `frames_in` int, `frames_out` int, `bytes_in` int, `bytes_out` int, `connections` int |
| `bluetooth.headset.status` | `connected` bool, `profile` str, `devices` array of `{id int, type str, type_id int, product str\|null, is_sink bool, is_source bool, sample_rates int[]}` |
| `audio.route.bluetooth` | `supported` bool, `detail` str |
| `audio.capabilities.run` | the probe report itself (schema in `docs/08` §2): `schema_version`, `source`, `device`, `route_output`, `route_input`, `duplex`, `latency` |
| `audio.capabilities.last` | `available` bool; when true also `report` object with the schema above; when false `reason` str |
| `audio.playback.start` | `ok` bool, `format` object (`sample_rate` int, `channels` int, `frame_ms` int, `codec` int) — the format the phone will actually play |
| `audio.playback.stop` | `ok` bool, `playback` str (`idle`) |
| `audio.playback.mute` | `ok` bool, `muted` bool |
| `audio.playback.volume` | `ok` bool, `percent` int (0-100, echoed after clamping) |
| `audio.mic.start` | `ok` bool, `format` object with the same four keys — microphone frames follow on stream 2 |
| `audio.mic.stop` | `ok` bool, `mic` str (`idle`) |
| `audio.status` | `playback` str, `mic` str, `volume` int, `muted` bool, `permitted` bool |
| `camera.start` | `ok` bool, `config` object (`width` int, `height` int, `fps` int, `quality` int, `facing` str) |
| `camera.stop` | `ok` bool, `streaming` bool (always false) |
| `camera.switch` | `ok` bool, `facing` str, `streaming` bool |
| `camera.status` | `streaming` bool, `facing` str, `width` int, `height` int, `fps` int, `frames_sent` int, `dropped` int, `permitted` bool, `hardware` bool, `last_error` str |

`capabilities` (from `system.info`) has, for each of the eight keys, a bool
`<key>`, a bool `hardware.<key>` and a string `detail.<key>`; the strings
`audio.bluetooth_profile` and int `protocol_version` may also appear.

#### `MSG_AUDIO` body (normative, Phase 2)

Audio travels as `MSG_AUDIO` (0x30) inside the encrypted envelope, one frame per
message. Stream ids are fixed: **1 = PC to phone** (playback), **2 = phone to PC**
(microphone). Both directions use this body:

| offset | size | field |
| --- | --- | --- |
| 0 | 1 | `codec` — `1` = PCM16LE (the only codec defined) |
| 1 | 1 | `flags` — bit0 `START`, bit1 `STOP`, bit2 `MUTED` |
| 2 | 2 | `seq` — big-endian, wraps at 65535 |
| 4 | 4 | `ts_ms` — big-endian, sender's monotonic ms since the stream started |
| 8 | n | PCM16LE, whole samples only, at most 512 KiB |

Rules both sides enforce:

* the default format is 48 000 Hz, mono, 20 ms frames (960 samples, 1920 bytes);
  `audio.playback.start` / `audio.mic.start` may adjust it, and the phone's answer
  is authoritative;
* a frame shorter than 8 bytes, with an unknown codec, an odd payload, or over the
  size limit is **dropped and counted**, never fatal;
* `STOP` ends the direction without closing the session; `MUTED` frames carry
  silence and keep the timing (so "muted" is distinguishable from "disconnected");
* a receiver tolerates a bounded amount of reordering, drops duplicates and frames
  older than its window, and resynchronises if the sender jumps forward.

#### `MSG_VIDEO` body (normative, Phase 4)

Camera frames travel as `MSG_VIDEO` (0x40) inside the same encrypted envelope as
audio and control — there is no second socket and no unencrypted media path. The
stream id is fixed: **3 = phone to PC**. One message carries **one complete JPEG**,
so a lost message costs a picture and never a corrupted frame, and there is no
codec state to rebuild after a reconnect.

| offset | size | field |
| --- | --- | --- |
| 0 | 1 | `codec` — `1` = JPEG (the only codec defined) |
| 1 | 1 | `flags` — bit0 `START`, bit1 `STOP`, bit2 `KEY` |
| 2 | 2 | `seq` — big-endian, wraps at 65535 |
| 4 | 4 | `ts_ms` — big-endian, sender's monotonic ms since this stream started |
| 8 | 2 | `width` — big-endian pixels, as encoded |
| 10 | 2 | `height` — big-endian pixels, as encoded |
| 12 | 2 | `rotation` — big-endian degrees the receiver must rotate before display: 0/90/180/270 |
| 14 | ø | JPEG bytes, starting `FF D8` and ending `FF D9`, at most 2 MiB |

Rules both sides enforce:

* the header is 14 bytes; a body shorter than that, with an unknown codec, an
  impossible size, a rotation outside 0/90/180/270, a payload that does not start
  with `FF D8` or does not end with `FF D9`, or a body over 2 MiB + header is
  **dropped and counted, never fatal** — a bad frame must not kill a preview or a
  session;
* `rotation` says how the *receiver* should rotate; the sender never rotates the
  bytes, so the same frame is useful to a client that cannot decode;
* `START` marks the first frame of a stream (the receiver uses it to reset its
  reorder window); `STOP` ends it, carries no picture and may have an empty
  payload;
* a receiver tolerates a bounded amount of reordering, drops duplicates and frames
  older than its window, drops anything that arrives after a newer frame (the
  preview never rewinds), and resynchronises if the sender jumps forward;
* frames go through a **bounded** queue (5 frames on the PC, 2 on the phone). When
  a consumer is behind the *oldest* frame is discarded, because a late frame is
  worth less than a current one; latency stays flat instead of growing;
* video is **one-way in protocol v1**: phone to PC. A `MSG_VIDEO` arriving at the
  phone is ignored and logged; it is not an error and does not close the session;
* `camera.stop`, a closed session, a failed start, a phone-side stop and
  `BridgeService.onDestroy` all end transmission. Stop really stops: the phone
  unbinds every CameraX use case and the camera becomes available to other apps.

Conservative defaults: **1280×720, 15 fps, JPEG quality 70, rear camera.** Valid
ranges are 320–3840 × 240–2160, 1–60 fps, quality 20–95, facing `back`/`front`.

Error replies are `{"k":"res","id":<id>,"ok":false,"error":{"code":str,"message":str}}`
with one of the codes listed above. An unknown method **must** be answered with
`method_not_found` and must not close the session.

**There is no other operation.** File transfer (both directions) and sensor
retrieval have no method in this protocol: the message kinds `0x50 FILE` and `0x60
SENSOR` are reserved numbers for later phases with no body format defined and no
handler on either side. (`0x30 AUDIO` and `0x40 VIDEO` are implemented, as
described above.) A client that invents a method name will receive
`method_not_found`, which is the correct answer.

#### `system.info` device block

`device_id` and `name` as before, plus flat fields (all additive; nothing was
removed): `model`, `manufacturer`, `brand`, `device`, `product`, `hardware`,
`android_release`, `android_version` (same value, kept for older clients),
`sdk_int`, `target_sdk`, `app_version`, `app_version_code`, `package`, `rom`,
`rom_version`, `is_emulator`, `protocol_version`.

#### Capability keys: measured, not asserted

Two separate questions, deliberately kept apart:

* `hardware.<key>` — does this phone have the hardware? Measured with the
  platform APIs (camera enumeration via `CameraManager`, `SensorManager`, audio
  device lists, Bluetooth feature flags). No permission is required to ask.
* `<key>` — can PhoneBridge **use** it today? True only when the feature is
  implemented, the hardware is present and any runtime permission is granted.
* `detail.<key>` — one sentence explaining why a capability is not available.

Keys: `audio.playback`, `audio.capture`, `audio.bluetooth_sco`, `camera.back`,
`camera.front`, `sensors`, `file_transfer`, `bluetooth_scan`, plus the
string-valued `audio.bluetooth_profile` and the integer `protocol_version`.

In this phase every bare capability key is `false` on a real phone: the audio
relay is Phase 2, camera streaming Phase 4, file transfer and sensors Phase 5.
A PC must treat `hardware.*` as "what the device has" and the bare key as "what
works now"; showing the former as if it were the latter is the bug this contract
exists to prevent. The simulator (`tools/simulator/phone_sim.py`) reports
`simulated: true` and may advertise capabilities the real app does not have yet —
it is a protocol double, never evidence about a device.

#### `permissions`

`system.info.permissions` maps the short permission name to
`granted` / `denied` / `permanently_denied` / `not_applicable`, for the runtime
permissions this build declares (`RECORD_AUDIO`, `BLUETOOTH_CONNECT` on Android
12+, `POST_NOTIFICATIONS` on Android 13+). A permission the app has not declared
is never reported, and no permission is requested that the app does not use.

Adding a method is backwards compatible: unknown methods return
`method_not_found` rather than killing the session, so a newer PC can talk to an
older phone.

## 7. Discovery beacon (not part of the protocol)

UDP broadcast to port 47823 every 2 s:

```json
{"app":"phonebridge","v":1,"id":"<device id>","name":"Pixel 7",
 "port":8765,"method":"wifi"}
```

The PC only listens. The beacon carries no keys and grants no trust; it exists so
the user does not have to type an IP address. Broadcast does not cross subnets
and some APs block it, so manual entry and the cached last endpoint always work.

## 8. Compatibility rules

1. Bump `PROTOCOL_VERSION` for any change to envelope, handshake, key derivation
   or message framing. Unknown versions must fail loudly.
2. New RPC methods, new event names and new message types are additive and do
   not need a version bump.
3. The Kotlin implementation must satisfy the vectors in
   `ProtocolVectors.kt`; regenerate them with
   `python tools/vectorgen/gen_vectors.py` after any crypto change.
