# Phase 1 — connection verification procedure

Everything in this document is what **you** run on your own Windows PC with your
own phone. Nothing here has been executed against real hardware by the assistant;
where a number is an estimate it says so.

---

## 0. Preconditions (do these once)

### 0.1 PC side

```powershell
cd phone-bridge\windows
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest -q
phonebridge doctor
```

**Expected:** `96 passed` and a doctor report whose only `[warn]` lines are about
Windows-only packages (`soundcard`, `PySide6`, `pyvirtualcam`) — those belong to
later phases.

> If Windows Firewall prompts for `python.exe`, allow **Private networks only**.
> That prompt is what permits the inbound UDP discovery listener.

### 0.2 Phone side

Build and install (exact steps: `docs/07-device-testing-guide.md` §3–§4),
then in the app:

1. Grant the notification permission when asked.
2. Flip **Bridge** on. You should see *"Service running on port 8765"* and a
   persistent notification.
3. Press **Run protocol self-test**.

**Expected:** *"All 8 checks passed"*, showing ECDH, HKDF, SAS, AES-GCM seal,
AES-GCM open + replay rejection, and framing.

> **Stop here if any self-test check fails.** That means the Kotlin crypto does
> not match the PC's, and no amount of connection troubleshooting will help.
> Send me the failing check names and details.

### 0.3 What "success" looks like

A verified connection means all of these, in order:

| Check | Pass criterion |
| --- | --- |
| TCP reachability | `phonebridge info` prints JSON instead of hanging or refusing |
| Handshake | the SAS prompt appears (first time) or no prompt (already paired) |
| Identity | the printed `device_id` matches the one on the phone's screen |
| RPC | `system.info` returns `protocol_version: 1` |
| Latency | `phonebridge ping` succeeds 20/20 times |
| Throughput | `phonebridge bench` reports zero payload corruption |
| Reconnect | `phonebridge watch` recovers after you break the link |

---

## 1. Wi-Fi (LAN)

Both the PC and the phone are on the same Wi-Fi network.

### Steps

1. Find the phone's IPv4 address: **Settings → Network & internet → Wi-Fi → the
   connected network → IP address**. It looks like `192.168.1.42`.
2. Confirm the PC is on the same subnet:

   ```powershell
   ipconfig | Select-String -Pattern "IPv4"
   ```

   The first three octets should match (`192.168.1.x`).
3. Try discovery first, so you do not have to trust the address you read:

   ```powershell
   phonebridge discover --timeout 10
   ```

4. Connect:

   ```powershell
   phonebridge info --host 192.168.1.42
   ```

### Expected

```
Listening for phone beacons on UDP 47823 for 10.0s ...
  found: Pixel 7 (13000791ea89b7e7) at ws://192.168.1.42:8765 via 192.168.1.42
```

```json
{
  "android_version": "14",
  "capabilities": {
    "audio.bluetooth_sco": false,
    "audio.bluetooth_profile": "none",
    "audio.capture": true,
    "audio.playback": true,
    "bluetooth_scan": false,
    "camera.back": true,
    "camera.front": true,
    "file_transfer": true,
    "protocol_version": 1,
    "sensors": true
  },
  "device_id": "13000791ea89b7e7",
  "model": "Pixel 7",
  "name": "Pixel 7",
  "protocol_version": 1,
  "sdk_int": 34
}
```

`audio.bluetooth_sco` reflects whether a headset is currently connected to the
phone — connect your headphones and run `info` again to watch it flip to `true`.
That flag is the Phase 2 go/no-go signal.

### Measure it

```powershell
phonebridge ping  --host 192.168.1.42 --count 20
phonebridge bench --host 192.168.1.42 --frames 3000 --size 1024 --pipeline 16
```

**Expected on a healthy Wi-Fi link (5 GHz, same room):** median RTT **1–8 ms**,
`bench` **> 5 MB/s**, `frames echoed` equal to `frames`, no
`payload corruption detected`.

**Expected on a congested 2.4 GHz link:** median RTT up to ~30 ms, occasional
spikes. Still usable — audio frames are 20 ms, so jitter this size is absorbed by
the jitter buffer in Phase 2 — but record the numbers, because they decide
whether Phase 2 needs a UDP media path.

---

## 2. USB with ADB forwarding (development path)

This is the most stable link for development. It requires `adb` and a USB cable;
it does **not** require USB debugging to stay enabled forever, but the forward
dies whenever the adb server restarts or the cable is unplugged.

### Steps

1. On the phone: **Settings → About phone → tap Build number 7×** to enable
   developer options, then **Developer options → USB debugging = on**.
2. Plug in the cable, accept the "Allow USB debugging?" prompt on the phone.

   ```powershell
   adb devices
   ```

   **Expected:**
   ```
   List of devices attached
   R58M12ABCDE     device
   ```
   `unauthorized` means the prompt was not accepted; `offline` means re-plug.

3. Create the forward (PC port 8765 → phone port 8765):

   ```powershell
   adb forward tcp:8765 tcp:8765
   adb forward --list
   ```

   **Expected:** `R58M12ABCDE tcp:8765 tcp:8765`

4. Connect over the loopback tunnel:

   ```powershell
   phonebridge info  --host 127.0.0.1
   phonebridge ping  --host 127.0.0.1 --count 20
   phonebridge bench --host 127.0.0.1 --frames 3000 --size 1024
   ```

### Expected

RTT over USB+adb is normally **< 2 ms** and `bench` well above 10 MB/s. This is
the lowest-latency path available and the one to use while developing audio.

### If the port is already taken

`adb forward` fails with `cannot bind listener: cannot bind to 0.0.0.0:8765`.
Something else owns 8765 on the PC. Either free it or pick another pair:

```powershell
adb forward tcp:9765 tcp:8765
phonebridge info --host 127.0.0.1 --port 9765
```

---

## 3. USB tethering (standalone USB, no adb)

This is the answer to "the final solution should aim for standalone
connectivity": the cable becomes a private network interface, so the same
WebSocket path works with no adb and no Wi-Fi at all.

### Steps

1. On the phone: **Settings → Network & internet → Hotspot & tethering → USB
   tethering = on**. (Some OEMs hide this until the cable is plugged in.)
2. On the PC, look for the new adapter:

   ```powershell
   Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -ne '127.0.0.1' }
   ```

   **Expected:** a new entry like `192.168.42.129/24 on 'Ethernet 3'`.
   The phone is at the gateway of that subnet, usually **`192.168.42.129`** when
   the PC is `.x` — take the `NextHop` from:

   ```powershell
   Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Format-Table -AutoSize
   ```

   or simply try the two common addresses: `192.168.42.129` and `192.168.42.1`.

3. Connect:

   ```powershell
   phonebridge info --host 192.168.42.129
   ```

### Expected

Same JSON as §1, with RTT in the **1–3 ms** range.

### Important side effect

USB tethering normally shares the phone's internet with the PC, so Windows may
make the tethering adapter the default route. If your PC suddenly has no
internet, raise the adapter's metric so it stays a secondary link:

```powershell
# find the index of the tethering adapter, then set a high metric
Get-NetIPInterface -AddressFamily IPv4 | Format-Table InterfaceIndex,InterfaceAlias,InterfaceMetric
Set-NetIPInterface -InterfaceIndex <index> -InterfaceMetric 9000   # needs admin
```

PhoneBridge does not need a default route over that adapter — only direct
reachability to the phone's address — so a high metric is the correct setup and
breaks nothing.

---

## 4. Phone hotspot

The PC joins the AP the phone creates. No other network is involved.

### Steps

1. On the phone: **Settings → Network & internet → Hotspot & tethering → Wi-Fi
   hotspot = on**. Note the SSID and password.
2. On the PC, join that SSID.
3. Find the phone's address — it is the **default gateway** of the hotspot
   adapter:

   ```powershell
   Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Format-Table -AutoSize
   ```

   Stock Android uses **`192.168.43.1`**; some OEMs use `192.168.137.1` or
   `172.20.10.1`. Use whatever the gateway shows.
4. Connect:

   ```powershell
   phonebridge discover --timeout 10     # the beacon should appear here
   phonebridge info --host 192.168.43.1
   ```

### Expected

Same JSON as §1. RTT is usually a little higher than a LAN (**3–15 ms**) because
the phone is doing AP work, but it is stable and needs no other infrastructure.

---

## 5. Pairing (first connection only)

On the very first connection to a new phone, both sides display a six-digit code.

**PC:**
```
  ┌─────────────────────────────────────────────────────────┐
  │  PAIRING CODE:  123 456                                 │
  └─────────────────────────────────────────────────────────┘
  The phone shows the same six digits if the link is genuine.
  Codes match? [y/N]
```

**Phone:** a red *Pairing request* card with the same digits and
**"Codes match — allow"** / **"Refuse"** buttons.

Rules:

* The digits **must be identical**. They are derived from the session keys, so a
  man-in-the-middle produces different digits on the two sides.
* Confirm on the PC first, then allow on the phone (the phone requires both).
* If they differ: press **Refuse** on the phone, answer `n` on the PC, and
  investigate the network — do not proceed.

After pairing, both sides pin each other's identity key. Subsequent connections
ask for nothing.

### Expected second-connection behaviour

```
$ phonebridge info --host 192.168.1.42
{ ... }        # no prompt at all
```

---

## 6. Reconnection

```powershell
phonebridge watch --host 192.168.1.42
```

Then break and restore the link — for example turn the phone's Wi-Fi off and on,
or press Stop then Start in the app. Expect:

```
[12:00:01] connected    {'phone': 'Pixel 7', 'device_id': '1300...'}
[12:00:31] reconnecting {'reason': 'link closed: ...'}
[12:01:02] reconnecting {'retry_in_s': 1.53, 'reason': '...'}
[12:01:04] connected    {'phone': 'Pixel 7', ...}
```

Notes:

* Backoff starts at 0.5 s and doubles to a 15 s ceiling, **and resets after every
  successful connection** (a defect fixed in this pass — previously a drop after
  a long session could wait 15 s).
* No pairing prompt should appear again.
* `Ctrl-C` stops cleanly.

---

## 7. Verifying without a phone (regression baseline)

The reference phone implementation exercises the same code paths on one machine:

```powershell
# terminal 1
python tools\simulator\phone_sim.py --port 8765 --pair-policy auto --beacon

# terminal 2
cd windows
.venv\Scripts\activate
python -m pytest -q
phonebridge info  --host 127.0.0.1 --auto-pair
phonebridge ping  --host 127.0.0.1 --count 10
phonebridge bench --host 127.0.0.1 --frames 3000 --size 1024 --expect-ms 50
```

**Measured in the assistant's environment (loopback, not your hardware):**

```
min 0.54  median 0.65  p95 0.72  max 0.78  (ms, application-level RTT)
throughput 29.78 MB/s (238.2 Mbit/s) payload, both directions
```

Use this as the baseline: if a real link is dramatically worse than these numbers
relative to its transport, the problem is the network, not the protocol.

---

## 8. Troubleshooting

| Symptom | Most likely cause | Fix |
| --- | --- | --- |
| `cannot connect to ws://...: [Errno 111] Connection refused` | App not running, service stopped, or wrong port | Open the app, flip **Bridge** on, confirm the port matches (`8765` default) |
| `timed out connecting to ...` | Firewall, wrong IP, or Wi-Fi client isolation | Allow python.exe on Private networks; re-check the IP; try the hotspot instead |
| `discovery` finds nothing | Broadcast blocked, AP isolation, or beacon off | Type the IP manually — discovery is a convenience, never required |
| `phone identity key does not match the pinned key` | The phone app was reinstalled / identity regenerated | `phonebridge identity` to see the pinned id, then `phonebridge forget <old-id>`, then re-pair |
| `phone refused authentication: identity key changed` | The PC's identity key changed (new config dir / new machine) | On the phone press **Forget all paired PCs**, then re-pair |
| SAS codes differ between PC and phone | Someone is intercepting, or state is stale mid-pair | Refuse on both sides, unpair both, check the network before retrying |
| `pairing did not complete` | The phone's screen was not answered within ~90 s | Press **Bridge** off/on and retry; answer within the timeout |
| `error: port already in use` | Two PC-side instances, or a stale server | Close other instances; or change the phone's port in **Settings** and `--port` |
| `bench`: `payload corruption detected` | A real bug | Stop and report it with the command output |
| App notification missing | `POST_NOTIFICATIONS` denied | Grant it in system settings; the bridge still runs |
| Service dies when the screen locks | Aggressive OEM battery optimisation | Allow background activity / disable battery optimisation for PhoneBridge |
| Android 14+ `SecurityException` on start | `connectedDevice` foreground-service prerequisite | Fixed in this pass by declaring `CHANGE_NETWORK_STATE`; confirm with `adb logcat` if you still see it |

### Diagnostics to capture if something fails

```powershell
# phone-side protocol log
adb logcat -s BridgeServer:V DiscoveryBeacon:W

# PC-side debug log
phonebridge -v info --host <ip>

# what the PC has pinned
phonebridge identity

# full Windows environment report
powershell -ExecutionPolicy Bypass -File tools\diagnostics\env_report.ps1
```

Send me: the failing command, its complete output, the self-test result, and the
logcat lines.

---

## 9. What this procedure does *not* verify

Phase 1 ends at "an authenticated, encrypted, ordered channel moves RPC, events
and binary data both ways and survives a drop". It does **not** verify audio,
camera, file transfer or sensors — those are Phases 2–5 and have no
implementation to test yet. In particular, `audio.bluetooth_sco` is only a
*probe* of the platform's current state, not proof that a SCO link can be
established off-call; that is the first thing Phase 2 measures.
