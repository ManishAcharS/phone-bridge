"""Multi-device audio compatibility records.

Phase 2 starts with a question that only a real handset can answer: *will this
phone give us a working, bidirectional, low-latency audio path to a Bluetooth
headset?* The answer differs per device, per ROM build, and per headset, so it
has to be **measured on each phone** and recorded in a comparable form.

This module is the recorder/analyser. It:

* validates the JSON report the Android probe sends (``CapabilityProbe``),
* derives findings and a verdict from measurements only,
* renders one Markdown compatibility report covering every planned device,
  including the ones that have not been measured yet.

Deliberate design rules
-----------------------
1. **Nothing is inferred from the Bluetooth version.** ``bluetooth.version`` is
   recorded for the report table and is never read by ``derive_findings`` or
   ``verdict``. A BT 5.0 handset and a BT 5.4 handset can behave completely
   differently (off-call SCO is a *policy* decision made by the ROM, not a radio
   feature), so the only admissible evidence is a measurement. See
   ``test_bluetooth_version_is_never_used_for_verdicts``.
2. **A declaration is not a capability.** ``isBluetoothScoAvailableOffCall()``
   returning true means "the API says yes"; it does not mean audio flows. When
   the declaration and the measurement disagree, the measurement wins and the
   disagreement is reported as ``SCO_DECLARED_BUT_UNUSABLE`` - that finding is
   the single most useful output of this whole exercise.
3. **Unmeasured is a first-class value.** Every field can be absent. A missing
   measurement renders as ``unmeasured`` in the report and produces verdict
   ``unmeasured``, never a guess.
4. **Simulated data never counts as evidence.** Reports from the reference phone
   implementation are accepted for pipeline testing but get verdict
   ``simulated`` and are marked in the report.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

#: Where a report came from. Only "device" counts as real evidence.
VALID_SOURCES = ("device", "simulator")

#: Status values a probe test may report.
VALID_STATUSES = ("pass", "fail", "skipped", "error")

#: Verdicts, worst-case last.
VERDICTS = (
    "duplex_supported",
    "output_only",
    "no_headset",
    "unsupported",
    "inconclusive",
    "simulated",
    "unmeasured",
)

#: Latency acceptance bands, in milliseconds, per measurement method.
#:
#: Acoustic loopback is a *round trip* through both Bluetooth legs and the air,
#: so it is compared against a larger budget than the buffer estimate, which
#: only covers OS buffering inside the phone. Mixing the two up would make a
#: healthy link look broken.
LATENCY_BANDS = {
    "acoustic_loopback": (150.0, 250.0),  # good <= 150, acceptable <= 250
    "timestamp_estimate": (60.0, 120.0),  # good <= 60, acceptable <= 120
}

#: Planned test devices. Labels are matched case-insensitively against the
#: report's label, model and codename so a record finds its section even if you
#: spelled the label differently.
PLANNED_DEVICES: tuple[dict[str, str], ...] = (
    {
        "key": "redmi-9-power",
        "label": "Redmi 9 Power",
        "role": "oldest device - the legacy-path test",
        "notes": (
            "Shipped Android 10 / MIUI 12 and later received MIUI 14 on Android 12, "
            "so the same model can sit on either side of the SDK 31 API boundary. "
            "Treat the installed build as unknown until the probe reports it."
        ),
        "aliases": "lime, Redmi 9 Power",
        "watch": (
            "Two possibilities, and the probe decides which one this unit is: on "
            "Android 10/11 (SDK 29/30) routing must use the legacy "
            "startBluetoothSco() path; on Android 12 (SDK 31) the same model uses "
            "setCommunicationDevice(). Record the installed build - do not assume "
            "the launch firmware."
        ),
    },
    {
        "key": "redmi-note-9-pro",
        "label": "Redmi Note 9 Pro",
        "role": "middle device - model confirmation pending",
        "notes": (
            "Shipped Android 10 / MIUI 11 and finished on Android 12 / MIUI 13-14. "
            "Confirm the exact model and codename from the probe (curtana/joyeuse "
            "are different SKUs with possibly different ROM builds)."
        ),
        "aliases": "curtana, joyeuse, Redmi Note 9 Pro, Redmi Note 9S",
        "watch": (
            "Confirm the model and codename first: curtana (India) and joyeuse "
            "(global) are different SKUs. Both ended on Android 12 / SDK 31, so the "
            "modern path is likely, but MIUI's off-call SCO policy - not the OS "
            "version - decides whether the microphone works."
        ),
    },
    {
        "key": "redmi-note-14-pro-plus",
        "label": "Redmi Note 14 Pro+ 5G",
        "role": "PRIMARY development device",
        "notes": (
            "Newest device and the one Phase 2 will be built against. Everything "
            "else is measured for comparison and as a fallback."
        ),
        "aliases": "Redmi Note 14 Pro+, Note 14 Pro Plus, amethyst",
        "watch": (
            "Primary device, so this result drives the Phase 2 design. Expect SDK 34+ "
            "and the modern path. The decisive pair is declared-on-call availability "
            "*and* a non-silent microphone: a handset that declares off-call SCO but "
            "delivers silence forces a different design than one that works."
        ),
    },
)

PRIMARY_DEVICE_KEY = "redmi-note-14-pro-plus"


@dataclass(frozen=True)
class Finding:
    """One derived statement about a device, with a severity."""

    code: str
    level: str  # info | warn | critical
    text: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "level": self.level, "text": self.text}


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def validate_report(report: Any) -> list[str]:
    """Return a list of problems; empty means the report is usable."""
    problems: list[str] = []
    if not isinstance(report, dict):
        return ["report is not a JSON object"]

    if report.get("schema_version") != SCHEMA_VERSION:
        problems.append(
            f"schema_version is {report.get('schema_version')!r}, expected {SCHEMA_VERSION}"
        )
    source = report.get("source")
    if source not in VALID_SOURCES:
        problems.append(f"source {source!r} is not one of {VALID_SOURCES}")
    if not isinstance(report.get("device_id"), str) or not report.get("device_id"):
        problems.append("device_id is missing")

    device = _as_dict(report.get("device"))
    if not device:
        problems.append("device block is missing")
    else:
        for key in ("manufacturer", "model", "sdk_int"):
            if key not in device:
                problems.append(f"device.{key} is missing")
        if "sdk_int" in device and not isinstance(device["sdk_int"], int):
            problems.append("device.sdk_int must be an integer")

    bluetooth = _as_dict(report.get("bluetooth"))
    if "supported" in bluetooth and not isinstance(bluetooth["supported"], bool):
        problems.append("bluetooth.supported must be a boolean")

    for block in ("route_output", "route_input", "duplex"):
        section = _as_dict(report.get(block))
        status = section.get("status")
        if status is not None and status not in VALID_STATUSES:
            problems.append(f"{block}.status {status!r} is not one of {VALID_STATUSES}")

    latency = _as_dict(report.get("latency"))
    method = latency.get("method")
    if method not in (None, "none", *LATENCY_BANDS):
        problems.append(f"latency.method {method!r} is unknown")
    value = latency.get("round_trip_ms", latency.get("estimate_ms"))
    if value is not None and not isinstance(value, (int, float)):
        problems.append("latency value must be a number")

    if not isinstance(report.get("tests_run", []), list):
        problems.append("tests_run must be a list")
    return problems


# --------------------------------------------------------------------------- #
# findings
# --------------------------------------------------------------------------- #
def _status(report: dict[str, Any], block: str) -> str | None:
    value = _as_dict(report.get(block)).get("status")
    return value if value in VALID_STATUSES else None


def derive_findings(report: dict[str, Any]) -> list[Finding]:
    """Turn measurements into statements.

    Reads: ``source``, ``device``, ``bluetooth``, ``sco``, ``route_*``,
    ``duplex``, ``latency``, ``interruptions``, ``errors``.

    Never reads: ``bluetooth.version`` (see the module docstring).
    """
    findings: list[Finding] = []
    source = report.get("source")

    if source == "simulator":
        findings.append(
            Finding(
                "SIMULATED",
                "critical",
                "Report came from the reference phone implementation, not a handset. "
                "It exercises the pipeline but says nothing about Android behaviour.",
            )
        )

    device = _as_dict(report.get("device"))
    sdk = device.get("sdk_int")
    if isinstance(sdk, int) and sdk < 31:
        findings.append(
            Finding(
                "LEGACY_SCO_PATH",
                "info",
                f"SDK {sdk} < 31: routing must use startBluetoothSco() + "
                "MODE_IN_COMMUNICATION; setCommunicationDevice() does not exist here.",
            )
        )
    elif isinstance(sdk, int):
        findings.append(
            Finding(
                "MODERN_SCO_PATH",
                "info",
                f"SDK {sdk} >= 31: setCommunicationDevice() is available and is the "
                "supported path; the legacy API is deprecated on this device.",
            )
        )

    bluetooth = _as_dict(report.get("bluetooth"))
    if bluetooth.get("supported") is False:
        findings.append(Finding("NO_BLUETOOTH", "critical", "No Bluetooth adapter present."))
    elif bluetooth.get("enabled") is False:
        findings.append(
            Finding("BT_DISABLED", "warn", "Bluetooth is off; nothing can be routed.")
        )

    sco = _as_dict(report.get("sco"))
    declared = sco.get("declared_offcall_available")
    if declared is False:
        findings.append(
            Finding(
                "SCO_NOT_OFFCALL",
                "warn",
                "Platform reports no off-call SCO (isBluetoothScoAvailableOffCall=false). "
                "Expect the route request to be refused or to produce silence.",
            )
        )

    output_status = _status(report, "route_output")
    input_status = _status(report, "route_input")
    signal = _as_dict(report.get("route_input")).get("signal_detected")

    if output_status == "pass":
        findings.append(Finding("OUTPUT_OK", "info", "Playback routed to the headset."))
    elif output_status in ("fail", "error"):
        findings.append(
            Finding("OUTPUT_FAIL", "critical", "Playback could not be routed to the headset.")
        )

    if input_status == "pass" and signal is True:
        findings.append(
            Finding("INPUT_OK", "info", "Headset microphone captured non-silent audio.")
        )
    elif input_status == "pass" and signal is False:
        findings.append(
            Finding(
                "INPUT_SILENT",
                "critical",
                "The microphone was opened and routed, but every sample was silence. "
                "Routing reported success - the audio path did not work.",
            )
        )
    elif input_status in ("fail", "error"):
        findings.append(
            Finding(
                "INPUT_FAIL",
                "critical",
                "Headset microphone could not be opened/routed; the link is output-only "
                "in practice.",
                )
        )

    if declared is True and input_status in ("fail", "error"):
        findings.append(
            Finding(
                "SCO_DECLARED_BUT_UNUSABLE",
                "critical",
                "The platform declares off-call SCO as available but the microphone did "
                "not work. A declaration is not a capability - this device needs the "
                "fallback path (sequential switching or HFP-only mode).",
            )
        )

    duplex_status = _status(report, "duplex")
    if duplex_status == "pass":
        findings.append(
            Finding("DUPLEX_OK", "info", "Simultaneous playback + capture held for the soak.")
        )
    elif duplex_status in ("fail", "error"):
        findings.append(
            Finding("DUPLEX_FAIL", "critical", "Simultaneous playback + capture did not hold.")
        )

    interruptions = report.get("interruptions") or []
    if isinstance(interruptions, list) and interruptions:
        findings.append(
            Finding(
                "INTERRUPTIONS",
                "warn",
                f"{len(interruptions)} interruption(s) during the soak: "
                + ", ".join(str(_as_dict(i).get("what", "?")) for i in interruptions[:5]),
            )
        )

    errors = report.get("errors") or []
    if isinstance(errors, list):
        for message in errors:
            text = str(message)
            level = "critical" if "SecurityException" in text or "permission" in text.lower() \
                else "warn"
            findings.append(Finding("PROBE_ERROR", level, text))

    findings.extend(_latency_findings(_as_dict(report.get("latency"))))
    return findings


def _latency_findings(latency: dict[str, Any]) -> list[Finding]:
    method = latency.get("method")
    if method not in LATENCY_BANDS:
        return []
    value = latency.get("round_trip_ms" if method == "acoustic_loopback" else "estimate_ms")
    if not isinstance(value, (int, float)):
        return []
    good, acceptable = LATENCY_BANDS[method]
    if value <= good:
        level, label = "info", "within budget"
    elif value <= acceptable:
        level, label = "info", "acceptable but not ideal"
    else:
        level, label = "warn", "above budget"
    return [
        Finding(
            "LATENCY",
            level,
            f"{value:.0f} ms ({method}, {label}; budget {good:.0f}/{acceptable:.0f} ms).",
        )
    ]


# --------------------------------------------------------------------------- #
# verdict
# --------------------------------------------------------------------------- #
def verdict(report: dict[str, Any]) -> str:
    """One word a human can act on.

    Order matters: simulation and missing evidence are checked before
    capability, so an empty report can never masquerade as a passing one.
    """
    if report.get("source") == "simulator":
        return "simulated"

    bluetooth = _as_dict(report.get("bluetooth"))
    input_status = _status(report, "route_input")
    output_status = _status(report, "route_output")
    duplex_status = _status(report, "duplex")
    signal = _as_dict(report.get("route_input")).get("signal_detected")

    # "skipped" is not a measurement: it means the test could not run.
    measured = [
        status
        for status in (output_status, input_status, duplex_status)
        if status in ("pass", "fail", "error")
    ]
    if not measured:
        hfp = bluetooth.get("hfp_connected")
        if isinstance(hfp, list) and not hfp:
            return "no_headset"
        return "unmeasured"

    if not _as_dict(report.get("device")):
        return "inconclusive"
    if bluetooth.get("supported") is False:
        return "unsupported"

    if output_status != "pass" and input_status != "pass":
        return "unsupported"
    if input_status == "pass" and signal is True and duplex_status == "pass":
        return "duplex_supported"
    if output_status == "pass" and input_status != "pass":
        return "output_only"
    if input_status == "pass" and signal is True:
        return "output_only" if output_status != "pass" else "duplex_supported"
    return "inconclusive"


VERDICT_LABELS = {
    "duplex_supported": "duplex audio works",
    "output_only": "output only (no usable headset mic)",
    "no_headset": "no headset attached",
    "unsupported": "audio routing unsupported",
    "inconclusive": "measurements incomplete or contradictory",
    "simulated": "simulated data - not a measurement",
    "unmeasured": "not measured yet",
}


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def compat_dir(config_dir: Path) -> Path:
    path = Path(config_dir) / "compat"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_report(config_dir: Path, report: dict[str, Any]) -> Path:
    problems = validate_report(report)
    if problems:
        raise ValueError("refusing to store an invalid compat report: " + "; ".join(problems))
    device_id = str(report["device_id"])
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in device_id)
    path = compat_dir(config_dir) / f"{safe}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", "utf-8")
    return path


def load_reports(config_dir: Path) -> list[dict[str, Any]]:
    directory = Path(config_dir) / "compat"
    if not directory.is_dir():
        return []
    reports = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        data.setdefault("_file", str(path))
        reports.append(data)
    return reports


# --------------------------------------------------------------------------- #
# report rendering
# --------------------------------------------------------------------------- #
def _label(report: dict[str, Any]) -> str:
    explicit = report.get("label")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    device = _as_dict(report.get("device"))
    model = str(device.get("model") or "").strip()
    if model:
        manufacturer = str(device.get("manufacturer") or "").strip()
        return f"{manufacturer} {model}".strip()
    return str(report.get("device_id") or "unknown device")


def _haystack(report: dict[str, Any]) -> str:
    device = _as_dict(report.get("device"))
    parts = [
        _label(report),
        str(device.get("model") or ""),
        str(device.get("manufacturer") or ""),
        str(device.get("codename") or report.get("codename") or ""),
    ]
    return " ".join(parts).lower()


def match_planned(
    planned: dict[str, str], reports: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    """Find the record for a planned device, tolerating label spelling."""
    aliases = [
        planned["label"].lower(),
        *[a.strip().lower() for a in planned.get("aliases", "").split(",") if a.strip()],
    ]
    for report in reports:
        haystack = _haystack(report)
        if any(alias and alias in haystack for alias in aliases):
            return report
    return None


def _fmt(value: Any, missing: str = "unmeasured") -> str:
    if value is None or value == "":
        return missing
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.0f}" if value.is_integer() else f"{value:.1f}"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "none"
    return str(value)


def _status_cell(report: dict[str, Any] | None, block: str) -> str:
    if report is None:
        return "unmeasured"
    status = _status(report, block)
    if status is None:
        return "unmeasured"
    symbol = {"pass": "PASS", "fail": "FAIL", "error": "ERROR", "skipped": "skipped"}[status]
    if block == "route_input" and status == "pass":
        signal = _as_dict(report.get("route_input")).get("signal_detected")
        if signal is False:
            return "FAIL (silent)"
        if signal is None:
            return "PASS (signal unverified)"
    return symbol


def compare_table(reports: Sequence[dict[str, Any]]) -> str:
    """Compact table for the terminal."""
    header = (
        f"{'device':28s} {'sdk':>4s} {'output':>7s} {'input':>13s} "
        f"{'duplex':>7s} {'latency':>9s}  verdict"
    )
    rows = [header, "-" * len(header)]
    for report in reports:
        device = _as_dict(report.get("device"))
        latency = _as_dict(report.get("latency"))
        value = latency.get("round_trip_ms", latency.get("estimate_ms"))
        rows.append(
            f"{_label(report)[:28]:28s} {str(device.get('sdk_int', '?')):>4s} "
            f"{_status_cell(report, 'route_output'):>7s} "
            f"{_status_cell(report, 'route_input'):>13s} "
            f"{_status_cell(report, 'duplex'):>7s} "
            f"{(f'{value:.0f} ms' if isinstance(value, (int, float)) else '-'):>9s}  "
            f"{verdict(report)}"
        )
    return "\n".join(rows)


def _device_section(
    planned: dict[str, str], report: dict[str, Any] | None, config_hint: str
) -> list[str]:
    lines = [f"### {planned['label']}", ""]
    if planned.get("role"):
        lines.append(f"*{planned['role']}*")
        lines.append("")
    if planned.get("notes"):
        lines.append(f"> {planned['notes']}")
        lines.append("")

    watch = planned.get("watch")
    if watch:
        lines.extend(["**What to watch for on this device**", "", f"{watch}", ""])

    if report is None:
        lines.extend(
            [
                "**Status: not measured yet.**",
                "",
                "Run, with the phone bridged and the headset connected:",
                "",
                "```powershell",
                f'{config_hint} --label "{planned["label"]}"',
                "```",
                "",
                "Then regenerate this report: `phonebridge compat report`.",
                "",
            ]
        )
        return lines

    device = _as_dict(report.get("device"))
    bluetooth = _as_dict(report.get("bluetooth"))
    sco = _as_dict(report.get("sco"))
    latency = _as_dict(report.get("latency"))
    findings = derive_findings(report)
    device_verdict = verdict(report)

    lines.extend(
        [
            f"**Verdict: `{device_verdict}` — {VERDICT_LABELS[device_verdict]}**",
            "",
            "| Identity | Value |",
            "| --- | --- |",
            f"| Device | {_fmt(device.get('manufacturer'))} {_fmt(device.get('model'), '')} |",
            f"| Codename | {_fmt(device.get('codename'))} |",
            f"| Android | {_fmt(device.get('android_release'))} (SDK {_fmt(device.get('sdk_int'))}) |",
            f"| ROM | {_fmt(device.get('rom'))} {_fmt(device.get('rom_version'), '')} |",
            f"| Build | {_fmt(device.get('incremental'))} |",
            f"| Device id | `{report.get('device_id')}` |",
            f"| Captured | {_fmt(report.get('captured_at'))} |",
            f"| Source | {_fmt(report.get('source'))} |",
            "",
            "| Bluetooth / routing | Value |",
            "| --- | --- |",
            f"| Adapter supported / enabled | {_fmt(bluetooth.get('supported'))} / "
            f"{_fmt(bluetooth.get('enabled'))} |",
            f"| Adapter name | {_fmt(bluetooth.get('adapter_name'))} |",
            f"| HFP/SCO connected | {_fmt(bluetooth.get('hfp_connected'))} |",
            f"| A2DP connected | {_fmt(bluetooth.get('a2dp_connected'))} |",
            f"| Bonded devices | {_fmt(bluetooth.get('bonded_count'))} |",
            f"| LE Audio supported | {_fmt(bluetooth.get('le_audio_supported'))} |",
            f"| Recorded BT version | {_fmt(bluetooth.get('version'))} *(never used for the verdict)* |",
            f"| SCO path | {_fmt(sco.get('path'))} |",
            f"| Declared off-call SCO | {_fmt(sco.get('declared_offcall_available'))} |",
            f"| Route request accepted | {_fmt(sco.get('request_accepted'))} |",
            f"| Route observed after | {_fmt(sco.get('route_observed_ms'))} ms |",
            f"| Communication device after | {_fmt(sco.get('communication_device_after'))} |",
            "",
            "| Measurement | Result |",
            "| --- | --- |",
            f"| Playback → headset | {_status_cell(report, 'route_output')} |",
            f"| Microphone ← headset | {_status_cell(report, 'route_input')} |",
            f"| Simultaneous duplex | {_status_cell(report, 'duplex')} |",
            f"| Latency | {_latency_cell(latency)} |",
            f"| Interruptions | {_fmt(report.get('interruptions'), 'none recorded')} |",
            f"| Soak length | {_fmt(_as_dict(report.get('duplex')).get('seconds'), 'n/a')} s |",
            "",
        ]
    )

    if report.get("source") == "simulator":
        lines.extend(
            [
                "> **Simulated record.** Produced by the reference phone implementation, "
                "not by a handset. It proves the tooling works; it is not evidence about "
                "this device. Re-run on the real phone before drawing conclusions.",
                "",
            ]
        )

    if findings:
        lines.extend(["**Findings**", ""])
        for finding in findings:
            lines.append(f"- `{finding.level}` **{finding.code}** — {finding.text}")
        lines.append("")

    notes = report.get("notes")
    if isinstance(notes, str) and notes.strip():
        lines.extend(["**Operator notes**", "", notes.strip(), ""])
    return lines


def _latency_cell(latency: dict[str, Any]) -> str:
    method = latency.get("method")
    if method == "acoustic_loopback" and isinstance(latency.get("round_trip_ms"), (int, float)):
        return f"{latency['round_trip_ms']:.0f} ms round trip (acoustic loopback)"
    if isinstance(latency.get("estimate_ms"), (int, float)):
        return f"{latency['estimate_ms']:.0f} ms buffering estimate (excludes radio)"
    return "unmeasured"


def render_markdown(
    reports: Sequence[dict[str, Any]],
    *,
    planned: Iterable[dict[str, str]] = PLANNED_DEVICES,
    generated_at: datetime | None = None,
    config_hint: str = "phonebridge compat run --host <phone-ip>",
) -> str:
    stamp = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    planned_list = list(planned)
    matched = {p["key"]: match_planned(p, reports) for p in planned_list}

    lines = [
        "# Multi-device audio compatibility report",
        "",
        f"Generated {stamp} by `phonebridge compat report`.",
        "",
        "This report exists to answer one question per handset: **can PhoneBridge carry "
        "audio in both directions through a Bluetooth headset on this device?** "
        "Everything below is either measured on the phone or explicitly marked "
        "`unmeasured` — nothing is inferred from a specification sheet.",
        "",
        "## Evidence rules",
        "",
        "| Term | Meaning |",
        "| --- | --- |",
        "| measured | produced by `CapabilityProbe` on this handset, this session |",
        "| declared | what an Android API *says* (e.g. `isBluetoothScoAvailableOffCall`); "
        "treated as a hint, never as a capability |",
        "| unmeasured | not yet tested here; no conclusion is drawn |",
        "",
        "A declared-but-unusable result is reported explicitly (`SCO_DECLARED_BUT_UNUSABLE`): "
        "it is the failure mode this whole exercise is designed to catch. "
        "The Bluetooth version is recorded for reference and is **never** used to decide "
        "anything, because off-call SCO is a ROM policy, not a radio feature.",
        "",
        "## Summary",
        "",
    ]

    summary_header = (
        "| Device | Android (SDK) | Output | Microphone | Duplex | Latency | Verdict |"
    )
    lines.extend(
        [
            summary_header,
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for planned_device in planned_list:
        report = matched[planned_device["key"]]
        if report is None:
            lines.append(
                f"| {planned_device['label']} | — | unmeasured | unmeasured | unmeasured "
                "| unmeasured | `unmeasured` |"
            )
            continue
        device = _as_dict(report.get("device"))
        lines.append(
            f"| {planned_device['label']} | {_fmt(device.get('android_release'))} "
            f"({_fmt(device.get('sdk_int'))}) | {_status_cell(report, 'route_output')} "
            f"| {_status_cell(report, 'route_input')} | {_status_cell(report, 'duplex')} "
            f"| {_latency_cell(_as_dict(report.get('latency')))} "
            f"| `{verdict(report)}` |"
        )

    extras = [r for r in reports if not any(r is m for m in matched.values())]
    for report in extras:
        device = _as_dict(report.get("device"))
        lines.append(
            f"| {_label(report)} (unplanned) | {_fmt(device.get('android_release'))} "
            f"({_fmt(device.get('sdk_int'))}) | {_status_cell(report, 'route_output')} "
            f"| {_status_cell(report, 'route_input')} | {_status_cell(report, 'duplex')} "
            f"| {_latency_cell(_as_dict(report.get('latency')))} | `{verdict(report)}` |"
        )
    lines.append("")

    primary = next((p for p in planned_list if p["key"] == PRIMARY_DEVICE_KEY), None)
    if primary is not None:
        primary_report = matched.get(primary["key"])
        lines.extend(["## Phase 2 gate", ""])
        if primary_report is None:
            lines.append(
                f"The primary device (**{primary['label']}**) has not been measured. "
                "Phase 2 (AudioRelay) does not start until it is: the design depends on "
                "whether duplex SCO works there, not on what the other two phones do."
            )
        else:
            pv = verdict(primary_report)
            lines.append(
                f"Primary device **{primary['label']}** verdict: `{pv}` — "
                f"{VERDICT_LABELS[pv]}. "
                + (
                    "Duplex is available: AudioRelay can be built as planned."
                    if pv == "duplex_supported"
                    else "Duplex is not available as measured: AudioRelay must be designed "
                    "for output-only or sequential switching, and the UI must say so."
                )
            )
        lines.append("")

    lines.extend(["## Per-device detail", ""])
    for planned_device in planned_list:
        lines.extend(
            _device_section(planned_device, matched[planned_device["key"]], config_hint)
        )

    if extras:
        lines.extend(["## Unplanned devices", ""])
        for report in extras:
            lines.extend(
                _device_section(
                    {
                        "key": "extra",
                        "label": _label(report),
                        "role": "",
                        "notes": "",
                    },
                    report,
                    config_hint,
                )
            )

    lines.extend(
        [
            "## How to run the probe",
            "",
            "On the phone: open PhoneBridge → **Capability probe** → attach the headset → "
            "**Run full probe**. Wait for it to finish (30 s default, up to 5 min for the "
            "soak), then copy the JSON or connect the PC.",
            "",
            "On the PC:",
            "",
            "```powershell",
            'phonebridge compat run --host <phone-ip> --label "Redmi Note 14 Pro+ 5G" --acoustic',
            "phonebridge compat report",
            "```",
            "",
            "`--acoustic` adds the chirp round-trip measurement (needs the earpiece to be "
            "audible to its own microphone — wear the headset or rest the earpiece against "
            "the boom). Without it the report carries a buffering estimate that excludes "
            "the Bluetooth radio and therefore is not comparable between devices.",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"
