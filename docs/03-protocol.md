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
| `0x30` | AUDIO | `u8 codec ‖ u8 flags ‖ u16 seq ‖ frame` (Phase 2) |
| `0x40` | VIDEO | Phase 4 |
| `0x50` | FILE | Phase 5 |
| `0x60` | SENSOR | Phase 5 |
| `0x7F` | BYE | UTF-8 reason |

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
  `bad_request`, `unsupported_by_device`.

### Implemented RPC methods (phone side)

| method | result |
| --- | --- |
| `system.info` | device id, name, model, Android version, capabilities, protocol version |
| `system.echo` | echoes `params.value` |
| `session.stats` | frames/bytes counters, active connections |
| `bluetooth.headset.status` | headset connected?, profile name, communication devices |
| `audio.route.bluetooth` | Phase 2 placeholder that reports support honestly |

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
