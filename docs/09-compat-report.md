# Multi-device audio compatibility report

Generated 2026-11-08 16:42 UTC by `phonebridge compat report`.

This report exists to answer one question per handset: **can PhoneBridge carry audio in both directions through a Bluetooth headset on this device?** Everything below is either measured on the phone or explicitly marked `unmeasured` — nothing is inferred from a specification sheet.

## Evidence rules

| Term | Meaning |
| --- | --- |
| measured | produced by `CapabilityProbe` on this handset, this session |
| declared | what an Android API *says* (e.g. `isBluetoothScoAvailableOffCall`); treated as a hint, never as a capability |
| unmeasured | not yet tested here; no conclusion is drawn |

A declared-but-unusable result is reported explicitly (`SCO_DECLARED_BUT_UNUSABLE`): it is the failure mode this whole exercise is designed to catch. The Bluetooth version is recorded for reference and is **never** used to decide anything, because off-call SCO is a ROM policy, not a radio feature.

## Summary

| Device | Android (SDK) | Output | Microphone | Duplex | Latency | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| Redmi 9 Power | — | unmeasured | unmeasured | unmeasured | unmeasured | `unmeasured` |
| Redmi Note 9 Pro | — | unmeasured | unmeasured | unmeasured | unmeasured | `unmeasured` |
| Redmi Note 14 Pro+ 5G | — | unmeasured | unmeasured | unmeasured | unmeasured | `unmeasured` |

## Phase 2 gate

The primary device (**Redmi Note 14 Pro+ 5G**) has not been measured. Phase 2 (AudioRelay) does not start until it is: the design depends on whether duplex SCO works there, not on what the other two phones do.

## Per-device detail

### Redmi 9 Power

*oldest device - the legacy-path test*

> Shipped Android 10 / MIUI 12 and later received MIUI 14 on Android 12, so the same model can sit on either side of the SDK 31 API boundary. Treat the installed build as unknown until the probe reports it.

**What to watch for on this device**

Two possibilities, and the probe decides which one this unit is: on Android 10/11 (SDK 29/30) routing must use the legacy startBluetoothSco() path; on Android 12 (SDK 31) the same model uses setCommunicationDevice(). Record the installed build - do not assume the launch firmware.

**Status: not measured yet.**

Run, with the phone bridged and the headset connected:

```powershell
phonebridge compat run --host <phone-ip> --label "Redmi 9 Power"
```

Then regenerate this report: `phonebridge compat report`.

### Redmi Note 9 Pro

*middle device - model confirmation pending*

> Shipped Android 10 / MIUI 11 and finished on Android 12 / MIUI 13-14. Confirm the exact model and codename from the probe (curtana/joyeuse are different SKUs with possibly different ROM builds).

**What to watch for on this device**

Confirm the model and codename first: curtana (India) and joyeuse (global) are different SKUs. Both ended on Android 12 / SDK 31, so the modern path is likely, but MIUI's off-call SCO policy - not the OS version - decides whether the microphone works.

**Status: not measured yet.**

Run, with the phone bridged and the headset connected:

```powershell
phonebridge compat run --host <phone-ip> --label "Redmi Note 9 Pro"
```

Then regenerate this report: `phonebridge compat report`.

### Redmi Note 14 Pro+ 5G

*PRIMARY development device*

> Newest device and the one Phase 2 will be built against. Everything else is measured for comparison and as a fallback.

**What to watch for on this device**

Primary device, so this result drives the Phase 2 design. Expect SDK 34+ and the modern path. The decisive pair is declared-on-call availability *and* a non-silent microphone: a handset that declares off-call SCO but delivers silence forces a different design than one that works.

**Status: not measured yet.**

Run, with the phone bridged and the headset connected:

```powershell
phonebridge compat run --host <phone-ip> --label "Redmi Note 14 Pro+ 5G"
```

Then regenerate this report: `phonebridge compat report`.

## How to run the probe

On the phone: open PhoneBridge → **Capability probe** → attach the headset → **Run full probe**. Wait for it to finish (30 s default, up to 5 min for the soak), then copy the JSON or connect the PC.

On the PC:

```powershell
phonebridge compat run --host <phone-ip> --label "Redmi Note 14 Pro+ 5G" --acoustic
phonebridge compat report
```

`--acoustic` adds the chirp round-trip measurement (needs the earpiece to be audible to its own microphone — wear the headset or rest the earpiece against the boom). Without it the report carries a buffering estimate that excludes the Bluetooth radio and therefore is not comparable between devices.
