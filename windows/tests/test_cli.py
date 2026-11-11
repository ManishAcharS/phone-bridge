"""Tests for the ``phonebridge`` command line.

Two kinds of test live here:

* everything that can be checked without a phone (argument validation, target
  resolution, saved devices, exit codes, the pairing prompt) uses a temporary
  config directory and nothing else;
* everything that needs a live link talks to the reference phone implementation
  (`tools/simulator/phone_sim.py`) running in a background thread with its own
  event loop, because each CLI command creates its own loop via ``asyncio.run``.

The CLI is called in-process through ``main([...])``, so the tests assert the
exit code and the two output streams exactly as a script would see them.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import threading
from argparse import Namespace
from dataclasses import dataclass

import pytest
from phone_sim import SimulatedPhone, _identity_for

from phonebridge.cli import format as fmt
from phonebridge.cli.main import main
from phonebridge.cli.target import CliError, pairing_prompt, resolve_target
from phonebridge.core.device_store import DeviceStore


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
@dataclass
class RunningPhone:
    phone: SimulatedPhone
    loop: asyncio.AbstractEventLoop
    thread: threading.Thread
    stop_event: asyncio.Event

    @property
    def port(self) -> int:
        return self.phone.port


def _serve(phone: SimulatedPhone) -> tuple[asyncio.AbstractEventLoop, threading.Thread, asyncio.Event]:
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    box: dict[str, asyncio.Event] = {}

    async def run() -> None:
        await phone.start()
        box["stop"] = asyncio.Event()
        ready.set()
        try:
            await box["stop"].wait()
        finally:
            await phone.stop()

    def target() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run())

    thread = threading.Thread(target=target, name="phone-sim", daemon=True)
    thread.start()
    if not ready.wait(20):
        raise RuntimeError("the simulator did not start")
    return loop, thread, box["stop"]


def _shutdown(running: RunningPhone) -> None:
    running.loop.call_soon_threadsafe(running.stop_event.set)
    running.thread.join(20)
    if not running.loop.is_closed():
        running.loop.close()


def make_phone(state_dir, name: str = "Pixel-cli", **kwargs) -> RunningPhone:
    """Construct and serve a reference phone in a background event loop."""
    ident = _identity_for(state_dir / "phone_identity.pem", name)
    phone = SimulatedPhone(
        identity=ident,
        state_file=state_dir / "paired.json",
        host="127.0.0.1",
        port=0,
        pair_policy="auto",
        name=name,
        **kwargs,
    )
    loop, thread, stop_event = _serve(phone)
    return RunningPhone(phone=phone, loop=loop, thread=thread, stop_event=stop_event)


@pytest.fixture()
def running_phone(sim_state):
    running = make_phone(sim_state)
    try:
        yield running
    finally:
        _shutdown(running)


def cli(pc_config, *argv: str) -> list[str]:
    return ["--config-dir", str(pc_config), *argv]


def target_args(running: RunningPhone) -> list[str]:
    return ["--host", "127.0.0.1", "--port", str(running.port), "--auto-pair"]


def store_of(pc_config) -> DeviceStore:
    return DeviceStore(pc_config)


# --------------------------------------------------------------------------- #
# installation / version / help
# --------------------------------------------------------------------------- #
def test_version_reports_the_protocol_it_speaks(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "phonebridge" in out
    assert "protocol v1" in out


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for command in (
        "doctor", "identity", "devices", "discover", "info", "capabilities",
        "audio", "ping", "bench", "watch", "forget", "compat",
    ):
        assert command in out
    assert "Exit codes" in out


def test_doctor_json_describes_the_installation(pc_config, capsys):
    assert main(cli(pc_config, "doctor", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["protocol_version"] == 1
    assert payload["python"]
    modules = {entry["module"]: entry for entry in payload["packages"]}
    assert modules["websockets"]["version"] is not None
    assert modules["cryptography"]["version"] is not None
    assert payload["config_dir"] == str(pc_config)


def test_doctor_text_still_prints_the_environment_report(pc_config, capsys):
    assert main(cli(pc_config, "doctor")) == 0
    out = capsys.readouterr().out
    assert "PhoneBridge environment report" in out
    assert "console script" in out


def test_console_reconfiguration_is_tolerated_on_a_stream_without_one(
    pc_config, capsys, monkeypatch
):
    """A redirected or captured stdout has no reconfigure(); that must not fail."""
    monkeypatch.setattr("sys.stdout", io.StringIO())
    assert main(cli(pc_config, "identity")) == 0


def test_console_reconfiguration_only_relaxes_error_handling(pc_config, monkeypatch):
    calls: list[dict] = []

    class FakeStream(io.StringIO):
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    fake = FakeStream()
    monkeypatch.setattr("sys.stdout", fake)
    monkeypatch.setattr("sys.stderr", FakeStream())
    assert main(cli(pc_config, "identity")) == 0
    assert calls and all("encoding" not in call for call in calls)
    assert calls[0]["errors"] == "replace"


# --------------------------------------------------------------------------- #
# argument validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", ["0", "65536", "-1", "abc"])
def test_port_must_be_a_real_port(pc_config, value, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(cli(pc_config, "ping", "--host", "127.0.0.1", "--port", value))
    assert exit_info.value.code == 2
    assert "--port" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "-3", "999999"])
def test_ping_count_is_validated(pc_config, value, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(cli(pc_config, "ping", "--host", "127.0.0.1", "--count", value))
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "count" in err


def test_bench_payload_size_is_validated(pc_config):
    with pytest.raises(SystemExit) as exit_info:
        main(cli(pc_config, "bench", "--host", "127.0.0.1", "--size", "0"))
    assert exit_info.value.code == 2


def test_bench_payload_size_cannot_exceed_one_envelope(pc_config, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(cli(pc_config, "bench", "--host", "127.0.0.1", "--size", str(99 * 1024 * 1024)))
    assert exit_info.value.code == 2
    assert "size" in capsys.readouterr().err


def test_empty_identity_name_is_refused(pc_config, capsys):
    assert main(cli(pc_config, "identity", "--name", "   ")) == 2
    err = capsys.readouterr().err
    assert "error: --name must not be empty" in err
    assert "hint:" in err


def test_forget_without_a_target_explains_itself(pc_config, capsys):
    assert main(cli(pc_config, "forget")) == 2
    err = capsys.readouterr().err
    assert "nothing to forget" in err
    assert "phonebridge identity" in err


# --------------------------------------------------------------------------- #
# target resolution (saved devices, config handling)
# --------------------------------------------------------------------------- #
def test_no_target_and_no_history_is_a_usage_error(pc_config, capsys):
    assert main(cli(pc_config, "info")) == 2
    err = capsys.readouterr().err
    assert "no phone chosen" in err
    assert "--host" in err


def test_a_url_is_not_a_host(pc_config, capsys):
    assert main(cli(pc_config, "info", "--host", "ws://127.0.0.1:8765")) == 2
    err = capsys.readouterr().err
    assert "not a URL" in err
    assert "--port" in err


def test_host_port_pair_is_split_for_the_user(pc_config, capsys):
    assert main(cli(pc_config, "info", "--host", "192.168.1.5:8765")) == 2
    err = capsys.readouterr().err
    assert "must not include a port" in err
    assert "--host 192.168.1.5 --port 8765" in err


def test_last_without_history_is_explained(pc_config, capsys):
    assert main(cli(pc_config, "info", "--last")) == 2
    assert "nothing for --last to reuse" in capsys.readouterr().err


def test_unknown_device_is_named_with_the_known_ones(pc_config, capsys):
    store = store_of(pc_config)
    store.pin_peer("abc123", "Pixel 7", b"\x01" * 65)
    assert main(cli(pc_config, "info", "--device", "Nexus")) == 2
    err = capsys.readouterr().err
    assert "no pinned phone matches" in err
    assert "Pixel 7 (abc123)" in err


def test_ambiguous_device_name_lists_the_candidates(pc_config, capsys):
    store = store_of(pc_config)
    store.pin_peer("id-one", "Pixel 7", b"\x01" * 65)
    store.pin_peer("id-two", "Pixel 7 Pro", b"\x02" * 65)
    assert main(cli(pc_config, "info", "--device", "pixel")) == 2
    err = capsys.readouterr().err
    assert "ambiguous" in err
    assert "id-one" in err and "id-two" in err


def test_pinned_device_without_an_address_says_so(pc_config, capsys):
    store = store_of(pc_config)
    store.pin_peer("abc123", "Pixel 7", b"\x01" * 65)
    assert main(cli(pc_config, "info", "--device", "abc123")) == 2
    err = capsys.readouterr().err
    assert "no saved address" in err
    assert "--host" in err


def _target_args_ns(**overrides) -> Namespace:
    base = dict(host=None, port=None, transport="auto", device=None, last=False)
    base.update(overrides)
    return Namespace(**base)


def test_explicit_port_overrides_the_saved_one(pc_config):
    store = store_of(pc_config)
    store.pin_peer("abc", "Phone", b"\x01" * 65)
    store.remember_endpoint("wifi", "10.0.0.5", 8765, peer_id="abc")
    target = resolve_target(store, _target_args_ns(device="abc", port=9000))
    assert (target.host, target.port) == ("10.0.0.5", 9000)


def test_saved_port_is_kept_when_no_port_is_given(pc_config):
    store = store_of(pc_config)
    store.remember_endpoint("usb", "127.0.0.1", 18765)
    target = resolve_target(store, _target_args_ns(last=True))
    assert (target.host, target.port) == ("127.0.0.1", 18765)


def test_host_without_a_port_uses_the_default(pc_config):
    store = store_of(pc_config)
    target = resolve_target(store, _target_args_ns(host="10.1.2.3"))
    assert target.port == 8765
    assert target.origin == "--host"


def test_a_saved_endpoint_is_reused_and_announced(pc_config, capsys):
    store_of(pc_config).remember_endpoint("wifi", "127.0.0.1", 1)
    assert main(cli(pc_config, "info")) == 2  # nothing listens on port 1
    err = capsys.readouterr().err
    assert "using the last seen endpoint 127.0.0.1:1" in err
    assert "error:" in err


# --------------------------------------------------------------------------- #
# unreachable phones
# --------------------------------------------------------------------------- #
def test_unreachable_phone_is_a_clean_error_not_a_traceback(pc_config, capsys):
    assert main(cli(pc_config, "info", "--host", "127.0.0.1", "--port", "1")) == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "error: cannot connect" in captured.err
    assert "adb forward" in captured.err  # the actionable hint for loopback
    assert captured.out == ""


def test_retries_are_attempted_and_reported(pc_config, capsys):
    assert main(cli(pc_config, "info", "--host", "127.0.0.1", "--port", "1",
                    "--retries", "1")) == 2
    err = capsys.readouterr().err
    assert "attempt 1 failed" in err
    assert "retrying in" in err
    assert err.count("error: cannot connect") == 1


def test_non_loopback_failure_suggests_the_network(pc_config, capsys):
    assert main(cli(pc_config, "info", "--host", "192.0.2.1", "--port", "1",
                    "--connect-timeout", "0.2")) == 2
    err = capsys.readouterr().err
    assert "same network" in err


# --------------------------------------------------------------------------- #
# pairing prompt
# --------------------------------------------------------------------------- #
def test_pairing_prompt_refuses_a_non_interactive_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO())
    with pytest.raises(CliError) as err:
        pairing_prompt("123456")
    assert "interactive terminal" in str(err.value)
    assert err.value.code == 2


def test_pairing_prompt_accepts_a_matching_code(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO())
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "y")
    assert pairing_prompt("123456") is True
    err = capsys.readouterr().err
    assert "PAIRING CODE" in err
    assert "123456" in err


def test_pairing_prompt_rejects_on_n(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO())
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "n")
    assert pairing_prompt("123456") is False


# --------------------------------------------------------------------------- #
# live commands against the reference phone
# --------------------------------------------------------------------------- #
def test_info_defaults_to_json_on_stdout(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "info", *target_args(running_phone))) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # stdout is pure JSON
    assert payload["device_id"] == running_phone.phone.identity.device_id
    assert payload["protocol_version"] == 1
    assert "capabilities" in payload
    assert "connecting to 127.0.0.1" in captured.err  # chatter goes to stderr


def test_info_text_view_names_the_device(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "info", "--format", "text",
                    *target_args(running_phone))) == 0
    out = capsys.readouterr().out
    assert "device information from Pixel-cli" in out
    assert "codename" in out
    assert "runtime permissions" in out


def test_info_survives_non_ascii_device_strings(tmp_path, sim_state, capsys):
    running = make_phone(sim_state, "Pixel-café", model="Pixel 7 café")
    try:
        assert main(cli(tmp_path, "info", "--format", "text",
                        *target_args(running))) == 0
        assert "café" in capsys.readouterr().out
    finally:
        _shutdown(running)


def test_capabilities_prints_the_whole_contract(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "capabilities", *target_args(running_phone))) == 0
    out = capsys.readouterr().out
    assert "HARDWARE" in out and "USABLE" in out
    for key in fmt.KNOWN_CAPABILITIES:
        assert key in out
    assert "simulated phone" in out
    assert "no RPC in protocol v1" in " ".join(out.split())


def test_capabilities_json_is_machine_readable(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "capabilities", "--json",
                    *target_args(running_phone))) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["simulated"] is True
    keys = {row["key"] for row in payload["capabilities"]}
    assert set(fmt.KNOWN_CAPABILITIES) <= keys
    scoped = next(row for row in payload["capabilities"] if row["key"] == "audio.bluetooth_sco")
    assert scoped["usable"] is True


def test_require_passes_when_the_capability_is_usable(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "capabilities", "--require", "audio.bluetooth_sco",
                    *target_args(running_phone))) == 0
    assert "requirements met: audio.bluetooth_sco" in capsys.readouterr().out


def test_require_fails_with_the_reason(pc_config, running_phone, capsys):
    # `sensors` is the honest example now: the Phase 4 camera *is* available on
    # the simulator, so requiring it would succeed.
    assert main(cli(pc_config, "capabilities", "--require", "sensors",
                    *target_args(running_phone))) == 4
    err = capsys.readouterr().err
    assert "requirement not met: sensors" in err


def test_require_warns_about_a_key_the_protocol_does_not_define(
    pc_config, running_phone, capsys
):
    assert main(cli(pc_config, "capabilities", "--require", "telepathy",
                    *target_args(running_phone))) == 4
    err = capsys.readouterr().err
    assert "not a capability this protocol defines" in err


def test_audio_status_reads_the_headset_and_the_relay(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "audio", "status", *target_args(running_phone))) == 0
    out = capsys.readouterr().out
    assert "headset connected" in out
    assert "audio relay (Phase 2)" in out
    assert "supported=" in out


def test_audio_last_explains_that_nothing_was_measured(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "audio", "last", *target_args(running_phone))) == 1
    err = capsys.readouterr().err
    assert "no stored probe report" in err
    assert "phonebridge compat run" in err


def test_compat_run_then_audio_last_shows_the_same_report(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "compat", "run", "--seconds", "5",
                    *target_args(running_phone))) == 0
    run_out = capsys.readouterr()
    assert "stored:" in run_out.out
    assert "verdict" in run_out.out  # compare_table column
    assert "duplex" in run_out.out

    assert main(cli(pc_config, "audio", "last", *target_args(running_phone))) == 0
    out = capsys.readouterr().out
    assert "verdict" in out and "latency" in out  # the compare table rendered
    assert "nothing was stored locally" in out


def test_audio_status_marks_a_phone_without_the_relay_method(pc_config, sim_state, capsys):
    """An older app has no audio.route.bluetooth; that is reported, not guessed."""
    ident = _identity_for(sim_state / "old.pem", "Old-phone")
    phone = SimulatedPhone(
        identity=ident, state_file=None, host="127.0.0.1", port=0, pair_policy="auto"
    )
    del phone.router.handlers["audio.route.bluetooth"]
    loop, thread, stop_event = _serve(phone)
    running = RunningPhone(phone=phone, loop=loop, thread=thread, stop_event=stop_event)
    try:
        assert main(cli(pc_config, "audio", "status", *target_args(running))) == 0
        captured = capsys.readouterr()
        assert "does not report a relay state" in captured.err
        assert "headset connected" in captured.out
    finally:
        _shutdown(running)


def test_ping_reports_a_median(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "ping", "--count", "3", "--interval", "0",
                    *target_args(running_phone))) == 0
    out = capsys.readouterr().out
    assert "median" in out
    assert out.count("seq=") == 3


def test_bench_confirms_integrity_and_honours_the_threshold(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "bench", "--frames", "20", "--size", "256",
                    "--expect-ms", "60000", *target_args(running_phone))) == 0
    out = capsys.readouterr().out
    assert "byte-identical" in out
    assert "PASS" in out


def test_bench_threshold_failure_exits_one(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "bench", "--frames", "20", "--size", "256",
                    "--expect-ms", "0.01", *target_args(running_phone))) == 1
    assert "FAIL" in capsys.readouterr().out


def test_watch_streams_states_then_stops(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "watch", "--timeout", "1",
                    *target_args(running_phone))) == 0
    out = capsys.readouterr().out
    assert "connected" in out
    assert "stopped" in out


def test_watch_json_is_one_object_per_line(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "watch", "--timeout", "1", "--json",
                    *target_args(running_phone))) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("{")]
    assert lines, "watch --json must emit JSON lines"
    first = json.loads(lines[0])
    assert first["state"] in {"connecting", "connected"}


# --------------------------------------------------------------------------- #
# selftest: the verification matrix
# --------------------------------------------------------------------------- #
def test_selftest_runs_every_supported_workflow(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "selftest", "--frames", "2",
                    *target_args(running_phone))) == 0
    out = capsys.readouterr().out
    assert "WORKFLOW" in out and "RESULT" in out and "EVIDENCE" in out
    assert "12 of 12 supported workflows passed" in out
    assert "authenticated connection" in out
    assert "binary integrity" in out
    assert "NOT IMPLEMENTED" in out
    assert "not in protocol v1" in out


def test_selftest_json_is_a_matrix_of_rows(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "selftest", "--json", "--frames", "2",
                    *target_args(running_phone))) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["protocol_version"] == 1
    assert payload["summary"]["PASS"] == 12
    # Phase 2 removed audio and Phase 4 removed the camera: three remain.
    assert payload["summary"]["NOT IMPLEMENTED"] == 3
    statuses = {row["status"] for row in payload["rows"]}
    assert statuses == {"PASS", "NOT IMPLEMENTED"}
    unsupported = [row for row in payload["rows"] if row["category"] == "unsupported"]
    assert len(unsupported) == 3
    assert all("protocol v1" in row["evidence"] for row in unsupported)
    assert not any("audio" in str(row).lower() for row in unsupported), (
        "Phase 2 implemented audio; it must appear as a supported workflow"
    )


def test_selftest_exits_one_when_a_workflow_cannot_run(pc_config, sim_state, capsys):
    phone = make_phone(sim_state, "Broken-phone")
    del phone.phone.router.handlers["session.stats"]
    try:
        assert main(cli(pc_config, "selftest", "--frames", "2",
                        *target_args(phone))) == 1
        out = capsys.readouterr().out
        assert "session statistics" in out
        assert "FAIL" in out
        assert "11 of 12 supported workflows passed, 1 failed" in out
    finally:
        _shutdown(phone)


# --------------------------------------------------------------------------- #
# saved devices after a real connection
# --------------------------------------------------------------------------- #
def test_a_connection_is_remembered_and_reused(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "info", *target_args(running_phone))) == 0
    capsys.readouterr()

    # No target at all: the saved endpoint is used and announced.
    assert main(cli(pc_config, "info")) == 0
    err = capsys.readouterr().err
    assert "using the last seen endpoint" in err
    assert "connected to Pixel-cli (paired)" in err


def test_device_name_resolves_to_the_saved_address(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "info", *target_args(running_phone))) == 0
    capsys.readouterr()
    assert main(cli(pc_config, "info", "--device", "pixel-cli")) == 0
    captured = capsys.readouterr()
    assert f"saved address for Pixel-cli: 127.0.0.1:{running_phone.port}" in captured.err
    assert json.loads(captured.out)["name"] == "Pixel-cli"


def test_devices_lists_pinned_phones_and_their_address(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "info", *target_args(running_phone))) == 0
    capsys.readouterr()
    assert main(cli(pc_config, "devices", "--no-discover")) == 0
    out = capsys.readouterr().out
    assert "pinned phones (1)" in out
    assert "Pixel-cli" in out
    assert f"127.0.0.1:{running_phone.port}" in out
    assert "live beacons: skipped" in out


def test_devices_json_has_endpoints_and_beacons(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "info", *target_args(running_phone))) == 0
    capsys.readouterr()
    assert main(cli(pc_config, "devices", "--json", "--no-discover")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["identity"]["device_id"]
    assert len(payload["pinned"]) == 1
    assert payload["pinned"][0]["endpoint"]["port"] == running_phone.port
    assert payload["live"] == []


def test_devices_with_nothing_known_explains_the_first_step(pc_config, capsys):
    assert main(cli(pc_config, "devices", "--no-discover")) == 1
    out = capsys.readouterr().out
    assert "none yet" in out
    assert "phonebridge info --host" in out


def test_identity_name_is_persisted(pc_config, capsys):
    assert main(cli(pc_config, "identity", "--name", "Desk PC")) == 0
    assert "device name set to Desk PC" in capsys.readouterr().out
    assert main(cli(pc_config, "identity", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "Desk PC"


def test_forget_all_unpins_everything(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "info", *target_args(running_phone))) == 0
    capsys.readouterr()
    assert main(cli(pc_config, "forget", "--all")) == 0
    assert "forgot 1 phone(s)" in capsys.readouterr().out
    assert store_of(pc_config).list_peers() == []


def test_forget_unknown_id_returns_one(pc_config, capsys):
    assert main(cli(pc_config, "forget", "nope")) == 1
    assert "no pinned phone with id nope" in capsys.readouterr().err


def test_forget_by_id(pc_config, running_phone, capsys):
    assert main(cli(pc_config, "info", *target_args(running_phone))) == 0
    capsys.readouterr()
    peer_id = running_phone.phone.identity.device_id
    assert main(cli(pc_config, "forget", peer_id)) == 0
    assert f"forgot {peer_id}" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def test_discover_json_is_a_list_and_the_exit_code_matches_reality(pc_config, capsys):
    """A beaconing phone may be present on the machine running the tests, so the
    assertion is about the contract, not about the environment being empty."""
    code = main(cli(pc_config, "discover", "--json", "--timeout", "0.4"))
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert code == (0 if payload else 1)
    for phone in payload:
        assert {"device_id", "name", "host", "port", "uri"} <= set(phone)


def test_discover_text_explains_itself_when_nothing_is_heard(pc_config, capsys):
    code = main(cli(pc_config, "discover", "--timeout", "0.4"))
    out = capsys.readouterr().out
    assert "Listening for phone beacons on UDP 47823" in out
    if code == 1:
        assert "no phones found" in out
        assert "UDP 47823" in out  # the troubleshooting help
    else:
        assert "found:" in out
        assert "connect to it with" in out


# --------------------------------------------------------------------------- #
# formatting helpers (kept honest by design)
# --------------------------------------------------------------------------- #
def test_capability_rows_do_not_invent_values():
    rows = fmt.capability_rows({"capabilities": {"sensors": True}})
    by_key = {row.key: row for row in rows}
    assert by_key["sensors"].usable is True
    assert by_key["sensors"].hardware is None
    assert by_key["camera.back"].usable is None
    assert by_key["camera.back"].hardware is None
    assert "not reported" in by_key["camera.back"].note()


def test_unmet_requirements_only_accepts_true():
    info = {"capabilities": {"sensors": True, "file_transfer": False, "camera.back": 1}}
    assert fmt.unmet_requirements(info, ["sensors", "camera.back"]) == []
    assert fmt.unmet_requirements(info, ["file_transfer"]) == ["file_transfer"]
    assert fmt.unmet_requirements(info, ["nope"]) == ["nope"]


def test_documented_methods_are_the_ones_the_reference_phone_implements(running_phone):
    """`docs/03` lists protocol v1's methods; the reference phone must answer all
    of them, and the CLI's own note must not name a method that does not exist."""
    handlers = set(running_phone.phone.router.handlers)
    assert set(fmt.PROTOCOL_V1_METHODS) <= handlers
    # Everything else the simulator adds is explicitly simulator-only.
    assert handlers - set(fmt.PROTOCOL_V1_METHODS) <= {"system.shutdown"}


@pytest.mark.parametrize("verb", ["files", "camera", "sensors"])
def test_there_is_no_command_for_a_protocol_v1_gap(pc_config, verb, capsys):
    """No fake verbs: an unimplemented feature is absent, not stubbed to fail."""
    with pytest.raises(SystemExit) as exit_info:
        main(cli(pc_config, verb))
    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_text_is_ascii_so_a_legacy_console_cannot_crash(pc_config, capsys):
    outputs: list[str] = []
    for argv in (["--help"], ["doctor"], ["devices", "--no-discover"],
                 ["info", "--host", "127.0.0.1", "--port", "1"]):
        try:
            main(cli(pc_config, *argv))
        except SystemExit:
            pass
        captured = capsys.readouterr()
        outputs.append(captured.out + captured.err)
    joined = "".join(outputs)
    assert joined.isascii(), "non-ASCII in CLI text would break cp437 consoles"


def test_table_pads_columns_and_keeps_ascii():
    rendered = fmt.table(["A", "B"], [["x", "long-value"], ["yy", "z"]])
    assert rendered.splitlines()[0].startswith("  A")
    assert "long-value" in rendered
    assert rendered.isascii()
