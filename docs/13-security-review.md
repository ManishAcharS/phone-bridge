# 13. Security review and hardening

Date: 2026-11-08 · Scope: Android app, PC client, WebSocket protocol v1,
authentication, pairing, file transfer · Status: **13 findings, 13 fixed, every
fix has a regression test**; 6 residual risks accepted and listed in §6.

Nothing in this document is aspirational. Every "fix" row names the code that
now enforces it and the test that fails without it; every "remaining risk" is a
thing the code still does *not* do, written down so nobody assumes otherwise.

---

## 1. What was reviewed, and how

Reviewed by reading the code and then attacking it:

| Area | Files |
| --- | --- |
| Transport + framing | `windows/phonebridge/protocol/framing.py`, `android/.../protocol/Framing.kt` |
| Handshake, keys, replay | `protocol/crypto.py`, `Crypto.kt` |
| Session, pinning, pairing | `core/session.py`, `net/BridgeServer.kt`, `net/PairingStore.kt`, `net/IdentityStore.kt` |
| Local state | `core/device_store.py`, `IdentityStore.kt`, `PairingStore.kt` |
| Discovery | `transport/discovery.py`, `net/DiscoveryBeacon.kt` |
| Android platform surface | `AndroidManifest.xml`, `Permissions.kt`, `service/BridgeService.kt`, `net/CapabilityProbe.kt` |
| Reference phone | `tools/simulator/phone_sim.py` |

Method: for each check below, either (a) show the control exists and drive it,
or (b) show it does not and fix it. Behavioural tests attack the real
implementation — real WebSocket, real P-256 handshake, real AES-GCM. The Kotlin
side cannot be built or run on this machine, so Android-only guards are pinned
by source assertions in `tests/test_security_hardening.py` **and** implemented
identically in the reference phone, where they *are* exercised end to end. Where
that distinction matters it is stated in the finding.

Reproduce:

```powershell
cd windows
pytest -q tests/test_security_hardening.py      # 37 tests
pytest -q                                        # 479 tests
cd .. ; ruff check . ; python tools/verify.py
```

---

## 2. Verdict on the thirteen areas

| # | Area | Verdict |
| --- | --- | --- |
| 1 | Pairing-code generation and expiration | **Two findings, both fixed** (S5, S9). The six-digit code is derived from the session key (`HMAC(c2s,"phonebridge/1 sas")`), not generated randomly, and both sides compare it. It now expires with the request and is never logged. |
| 2 | Authentication and authorization | **Two fixed** (S2, S4). Mutual ECDH + pinned identity. Authorization is binary: paired ⇒ all methods. No per-method scoping (§6). |
| 3 | Replay and unauthorized-request protection | **Sound as designed, one gap fixed** (S6). Monotonic counter with the counter as AEAD AAD; plaintext frames are refused mid-session; unauthenticated pre-auth chatter is now budgeted. |
| 4 | WebSocket origin and connection validation | **One high finding, fixed** (S1). Origin was not checked at all. |
| 5 | Sensitive data handling | **Two fixed** (S9, S10); one accepted risk (unencrypted identity key, §6). |
| 6 | File path traversal | **Not applicable — there is no file transfer.** No file I/O exists in the Android app (no `java.io`, `FileOutputStream`, `cacheDir`, `MediaStore`) and no protocol method takes or returns a path. Kind `0x50 FILE` is a reserved number with no body format (see `docs/12` §6). Nothing to traverse. |
| 7 | File size and type validation | **Not applicable**, same evidence. The one size ceiling that does exist — 8 MiB per envelope — is enforced on both sides (`Framing.decode`, `MAX_ENVELOPE_BODY`) and now checked *before* the Android copy (S7). |
| 8 | Unsafe file overwrite | **Not applicable.** The only writes are PhoneBridge's own state files (`_write_private`, atomic `os.replace`, mode 0600) and a compat report to a path the *user* typed (`--out`), never a path a peer chose. |
| 9 | Input validation | **Three fixed** (S2, S8, S11). JSON is parsed into typed fields; `seconds` is clamped to 5–300; device ids must match `^[0-9a-f]{16}$`; beacon fields are validated; peer names are neutralised. |
| 10 | Logging of secrets or private data | **Two fixed** (S9, S10). No key material was ever logged; the pairing code was, on three sides. |
| 11 | Network exposure and local network trust | **One fixed** (S1), rest accepted (§6): the listener is bound to `0.0.0.0` by design, the beacon advertises the phone on the LAN, and the trust model is "the LAN is hostile, the protocol is not". |
| 12 | Resource exhaustion and oversized messages | **Four fixed** (S5, S6, S7, S8). |
| 13 | Permission boundaries on Android | **No vulnerability found**; the manifest declares no `CAMERA`, `READ_MEDIA_*` or storage permission, and runtime permissions are requested at the moment of use. `RECORD_AUDIO` is only touched by the capability probe, which is now gated to one run at a time (S8). |

---

## 3. Findings

Severity: **High** = exploitable by someone on the same network with no
credentials; **Medium** = needs a paired/stalled peer or a local precondition;
**Low** = information or hygiene, no direct compromise.

### S1 — Any web page could drive the bridge (no `Origin` check)

* **Severity: High.**
* **Vulnerability.** The phone accepts a WebSocket from anyone who can reach
  port 8765, and a WebSocket handshake is exempt from the same-origin policy: no
  preflight, no CORS. Any page the user visits while on the same network could
  open `ws://<phone-ip>:8765`, run the handshake with a key pair *it* generates
  (WebCrypto has P-256 ECDH), and land in the pairing path — putting an
  attacker-controlled "PC" prompt on the user's screen with an attacker-chosen
  name. One tap pairs the attacker; failing that, the page can spam prompts and
  read `system.info`-class metadata. This is the classic DNS-rebinding/local-WS
  attack, and it also covers a rebinding attack because browsers always send
  `Origin`.
* **Evidence.** `BridgeServer.onOpen` created a `PeerSession` unconditionally;
  `_serve_connection` in the reference phone did the same. Neither read
  `handshake.getFieldValue("Origin")`. Test
  `test_the_phone_refuses_a_browser_origin` (fails before the fix: the
  connection completes and the phone records a handshake).
* **Fix.** Reject any handshake that carries an `Origin` header (`1008`,
  "browser origins are not accepted") before a session is created — native
  clients never send one, browsers always do. `BridgeServer.kt` `onOpen`;
  `phone_sim.py` `_origin_of` / `_handle_connection`.
* **Verification.** `test_the_phone_refuses_a_browser_origin` (behavioural, on
  the reference phone: 1008, `rejected_origins == 1`, `handshakes == 0`, and a
  native client still connects) and
  `test_android_refuses_browser_origins_in_on_open` (the Android check precedes
  session creation).
* **Remaining risk.** A native client that *does* send an `Origin` header would
  be refused; that is the intended trade-off and is documented in
  `docs/11-pc-client.md`. An attacker who can run native code on the LAN is not
  stopped by this — that is the pairing model's job (S2/S4).

### S2 — A peer could claim any device id on a first-time connection

* **Severity: Medium-High.**
* **Vulnerability.** The device id is *self-certifying* —
  `sha256(public_key)[:16]` — but neither side ever checked that. The PC used the
  claimed id to look up a pin and, finding none, continued happily; the phone
  used it to look up `PairingStore`. So an unpaired peer could present itself
  under an id it does not own: the id of a phone the user had pinned on the PC
  (blocked only by the key comparison, which is skipped when the guess is wrong
  *and* the pin is missing), or of another PC in the phone's pin list. On a
  trust-on-first-use connection nothing else would catch it.
* **Evidence.** `HandshakeInitiator.on_hello_ack` decoded `static` and read
  `id` only as a dictionary key; `HandshakeResponder.on_hello` stored
  `peer_device_id = str(msg["id"])` unchecked. Tests
  `test_hello_ack_with_someone_elses_id_is_refused`,
  `test_the_phone_side_also_requires_the_id_to_certify_the_key`.
* **Fix.** New `crypto.check_identity_binding()`: the id must match
  `^[0-9a-f]{16}$` and equal `sha256(static)[:16]` (constant-time compare),
  otherwise `IdentityBindingError`. Applied in *both* the initiator and the
  responder, before any pin lookup or key derivation.
* **Verification.** `test_a_device_id_is_the_hash_of_its_public_key`,
  `test_hello_ack_with_someone_elses_id_is_refused`,
  `test_hello_ack_with_a_malformed_id_is_refused` (6 malformed shapes),
  `test_the_phone_side_also_requires_the_id_to_certify_the_key`.
* **Remaining risk.** None for id spoofing. The id is still only 64 bits, so a
  deliberate collision is conceivable in principle; it would also have to
  collide in the *key*, which the pin comparison covers.

### S3 — A corrupt pin list silently unpinned every phone

* **Severity: Medium.**
* **Vulnerability.** `_read_json` returned the default on any parse error, so a
  truncated or hand-edited `peers.json` read as "no phones are pinned". The next
  connection then took the trust-on-first-use path for a phone that had already
  been verified — a security control failing *open* and silently.
* **Evidence.** `device_store._read_json` swallowed `JSONDecodeError`;
  `list_peers()` therefore returned `[]`. Test
  `test_a_corrupt_pin_store_refuses_to_connect` (before the fix: the session
  opened and started a pairing).
* **Fix.** `StoreCorruptError`; `list_peers()` reads strictly and validates the
  document shape and every entry. `BridgeSession._handshake` converts it to a
  `TerminalSessionError` *before* sending anything, so the user gets "peers.json
  is unreadable or corrupt … fix or delete that file" instead of a silent
  re-pair.
* **Verification.** `test_a_corrupt_pin_store_refuses_to_connect`
  (`phone.pairing_requests == 0`), `test_pin_store_rejects_a_truncated_key`.
* **Remaining risk.** An attacker who can already write to the config directory
  can delete the file; that is local compromise, out of scope (§6).

### S4 — A lost local pin meant *permanently* unpinned

* **Severity: Medium.**
* **Vulnerability.** Pinning happened only in the pairing path. If `peers.json`
  was lost (new profile, restored backup) while the phone still had the PC
  pinned, the phone answered `auth_ok` and the PC accepted it **without storing
  anything** — so every later connection was unpinned too, and a substituted
  phone would be accepted silently.
* **Evidence.** `_handshake`'s `auth_ok` branch called `_mark_seen()`, which is
  a no-op when `find_peer()` returns `None`. Test
  `test_a_paired_phone_is_repinned_if_the_local_pin_was_lost`.
* **Fix.** On `auth_ok`, if this PC has no pin for the authenticated peer, pin
  the key the handshake just proved. `auth_ok` is only sent when the *phone* has
  this PC pinned, so mutual trust already exists; recording it restores mutual
  pinning instead of downgrading.
* **Verification.** `test_a_paired_phone_is_repinned_if_the_local_pin_was_lost`
  (delete `peers.json`, reconnect, assert the same key is pinned again).
* **Remaining risk.** If the pin list is lost *and* an attacker substitutes the
  phone before the next connection, this PC would adopt the attacker's key on
  the strength of the attacker's claim to be paired. The phone's own pin list is
  authoritative for the phone's side; the PC has no way to query it. Accepted
  and noted in §6.

### S5 — Pairing prompts outlived their sessions and were unlimited

* **Severity: Medium.**
* **Vulnerability.** `pendingPairing` entries were removed only when the user
  answered them. A session closed by the setup timeout left its entry behind:
  the prompt stayed on screen for a PC that was long gone (and could still be
  "approved"), and the list grew without bound for as long as the process ran —
  a LAN attacker cycling connections could accumulate entries indefinitely.
* **Evidence.** `dropPendingPairing` did not exist; `PeerSession.close()` and
  `onClosed()` only called `unregisterSession`. Reference phone: no cap at all,
  and a 90 s wait that never expired the request in the record. Test
  `test_pending_pairing_requests_are_capped`,
  `test_an_unanswered_pairing_request_expires_and_cleans_up`.
* **Fix.** (a) Android: `dropPendingPairing(session)` called from both teardown
  paths, `MAX_PENDING_PAIRINGS = 4` beyond which the request is refused with
  `pair_fail` + `1008`, and the setup-timeout path now logs an explicit
  "pairing request … expired" message. (b) Reference phone: `pairing_window_s`
  expiry, `max_pending_pairings`, and a `finally` that always forgets the
  request. (c) PC: an early `pair_fail` is now reported as *"phone refused
  pairing: …"* instead of "unexpected handshake message".
* **Verification.** `test_an_unanswered_pairing_request_expires_and_cleans_up`
  (`_pending_pairings == set()` afterwards, request retryable),
  `test_pending_pairing_requests_are_capped`,
  `test_android_drops_pairing_prompts_for_dead_sessions`.
* **Remaining risk.** A user who leaves a prompt up still pairs whatever is on
  screen after up to 20 s (Android setup timeout) — that is the human step.

### S6 — Unauthenticated frames were free

* **Severity: Medium.**
* **Vulnerability.** Before `auth_ok`, every frame is unauthenticated. The PC
  looped on `recv()` until its deadline, accepting any number of frames — so a
  peer could open a socket and stream junk for 15 s per attempt, consuming CPU,
  memory and log volume for free.
* **Evidence.** `_expect_control` had no counter; `test_flooding_the_handshake_fails_fast`
  (before the fix: no error until the request timeout).
* **Fix.** `HANDSHAKE_FRAME_BUDGET = 32` frames and `HANDSHAKE_BYTE_BUDGET =
  256 KiB` for everything before the session is authenticated; exceeding either
  is a `SessionError`, not a retryable timeout. `WebSocketLink` also gained an
  explicit `close_timeout = 1.0` (the library default is 10 s, which a hostile
  peer could otherwise hold open after every refusal).
* **Verification.** `test_flooding_the_handshake_fails_fast` (200 frames ⇒
  failure in under 10 s, with a 20 s request timeout).
* **Remaining risk.** The transport still buffers up to `max_queue × 8 MiB`
  before the session layer sees it; the budget stops the *work*, not the
  socket-level buffering.

### S7 — The Android server copied a frame before checking its size

* **Severity: Medium.**
* **Vulnerability.** `onMessage(conn, message: ByteBuffer)` did
  `ByteArray(message.remaining())` first and only then let `Framing.decode`
  reject anything over 8 MiB. Every oversized frame therefore cost a fresh
  8 MiB+ allocation on the way to being rejected, and could be repeated.
* **Evidence.** `BridgeServer.onMessage` — copy precedes `Framing.decode`.
* **Fix.** Check `message.remaining() > Framing.MAX_ENVELOPE_BODY` first and
  close with `1009` ("frame too large"). The reference phone relies on
  `serve(max_size=MAX_ENVELOPE_BODY + 64)`, which closes with `1009` too — and
  that is the behaviour the behavioural test asserts.
* **Verification.** `test_android_checks_frame_size_before_copying` (guard exists
  and precedes the copy) and `test_the_phone_refuses_an_oversized_frame`
  (behavioural: 9 MiB ⇒ `1009`, `frames_in == 0`).
* **Remaining risk.** Java-WebSocket still buffers the offending frame once
  before the callback; the app-level guard prevents the second, repeatable copy.

### S8 — The capability probe could be queued without limit

* **Severity: Medium.**
* **Vulnerability.** `audio.capabilities.run` queued work on a single-thread
  executor with no bound. Each probe can hold the microphone for up to 300 s, so
  a paired PC (or a looping script, or a compromised one) could keep the mic
  busy indefinitely and accumulate answers nobody was waiting for.
* **Evidence.** `runCapabilityProbeAsync` → `probeExecutor.execute` with no
  in-flight check; reference phone returned a report for every request.
* **Fix.** `AtomicBoolean probeInFlight` / `_probe_in_flight`: the second
  concurrent request is answered `busy` (new, documented error code, additive to
  v1) instead of being queued; released in a `finally`.
* **Verification.** `test_a_second_probe_is_refused_as_busy_not_queued`
  (`busy` returned, session stays usable, next probe succeeds),
  `test_android_gates_the_capability_probe`, plus
  `test_protocol_contract.py::test_kotlin_only_returns_documented_error_codes`
  forcing `busy` into `docs/03-protocol.md`.
* **Remaining risk.** A paired PC can still run one probe repeatedly back to
  back; rate-limiting a trusted peer is a Phase 2 concern (the probe window is
  bounded by `RECORD_AUDIO` being visible to the user).

### S9 — The pairing code was written to logs

* **Severity: Low.**
* **Vulnerability.** The six-digit SAS is the value a human compares out of
  band. It was logged on the PC (`log.debug`), on the phone (`addLog`) and by the
  reference phone. The digits are derived from the session key, so knowing them
  does not yield the key — but a code in a log is a code that can be compared
  *for* the user by anyone who reads the log.
* **Evidence.** `session._handshake`: `log.debug("handshake keys derived, SAS=%s", …)`;
  `BridgeServer.onHello`: `addLog(..., "SAS=${r.keys?.sasDigits}")`;
  `phone_sim._authenticate`: `log.warning("… SAS %s", sas)`.
* **Fix.** All three redacted. The digits still reach the phone's pairing
  dialog (`PairingRequest.sas`), which is where a human must see them.
* **Verification.** `test_the_pairing_code_never_reaches_the_log` (captures
  DEBUG and asserts the six digits never appear),
  `test_android_never_puts_the_pairing_code_in_a_log_line` (scans every
  `addLog(...)` call, and asserts the on-screen prompt still formats the code).
* **Remaining risk.** A user who screenshots the prompt leaks it; that is the
  mechanism working as designed.

### S10 — Peer-chosen names went to the terminal, the log and disk unescaped

* **Severity: Low.**
* **Vulnerability.** `name` in HELLO and in the beacon is attacker-chosen text.
  It was logged, printed by the CLI and stored in `peers.json`, so a name could
  carry ANSI escapes (repaint the terminal), CR/LF (forge log entries), NULs, or
  simply be 4 KiB long.
* **Evidence.** `str(msg.get("name"))` at the handshake boundary;
  `PeerInfo(peerName)` on Android; `DiscoveredPhone(name=msg["name"])`.
* **Fix.** One sanitizer at the boundary: `core/sanitize.device_label()`
  (strip ANSI + C0/C1 controls, collapse whitespace, cap at 64) applied to the
  session's `PhoneInfo.name`, the stored pin name and beacon fields; Kotlin
  `BridgeServer.safeLabel()` for `peerName`; phone and simulator log names
  through the same idea.
* **Verification.** `test_device_label_is_safe_for_a_terminal_and_a_log` (9
  cases), `test_a_hostile_phone_name_is_stored_clean`,
  `test_android_sanitises_peer_supplied_names`,
  `test_beacons_with_hostile_fields_are_ignored_or_cleaned`.
* **Remaining risk.** Any *future* place that prints a peer string without going
  through the helper. Two entry points exist today and both are covered.

### S11 — Discovery beacons were parsed without validation

* **Severity: Low.**
* **Vulnerability.** The beacon is unauthenticated UDP from anyone on the LAN.
  Any JSON with the right magic became an entry: arbitrary-length ids and names,
  any port, and a boolean port (`True` is an `int` in Python) were all accepted.
* **Evidence.** `_BeaconProtocol.datagram_received` only checked magic, version
  and `0 < port < 65536`.
* **Fix.** Bound the datagram (512 B), require the id to match the protocol's
  `DEVICE_ID_RE`, reject booleans and non-integers for `port`, and sanitize
  `name`/`method`. The source address — not any claimed host — is still what is
  used to connect.
* **Verification.** `test_beacons_with_hostile_fields_are_ignored_or_cleaned`
  (7 hostile datagrams rejected, a real one accepted and cleaned).
* **Remaining risk.** Beacon spoofing cannot be prevented by validation; a
  spoofer can still appear in `discover`. The handshake pin check is what stops
  it from mattering (§6).

### S12 — Refusing a connection could cost 10 seconds

* **Severity: Low.**
* **Vulnerability.** `WebSocketLink.close()` used the library's default
  `close_timeout` (10 s) waiting for the peer's close frame. A peer that
  provoked a refusal and then went silent made every refusal cost 10 s of the
  user's time.
* **Evidence.** `WebSocketLink.__init__` never passed `close_timeout`;
  `test_flooding_the_handshake_fails_fast` measured 10.0 s before the fix.
* **Fix.** `close_timeout = 1.0`, configurable per link.
* **Verification.** Same test: the same hostile peer now fails in well under a
  second.
* **Remaining risk.** None beyond a slower close against a very slow but honest
  peer.

### S13 — A refusal from the phone was reported as a protocol error

* **Severity: Low.**
* **Vulnerability.** Not an exploit but a security-relevant reporting bug: when
  the phone refused *before* asking to pair (`pair_fail`, e.g. too many pending
  requests), the PC treated it as "unexpected handshake message", hiding the
  reason the user needs to act on.
* **Evidence.** `_expect_control({"auth_ok", "pair_required", "auth_fail"})`.
* **Fix.** `pair_fail` is accepted there and surfaced as `phone refused
  pairing: <reason>` (sanitized, length-capped).
* **Verification.** `test_pending_pairing_requests_are_capped` asserts the reason
  reaches the user.

---

## 4. Controls that were checked and found sound

| Control | Where | Evidence |
| --- | --- | --- |
| Mutual authentication | `_derive()` mixes ECDH(static) and ECDH(ephemeral); only a holder of both private keys derives the keys | `test_crypto.py` vectors, `test_android_conformance.py` |
| Pin enforcement, both directions | `find_peer` + key equality (PC), `PairingStore.verify` (phone) | `test_integration.py`, `test_phase1_reliability.py` |
| Replay protection | 64-bit monotonic counter as AEAD nonce *and* AAD; `ReplayError` on `counter <= last` | `test_crypto.py`, `test_regressions.py` |
| Plaintext refusal | Any non-SECURE frame mid-session is fatal on both sides | `test_e2e_workflows.py` |
| Key separation | HKDF-SHA256, per-direction keys, per-direction counters from zero | `test_crypto.py` |
| Envelope ceiling | 8 MiB, enforced on encode and decode, both sides | `test_framing.py`, S7 |
| Nonce/counter exhaustion | Refuses to seal past `2**63` | `test_crypto.py` |
| Session ceilings | 8 sessions, 4 per device, 20 s setup deadline | `test_phase1_reliability.py`, `test_kotlin_phase1.py` |
| Constant-time comparisons | `hmac.compare_digest` for keys/ids/SAS; XOR loop in `PairingStore` | source + tests |
| Private key file mode | `_write_private` 0600 + atomic replace; config dir now 0700 | `test_cli.py` |
| No shared secret in RPC | `system.*` returns device facts only; probe reports carry no key material | `test_e2e_workflows.py` |
| Android permission surface | No `CAMERA`, no storage, no `READ_*` media permission; runtime permissions requested at point of use | `test_android_completion.py` |

---

## 5. Regression tests added by this pass

`windows/tests/test_security_hardening.py` — **37 tests**, one per finding plus
parametrised coverage:

* identity binding: 4 (incl. 6 malformed ids)
* mid-handshake identity change: 1
* pre-auth budget: 1
* pin store fail-closed / re-pin / key length: 3
* pairing code not logged: 1
* label sanitisation: 10 (9 parametrised + hostile phone name)
* beacon validation: 1
* phone-side behaviour (origin, oversize, busy, expiry, cap): 5
* Android source guards: 5

Total suite: **479 passed** (was 425; 37 from this pass, 14 release-metadata
tests and 3 Kotlin compile guards added later), `ruff` clean, `tools/verify.py` 7/7.

One non-security flake was found and removed while verifying the above: the
self-test's timeout row armed its deadline *after* awaiting the send, so on a
loopback link the reply could occasionally win the race and the row reported a
failure that was not real. It now retries a bounded number of times and fails
only when the deadline never fires — a check that cannot be satisfied by luck in
either direction (`cli/selftest.py::check_timeout`).
Kotlin: unchanged diagnostic baseline for the compiler stub run — one new
`variable expected` line, which is the same missing-SDK cascade the other 31
`StateFlow.value` assignments produce.

---

## 6. Remaining risk (accepted, not fixed)

1. **Trust on first use.** A first connection is trusted after the human compares
   the six digits. `--auto-pair` skips the comparison and prints a warning; it
   exists for automation and is the single biggest accepted risk. There is no
   certificate authority, and no plan for one.
2. **The identity key is stored unencrypted.** PC: `pc_identity.pem`, PKCS#8, no
   passphrase, 0600 (mode bits are advisory on Windows — ACLs are inherited).
   Android: app-private `SharedPreferences`, not the Keystore (EC
   `PURPOSE_AGREE_KEY` is not portable across OEM builds, and a Keystore key
   cannot be backed up). Root/administrator access ⇒ key theft ⇒ impersonation
   of that device.
3. **The LAN is trusted for confidentiality only through the app layer.** The
   listener binds `0.0.0.0`, the beacon advertises the phone, and the WebSocket
   itself is plaintext — the P-256/AES-GCM layer inside it carries the content.
   No TLS, by design (`AndroidManifest.xml` `usesCleartextTraffic`).
4. **Authorization is binary.** Any paired PC may call every method, including
   the capability probe that touches the microphone. There is no per-method
   scope, no revocation other than unpairing, and no audit trail beyond the
   in-memory 200-line log.
5. **A substituted phone can be adopted if the PC's pin list is lost** (see S4).
   The phone's pin list is authoritative for the phone; the PC cannot consult
   it.
6. **The simulator is a test tool**, not a hardened server: it accepts a
   `--pair-policy auto` by construction and its state directory is left
   readable by the user. Never expose it beyond loopback.

---

## 7. What this pass did *not* prove

* The Kotlin fixes are **compile-checked against a stub environment and pinned by
  source assertions**, not executed. `docs/12` §5 is still the checklist that
  closes that gap on a handset, and the security matrix in §2 carries the same
  caveat.
* No fuzzing was run (no AFL/libFuzzer harness exists). The parsers
  (`Json.toMap`, `decode_control`, `decode_envelope`) are size- and
  shape-limited but not fuzzed.
* No review of Android platform behaviour under a compromised OS, rooted device,
  or malicious accessibility service — explicitly out of scope.
