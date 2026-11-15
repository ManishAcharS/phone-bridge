package dev.phonebridge.service

import android.Manifest
import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.PowerManager
import android.util.Log
import dev.phonebridge.Permissions

/**
 * The ADB daily-use intent contract.
 *
 * `adb shell am start -n dev.phonebridge/.MainActivity --ez autostart true` is a
 * request to the visible Activity, not a background-service start broadcast.
 * MainActivity waits until RESUMED, asks for Camera/Microphone if needed, and only
 * then calls startForegroundService(). That is the Android 14+ while-in-use path.
 *
 * A cold start behind a secure keyguard is deliberately not forced: Android and
 * HyperOS can refuse camera/microphone foreground-service starts there. An already
 * running service keeps its wake/Wi-Fi locks and typed foreground state when the
 * screen later locks; a cold locked launch is deferred until the user unlocks once.
 */
object BridgeAutostart {
    const val TAG = "PhoneBridgeAutostart"
    const val EXTRA_AUTOSTART = "autostart"
    const val EXTRA_TOKEN = "autostart_token"

    fun requested(intent: Intent?): Boolean =
        intent?.getBooleanExtra(EXTRA_AUTOSTART, false) == true

    fun token(intent: Intent?): String =
        intent?.getStringExtra(EXTRA_TOKEN) ?: "intent-${System.identityHashCode(intent)}"

    /** Camera and microphone are required for the daily all-three path. */
    fun mediaGranted(context: Context): Boolean =
        Permissions.granted(context, Manifest.permission.CAMERA) &&
            Permissions.granted(context, Manifest.permission.RECORD_AUDIO)

    /** Notifications are useful but not a reason to block the bridge. */
    fun permissionsToRequest(): Array<String> = buildList {
        add(Manifest.permission.CAMERA)
        add(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            add(Manifest.permission.POST_NOTIFICATIONS)
        }
    }.toTypedArray()

    fun visibleAndUnlocked(context: Context): Boolean {
        val power = context.getSystemService(Context.POWER_SERVICE) as? PowerManager
        val keyguard = context.getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager
        return power?.isInteractive == true && keyguard?.isKeyguardLocked != true
    }

    /** Returns null on success, or the exact sentence the PC/user should see. */
    fun startFromVisibleActivity(context: Context): String? {
        if (!visibleAndUnlocked(context)) {
            return lockedMessage()
        }
        if (!mediaGranted(context)) {
            return deniedMessage()
        }
        return try {
            BridgeService.start(context)
            Log.i(TAG, "autostart requested from a visible, unlocked Activity")
            null
        } catch (e: RuntimeException) {
            val message =
                "Android refused the bridge foreground-service start: ${e.javaClass.simpleName}: ${e.message}. " +
                    lockedMessage()
            Log.w(TAG, message, e)
            BridgeState.note("ERROR", message)
            message
        }
    }

    fun lockedMessage(): String =
        "Unlock the phone once and leave PhoneBridge in front until the PC says the bridge is ready. " +
            "Android 14+/HyperOS can block a cold camera/microphone foreground-service start behind the lock screen. " +
            "An already running bridge continues while locked."

    fun deniedMessage(): String =
        "Autostart needs Camera and Microphone. Allow them in Settings > Apps > PhoneBridge > Permissions, " +
            "then run the launcher again. On HyperOS also enable Autostart and battery No restrictions."
}
