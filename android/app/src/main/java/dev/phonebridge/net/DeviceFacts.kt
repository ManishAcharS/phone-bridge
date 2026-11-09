package dev.phonebridge.net

import android.content.Context
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorManager
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.media.AudioManager
import android.os.Build
import android.util.Log
import dev.phonebridge.protocol.Framing
import java.net.Inet4Address
import java.net.NetworkInterface

/**
 * Facts about this phone, measured rather than assumed.
 *
 * Two questions are kept strictly apart, because conflating them is how a
 * capability report starts lying:
 *
 *  * **hardware** — does this device physically have it? (camera, sensors,
 *    speakers, microphone, Bluetooth radio). Read from the platform, no
 *    permission needed for mere presence.
 *  * **available** — can PhoneBridge v1 actually *use* it today? That requires
 *    the feature to be implemented *and*, where relevant, the runtime permission
 *    to be granted.
 *
 * `capabilities()` reports `available`. Today that is `false` for everything
 * except the protocol itself, because the audio relay (Phase 2), camera
 * streaming (Phase 4) and file transfer (Phase 5) do not exist yet. Reporting
 * what the hardware *could* do is fine — as long as it is labelled as hardware.
 * The previous version hard-coded `camera.back`, `camera.front`, `sensors` and
 * `file_transfer` to `true`, which a PC had no way to tell apart from a working
 * feature.
 *
 * Everything here is read-only and side-effect free: no camera is opened, no
 * sensor is registered, no audio route is changed.
 */
object DeviceFacts {

    private const val TAG = "PhoneBridgeFacts"

    /**
     * One capability, as shown in the app and reported to the PC.
     *
     * @param key the wire key (kept stable for the PC and for docs/03).
     * @param hardware whether the device has the necessary hardware, or null
     *   when the platform would not say (e.g. camera enumeration denied).
     * @param implemented whether PhoneBridge has the feature today.
     * @param note why it is unavailable, or what it still needs.
     */
    data class CapabilityRow(
        val key: String,
        val label: String,
        val hardware: Boolean?,
        val implemented: Boolean,
        val phase: Int?,
        val note: String,
        val permission: String? = null,
    ) {
        /** Usable through the bridge *today*, given hardware, code and permission. */
        fun available(permissionGranted: Boolean): Boolean =
            hardware == true && implemented && (permission == null || permissionGranted)

        /** One human sentence for the UI and for `capabilities_detail`. */
        fun summary(permissionGranted: Boolean): String = when {
            available(permissionGranted) -> "available"
            hardware == false -> "not available: this device has no $label"
            hardware == null -> "unknown: the platform did not report $label"
            permission != null && !permissionGranted ->
                "present, but the ${permission.substringAfterLast('.')} permission is not granted"
            !implemented && phase != null -> "present, not implemented yet (Phase $phase) — $note"
            !implemented -> "present, not implemented yet — $note"
            else -> "unavailable — $note"
        }
    }

    // ------------------------------------------------------------------ //
    // device / app identity
    // ------------------------------------------------------------------ //

    /**
     * The device block used by `system.info` and by the "This device" card.
     *
     * `model`, `manufacturer`, `android_version`, `sdk_int` and `app_version` are
     * kept because `system.info` already published them; the rest is additive.
     */
    fun deviceInfo(context: Context): Map<String, Any?> = linkedMapOf(
        "model" to Build.MODEL,
        "manufacturer" to Build.MANUFACTURER,
        "brand" to Build.BRAND,
        "device" to Build.DEVICE,
        "product" to Build.PRODUCT,
        "hardware" to Build.HARDWARE,
        "android_release" to Build.VERSION.RELEASE,
        "android_version" to Build.VERSION.RELEASE,
        "sdk_int" to Build.VERSION.SDK_INT,
        "target_sdk" to context.applicationInfo.targetSdkVersion,
        "app_version" to appVersion(context),
        "app_version_code" to appVersionCode(context),
        "package" to context.packageName,
        "rom" to romName(),
        "rom_version" to romVersion(),
        "protocol_version" to Framing.PROTOCOL_VERSION,
        "is_emulator" to isProbablyEmulator(),
    )

    /** e.g. "MIUI 14" / "HyperOS 1.0" / "unknown" — never guessed from the brand. */
    fun romName(): String {
        val hyper = systemProperty("ro.mi.os.version.name")
        val miui = systemProperty("ro.miui.ui.version.name")
        return when {
            !hyper.isNullOrBlank() -> "HyperOS"
            !miui.isNullOrBlank() -> "MIUI"
            Build.MANUFACTURER.equals("Xiaomi", ignoreCase = true) -> "MIUI/HyperOS (unresolved)"
            else -> "unknown"
        }
    }

    fun romVersion(): String? =
        systemProperty("ro.mi.os.version.name") ?: systemProperty("ro.miui.ui.version.name")

    fun appVersion(context: Context): String = try {
        val info = context.packageManager.getPackageInfo(context.packageName, 0)
        info.versionName ?: "unknown"
    } catch (e: Exception) {
        Log.w(TAG, "cannot read app version: ${e.javaClass.simpleName}: ${e.message}")
        "unknown"
    }

    fun appVersionCode(context: Context): Long = try {
        val info = context.packageManager.getPackageInfo(context.packageName, 0)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            info.longVersionCode
        } else {
            @Suppress("DEPRECATION")
            info.versionCode.toLong()
        }
    } catch (e: Exception) {
        Log.w(TAG, "cannot read app version code: ${e.javaClass.simpleName}: ${e.message}")
        0L
    }

    fun isProbablyEmulator(): Boolean {
        val fingerprint = Build.FINGERPRINT.lowercase()
        return fingerprint.startsWith("generic") ||
            fingerprint.contains("emulator") ||
            Build.MODEL.contains("sdk", ignoreCase = true) ||
            Build.PRODUCT.contains("sdk", ignoreCase = true) ||
            Build.HARDWARE.contains("goldfish") ||
            Build.HARDWARE.contains("ranchu")
    }

    /** Best-effort `android.os.SystemProperties` read; blocked on newer builds. */
    private fun systemProperty(key: String): String? = try {
        val clazz = Class.forName("android.os.SystemProperties")
        val getter = clazz.getMethod("get", String::class.java)
        (getter.invoke(null, key) as? String)?.takeIf { it.isNotBlank() }
    } catch (e: Throwable) {
        null
    }

    // ------------------------------------------------------------------ //
    // capabilities
    // ------------------------------------------------------------------ //

    /**
     * Every capability the PC can ask about, with hardware *and* implementation
     * status kept separate.
     */
    fun capabilityRows(context: Context): List<CapabilityRow> {
        val cameras = cameraFacing(context)
        val sensors = sensorNames(context)
        val audio = audioDevices(context)
        val bluetoothRadio = hasAnyFeature(
            context,
            PackageManager.FEATURE_BLUETOOTH,
            PackageManager.FEATURE_BLUETOOTH_LE,
        )
        val headset = AudioProbe.headsetConnected(context)

        return listOf(
            CapabilityRow(
                key = "audio.playback",
                label = "audio playback",
                hardware = audio.outputs > 0,
                implemented = false,
                phase = 2,
                note = "playing PC audio on this phone is the Phase 2 relay",
            ),
            CapabilityRow(
                key = "audio.capture",
                label = "microphone",
                hardware = audio.inputs > 0,
                implemented = false,
                phase = 2,
                note = "the microphone relay lands with Phase 2",
                permission = android.Manifest.permission.RECORD_AUDIO,
            ),
            CapabilityRow(
                key = "audio.bluetooth_sco",
                label = "Bluetooth headset route",
                hardware = headset,
                implemented = false,
                phase = 2,
                note = "an HFP/SCO link can only be opened by the relay (Phase 2)",
            ),
            CapabilityRow(
                key = "camera.back",
                label = "rear camera",
                hardware = cameras.back,
                implemented = false,
                phase = 4,
                note = "camera streaming is Phase 4",
            ),
            CapabilityRow(
                key = "camera.front",
                label = "front camera",
                hardware = cameras.front,
                implemented = false,
                phase = 4,
                note = "camera streaming is Phase 4",
            ),
            CapabilityRow(
                key = "sensors",
                label = "sensors",
                hardware = sensors.isNotEmpty(),
                implemented = false,
                phase = 5,
                note = if (sensors.isEmpty()) "none reported" else "present: ${sensors.joinToString(", ")}",
            ),
            CapabilityRow(
                key = "file_transfer",
                label = "file transfer",
                hardware = true, // storage is what it is; the feature is what is missing
                implemented = false,
                phase = 5,
                note = "chunked transfer with checksums is Phase 5",
            ),
            CapabilityRow(
                key = "bluetooth_scan",
                label = "Bluetooth scanning",
                hardware = bluetoothRadio,
                implemented = false,
                phase = 5,
                note = "BLE scan and GATT are Phase 5",
            ),
        )
    }

    /**
     * The flat map published in `system.info` and `phonebridge info`.
     *
     * Values stay booleans/strings so an older PC reads them unchanged. New keys
     * are additive: `hardware.<key>` says what the device has, and
     * `detail.<key>` says why a capability is not available yet.
     */
    fun capabilities(context: Context): Map<String, Any?> {
        val granted = android.Manifest.permission.RECORD_AUDIO.let { permission ->
            context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED
        }
        val out = linkedMapOf<String, Any?>()
        for (row in capabilityRows(context)) {
            out[row.key] = row.available(granted)
            out["hardware.${row.key}"] = row.hardware
            out["detail.${row.key}"] = row.summary(granted)
        }
        // Existing string-valued key (the PC prints it as the active profile).
        out["audio.bluetooth_profile"] = AudioProbe.bluetoothProfileName(context)
        out["protocol_version"] = Framing.PROTOCOL_VERSION
        return out
    }

    // ------------------------------------------------------------------ //
    // hardware probes (read-only)
    // ------------------------------------------------------------------ //

    private data class Cameras(val back: Boolean?, val front: Boolean?)

    private fun cameraFacing(context: Context): Cameras {
        val manager = context.getSystemService(Context.CAMERA_SERVICE) as? CameraManager
        if (manager != null) {
            try {
                var back = false
                var front = false
                var seen = false
                for (id in manager.cameraIdList) {
                    seen = true
                    when (manager.getCameraCharacteristics(id).get(CameraCharacteristics.LENS_FACING)) {
                        CameraCharacteristics.LENS_FACING_BACK -> back = true
                        CameraCharacteristics.LENS_FACING_FRONT -> front = true
                    }
                }
                if (seen) return Cameras(back = back, front = front)
            } catch (e: Exception) {
                // CameraAccessException / SecurityException: fall through to the
                // feature flags rather than claiming a camera we cannot see.
                Log.w(TAG, "camera enumeration failed: ${e.javaClass.simpleName}: ${e.message}")
            }
        }
        val features = context.packageManager
        val any = features.hasSystemFeature(PackageManager.FEATURE_CAMERA_ANY)
        if (!any) return Cameras(back = false, front = false)
        // FEATURE_CAMERA means a rear-facing camera; not every device sets it.
        return Cameras(
            back = features.hasSystemFeature(PackageManager.FEATURE_CAMERA) || any,
            front = features.hasSystemFeature(PackageManager.FEATURE_CAMERA_FRONT),
        )
    }

    private fun sensorNames(context: Context): List<String> {
        val manager = context.getSystemService(Context.SENSOR_SERVICE) as? SensorManager ?: return emptyList()
        return try {
            manager.getSensorList(Sensor.TYPE_ALL)
                .mapNotNull { it.name }
                .distinct()
                .sorted()
        } catch (e: Exception) {
            Log.w(TAG, "sensor enumeration failed: ${e.javaClass.simpleName}: ${e.message}")
            emptyList()
        }
    }

    private data class AudioDevices(val outputs: Int, val inputs: Int)

    private fun audioDevices(context: Context): AudioDevices {
        val manager = context.getSystemService(Context.AUDIO_SERVICE) as? AudioManager
            ?: return AudioDevices(0, 0)
        return try {
            AudioDevices(
                outputs = manager.getDevices(AudioManager.GET_DEVICES_OUTPUTS).size,
                inputs = manager.getDevices(AudioManager.GET_DEVICES_INPUTS).size,
            )
        } catch (e: Exception) {
            Log.w(TAG, "audio device enumeration failed: ${e.javaClass.simpleName}: ${e.message}")
            AudioDevices(0, 0)
        }
    }

    private fun hasAnyFeature(context: Context, vararg features: String): Boolean =
        features.any { context.packageManager.hasSystemFeature(it) }

    // ------------------------------------------------------------------ //
    // network
    // ------------------------------------------------------------------ //

    /**
     * First site-local IPv4 address, so the UI can show the exact URL to type
     * on the PC. Pure `NetworkInterface` enumeration: no permission, no Wi-Fi
     * manager, nothing that would need ACCESS_WIFI_STATE.
     */
    fun lanIpv4(): String? = try {
        NetworkInterface.getNetworkInterfaces().toList()
            .filter { it.isUp && !it.isLoopback }
            .flatMap { it.inetAddresses.toList() }
            .filterIsInstance<Inet4Address>()
            .firstOrNull { it.isSiteLocalAddress }
            ?.hostAddress
    } catch (e: Exception) {
        Log.w(TAG, "interface enumeration failed: ${e.javaClass.simpleName}: ${e.message}")
        null
    }
}
