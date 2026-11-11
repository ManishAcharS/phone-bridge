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
 * CAMERA arrived in Phase 4 with a real use, and Phase 5 added BLUETOOTH_SCAN
 * (scanning for peripherals) and ACCESS_FINE_LOCATION (the same job on Android
 * 11 and older, where Android had not split Bluetooth permissions yet). Nothing
 * else is declared: holding a permission the app cannot use is exactly the
 * dishonesty this project keeps removing. [futurePermissions] stays as the place
 * to say *why* a permission the roadmap mentions is still absent.
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
        Item(
            permission = Manifest.permission.BLUETOOTH_SCAN,
            title = "Nearby devices: scan (Android 12+)",
            why = "Phase 5 uses it to scan for Bluetooth LE peripherals when a " +
                "paired PC asks. Scanning never connects to anything by itself, and " +
                "the PC must name a device before a link is opened.",
            withoutIt = "The PC's scan button reports 'scan permission not granted' " +
                "and the phone reports bluetooth_scan as unavailable. Everything " +
                "else works.",
            minSdk = 31,
        ),
        Item(
            permission = Manifest.permission.ACCESS_FINE_LOCATION,
            title = "Location (Android 11 and older)",
            why = "Android 11 and older require the location permission before an app " +
                "may see Bluetooth scan results; Phase 5 scans for peripherals there. " +
                "PhoneBridge never reads your location and never asks for it on " +
                "Android 12+.",
            withoutIt = "On Android 11 and older, Bluetooth scanning is unavailable. " +
                "Nothing else changes.",
            minSdk = 23,
        ),
        Item(
            permission = Manifest.permission.CAMERA,
            title = "Camera",
            why = "Streams frames to the PC, and only after the PC asks with " +
                "camera.start while a paired session is open. The camera is " +
                "never opened at launch, in the background, or by a stranger.",
            withoutIt = "The PC's camera stop button reports 'camera permission not " +
                "granted' and the phone reports camera.stream as unavailable. " +
                "Everything else works.",
        ),
    )

    /**
     * Permissions the roadmap will need, shown as "not requested yet" so the UI
     * never implies a capability the app has not implemented.
     */
    val futurePermissions: List<Pair<String, String>> = listOf(
        // Nothing is waiting for a future phase right now: Phase 5 shipped with
        // every permission it needs. The list stays so the UI keeps a place to
        // explain a permission the roadmap mentions but this build does not use.
        "android.permission.ACCESS_BACKGROUND_LOCATION" to
            "not used: the bridge only works while a PC is connected",
    )

    // ------------------------------------------------------------------ //
    // state
    // ------------------------------------------------------------------ //

    fun applicable(item: Item): Boolean = Build.VERSION.SDK_INT >= item.minSdk

    /**
     * The permission a feature must actually hold, given the API level.
     *
     * Android 12 split Bluetooth permissions in two (`BLUETOOTH_SCAN` and
     * `BLUETOOTH_CONNECT`); before that, *both* jobs were covered by
     * `ACCESS_FINE_LOCATION`. Resolving that here means call sites ask one
     * question — "may I scan?" — instead of re-implementing a version check and
     * getting it wrong on one of the two paths.
     */
    fun effective(context: Context, permission: String): String = when {
        permission == Manifest.permission.BLUETOOTH_SCAN && Build.VERSION.SDK_INT < Build.VERSION_CODES.S ->
            Manifest.permission.ACCESS_FINE_LOCATION
        permission == Manifest.permission.BLUETOOTH_CONNECT && Build.VERSION.SDK_INT < Build.VERSION_CODES.S ->
            // On API <= 30 adapter and device names need BLUETOOTH (install-time),
            // and scanning needs location; there is no runtime permission to ask for.
            Manifest.permission.BLUETOOTH
        else -> permission
    }

    /** True when the effective permission is granted *and* actually applicable. */
    fun effectiveGranted(context: Context, permission: String): Boolean =
        granted(context, effective(context, permission))

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
        add(Manifest.permission.CAMERA)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            add(Manifest.permission.BLUETOOTH_CONNECT)
        }
    }

    /**
     * What the Phase 5 Bluetooth features need, resolved for this API level.
     *
     * Returned as separate lists because a scan and a GATT connection are
     * different powers: a user who allowed scanning has not automatically
     * allowed connecting, and the UI asks for exactly what the button needs.
     */
    fun bluetoothScanPermission(): String = Manifest.permission.BLUETOOTH_SCAN

    fun bluetoothConnectPermission(): String = Manifest.permission.BLUETOOTH_CONNECT

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
