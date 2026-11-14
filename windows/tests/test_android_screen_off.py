"""Screen off, app in the background, Wi-Fi power save: the three ways it dies.

Item 4 of the latency work ends with a question that is not about latency: does
the bridge keep running with the phone's screen off and another app in front, and
does it come back after the USB cable is pulled and re-plugged?

The answers live in three places, and each is pinned here:

* **the service** — `START_STICKY`, so the platform re-creates it instead of
  forgetting it; a partial wake lock, so the socket is not frozen with the screen;
* **the radio** — a `WIFI_MODE_FULL_HIGH_PERF` Wi-Fi lock, because a partial wake
  lock keeps the *CPU* awake and does nothing about Wi-Fi power save, which over a
  hotspot shows up as late frames and, eventually, a timed-out link;
* **the forward** — `adb forward` is deleted by an unplug, which is why the PC
  re-creates it before every reconnect attempt (see ``test_start_live_launcher``).

The phone-side permission half of the typed foreground service is already covered
by ``test_android_build.py``; this module only adds what the daily-use work
changed.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVICE = (
    REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
    / "service" / "BridgeService.kt"
)
MANIFEST = REPO / "android" / "app" / "src" / "main" / "AndroidManifest.xml"


def source() -> str:
    return SERVICE.read_text(encoding="utf-8")


def code_only(text: str) -> str:
    return "\n".join(
        line
        for line in text.splitlines()
        if not line.lstrip().startswith(("*", "/**", "//"))
    )


def function(name: str) -> str:
    text = code_only(source())
    start = text.index(name)
    rest = text[start + len(name):]
    ends = [
        rest.index(marker)
        for marker in ("\n    private fun ", "\n    fun ", "\n    override fun ")
        if marker in rest
    ]
    return rest[: min(ends)] if ends else rest


def test_the_service_asks_to_be_restarted_rather_than_forgotten() -> None:
    body = function("override fun onStartCommand")
    assert body.count("START_STICKY") == 2, (
        "both the action path and the 'system re-created me' path must return STICKY"
    )


def test_the_partial_wake_lock_is_still_the_screen_off_answer() -> None:
    body = function("private fun acquireWakeLock")
    assert "PARTIAL_WAKE_LOCK" in body
    assert "acquire(" in body
    release = function("private fun releaseWakeLock")
    assert "isHeld" in release and "release()" in release


def test_a_wifi_lock_keeps_the_radio_out_of_power_save() -> None:
    """A CPU wake lock does not stop Wi-Fi power save; this does."""
    body = function("private fun acquireWakeLock")
    assert "WIFI_MODE_FULL_HIGH_PERF" in body
    assert "createWifiLock" in body
    assert "runCatching" in body, "a device with no Wi-Fi must still run over USB"


def test_the_wifi_lock_is_released_with_the_wake_lock() -> None:
    release = function("private fun releaseWakeLock")
    assert "wifiLock?.let { if (it.isHeld) it.release() }" in release
    assert "wifiLock = null" in release


def test_the_wifi_lock_only_needs_a_permission_the_manifest_already_has() -> None:
    manifest = MANIFEST.read_text(encoding="utf-8")
    assert "android.permission.ACCESS_WIFI_STATE" in manifest
    assert "android.permission.WAKE_LOCK" in manifest


def test_the_service_still_claims_every_stream_type_it_uses() -> None:
    manifest = MANIFEST.read_text(encoding="utf-8")
    assert (
        'android:foregroundServiceType="connectedDevice|camera|microphone|mediaPlayback"'
        in manifest
    )
    body = code_only(source())
    for claim in (
        "FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE",
        "FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK",
        "FOREGROUND_SERVICE_TYPE_MICROPHONE",
        "FOREGROUND_SERVICE_TYPE_CAMERA",
    ):
        assert claim in body, f"{claim} is never requested"


def test_a_refused_type_falls_back_instead_of_killing_the_service() -> None:
    body = function("private fun promoteToForeground")
    assert "catch (e: RuntimeException)" in body
    assert "connectionType()" in body


def test_the_service_is_not_stopped_when_the_task_is_swiped_away() -> None:
    """`stopWithTask` would kill the bridge the moment the task switcher is used."""
    manifest = MANIFEST.read_text(encoding="utf-8")
    start = manifest.index('.service.BridgeService')
    block = manifest[start : start + 400]
    assert "stopWithTask" not in block
    assert 'android:exported="false"' in block
