# 23 — File transfer, both directions (Phase 5)

Files between the PC and the phone, chunked through the session that already
exists, hashed end to end, and reported complete only after the receiver's
SHA-256 matched. The PC never browses the phone's storage: the phone's owner
picks the files and the destination with the system file picker (SAF), and that
grant is the only access the app ever has.

Status: **implemented on both sides; exercised end-to-end against the reference
phone (`tools/simulator`) in software; not yet run on a real handset** (see §11).
Pull request: [#12](https://github.com/ManishAcharS/phone-bridge/pull/12), based
on `main` `99d9ac7`.

---

## 1. What this phase adds

| Piece | Where | What it does |
| --- | --- | --- |
| Wire format | `windows/phonebridge/files/frames.py` | 14-byte header, flags, path-safe names, `part_path`, SHA-256 helper |
| PC transfer engine | `windows/phonebridge/files/transfer.py` | `TransferManager`: send, receive, ack window, pause/resume/cancel, `.part` + sidecar resume, atomic rename after verify |
| Phone transfer engine | `android/.../net/FileTransferEngine.kt` | SAF-backed receive (`createDocument`), SAF-backed send (streamed from a picked URI), ack-resume metadata, rename after verify |
| SAF bridge | `android/.../net/SafStore.kt` | the only thing that reads a folder or file URI the user granted |
| RPCs | `BridgeServer.dispatchRpc`, `docs/03` §6 | `file.offer`, `file.request`, `file.complete`, `file.progress`, `file.cancel`, `file.pause`, `file.resume` |
| Streams | `MSG_FILE` on 5 (PC→phone) and 6 (phone→PC) | data one way, acks/cancel/pause the other |
| PC GUI | `controller/ui/planes.py` | **Files** tab: send/receive, destination picker, progress table (bytes, speed, state, integrity), pause/resume/cancel, history |
| PC manager | `controller/manager.py` | one `TransferManager` per session, closed with it |
| Capabilities | `DeviceFacts.kt`, `controller/capabilities.py` | `file_transfer`, plus `permitted.file_transfer` and `detail.file_transfer` (destination chosen?) |
| CLI | `phonebridge/cli/planes.py` | `phonebridge files send FILE…` and `files receive --into DIR` |
| Self-test | `cli/selftest.py` | "file PC→phone" and "file phone→PC", each verifying the hash |

---

## 2. Architecture

```
  PC (Windows)                                        phone (Android)
  ┌────────────────────────────┐                     ┌────────────────────────────┐
  │ TransferManager            │  MSG_FILE stream 5  │ FileTransferEngine         │
  │  chunk = 64 KiB            │ ────── chunks ────► │  .part in the SAF folder   │
  │  window = 8 unacked        │ ◄───── acks ─────── │  ack every 4 chunks        │
  │  ack every 4 chunks        │  MSG_FILE stream 6  │  SHA-256 while writing     │
  │  SHA-256 while sending     │ ◄───── chunks ───── │  rename after verify       │
  │  .part + .json sidecar     │ ────── acks ──────► │  resume metadata           │
  └─────────────┬──────────────┘                     └──────────────┬─────────────┘
                │  file.* RPCs over the authenticated session        │
                └────────────────────────────────────────────────────┘
                              GUI Files tab (Tk)
```

Four properties are structural:

1. **One transport.** `MSG_FILE` rides the existing AES-GCM session. There is no
   second socket, no HTTP upload and no unencrypted fallback.
2. **Nothing whole in memory.** Both engines stream: one chunk buffer plus the
   ack window, never the file. A 4 GiB file costs the same memory as a 4 KiB one.
3. **Never "done" before the hash.** A transfer is `complete` only when the
   receiver computed SHA-256 over what it wrote and it matched what the sender
   declared. A failure deletes the `.part`; the destination file is never
   half-written over an existing one.
4. **The user grants storage.** The phone writes only inside a folder chosen with
   `ACTION_OPEN_DOCUMENT_TREE`, and sends only files chosen with
   `ACTION_OPEN_DOCUMENT`. The PC writes only into a folder the user picked. No
   `MANAGE_EXTERNAL_STORAGE`, no `READ_EXTERNAL_STORAGE`, no background browsing.

---

## 3. Protocol

Normative reference: `docs/03-protocol.md` §6 (`MSG_FILE body`). Additive only.

**RPCs**

| method | params | result |
| --- | --- | --- |
| `file.offer` | `name`, `size`, `sha256`, `chunk_size` | `{transfer_id, chunk_size, window, resume_from}` |
| `file.request` | `name` (optional hint) | `{transfer_id, name, size, sha256, chunk_size}` |
| `file.complete` | `transfer_id` | `{ok, verified, sha256, bytes, path_hint}` |
| `file.progress` | `transfer_id` | `{transfer_id, state, bytes, chunks, sha256, error}` |
| `file.cancel` | `transfer_id` | `{ok, state:"cancelled"}` |
| `file.pause` | `transfer_id` | `{ok, state:"paused"}` |
| `file.resume` | `transfer_id`, `from_bytes` | `{ok, from_bytes, state:"active"}` |

**Frame** (`MSG_FILE`):

```
offset  size  field
0       4     transfer_id   from file.offer / file.request
4       4     chunk_index   0-based, contiguous
8       4     chunk_len     payload bytes (0 for control frames)
12      1     flags         0x01 START, 0x02 END, 0x04 ACK, 0x08 CANCEL,
                           0x10 PAUSE, 0x20 RESUME
13      1     reserved      must be zero
14      n     payload       at most 256 KiB; 64 KiB by default
```

The length is 32-bit on purpose: a full 64 KiB chunk is 65 536 bytes, which does
not fit a `u16`. That is the kind of off-by-one that only appears on the last
chunk of a large file, so the width was chosen rather than clamped.

**Flow, PC → phone**

1. `file.offer` with name, size and the SHA-256 the PC computed by reading the
   file once (off the event loop: a 4 GB read must not freeze heartbeats). The
   phone answers `transfer_id`, the chunk size it accepts, the window and
   `resume_from` — the whole chunks it still holds in its `.part` document.
2. The PC validates `resume_from`: it must be a multiple of the *negotiated*
   chunk size and strictly inside the file, or the PC starts from zero and says
   why in the log. A phone that answers nonsense is not trusted with a hole.
3. The PC seeks to `resume_from` and streams `FLAG_START`…`FLAG_END` chunks,
   keeping at most 8 unacknowledged.
4. The phone acks every chunk it accepts. It also acks a chunk it already holds
   (a restart, a retransmission after a lost ack) and writes nothing, because a
   silent skip is what used to stall a bounded window for 15 s and then fail it.
5. If the window stops advancing, the PC re-sends the newest unacknowledged
   chunk instead of only waiting again — a lost ack is the common cause, not a
   dead link. After 3 retries it gives up with a sentence naming the last acked
   chunk.
6. `file.complete` → the phone hashes the `.part`, renames it to the real name on
   a match, and answers `{verified: true}`. A mismatch (or a file the wrong size)
   answers `{verified: false}`, deletes the `.part` and its metadata, and the PC
   reports a failure with a sentence. Nothing is "complete" before that check.

**Flow, phone → PC**

1. `file.request` (optional name hint). The phone's owner has already picked the
   file; the phone hashes it and answers name, size, SHA-256 and chunk size.
2. Chunks arrive on stream 6; the PC writes to `<name>.part` and acks on stream 5.
3. On END the PC re-hashes the `.part` (a resume re-hashes from disk — a partial
   digest cannot be trusted across a session), checks the length, renames it, and
   calls `file.complete` so the phone can record that the PC verified it.

**Resume, phone → PC.** The PC keeps `<name>.part.json` next to the part file
with the name, size, expected digest and the chunk size it was written with. On
the next `file.request`, if all four agree with what the phone now offers, the PC
truncates the part to the last whole chunk, re-hashes that prefix and sends
`file.resume` with `from_bytes`. The phone replies with the offset it will
actually start at — normally the one asked for, but it may answer differently,
and the PC believes the reply rather than its own arithmetic. If the reply is
zero (an older phone) the PC truncates the part and starts over; the duplicate
chunks that follow are acked, not appended.

**What survives a failure.** An interrupted transfer (dropped link, closed
window, short stream) keeps the `.part` and its sidecar: those bytes are a valid
prefix and are exactly what a resume needs. A hash mismatch or an oversize file
deletes both, because those bytes can never be completed. Cancellation is
explicit, so it deletes them too. A verified file leaves neither behind.

**Error codes.** These are the codes the phone actually returns; the table in
`docs/03-protocol.md` is the normative list and `tests/test_protocol_contract.py`
checks the two agree.

| code | when | what the user sees |
| --- | --- | --- |
| `no_file` | the phone's picker returned nothing, or the chosen document could not be opened | "the phone did not send a file" plus the phone's sentence |
| `storage_denied` | no SAF folder has been granted, or the write failed | "choose a folder on the phone" |
| `busy` | a transfer is already running (`file.offer`/`file.request` while one is active) | the running transfer's id; retry later |
| `bad_params` | name unsafe, size outside 1 B…4 GiB, SHA-256 not 64 hex, chunk size or `from_bytes` outside the documented range or not a chunk multiple, missing/unknown `transfer_id` | the specific rule that was broken |
| `unknown_transfer` | a `file.*` call named a transfer this phone does not have | "this phone has no transfer N" |
| `hash_mismatch` | `file.complete` was called for a transfer that did not verify | the state and the reason |
| `cancelled` | the user cancelled on the phone | "cancelled on the phone" |

The PC adds two failures that never reach the wire: "not connected: a file needs
a live session" and "choose a destination folder before receiving a file".

---

## 4. Path safety

A remote peer names a local file, so the name is a security boundary, not a
formatting detail. Both sides apply the same rules and **reject** a bad name
instead of repairing it (a repaired name is how `..\..\Windows\System32` becomes
something plausible):

* no path separators (`/`, `\`), no drive or stream separator (`:`);
* not `.` or `..`, no leading/trailing spaces, no trailing dot;
* no control characters (`0x00`–`0x1f`, `0x7f`);
* not a Windows reserved device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`,
  `LPT1-9`), case-insensitively, including `NUL.txt`;
* at most 200 characters.

On the PC, `safe_destination()` resolves the target and checks that its parent is
exactly the chosen folder, so a name that survived the checks cannot still escape
via a symlink or `..`. On the phone, `SafStore.createDocument` creates the
document inside the granted tree URI; nothing outside that tree is reachable with
the granted permission even if a name were crafted.

Refusal is visible: the GUI shows the phone's sentence, the CLI exits non-zero
and says which rule was broken.

---

## 5. Permissions and storage access

| Side | What is granted | How | What the app can then do |
| --- | --- | --- | --- |
| Phone, receive | a folder URI | `ACTION_OPEN_DOCUMENT_TREE`, persisted | create documents **inside that folder only** |
| Phone, send | one or more file URIs | `ACTION_OPEN_DOCUMENT` (multi-select, ≤16) | read exactly those files |
| PC, receive | a directory path | the GUI's "Save incoming to…" (or `--into` on the CLI) | write inside that directory |

No storage permission is declared in the manifest —
`tests/test_android_build.py` pins that: `MANAGE_EXTERNAL_STORAGE`,
`READ/WRITE_EXTERNAL_STORAGE` and `QUERY_ALL_PACKAGES` must be absent. `file.offer`
without a destination answers `storage_denied` with "choose a folder on the phone
first"; `file.request` with nothing picked, or with a document that will not open,
answers `no_file` with the same instruction. A revoked grant produces
`the phone could not open <name> (the folder permission may have been revoked)`.

---

## 6. Configuration

| Setting | Default | Allowed |
| --- | --- | --- |
| Chunk size | 64 KiB | 1–256 KiB (`file.offer` negotiates; the phone's answer wins) |
| Unacked window | 8 chunks (512 KiB) | fixed by the protocol |
| Ack frequency | every 4 chunks | fixed |
| Max file size | 4 GiB | `bad_params` beyond it (the size is a parameter, not a special case) |
| Resume | on, when name+size+digest match | always safe: the `.part` is re-hashed |
| Retries | 3 re-sends of the unacked chunk, then failure | bounded, never an infinite loop |
| Timeouts | 15 s per ack, 120 s for `file.request` | a timeout means the window is stuck, not that the link is gone |

Rough throughput at the defaults: 64 KiB chunks with a 4-chunk ack cadence is
comfortable over USB and LAN Wi-Fi (~5–20 MB/s on USB, 2–8 MB/s on 5 GHz). On a
weak link the window applies backpressure instead of buffering, so the transfer
slows rather than the process growing.

---

## 7. Windows side

* **`files/frames.py`** — `FileChunk` encode/decode with strict validation
  (header length, chunk index, reserved byte, payload cap), `safe_name`,
  `safe_destination`, `part_path`, `sha256_of`.
* **`files/transfer.py`** — `TransferManager`:
  * `send_file(path)` — offer, stream with the ack window, verify, record;
  * `request_file(hint)` — subscribe *before* asking (the phone streams
    immediately, and a chunk that arrives before the pump exists would be lost;
    a bounded orphan buffer covers the remaining race);
  * `pause/resume/cancel(transfer_id)` — protocol frames plus local state;
  * `transfers`, `history()`, `get(id)`, `snapshot()` for the GUI;
  * `set_destination(dir)` — where incoming files land.
* **GUI (Files tab)** — Send file… (multi-select), Receive… (optional name hint),
  Save incoming to…, and a table with file, direction, progress, bytes
  (`done / total`), speed, state and **integrity** ("hash verified",
  "hash NOT confirmed", the failure reason, or "pending"). Pause / Resume / Cancel
  act on the selected row. History is kept for the session.
* **Nothing blocks the window.** Every operation is submitted to the runtime loop;
  the table is redrawn once a second from `files_status()`, which only reads
  manager state.
* **CLI** — `phonebridge files send a.png b.bin` prints a live progress line and
  the final integrity verdict; `phonebridge files receive --into Downloads` waits
  for completion and reports the hash. Both exit non-zero on a failed transfer.

---

## 8. Capabilities

| fact | source | meaning |
| --- | --- | --- |
| `implemented.file_transfer` | this build | the feature exists |
| `permitted.file_transfer` | `SafStore.canReceive` (phone) | a destination folder has been granted |
| `available` | implemented **and** permitted | a transfer could start now |
| `active` | a transfer in flight | bytes are moving now |
| `detail.file_transfer` | `SafStore.destinationLabel` | which folder, or why none |

A phone with the feature and no granted folder reports *unavailable* with
"choose a folder on the phone first" — not a broken feature, and not a silent
failure at the first chunk.

---

## 9. Tests

| Area | Where | What is covered |
| --- | --- | --- |
| Frames and safety | `windows/tests/test_files.py` | header round-trip, every name rule (traversal, separators, control chars, reserved names, length, trailing dot), `safe_destination` escaping the root, chunk caps |
| Transfer | `windows/tests/test_files.py` | small/large/empty file, chunk boundaries (exactly one chunk, one byte over), interrupt and resume, cancel, retry, wrong hash → failure and no target file, corrupted chunk, storage denial, atomic rename |
| End to end | `windows/tests/test_phase5_e2e.py` | PC→phone and phone→PC against the simulator with real AES-GCM; hash verified on both sides |
| Interruption and resume | `windows/tests/test_phase51_transfer_resume.py` | a dropped link mid-transfer in each direction, resumed over a second session: chunk-aligned offset, no duplicated or skipped bytes, digest at the end, `.part` + sidecar gone only after verification; a misaligned/out-of-range `file.resume` refused; a lost ack retried; a phone that cannot resume; a digest mismatch; a short stream that keeps its part; cancel that deletes it |
| Contract | `windows/tests/test_protocol_contract.py` | the seven methods and their result keys exist in Kotlin |
| Self-test | `cli/selftest.py` | "file PC→phone" and "file phone→PC" rows with hash evidence |
| GUI text | `windows/tests/test_controller_planeview.py` | integrity column never claims success before verification; progress/bytes/speed/state formatting |
| Android | — | **not compiled** (§11) |

---

## 10. Troubleshooting

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| `storage_denied` | no folder granted on the phone | press **Save to folder** in the Files tab on the phone |
| `no_file` | no file picked for sending, or it would not open | pick a file with **Send a file** on the phone |
| transfer stalls at 0% | the phone is waiting for the user, or the session dropped | check the phone screen; check the status line |
| "hash NOT confirmed" | the phone answered without verifying, or an old build | upgrade the phone app; do not trust the file |
| `hash mismatch: the phone said …, the file is …` | the file changed, or the link corrupted data | retry; the part is discarded, so the next attempt starts clean |
| resume starts from 0 | the name, size or digest changed | expected: a different file is a different transfer |
| `bad_params` (name) | the name has a separator, `..`, a reserved name, or is too long | the phone chooses names from its own picker; a hand-built client should send a bare file name |
| `bad_params` (size) | over 4 GiB, or a resumed offset that is not a chunk multiple | split the file; resume offsets are always whole chunks |
| speed far below the link | many small acks on a high-latency path | expected; the ack cadence trades throughput for bounded buffering |

---

## 11. Manual hardware checklist (not yet done)

No phone, no SDK and no physical transfer has been performed in this
environment.

1. Install the debug APK. In the Files tab press **Save to folder** and choose
   `Downloads`; confirm the app reports the folder name and no storage permission
   was requested.
2. Send a 10 MiB file from the PC: progress moves, integrity says *hash
   verified*, and `phonebridge files send` prints the same verdict.
3. Send a 1 GiB file: memory use on both ends stays flat (one chunk + window).
4. Send several files at once; each gets its own row and its own transfer id.
5. Interrupt a large transfer (turn Wi-Fi off mid-transfer, or press Pause), then
   resume: the phone continues from a chunk boundary and the final hash matches.
6. Cancel mid-transfer: no file appears in the folder, and no `.part` is left
   behind.
7. Receive a file from the phone: pick it on the phone, confirm it lands in the
   chosen PC folder and verifies.
8. Revoke the folder permission in Android settings, then try to receive: the
   error says the grant is gone rather than writing somewhere unexpected.
9. Try a file name the protocol forbids (build one by hand or rename on the phone
   to `..\\x.txt`): the transfer is refused with a sentence.
10. Transfer a 0-byte file and a file of exactly 65 536 bytes: both must verify
    (the two chunk-boundary cases that break naive implementations).
