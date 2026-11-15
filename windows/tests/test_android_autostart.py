"""Static contract for the ADB autostart intent and Android 14+ while-in-use start."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
KOTLIN = REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
ACTIVITY = KOTLIN / "MainActivity.kt"
AUTOSTART = KOTLIN / "service" / "BridgeAutostart.kt"
SERVICE = KOTLIN / "service" / "BridgeService.kt"
MANIFEST = REPO / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
LAUNCH = REPO / "windows" / "phonebridge" / "live" / "launch.py"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_pc_launches_the_activity_with_the_boolean_extra_not_a_background_service() -> None:
    source = text(LAUNCH)
    assert '"am", "start", "-W", "-n", APP_COMPONENT' in source
    assert '"--ez", "autostart", "true"' in source
    assert "autostart_token" in source
    assert "start-foreground-service" not in source
    assert "ping" in source and "remember_endpoint" in source


def test_activity_handles_cold_and_single_top_intents_from_resumed_state() -> None:
    source = text(ACTIVITY)
    assert "override fun onNewIntent(intent: Intent)" in source
    assert "setIntent(intent)" in source
    assert "DefaultLifecycleObserver" in source
    assert "override fun onResume(owner: LifecycleOwner)" in source
    assert "maybeAutostart()" in source
    assert "RequestMultiplePermissions()" in source
    assert "BridgeAutostart.startFromVisibleActivity(this)" in source


def test_autostart_is_deferred_behind_the_keyguard_and_never_bypasses_it() -> None:
    source = text(AUTOSTART)
    assert "PowerManager" in source and "isInteractive" in source
    assert "KeyguardManager" in source and "isKeyguardLocked" in source
    assert "Unlock the phone once" in source
    assert "showWhenLocked" not in source
    assert "START_ACTIVITIES_FROM_BACKGROUND" not in source
    manifest = text(MANIFEST)
    assert "showWhenLocked" not in manifest


def test_camera_and_microphone_are_required_but_notification_denial_is_not_fatal() -> None:
    source = text(AUTOSTART)
    assert "Manifest.permission.CAMERA" in source
    assert "Manifest.permission.RECORD_AUDIO" in source
    assert "POST_NOTIFICATIONS" in source
    assert "mediaGranted" in source
    activity = text(ACTIVITY)
    assert "if (BridgeAutostart.mediaGranted(this))" in activity


def test_service_promotion_failure_is_reported_instead_of_crashing() -> None:
    source = text(SERVICE)
    assert "private fun promoteToForeground(notification: Notification): Boolean" in source
    assert "if (!promoteToForeground(" in source
    assert "return START_NOT_STICKY" in source
    assert "BridgeState.note(\"ERROR\", message)" in source
    assert "stopSelf()" in source


def test_manifest_keeps_the_service_private_and_typed() -> None:
    manifest = text(MANIFEST)
    assert 'android:launchMode="singleTop"' in manifest
    assert 'android:exported="false"' in manifest
    assert 'android:foregroundServiceType="connectedDevice|camera|microphone|mediaPlayback"' in manifest
