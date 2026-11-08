package dev.phonebridge.net

import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build

/**
 * Read-only audio-routing inspection.
 *
 * This exists because the whole Bluetooth-relay question hinges on *what the
 * platform is actually offering right now*, and guessing is not acceptable.  It
 * never changes a route: Phase 2 owns the SCO handshake.  Everything here is
 * reported to the PC through `system.info` / `bluetooth.headset.status` so the
 * dashboard shows facts instead of assumptions.
 *
 * Facts this surfaces:
 *  - a headset present as `TYPE_BLUETOOTH_SCO` means the platform will accept a
 *    *bidirectional* HFP/SCO link (that is the only way to get the headset mic);
 *  - a headset present only as `TYPE_BLUETOOTH_A2DP` / `TYPE_BLE_HEADSET` means
 *    output-only or LE Audio, which changes what Phase 2 can promise.
 */
object AudioProbe {

    private fun audioManager(context: Context) =
        context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

    fun communicationDevices(context: Context): List<AudioDeviceInfo> =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            audioManager(context).availableCommunicationDevices
        } else {
            // Pre-12: no communication-device API, so approximate from outputs.
            audioManager(context).getDevices(AudioManager.GET_DEVICES_OUTPUTS).filter {
                it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO ||
                    it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP
            }
        }

    fun headsetConnected(context: Context): Boolean =
        communicationDevices(context).any { it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO }

    fun bluetoothProfileName(context: Context): String {
        val device = communicationDevices(context).firstOrNull() ?: return "none"
        return typeName(device.type)
    }

    fun describeCommunicationDevices(context: Context): List<Map<String, Any?>> =
        communicationDevices(context).map { device ->
            mapOf(
                "id" to device.id,
                "type" to typeName(device.type),
                "type_id" to device.type,
                "product" to device.productName?.toString()?.ifBlank { null },
                "is_sink" to device.isSink,
                "is_source" to device.isSource,
                "sample_rates" to device.sampleRates.toList(),
            )
        }

    fun typeName(type: Int): String = when (type) {
        AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "builtin_speaker"
        AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "builtin_earpiece"
        AudioDeviceInfo.TYPE_BUILTIN_MIC -> "builtin_mic"
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "bluetooth_sco"   // HFP, bidirectional
        AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> "bluetooth_a2dp" // output only
        AudioDeviceInfo.TYPE_BLE_HEADSET -> "ble_headset"
        AudioDeviceInfo.TYPE_BLE_SPEAKER -> "ble_speaker"
        AudioDeviceInfo.TYPE_USB_DEVICE -> "usb_device"
        AudioDeviceInfo.TYPE_USB_HEADSET -> "usb_headset"
        AudioDeviceInfo.TYPE_WIRED_HEADSET -> "wired_headset"
        else -> "other_$type"
    }
}
