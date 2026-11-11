"""The Phase 5 panels, tested without a display.

The window itself needs tkinter; everything the window *says* lives in
:mod:`phonebridge.controller.planeview` so it can be checked here. These tests
are about honesty as much as formatting:

* a sensor with no frame must say so, not show zeros;
* a transfer that has not verified must not read as success;
* a radio that is present but switched off must not read as missing;
* "no adapter report yet" must not read as "denied".
"""

from __future__ import annotations

import base64

import pytest

from phonebridge.controller import planeview as pv
from phonebridge.controller.planeview import (
    bluetooth_headline,
    device_rows,
    file_rows,
    file_values,
    files_headline,
    format_bytes,
    gatt_headline,
    gatt_rows,
    interval_label,
    notification_line,
    permission_word,
    reading_text,
    sensor_headline,
    sensor_rows,
    short_uuid,
    value_words,
)


# --------------------------------------------------------------------------- #
# formatters
# --------------------------------------------------------------------------- #
def test_format_bytes_uses_the_unit_the_number_is_in():
    assert format_bytes(0) == "0 B"
    assert format_bytes(1023) == "1023 B"
    assert format_bytes(1024) == "1.0 KiB"
    assert format_bytes(5 * 1024 * 1024) == "5.0 MiB"
    assert format_bytes(3 * 1024**3) == "3.0 GiB"


def test_short_uuid_collapses_the_bluetooth_base_uuid():
    assert short_uuid("0000180f-0000-1000-8000-00805f9b34fb") == "180f"
    # A vendor UUID is kept whole: shortening it would hide which device it is.
    assert short_uuid("6e400001-b5a3-f393-e0a9-e50e24dcca9e") == (
        "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
    )
    assert short_uuid("") == ""


def test_permission_word_never_turns_unknown_into_denied():
    assert permission_word(True) == "granted"
    assert permission_word(False) == "denied"
    assert permission_word(None) == "not reported", (
        "a phone that did not answer must not be described as refusing"
    )


def test_interval_label_shows_rate_and_period():
    assert interval_label(20_000) == "50 Hz (20.000 ms)"
    assert interval_label(1_000_000) == "1 Hz (1000.000 ms)"


def test_reading_text_never_invents_a_reading():
    info = {"kind": 1, "unit": "m/s²"}
    assert reading_text({}, info) == "no frame yet"
    assert reading_text({"values": []}, info) == "no frame yet"
    assert reading_text({"values": [1.0, -0.5, 9.81]}, info) == "+1.000, -0.500, +9.810 m/s²"
    assert reading_text({"values": ["x"]}, info) == "unreadable frame"


def test_value_words_decodes_hex_and_text():
    payload = base64.b64encode(b"hello").decode()
    assert "hello" in value_words(payload)
    assert value_words("") == "empty value"
    assert "could not decode" in value_words("not base64!!")


def test_a_notification_line_carries_time_and_sequence():
    payload = base64.b64encode(bytes([87])).decode()
    line = notification_line(
        {
            "characteristic": "00002a19-0000-1000-8000-00805f9b34fb",
            "value_b64": payload,
            "seq": 3,
            "timestamp_ms": 1_700_000_000_000,
        }
    )
    assert "2a19" in line and "#3" in line and "1700000000000 ms" in line


# --------------------------------------------------------------------------- #
# bluetooth
# --------------------------------------------------------------------------- #
def _adapter(**over: object) -> dict[str, object]:
    base = {
        "supported": True,
        "enabled": True,
        "le_supported": True,
        "permitted": True,
        "detail": "",
    }
    base.update(over)
    return base


def test_bluetooth_headline_separates_off_from_missing():
    enabled = bluetooth_headline({"available": True, "adapter": _adapter(), "scan": {}})
    assert "supported" in enabled and "BLE yes" in enabled

    off = bluetooth_headline(
        {"available": True, "adapter": _adapter(enabled=False), "scan": {}}
    )
    assert "off" in off and "supported" in off, (
        "a radio that is switched off must not read as unsupported"
    )

    unsupported = bluetooth_headline(
        {"available": True, "adapter": _adapter(supported=False, enabled=False), "scan": {}}
    )
    assert "not supported" in unsupported


def test_bluetooth_headline_says_not_connected_without_a_session():
    text = bluetooth_headline({"available": False, "detail": "not connected"})
    assert text == "not connected"


def test_bluetooth_headline_shows_a_dropped_permission():
    text = bluetooth_headline(
        {"available": True, "adapter": _adapter(permitted=False), "scan": {}}
    )
    assert "permission denied" in text


def test_scan_state_and_error_are_reported():
    text = bluetooth_headline(
        {
            "available": True,
            "adapter": _adapter(),
            "scan": {"state": "error", "scanning": False, "error": "adapter busy"},
        }
    )
    assert "scan error" in text and "adapter busy" in text

    running = bluetooth_headline(
        {"available": True, "adapter": _adapter(), "scan": {"state": "scanning", "scanning": True}}
    )
    assert "scan running" in running


def test_device_rows_keep_signal_and_advertised_services():
    status = {
        "scan": {
            "devices": [
                {
                    "address": "C4:7F:51:00:00:01",
                    "name": "Thermo",
                    "rssi": -54,
                    "service_uuids": ["0000180f-0000-1000-8000-00805f9b34fb"],
                    "connectable": True,
                    "sightings": 4,
                },
                {"address": "", "name": "no address"},
            ]
        }
    }
    rows = device_rows(status)
    assert len(rows) == 1, "a device without an address cannot be connected, so it is skipped"
    iid, values = rows[0]
    assert iid == "C4:7F:51:00:00:01"
    assert values[0] == "Thermo" and values[2] == -54 and values[3] == "180f"
    assert values[4] == "yes" and values[5] == 4


def test_device_rows_mark_unconnectable_devices():
    rows = device_rows(
        {"scan": {"devices": [{"address": "AA:BB", "rssi": -88, "connectable": False}]}}
    )
    assert rows[0][1][0] == "(unnamed device)"
    assert rows[0][1][4] == "no"


def test_gatt_rows_build_a_service_tree_with_subscription_marks():
    connection = {
        "address": "C4:7F:51:00:00:01",
        "state": "connected",
        "subscriptions": ["0000180f-0000-1000-8000-00805f9b34fb/00002a19-0000-1000-8000-00805f9b34fb"],
        "tree": {
            "mtu": 185,
            "services": [
                {
                    "uuid": "0000180f-0000-1000-8000-00805f9b34fb",
                    "characteristics": [
                        {
                            "uuid": "00002a19-0000-1000-8000-00805f9b34fb",
                            "properties": ["read", "notify"],
                        }
                    ],
                }
            ],
        },
    }
    rows = gatt_rows(connection)
    assert rows[0] == ("srv:0000180f-0000-1000-8000-00805f9b34fb", "180f", "", None)
    iid, label, props, parent = rows[1]
    assert iid.startswith("char:") and "2a19" in label
    assert "[notifying]" in label and props == "read, notify"
    assert parent == rows[0][0]

    headline = gatt_headline(connection, fallback="")
    assert "MTU 185" in headline and "1 subscription" in headline


def test_gatt_without_a_connection_says_what_to_do():
    assert gatt_rows(None) == []
    assert gatt_headline(None, fallback="services appear after Connect") == (
        "services appear after Connect"
    )


# --------------------------------------------------------------------------- #
# sensors
# --------------------------------------------------------------------------- #
def _sensor_status(**over: object) -> dict[str, object]:
    status: dict[str, object] = {
        "available": True,
        "state": "active",
        "interval_us": 20_000,
        "registry": {
            "sensors": [
                {"id": 1, "name": "Accelerometer", "kind": 1, "unit": "m/s²"},
                {"id": 4, "name": "Gyroscope", "kind": 4, "unit": "rad/s"},
            ],
            "active": [1],
            "permitted": True,
            "readings": {
                "1": {"values": [0.0, 0.0, 9.81], "seq": 7},
            },
        },
        "stats": {
            "1": {"hz": 49.8, "frames": 1234, "lost": 2, "age_ms": 18},
            "4": {"hz": 0, "frames": 0, "lost": 0, "age_ms": 0},
        },
    }
    status.update(over)
    return status


def test_sensor_rows_show_streaming_state_per_sensor():
    rows = sensor_rows(_sensor_status())
    assert [row[0] for row in rows] == ["sensor:1", "sensor:4"]
    accel = rows[0][1]
    assert accel[0] == "Accelerometer"
    assert accel[1] == "streaming"
    assert accel[2] == "49.8 Hz"
    assert accel[3] == 1234 and accel[4] == 2
    assert accel[5] == "+0.000, +0.000, +9.810 m/s²"
    gyro = rows[1][1]
    assert gyro[1] == "idle", "a sensor that is not streaming must not look live"
    assert gyro[5] == "no frame yet", "no reading is not a reading of zero"


def test_sensor_headline_reports_the_interval_and_permission():
    text = sensor_headline(_sensor_status())
    assert "50 Hz (20.000 ms)" in text
    denied = _sensor_status()
    denied["registry"] = {**_sensor_status()["registry"], "permitted": False}
    assert "not granted sensor access" in sensor_headline(denied)
    assert sensor_headline({"available": False, "detail": "not connected"}) == "not connected"


def test_sensor_rows_tolerate_a_phone_that_lists_nothing():
    assert sensor_rows({"registry": {"sensors": []}}) == []
    assert sensor_rows({}) == []


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #
def test_file_values_never_report_success_before_verification():
    pending = file_values(
        {
            "transfer_id": 3,
            "name": "report.pdf",
            "size": 2048,
            "bytes_done": 2048,
            "state": "complete",
            "verified": None,
            "direction": "to_phone",
        }
    )
    assert pending[5] == "complete"
    assert pending[6] == "hash NOT confirmed", (
        "bytes on the wire are not proof; only the phone's hash verdict is"
    )
    verified = file_values(
        {
            "transfer_id": 3,
            "name": "report.pdf",
            "size": 2048,
            "bytes_done": 2048,
            "state": "complete",
            "verified": True,
            "direction": "to_phone",
        }
    )
    assert verified[6] == "hash verified"


def test_file_values_show_progress_bytes_and_speed():
    values = file_values(
        {
            "transfer_id": 1,
            "name": "big.bin",
            "size": 10 * 1024 * 1024,
            "bytes_done": 5 * 1024 * 1024,
            "state": "active",
            "direction": "from_phone",
            "speed_bps": 2 * 1024 * 1024,
            "fraction": 0.5,
        }
    )
    assert values[1] == "from phone"
    assert values[2] == "50%"
    assert values[3] == "5.0 MiB / 10.0 MiB"
    assert values[4] == "2.0 MiB/s"
    assert values[5] == "transferring"
    assert values[6] == "pending"


def test_a_failed_transfer_shows_the_reason():
    values = file_values(
        {
            "transfer_id": 9,
            "name": "x.bin",
            "state": "failed",
            "error": "hash mismatch: the file arrived corrupted",
        }
    )
    assert values[5] == "failed"
    assert "hash mismatch" in values[6]


def test_file_rows_keep_ids_stable_for_selection():
    status = {
        "transfers": [{"transfer_id": 4, "name": "a", "state": "active"}],
        "history": [{"transfer_id": 2, "name": "b", "state": "complete", "verified": True}],
    }
    rows = file_rows(status)
    assert [row[0] for row in rows] == ["file:4", "file:2"]


def test_files_headline_counts_what_is_in_flight():
    live = files_headline(
        {
            "available": True,
            "destination": "C:/Users/me/Downloads",
            "transfers": [
                {"state": "active"},
                {"state": "paused"},
                {"state": "complete"},
            ],
        }
    )
    assert "2 transfer(s) in flight" in live
    assert "C:/Users/me/Downloads" in live
    quiet = files_headline({"available": True, "transfers": []})
    assert "no transfers in flight" in quiet
    assert "the folder you choose" in quiet, (
        "with no destination chosen the panel must not imply a default folder"
    )


def test_files_headline_says_not_connected():
    assert files_headline({"available": False, "detail": "not connected"}) == "not connected"


# --------------------------------------------------------------------------- #
# the tables the window offers
# --------------------------------------------------------------------------- #
def test_the_offered_rates_and_durations_are_ordered_and_sane():
    rates = [us for _label, us in pv.SENSOR_RATES]
    assert rates == sorted(rates), "fastest first is the order the list is read in"
    assert min(rates) >= 5_000 and max(rates) <= 1_000_000
    durations = [ms for _label, ms in pv.SCAN_DURATIONS]
    assert 0 not in durations[:-1], "'until stopped' belongs last"
    assert durations[-1] == 0


def test_a_reading_with_the_wrong_axis_count_still_renders():
    """A phone that reports three values for a one-axis sensor is not a crash."""
    assert reading_text({"values": [9.81]}, {"kind": 5, "unit": "cm"}) == "+9.810 cm"


@pytest.mark.parametrize("state", ["pending", "active", "paused", "complete", "failed"])
def test_every_file_state_has_words(state: str):
    assert state in pv.FILE_STATE_WORDS
    assert pv.FILE_STATE_WORDS[state]
