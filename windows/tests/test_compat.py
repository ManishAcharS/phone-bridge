"""Tests for the multi-device compatibility records.

The rules these tests defend, in order of importance:

1. Nothing is inferred from the Bluetooth version. Off-call SCO is a ROM policy,
   not a radio feature, so a BT 5.0 and a BT 5.4 handset can behave differently.
2. A platform *declaration* never becomes a capability on its own. The
   "declared but unusable" case must always be reported, because that is the
   failure this whole measurement exists to catch.
3. Unmeasured stays unmeasured. An empty or partial report can never produce a
   passing verdict.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

from phonebridge.core import compat


# --------------------------------------------------------------------------- #
# fixtures - shaped exactly like the Android probe's output
# --------------------------------------------------------------------------- #
def make_report(**overrides) -> dict:
    report = {
        "schema_version": 1,
        "source": "device",
        "device_id": "aabbccdd11223344",
        "label": "Redmi Note 14 Pro+ 5G",
        "captured_at": "2026-11-08T10:00:00Z",
        "device": {
            "manufacturer": "Xiaomi",
            "model": "Redmi Note 14 Pro+ 5G",
            "codename": "amethyst",
            "android_release": "14",
            "sdk_int": 34,
            "rom": "HyperOS",
            "rom_version": "1.0.4.0",
            "incremental": "OS1.0.4.0",
            "is_emulator": False,
        },
        "bluetooth": {
            "supported": True,
            "enabled": True,
            "adapter_name": "Redmi Note 14 Pro+ 5G",
            "version": None,
            "hfp_connected": ["AA:BB:CC:DD:EE:FF"],
            "a2dp_connected": ["AA:BB:CC:DD:EE:FF"],
            "bonded_count": 2,
            "le_audio_supported": False,
        },
        "sco": {
            "path": "modern",
            "declared_offcall_available": True,
            "request_accepted": True,
            "request_succeeded": True,
            "route_observed_ms": 312,
            "communication_device_after": "bluetooth_sco",
        },
        "route_output": {
            "status": "pass",
            "routed_device": "bluetooth_sco",
            "underruns": 0,
            "track_rate_hz": 16000,
        },
        "route_input": {
            "status": "pass",
            "routed_device": "bluetooth_sco",
            "record_rate_hz": 16000,
            "rms": 1450.0,
            "peak": 9120,
            "signal_detected": True,
        },
        "duplex": {
            "status": "pass",
            "seconds": 30,
            "underruns": 0,
            "read_failures": 0,
            "late_chunks": 0,
        },
        "latency": {"method": "acoustic_loopback", "round_trip_ms": 128.0, "confidence": 5.2},
        "interruptions": [],
        "errors": [],
        "tests_run": [
            "device",
            "bluetooth",
            "sco",
            "route_output",
            "route_input",
            "duplex",
            "latency_acoustic",
        ],
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(report.get(key), dict):
            report[key] = {**report[key], **value}
        else:
            report[key] = value
    return report


@pytest.fixture()
def full_duplex() -> dict:
    return make_report()


@pytest.fixture()
def output_only() -> dict:
    return make_report(
        label="Redmi Note 9 Pro",
        device_id="1122334455667788",
        device={"model": "Redmi Note 9 Pro", "codename": "curtana", "android_release": "12",
                "sdk_int": 31, "rom": "MIUI", "rom_version": "14.0.3.0"},
        route_input={"status": "fail", "routed_device": "builtin_mic", "signal_detected": False},
        duplex={"status": "fail"},
        latency={"method": "timestamp_estimate", "estimate_ms": 48.0},
    )


@pytest.fixture()
def no_headset() -> dict:
    return make_report(
        label="Redmi 9 Power",
        device_id="99aabbccddeeff00",
        device={"model": "Redmi 9 Power", "codename": "lime", "android_release": "11",
                "sdk_int": 30, "rom": "MIUI", "rom_version": "12.0.9.0"},
        bluetooth={"enabled": False, "hfp_connected": [], "a2dp_connected": [],
                   "bonded_count": 1, "le_audio_supported": None},
        sco={"path": "legacy", "declared_offcall_available": False,
             "request_succeeded": False, "request_accepted": False,
             "route_observed_ms": None, "communication_device_after": None},
        route_output={"status": "skipped", "routed_device": None},
        route_input={"status": "skipped", "routed_device": None, "signal_detected": None},
        duplex={"status": "skipped"},
        latency={"method": "none"},
        errors=["no Bluetooth headset attached"],
    )


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def test_full_duplex_report_is_valid():
    assert compat.validate_report(make_report()) == []


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        ({"schema_version": 99}, "schema_version"),
        ({"source": "guess"}, "source"),
        ({"device_id": ""}, "device_id"),
        ({"device": {}}, "device block"),
        ({"route_output": {"status": "maybe"}}, "route_output.status"),
        ({"latency": {"method": "telepathy"}}, "latency.method"),
    ],
)
def test_invalid_reports_are_rejected(mutation, needle):
    report = {**make_report(), **mutation}
    problems = compat.validate_report(report)
    assert problems, "invalid report was accepted"
    assert any(needle in p for p in problems), problems


def test_save_refuses_an_invalid_report(tmp_path):
    with pytest.raises(ValueError):
        compat.save_report(tmp_path, {"schema_version": 1})


# --------------------------------------------------------------------------- #
# verdicts
# --------------------------------------------------------------------------- #
def test_duplex_supported(full_duplex):
    assert compat.verdict(full_duplex) == "duplex_supported"


def test_output_only(output_only):
    assert compat.verdict(output_only) == "output_only"


def test_no_headset(no_headset):
    assert compat.verdict(no_headset) == "no_headset"


def test_unmeasured_when_no_test_ran():
    bare = make_report()
    bare["route_output"] = {}
    bare["route_input"] = {}
    bare["duplex"] = {}
    assert compat.verdict(bare) == "unmeasured"


def test_simulated_is_never_evidence(full_duplex):
    full_duplex["source"] = "simulator"
    assert compat.verdict(full_duplex) == "simulated"
    assert any(f.code == "SIMULATED" for f in compat.derive_findings(full_duplex))


def test_sdk_selects_the_documented_path(full_duplex, no_headset):
    modern = {f.code for f in compat.derive_findings(full_duplex)}
    legacy = {f.code for f in compat.derive_findings(no_headset)}
    assert "MODERN_SCO_PATH" in modern
    assert "LEGACY_SCO_PATH" in legacy


# --------------------------------------------------------------------------- #
# the rules that matter
# --------------------------------------------------------------------------- #
def test_declared_but_unusable_is_reported(full_duplex):
    """The platform says off-call SCO is available; the microphone disagrees."""
    full_duplex["route_input"] = {
        "status": "fail",
        "routed_device": "builtin_mic",
        "signal_detected": False,
    }
    full_duplex["duplex"] = {"status": "fail"}
    codes = {f.code for f in compat.derive_findings(full_duplex)}
    assert "SCO_DECLARED_BUT_UNUSABLE" in codes
    assert "INPUT_FAIL" in codes
    assert compat.verdict(full_duplex) == "output_only"


def test_routed_but_silent_microphone_is_a_failure(full_duplex):
    full_duplex["route_input"] = {
        "status": "pass",
        "routed_device": "bluetooth_sco",
        "signal_detected": False,
        "rms": 0,
        "peak": 0,
    }
    codes = {f.code for f in compat.derive_findings(full_duplex)}
    assert "INPUT_SILENT" in codes
    assert compat.verdict(full_duplex) != "duplex_supported"


def test_declared_false_is_a_warning_not_a_verdict(full_duplex):
    full_duplex["sco"]["declared_offcall_available"] = False
    findings = compat.derive_findings(full_duplex)
    assert any(f.code == "SCO_NOT_OFFCALL" and f.level == "warn" for f in findings)
    # The measurement still decides: declarations do not override evidence.
    assert compat.verdict(full_duplex) == "duplex_supported"


@pytest.mark.parametrize("base", ["full_duplex", "output_only", "no_headset"])
@pytest.mark.parametrize("version", ["4.2", "5.0", "5.2", "5.4", "6.0", None])
def test_bluetooth_version_never_affects_findings_or_verdict(base, version, request):
    """Requirement: never infer SCO availability from the Bluetooth version.

    Run against every profile, not just the happy one: a version-sensitive
    bug would most likely show up by turning a failing device into a passing
    one, which a duplex-only fixture could never catch.
    """
    report = request.getfixturevalue(base)
    report["bluetooth"]["version"] = None
    reference = compat.verdict(report)
    reference_codes = sorted(f.code for f in compat.derive_findings(report))

    mutated = copy.deepcopy(report)
    mutated["bluetooth"]["version"] = version

    assert compat.verdict(mutated) == reference
    assert sorted(f.code for f in compat.derive_findings(mutated)) == reference_codes


def test_interruptions_are_surfaced(full_duplex):
    full_duplex["interruptions"] = [
        {"at_ms": 1200, "what": "sco_device_removed", "detail": "route lost"},
    ]
    findings = compat.derive_findings(full_duplex)
    assert any(f.code == "INTERRUPTIONS" and f.level == "warn" for f in findings)


def test_probe_errors_are_surfaced(full_duplex):
    full_duplex["errors"] = ["RECORD_AUDIO: SecurityException (denied)"]
    findings = compat.derive_findings(full_duplex)
    assert any(f.code == "PROBE_ERROR" and f.level == "critical" for f in findings)


# --------------------------------------------------------------------------- #
# latency bands are method-specific
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "level"),
    [(90.0, "info"), (200.0, "info"), (400.0, "warn")],
)
def test_acoustic_latency_bands(full_duplex, value, level):
    full_duplex["latency"] = {"method": "acoustic_loopback", "round_trip_ms": value}
    finding = next(f for f in compat.derive_findings(full_duplex) if f.code == "LATENCY")
    assert finding.level == level


@pytest.mark.parametrize(
    ("value", "level"),
    [(30.0, "info"), (90.0, "info"), (150.0, "warn")],
)
def test_estimate_latency_bands_are_tighter(full_duplex, value, level):
    full_duplex["latency"] = {"method": "timestamp_estimate", "estimate_ms": value}
    finding = next(f for f in compat.derive_findings(full_duplex) if f.code == "LATENCY")
    assert finding.level == level


# --------------------------------------------------------------------------- #
# storage and matching
# --------------------------------------------------------------------------- #
def test_save_and_load_roundtrip(tmp_path, full_duplex):
    path = compat.save_report(tmp_path, full_duplex)
    assert path.exists()
    loaded = compat.load_reports(tmp_path)
    assert len(loaded) == 1
    assert loaded[0]["device_id"] == full_duplex["device_id"]
    assert compat.verdict(loaded[0]) == "duplex_supported"


def test_corrupt_files_are_skipped_not_fatal(tmp_path, full_duplex):
    compat.save_report(tmp_path, full_duplex)
    (compat.compat_dir(tmp_path) / "broken.json").write_text("{not json", "utf-8")
    assert len(compat.load_reports(tmp_path)) == 1


def test_planned_devices_match_by_alias(full_duplex):
    note14 = next(p for p in compat.PLANNED_DEVICES if p["key"] == compat.PRIMARY_DEVICE_KEY)
    assert compat.match_planned(note14, [full_duplex]) is full_duplex

    renamed = copy.deepcopy(full_duplex)
    renamed["label"] = "note 14 pro plus (work phone)"
    assert compat.match_planned(note14, [renamed]) is renamed


def test_unrelated_device_matches_nothing(full_duplex):
    pixel = make_report(label="Pixel 8", device={"model": "Pixel 8", "codename": "shiba"})
    for planned in compat.PLANNED_DEVICES:
        assert compat.match_planned(planned, [pixel]) is None


# --------------------------------------------------------------------------- #
# report rendering
# --------------------------------------------------------------------------- #
def test_report_covers_all_three_devices_even_with_no_data():
    text = compat.render_markdown([])
    for planned in compat.PLANNED_DEVICES:
        assert planned["label"] in text
    assert text.count("not measured yet") >= len(compat.PLANNED_DEVICES)
    assert "`unmeasured`" in text


def test_report_puts_each_device_in_its_own_section(full_duplex, output_only, no_headset):
    text = compat.render_markdown([full_duplex, output_only, no_headset])
    assert "### Redmi Note 14 Pro+ 5G" in text
    assert "### Redmi Note 9 Pro" in text
    assert "### Redmi 9 Power" in text
    assert text.count("**Verdict:") == 3
    assert "`duplex_supported`" in text
    assert "`output_only`" in text
    assert "`no_headset`" in text


def test_report_marks_the_primary_device_gate(full_duplex):
    text = compat.render_markdown([full_duplex])
    assert "Phase 2 gate" in text
    assert "duplex_supported" in text


def test_report_says_the_gate_is_closed_without_the_primary_device(output_only):
    text = compat.render_markdown([output_only])
    gate = text.split("## Phase 2 gate", 1)[1].split("##", 1)[0]
    assert "has not been measured" in gate


def test_simulated_records_are_loudly_marked(full_duplex):
    full_duplex["source"] = "simulator"
    text = compat.render_markdown([full_duplex])
    assert "**Simulated record.**" in text
    assert "`simulated`" in text


def test_compare_table_lists_every_device(full_duplex, output_only):
    table = compat.compare_table([full_duplex, output_only])
    assert "Redmi Note 14 Pro+ 5G" in table
    assert "Redmi Note 9 Pro" in table
    assert "duplex_supported" in table
    assert "output_only" in table


def test_report_is_stable_markdown(full_duplex):
    """Regenerating with the same records must not churn the file."""
    moment = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    first = compat.render_markdown([full_duplex], generated_at=moment)
    second = compat.render_markdown([full_duplex], generated_at=moment)
    assert first == second
    assert first.endswith("\n")
    assert "2026-11-08 12:00 UTC" in first


# --------------------------------------------------------------------------- #
# end to end through the real RPC surface
# --------------------------------------------------------------------------- #
async def test_probe_rpc_round_trip(sim_state, pc_config):
    """The PC asks the (simulated) phone for a report and stores it."""
    from phone_sim import SimulatedPhone, _identity_for

    from phonebridge.core.device_store import DeviceStore
    from phonebridge.core.session import BridgeSession
    from phonebridge.transport.link import websocket_uri

    identity = _identity_for(sim_state / "phone.pem", "Redmi Note 14 Pro+ 5G")
    phone = SimulatedPhone(
        identity=identity,
        state_file=sim_state / "paired.json",
        host="127.0.0.1",
        port=0,
        pair_policy="auto",
        name="Redmi Note 14 Pro+ 5G",
        model="Redmi Note 14 Pro+ 5G",
        manufacturer="Xiaomi",
        codename="amethyst",
        sdk_int=34,
        probe_profile="duplex",
    )
    await phone.start()
    try:
        store = DeviceStore(pc_config)
        session = BridgeSession(
            websocket_uri("127.0.0.1", phone.port),
            store.load_or_create_identity(),
            store,
            confirm_pairing=lambda _sas: True,
        )
        await session.open()
        try:
            report = await session.call(
                "audio.capabilities.run", {"seconds": 5, "acoustic": True}, timeout=30
            )
            stored = await session.call("audio.capabilities.last", timeout=30)
        finally:
            await session.close()
    finally:
        await phone.stop()

    assert compat.validate_report(report) == []
    assert report["source"] == "simulator"
    assert stored["available"] is True
    assert stored["report"]["device_id"] == report["device_id"]

    report["label"] = "Redmi Note 14 Pro+ 5G"
    path = compat.save_report(pc_config, report)
    assert path.exists()
    assert compat.load_reports(pc_config)[0]["device"]["sdk_int"] == 34


async def test_three_devices_produce_three_sections(sim_state, pc_config):
    """The whole pipeline: three handsets measured, one report."""
    from phone_sim import SimulatedPhone, _identity_for

    from phonebridge.core.device_store import DeviceStore
    from phonebridge.core.session import BridgeSession
    from phonebridge.transport.link import websocket_uri

    profiles = [
        ("Redmi Note 14 Pro+ 5G", "Xiaomi", "amethyst", 34, "duplex"),
        ("Redmi Note 9 Pro", "Xiaomi", "curtana", 31, "output_only"),
        ("Redmi 9 Power", "Xiaomi", "lime", 30, "no_headset"),
    ]
    store = DeviceStore(pc_config)
    for index, (name, manufacturer, codename, sdk, profile) in enumerate(profiles):
        identity = _identity_for(sim_state / f"phone{index}.pem", name)
        phone = SimulatedPhone(
            identity=identity,
            state_file=sim_state / f"paired{index}.json",
            host="127.0.0.1",
            port=0,
            pair_policy="auto",
            name=name,
            model=name,
            manufacturer=manufacturer,
            codename=codename,
            sdk_int=sdk,
            android_release={34: "14", 31: "12", 30: "11"}[sdk],
            probe_profile=profile,
        )
        await phone.start()
        try:
            session = BridgeSession(
                websocket_uri("127.0.0.1", phone.port),
                store.load_or_create_identity(),
                store,
                confirm_pairing=lambda _sas: True,
            )
            await session.open()
            try:
                report = await session.call("audio.capabilities.run", {"seconds": 5}, timeout=30)
            finally:
                await session.close()
        finally:
            await phone.stop()
        report["label"] = name
        compat.save_report(pc_config, report)

    records = compat.load_reports(pc_config)
    assert len(records) == 3
    text = compat.render_markdown(records)
    assert "### Redmi Note 14 Pro+ 5G" in text
    assert "### Redmi Note 9 Pro" in text
    assert "### Redmi 9 Power" in text
    # Every record came from the simulator, so nothing may claim to be a
    # measurement of a real handset.
    assert text.count("**Simulated record.**") == 3

    verdicts = {compat._label(r): compat.verdict(r) for r in records}
    assert verdicts["Redmi Note 14 Pro+ 5G"] == "simulated"
    assert "LEGACY_SCO_PATH" in {
        f.code for r in records if r["device"]["sdk_int"] == 30
        for f in compat.derive_findings(r)
    }


def test_report_file_is_valid_json_per_record(tmp_path, full_duplex):
    path = compat.save_report(tmp_path, full_duplex)
    data = json.loads(path.read_text("utf-8"))
    assert data["device"]["model"] == "Redmi Note 14 Pro+ 5G"
