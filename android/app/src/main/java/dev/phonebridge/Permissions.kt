package dev.phonebridge

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings

/**
 * The app's permissions, in one place.
 *
 * Rules this encodes (all three were being broken before):
 *
 *  1. **Ask when needed, not at launch.** A permission is requested the moment
 *     the user starts the feature that needs it, never on app start.
 *  2. **Distinguish "denied" from "denied permanently".** After a permanent
 *     denial the system no longer shows a dialog, so a "Grant" button would do
 *     nothing: that case gets an "Open app settings" button instead.
 *  3. **Say why.** Each entry carries the reason and the consequence of
 *     refusing, so the UI can explain instead of nagging.
 *
 * The app deliberately declares no CAMERA, BLUETOOTH_SCAN or location
 * permission: nothing uses them yet, and holding a permission the app cannot
 * use is exactly the dishonesty this phase removes. They are listed in
 * [futurePermissions] so the UI can say *why* they are not there.
 */
object Permissions {

    enum class Status { GRANTED, DENIED, PERMANENTLY_DENIED, NOT_APPLICABLE }

    data class Item(
        val permission: String,
        val title: String,
        val why: String,
        val withoutIt: String,
        val minSdk: Int = 0,
    ) {
        /** e.g. "RECORD_AUDIO" — short enough for a compact status table. */
        val shortName: String get() = permission.substringAfterLast('.')
    }

    const val PREFS = "phonebridge_permissions"
    private const val KEY_PREFIX = "asked:"
    private const val BLOCKED_PREFIX = "blocked:"

    /** Runtime permissions this build actually has a use for. */
    val runtime: List<Item> = listOf(
        Item(
            permission = Manifest.permission.POST_NOTIFICATIONS,
            title = "Notifications (Android 13+)",
            why = "Android only shows a foreground service's notification — the one " +
                "with the Stop action — if this is granted.",
            withoutIt = "The bridge still runs and still listens; you just get no " +
                "notification and no Stop shortcut.",
            minSdk = 33,
        ),
        Item(
            permission = Manifest.permission.RECORD_AUDIO,
            title = "Microphone",
            why = "Used by the capability probe's capture tests, and by the " +
                "microphone relay in Phase 2. Nothing records audio in the " +
                "background, and nothing records unless you start a test.",
            withoutIt = "Capture and duplex tests are skipped and the phone reports " +
                "audio.capture as unavailable. The control channel is unaffected.",
        ),
        Item(
            permission = Manifest.permission.BLUETOOTH_CONNECT,
            title = "Nearby devices (Android 12+)",
            why = "Reads whether a Bluetooth headset is connected and which " +
                "profiles it offers — the facts Phase 2 depends on.",
            withoutIt = "Bluetooth details are reported as unavailable; everything " +
                "else works.",
            minSdk = 31,
        ),
    )

    /**
     * Permissions the roadmap will need, shown as "not requested yet" so the UI
     * never implies a capability the app has not implemented.
     */
    val futurePermissions: List<Pair<String, String>> = listOf(
        "android.permission.CAMERA" to "camera streaming (Phase 4)",
        "android.permission.BLUETOOTH_SCAN" to "Bluetooth device scanning (Phase 5)",
        "android.permission.ACCESS_FINE_LOCATION" to "GPS as a sensor (Phase 5)",
    )

    // ------------------------------------------------------------------ //
    // state
    // ------------------------------------------------------------------ //

    fun applicable(item: Item): Boolean = Build.VERSION.SDK_INT >= item.minSdk

    fun granted(context: Context, permission: String): Boolean =
        context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED

    fun status(context: Context, item: Item): Status {
        if (!applicable(item)) return Status.NOT_APPLICABLE
        if (granted(context, item.permission)) {
            // Granted again from Settings: forget the old "blocked" verdict.
            if (blocked(context, item.permission)) setFlag(context, BLOCKED_PREFIX, item.permission, false)
            return Status.GRANTED
        }
        val activity = context as? Activity
        if (activity != null) {
            // Only an Activity can ask the system whether the dialog is still
            // available; remember the answer for the service, which cannot.
            val isBlocked = wasAsked(context, item.permission) &&
                !activity.shouldShowRequestPermissionRationale(item.permission)
            setFlag(context, BLOCKED_PREFIX, item.permission, isBlocked)
            return if (isBlocked) Status.PERMANENTLY_DENIED else Status.DENIED
        }
        return if (blocked(context, item.permission)) Status.PERMANENTLY_DENIED else Status.DENIED
    }

    fun snapshot(context: Context): List<Pair<Item, Status>> =
        runtime.map { it to status(context, it) }

    /** Wire form for `system.info.permissions` — strings, JSON-safe. */
    fun wireSnapshot(context: Context): Map<String, Any?> =
        snapshot(context).associate { (item, status) -> item.shortName to status.name.lowercase() }


    fun wasAsked(context: Context, permission: String): Boolean =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getBoolean(KEY_PREFIX + permission, false)

    fun markAsked(context: Context, permission: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putBoolean(KEY_PREFIX + permission, true).apply()
    }

    /** Last known "the system will not show a dialog any more" verdict. */
    fun blocked(context: Context, permission: String): Boolean =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getBoolean(BLOCKED_PREFIX + permission, false)

    private fun setFlag(context: Context, prefix: String, permission: String, value: Boolean) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putBoolean(prefix + permission, value).apply()
    }

    /** What the capability probe would like; the probe runs either way. */
    fun probePermissions(): List<String> = buildList {
        add(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            add(Manifest.permission.BLUETOOTH_CONNECT)
        }
    }

    fun missingProbePermissions(context: Context): List<String> =
        probePermissions().filter { !granted(context, it) }

    /**
     * Send the user to this app's system page, where a permanently denied
     * permission can be re-enabled. There is no in-app path for that by design.
     */
    fun openAppSettings(context: Context) {
        val intent = Intent(
            Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
            Uri.fromParts("package", context.packageName, null),
        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
    }

    /** One-line explanation for a denied permission, used in error messages. */
    fun explainDenial(item: Item, status: Status): String = when (status) {
        Status.GRANTED -> "${item.shortName} granted"
        Status.NOT_APPLICABLE -> "${item.shortName} not needed on this Android version"
        Status.DENIED -> "${item.shortName} denied — ${item.withoutIt}"
        Status.PERMANENTLY_DENIED ->
            "${item.shortName} is blocked. Android will not ask again; enable it in " +
                "Settings › Apps › PhoneBridge › Permissions."
    }
}
