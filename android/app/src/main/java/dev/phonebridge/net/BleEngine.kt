package dev.phonebridge.net

import android.Manifest
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import android.os.SystemClock
import android.util.Base64
import android.util.Log
import dev.phonebridge.Permissions
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger

/**
 * A Bluetooth refusal the PC can show verbatim. It *is* an [RpcFailure], so
 * `BridgeServer` turns it into the documented code (`no_bluetooth`,
 * `bluetooth_off`, `bluetooth_denied`, `gatt_failed`, `not_connected`).
 */
class BleFailure(code: String, message: String) : RpcFailure(code, message)

/**
 * Phase 5 Bluetooth: the phone is the radio, the PC is the remote control.
 *
 * Six rules, each of which exists because the alternative is a security or
 * privacy problem:
 *
 *  1. **Nothing is connected to on its own.** Scanning is read-only and only
 *     starts when the PC asks. A GATT link is opened only by
 *     `bluetooth.gatt.connect` with an address the PC named, which itself came
 *     from a scan the *user* started. There is no "connect to anything that
 *     looks interesting", no pairing automation and no bond manipulation.
 *  2. **Only what the operation needs.** Each entry point checks its own
 *     permission (`BLUETOOTH_SCAN` for scanning, `BLUETOOTH_CONNECT` for GATT) and
 *     fails with `bluetooth_denied` rather than asking the platform and being
 *     denied silently.
 *  3. **Android 12 split the permission.** On API 31+ scanning and connecting are
 *     separate permissions; on API <= 30 both are `ACCESS_FINE_LOCATION`. Both
 *     spellings are resolved in one place ([Permissions.effective]).
 *  4. **Bound everything.** At most [MAX_DEVICES] scan results, at most
 *     [MAX_NOTIFICATIONS] notifications per link, at most [MAX_SUBSCRIPTIONS]
 *     subscriptions per link, every GATT operation under a timeout.
 *  5. **Time out and say so.** The platform can swallow a GATT callback (a
 *     peripheral out of range); a request therefore fails with a sentence instead
 *     of hanging the PC's GUI.
 *  6. **Clean up.** Disconnect drops the `BluetoothGatt` (`close()`), stops the
 *     scan, and clears notification buffers. The last session ending does the
 *     same, so no radio work outlives the window that asked for it.
 *
 * Device names and addresses are only read when the permission allows it; on
 * Android 12+ a denied `BLUETOOTH_CONNECT` yields an unnamed, unaddressable entry
 * rather than a crash or a hidden device.
 */
class BleEngine(
    private val context: Context,
    private val emit: (event: String, data: Map<String, Any?>) -> Unit,
    private val log: (level: String, message: String) -> Unit,
) {

    companion object {
        const val MAX_DEVICES = 200
        const val MAX_NOTIFICATIONS = 200
        const val MAX_SUBSCRIPTIONS = 32
        const val MAX_VALUE_BYTES = 512

        /** GATT operations that never answer must not outlive this. */
        const val GATT_TIMEOUT_MS = 15_000L

        /** Not a platform status: the operation was abandoned by us. */
        const val ABORTED = -1

        /** At most this many scan events per second, however busy the room is. */
        const val MAX_SCAN_EVENTS_PER_SECOND = 20

        /** Requested ATT MTU after connecting; 23 is the minimum every stack has. */
        const val REQUESTED_MTU = 517

        /** A scan with no duration is capped, so a forgotten stop cannot scan forever. */
        const val MAX_SCAN_MS = 120_000L
        const val DEFAULT_SCAN_MS = 10_000L

        private const val TAG = "PhoneBridgeBle"
        private val CCCD = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    }

    private data class Device(
        val address: String,
        val name: String,
        val rssi: Int,
        val serviceUuids: List<String>,
        val connectable: Boolean,
        val firstSeenMs: Long,
        var lastSeenMs: Long,
        var sightings: Int,
    ) {
        fun asMap(): Map<String, Any?> = linkedMapOf(
            "address" to address,
            "name" to name,
            "rssi" to rssi,
            "service_uuids" to serviceUuids,
            "connectable" to connectable,
            "first_seen_ms" to firstSeenMs,
            "last_seen_ms" to lastSeenMs,
            "sightings" to sightings,
        )
    }

    private data class Link(
        val address: String,
        // Written by the GATT callback thread and read by a worker waiting for it,
        // so it must be volatile: without that the wait loop is a data race whose
        // exit depends on the JIT.
        @Volatile var state: String = "connecting",
        var gatt: BluetoothGatt? = null,
        var error: String = "",
        val subscriptions: MutableList<String> = ArrayList(),
        val notifications: ArrayDeque<Map<String, Any?>> = ArrayDeque(),
        val services: MutableList<Map<String, Any?>> = ArrayList(),
        var mtu: Int = 23,
    )

    private val manager: BluetoothManager? =
        context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager

    private val adapter: BluetoothAdapter? get() = manager?.adapter

    private val handler = Handler(Looper.getMainLooper())
    private val devices = ConcurrentHashMap<String, Device>()
    private val links = ConcurrentHashMap<String, Link>()

    @Volatile
    private var scanning = false
    private var scanCallback: ScanCallback? = null
    private var scanStopRunnable: Runnable? = null
    private var scanStartedAt = 0L
    private var scanDuration = 0L
    private var scanFilterUuids: List<String> = emptyList()

    /** Only devices whose advertised name starts with this are reported. */
    @Volatile
    private var namePrefix = ""

    //: A rate limit on `bluetooth.device` events: a crowded room would otherwise
    //: push thousands of messages a second down the same channel as the RPCs.
    private var eventWindowStartedAt = 0L
    private var eventWindowCount = 0

    // ------------------------------------------------------------------ //
    // adapter
    // ------------------------------------------------------------------ //
    fun hasRadio(): Boolean = try {
        context.packageManager.hasSystemFeature(PackageManager.FEATURE_BLUETOOTH_LE) ||
            context.packageManager.hasSystemFeature(PackageManager.FEATURE_BLUETOOTH) ||
            adapter != null
    } catch (e: Exception) {
        false
    }

    fun hasLe(): Boolean = try {
        context.packageManager.hasSystemFeature(PackageManager.FEATURE_BLUETOOTH_LE)
    } catch (e: Exception) {
        false
    }

    fun isEnabled(): Boolean = try {
        adapter?.isEnabled == true
    } catch (e: SecurityException) {
        false
    }

    fun name(): String = try {
        if (Permissions.granted(context, Permissions.effective(context, Manifest.permission.BLUETOOTH_CONNECT))) {
            adapter?.name ?: ""
        } else {
            ""
        }
    } catch (e: SecurityException) {
        ""
    }

    /** The address is not readable on Android 12+ without the connect permission. */
    fun address(): String = try {
        if (Permissions.granted(context, Permissions.effective(context, Manifest.permission.BLUETOOTH_CONNECT))) {
            adapter?.address ?: ""
        } else {
            ""
        }
    } catch (e: SecurityException) {
        ""
    }

    private fun scanPermitted(): Boolean =
        Permissions.granted(context, Permissions.effective(context, Manifest.permission.BLUETOOTH_SCAN))

    private fun connectPermitted(): Boolean =
        Permissions.granted(context, Permissions.effective(context, Manifest.permission.BLUETOOTH_CONNECT))

    fun status(): Map<String, Any?> {
        val supported = hasRadio()
        val enabled = supported && isEnabled()
        val permitted = if (supported) scanPermitted() else true
        val detail = when {
            !supported -> "this device has no Bluetooth LE radio"
            !enabled -> "Bluetooth is switched off on the phone"
            !permitted -> "the Bluetooth scan permission is not granted"
            !hasLe() -> "this device has Bluetooth but not Bluetooth LE"
            else -> ""
        }
        return linkedMapOf(
            "supported" to supported,
            "enabled" to enabled,
            "le_supported" to hasLe(),
            "name" to name(),
            "address" to address(),
            "permitted" to permitted,
            "detail" to detail,
        )
    }

    /** Refuse early, with the sentence the user needs, before touching the radio. */
    private fun requireScanReady(): BluetoothAdapter {
        if (!hasRadio()) throw BleFailure("no_bluetooth", "this device has no Bluetooth LE radio")
        if (!scanPermitted()) {
            throw BleFailure(
                "bluetooth_denied",
                "the Bluetooth scan permission is not granted on the phone",
            )
        }
        val radio = adapter ?: throw BleFailure("no_bluetooth", "this device has no Bluetooth adapter")
        if (!radio.isEnabled) throw BleFailure("bluetooth_off", "Bluetooth is switched off on the phone")
        return radio
    }

    private fun requireConnectReady(): BluetoothAdapter {
        if (!hasRadio()) throw BleFailure("no_bluetooth", "this device has no Bluetooth LE radio")
        if (!connectPermitted()) {
            throw BleFailure(
                "bluetooth_denied",
                "the Bluetooth connect permission is not granted on the phone",
            )
        }
        val radio = adapter ?: throw BleFailure("no_bluetooth", "this device has no Bluetooth adapter")
        if (!radio.isEnabled) throw BleFailure("bluetooth_off", "Bluetooth is switched off on the phone")
        return radio
    }

    // ------------------------------------------------------------------ //
    // scanning
    // ------------------------------------------------------------------ //
    fun startScan(params: Map<*, *>): Map<String, Any?> {
        val radio = requireScanReady()
        if (scanning) {
            // A second start is a *new* scan, not a no-op: the old callback and its
            // timer are released first so a repeated Start cannot stack scans.
            runCatching { stopScan() }
        }
        val duration = ((params["duration_ms"] as? Number)?.toLong() ?: DEFAULT_SCAN_MS)
            .coerceIn(0L, MAX_SCAN_MS)
        val requested = (params["filter_uuids"] as? List<*>).orEmpty()
        val filters = ArrayList<UUID>(requested.size)
        for (raw in requested) {
            val uuid = runCatching { UUID.fromString(raw.toString()) }.getOrNull()
                ?: throw BleFailure("bad_params", "'$raw' is not a UUID to filter on")
            filters.add(uuid)
        }
        // A filter that is silently ignored shows the user every device in the room
        // while claiming to filter, so an unusable one is an error, not a shrug.
        val prefix = (params["name_prefix"] as? String)?.trim().orEmpty()
        namePrefix = prefix
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .setCallbackType(ScanSettings.CALLBACK_TYPE_ALL_MATCHES)
            .setMatchMode(ScanSettings.MATCH_MODE_AGGRESSIVE)
            .build()
        val scanFilters = filters.map { uuid ->
            ScanFilter.Builder().setServiceUuid(ParcelUuid(uuid)).build()
        }

        val callback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                record(result)
            }

            override fun onBatchScanResults(results: MutableList<ScanResult>) {
                for (result in results) record(result)
            }

            override fun onScanFailed(errorCode: Int) {
                scanning = false
                log("WARN", "BLE scan failed with error code $errorCode")
                emit("bluetooth.scan", mapOf("state" to "error", "detail" to "scan failed ($errorCode)"))
            }
        }
        scanCallback = callback
        devices.clear()
        scanFilterUuids = filters.map { it.toString() }
        val scanner = radio.bluetoothLeScanner
            ?: throw BleFailure("no_bluetooth", "this phone has no Bluetooth LE scanner")
        try {
            scanner.startScan(scanFilters, settings, callback)
        } catch (e: SecurityException) {
            scanCallback = null
            throw BleFailure("bluetooth_denied", "the scan permission was revoked: ${e.message}")
        } catch (e: IllegalStateException) {
            scanCallback = null
            throw BleFailure("bluetooth_off", "the Bluetooth stack is not ready: ${e.message}")
        }
        scanning = true
        val startedAt = SystemClock.elapsedRealtime()
        scanStartedAt = startedAt
        scanDuration = duration
        eventWindowStartedAt = startedAt
        eventWindowCount = 0
        if (duration > 0) {
            val stop = Runnable { runCatching { stopScan() } }
            scanStopRunnable = stop
            handler.postDelayed(stop, duration)
        }
        emit("bluetooth.scan", mapOf("state" to "scanning", "detail" to ""))
        log(
            "INFO",
            "BLE scan started for ${if (duration > 0) "${duration}ms" else "until stopped"}" +
                if (prefix.isEmpty()) "" else ", names starting with '$prefix'",
        )
        return linkedMapOf(
            "ok" to true,
            "scanning" to true,
            "started_at_ms" to startedAt,
            "duration_ms" to duration,
        )
    }

    fun stopScan(): Map<String, Any?> {
        val wasScanning = scanning
        scanning = false
        scanStopRunnable?.let { handler.removeCallbacks(it) }
        scanStopRunnable = null
        val callback = scanCallback
        scanCallback = null
        if (wasScanning && callback != null) {
            try {
                adapter?.bluetoothLeScanner?.stopScan(callback)
            } catch (e: SecurityException) {
                log("WARN", "stopping the scan needed a permission we no longer hold: ${e.message}")
            } catch (e: Exception) {
                log("WARN", "stopping the scan failed: ${e.message}")
            }
        }
        if (wasScanning) {
            emit("bluetooth.scan", mapOf("state" to "stopped", "detail" to ""))
            log("INFO", "BLE scan stopped after ${devices.size} device(s)")
        }
        return linkedMapOf("ok" to true, "scanning" to false, "devices" to devicesList())
    }

    private fun record(result: ScanResult) {
        if (!scanning) return
        val device: BluetoothDevice = result.device ?: return
        // `address` needs BLUETOOTH_CONNECT on API 31+: an entry without an address
        // is honest (the radio saw something), an exception is not.
        val address = try {
            device.address ?: ""
        } catch (e: SecurityException) {
            ""
        }
        val name = try {
            device.name ?: result.scanRecord?.deviceName ?: ""
        } catch (e: SecurityException) {
            result.scanRecord?.deviceName ?: ""
        }
        val services = result.scanRecord?.serviceUuids
            ?.mapNotNull { it?.uuid?.toString() }
            ?: emptyList()
        val now = android.os.SystemClock.elapsedRealtime()
        val key = address.ifEmpty { "anon:${device.hashCode()}" }
        val existing = devices[key]
        val merged = if (existing == null) {
            Device(
                address = address,
                name = name,
                rssi = result.rssi,
                serviceUuids = services,
                connectable = result.isConnectable,
                firstSeenMs = now,
                lastSeenMs = now,
                sightings = 1,
            )
        } else {
            existing.copy(
                name = name.ifEmpty { existing.name },
                rssi = result.rssi,
                serviceUuids = (existing.serviceUuids + services).distinct(),
                connectable = existing.connectable || result.isConnectable,
                lastSeenMs = now,
                sightings = existing.sightings + 1,
            )
        }
        if (existing == null && devices.size >= MAX_DEVICES) {
            log("WARN", "ignoring a scan result: the $MAX_DEVICES device cap is reached")
            return
        }
        devices[key] = merged
        if (!acceptsByName(merged.name)) return
        if (!emitScanEvent()) return
        emit("bluetooth.device", merged.asMap())
    }

    /** The documented `name_prefix` filter, applied as devices are seen. */
    private fun acceptsByName(name: String): Boolean {
        val prefix = namePrefix
        if (prefix.isEmpty()) return true
        return name.isNotEmpty() && name.startsWith(prefix, ignoreCase = true)
    }

    /**
     * Rate limit for `bluetooth.device` events.
     *
     * A room full of advertising peripherals can produce thousands of sightings a
     * second; every one of them is a JSON message on the control channel, which is
     * also carrying the RPC replies and heartbeats. Dropping the *surplus events*
     * loses nothing: the device list is deduplicated on the phone and the PC polls
     * `bluetooth.scan.results`, so the newest state per device always arrives.
     */
    private fun emitScanEvent(): Boolean {
        val now = SystemClock.elapsedRealtime()
        if (now - eventWindowStartedAt >= 1_000L) {
            eventWindowStartedAt = now
            eventWindowCount = 0
        }
        if (eventWindowCount >= MAX_SCAN_EVENTS_PER_SECOND) return false
        eventWindowCount += 1
        return true
    }

    fun results(): Map<String, Any?> = linkedMapOf(
        "scanning" to scanning,
        "state" to if (scanning) "scanning" else if (devices.isEmpty()) "idle" else "stopped",
        "devices" to devicesList(),
        "detail" to "",
        "started_at_ms" to scanStartedAt,
        "duration_ms" to scanDuration,
        "filter_uuids" to scanFilterUuids,
        "name_prefix" to namePrefix,
        "error" to "",
    )

    private fun devicesList(): List<Map<String, Any?>> =
        devices.values
            .sortedByDescending { it.rssi }
            .map { it.asMap() }

    // ------------------------------------------------------------------ //
    // GATT
    // ------------------------------------------------------------------ //
    /** Connect to the peripheral the PC named. Never called on its own. */
    fun connect(params: Map<*, *>): Map<String, Any?> {
        val radio = requireConnectReady()
        val address = (params["address"] as? String)?.trim()?.uppercase()
            ?: throw BleFailure("bad_params", "address is required")
        if (!isMac(address)) throw BleFailure("bad_params", "'$address' is not a device address")
        val existing = links[address]
        if (existing != null && existing.state == "connected") {
            return linkedMapOf(
                "address" to address,
                "state" to "connected",
                "services" to existing.services,
                "mtu" to existing.mtu,
            )
        }
        val device = try {
            radio.getRemoteDevice(address)
        } catch (e: IllegalArgumentException) {
            throw BleFailure("bad_params", "'$address' is not a device the phone can reach")
        } catch (e: SecurityException) {
            throw BleFailure("bluetooth_denied", "the connect permission is missing: ${e.message}")
        }
        val link = Link(address = address)
        links[address] = link
        val gatt = try {
            device.connectGatt(context, false, callback(link), BluetoothDevice.TRANSPORT_LE)
        } catch (e: SecurityException) {
            links.remove(address)
            throw BleFailure("bluetooth_denied", "the connect permission is missing: ${e.message}")
        } catch (e: Exception) {
            links.remove(address)
            throw BleFailure("gatt_failed", "could not connect to $address: ${e.message}")
        }
        link.gatt = gatt
        // The connect callback is asynchronous; wait for it with a timeout so the
        // PC gets an answer either way.
        val deadline = android.os.SystemClock.elapsedRealtime() + GATT_TIMEOUT_MS
        while (link.state == "connecting") {
            if (android.os.SystemClock.elapsedRealtime() > deadline) {
                disconnectLink(link)
                links.remove(address)
                throw BleFailure("gatt_failed", "connecting to $address timed out")
            }
            Thread.sleep(25)
        }
        if (link.state != "connected") {
            val why = link.error.ifEmpty { link.state }
            disconnectLink(link)
            links.remove(address)
            throw BleFailure("gatt_failed", "could not connect to $address: $why")
        }
        emit("bluetooth.gatt", mapOf("address" to address, "state" to "connected", "error" to ""))
        log("INFO", "GATT connected to $address (${link.services.size} service(s))")
        return linkedMapOf(
            "address" to address,
            "state" to "connected",
            "services" to link.services,
            "mtu" to link.mtu,
        )
    }

    fun services(params: Map<*, *>): Map<String, Any?> {
        val link = requireLink(params)
        return linkedMapOf(
            "address" to link.address,
            "mtu" to link.mtu,
            "services" to link.services,
        )
    }

    fun read(params: Map<*, *>): Map<String, Any?> {
        val link = requireLink(params)
        val (_, char) = requireCharacteristic(link, params)
        val gatt = link.gatt ?: throw BleFailure("not_connected", "${link.address} is not connected")
        val bytes = runOperation(link, "read", char) { op ->
            if (!gatt.readCharacteristic(char)) {
                throw BleFailure("gatt_failed", "the phone refused to read ${shortUuid(char.uuid)}")
            }
            val result = awaitOperation(op)
            if (result.status != BluetoothGatt.GATT_SUCCESS) {
                // The platform's own reason, not "timed out": a peripheral that
                // answers "insufficient authentication" says so.
                throw BleFailure(
                    "gatt_failed",
                    "reading ${shortUuid(char.uuid)} failed: ${gattStatus(result.status ?: ABORTED)}",
                )
            }
            result.value ?: ByteArray(0)
        }
        log("INFO", "GATT read ${shortUuid(char.uuid)}: ${bytes.size} B from ${link.address}")
        return linkedMapOf(
            "value_b64" to Base64.encodeToString(bytes, Base64.NO_WRAP),
            "bytes" to bytes.size,
            "hex" to hex(bytes),
        )
    }

    fun write(params: Map<*, *>): Map<String, Any?> {
        val link = requireLink(params)
        val (_, characteristic) = requireCharacteristic(link, params)
        val gatt = link.gatt ?: throw BleFailure("not_connected", "${link.address} is not connected")
        val bytes = try {
            Base64.decode((params["value_b64"] as? String) ?: "", Base64.NO_WRAP)
        } catch (e: IllegalArgumentException) {
            throw BleFailure("bad_params", "value_b64 is not valid base64")
        }
        if (bytes.size > MAX_VALUE_BYTES) {
            throw BleFailure("bad_params", "a write is limited to $MAX_VALUE_BYTES bytes")
        }
        val withResponse = params["with_response"] as? Boolean ?: true
        val type = if (withResponse) {
            BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
        } else {
            BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
        }
        // API 33 deprecated the whole-value setter; setWriteType + writeCharacteristic
        // (value setter) still works on every release this app supports.
        @Suppress("DEPRECATION")
        run {
            characteristic.writeType = type
            characteristic.value = bytes
        }
        // A write with response is only successful when Android says so in
        // `onCharacteristicWrite`; the boolean below only means "accepted for
        // transmission". Reporting success before the callback is how a rejected
        // write used to look identical to a successful one.
        runOperation(link, "write", characteristic) { op ->
            val started = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                gatt.writeCharacteristic(characteristic, bytes, type) == BluetoothGatt.GATT_SUCCESS
            } else {
                @Suppress("DEPRECATION")
                gatt.writeCharacteristic(characteristic)
            }
            if (!started) {
                throw BleFailure(
                    "gatt_failed",
                    "the phone refused to write ${shortUuid(characteristic.uuid)}",
                )
            }
            if (withResponse) {
                val result = awaitOperation(op)
                if (result.status != BluetoothGatt.GATT_SUCCESS) {
                    throw BleFailure(
                        "gatt_failed",
                        "writing ${shortUuid(characteristic.uuid)} failed: " +
                            gattStatus(result.status ?: ABORTED),
                    )
                }
            }
        }
        log("INFO", "GATT write ${shortUuid(characteristic.uuid)}: ${bytes.size} B to ${link.address}")
        return linkedMapOf("ok" to true, "bytes" to bytes.size, "confirmed" to withResponse)
    }

    fun subscribe(params: Map<*, *>): Map<String, Any?> {
        val link = requireLink(params)
        val (_, characteristic) = requireCharacteristic(link, params)
        val gatt = link.gatt ?: throw BleFailure("not_connected", "${link.address} is not connected")
        val enable = params["enable"] as? Boolean ?: true
        val key = characteristic.uuid.toString()
        if (!enable) {
            runCatching {
                runOperation(link, "descriptor", characteristic) { op ->
                    gatt.setCharacteristicNotification(characteristic, false)
                    writeCccd(gatt, characteristic, false)
                    // Best effort: an unsubscribed link is being torn down anyway.
                    runCatching { awaitOperation(op) }
                }
            }
            synchronized(link) { link.subscriptions.remove(key) }
            log("INFO", "unsubscribed ${shortUuid(characteristic.uuid)} on ${link.address}")
            return linkedMapOf("ok" to true, "subscribed" to false)
        }
        if (synchronized(link) { link.subscriptions.size } >= MAX_SUBSCRIPTIONS) {
            throw BleFailure("bad_params", "at most $MAX_SUBSCRIPTIONS subscriptions per link")
        }
        if (!characteristic.properties.let {
                it and (BluetoothGattCharacteristic.PROPERTY_NOTIFY or
                    BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0
            }
        ) {
            throw BleFailure("gatt_failed", "${shortUuid(characteristic.uuid)} does not notify")
        }
        runOperation(link, "descriptor", characteristic) { op ->
            if (!gatt.setCharacteristicNotification(characteristic, true)) {
                throw BleFailure("gatt_failed", "the phone refused the notification registration")
            }
            if (!writeCccd(gatt, characteristic, true)) {
                throw BleFailure(
                    "gatt_failed",
                    "the phone refused the descriptor write for ${shortUuid(characteristic.uuid)}",
                )
            }
            // The CCCD write is what actually enables notifications: a subscription
            // is only real once Android confirms it.
            val result = awaitOperation(op)
            if (result.status != BluetoothGatt.GATT_SUCCESS) {
                throw BleFailure(
                    "gatt_failed",
                    "subscribing to ${shortUuid(characteristic.uuid)} failed: " +
                        gattStatus(result.status ?: ABORTED),
                )
            }
        }
        synchronized(link) { if (key !in link.subscriptions) link.subscriptions.add(key) }
        log("INFO", "subscribed ${shortUuid(characteristic.uuid)} on ${link.address}")
        return linkedMapOf("ok" to true, "subscribed" to true)
    }

    fun disconnect(params: Map<*, *>): Map<String, Any?> {
        val address = (params["address"] as? String)?.trim()?.uppercase() ?: ""
        val link = links[address] ?: return linkedMapOf("ok" to true, "state" to "disconnected")
        disconnectLink(link)
        links.remove(address)
        emit("bluetooth.gatt", mapOf("address" to address, "state" to "disconnected", "error" to ""))
        log("INFO", "GATT disconnected from $address")
        return linkedMapOf("ok" to true, "state" to "disconnected")
    }

    fun connections(): Map<String, Any?> = linkedMapOf(
        "connections" to linkMaps(),
        "scanning" to scanning,
    )

    /** The same list `bluetooth.gatt.status` returns, for the phone's own card. */
    fun linkMaps(): List<Map<String, Any?>> = links.values.map {
        linkedMapOf(
            "address" to it.address,
            "state" to it.state,
            "mtu" to it.mtu,
            "subscriptions" to synchronized(it) { it.subscriptions.toList() },
            "error" to it.error,
        )
    }

    /** State of one link, used by tests and by the PC's reconciliation pass. */
    fun linkState(address: String): String? = links[address.uppercase()]?.state

    fun notifications(address: String): List<Map<String, Any?>> {
        val link = links[address.uppercase()] ?: return emptyList()
        return synchronized(link) { link.notifications.toList() }
    }

    // ------------------------------------------------------------------ //
    // operation bookkeeping
    // ------------------------------------------------------------------ //
    /**
     * One outstanding GATT operation.
     *
     * Android delivers GATT results on a callback, so "did the write work?" is only
     * answerable once `onCharacteristicWrite` arrives. Each operation therefore has
     * its own monitor, which the waiting worker sleeps on and the callback notifies;
     * the result is never inferred from the return value of the request call.
     */
    private class Op(val kind: String, val address: String, val uuid: String) {
        var status: Int? = null
        var value: ByteArray? = null
        var done: Boolean = false

        fun complete(status: Int, value: ByteArray? = null) {
            synchronized(this) {
                this.status = status
                this.value = value
                this.done = true
                (this as Object).notifyAll()
            }
        }

        fun abort() = complete(ABORTED)
    }

    private data class Key(val address: String, val uuid: String, val kind: String)

    /** In-flight operations, keyed by target. At most one per key, by construction. */
    private val operations = ConcurrentHashMap<Key, Op>()

    /**
     * One monitor per link: Android allows a single outstanding GATT operation per
     * connection, so operations on the *same* peripheral are serialised while
     * different peripherals still run in parallel. This is the queue docs/21
     * describes; the previous release ran three at once and let the platform refuse
     * the second with an error that looked like a peripheral fault.
     */
    private val linkLocks = ConcurrentHashMap<String, Any>()

    private fun linkLock(address: String): Any = linkLocks.getOrPut(address) { Any() }

    private fun callback(link: Link) = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(gatt: BluetoothGatt, status: Int, newState: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                // A failed link must not leak its client interface: Android allows a
                // few dozen per app, and a GATT 133 is the most common BLE error
                // there is, so a leak here bricks Bluetooth after a dozen drops.
                failLink(link, gattStatus(status), close = true)
                log("WARN", "GATT ${link.address} connection failed: ${link.error}")
                emit("bluetooth.gatt", mapOf("address" to link.address, "state" to "failed", "error" to link.error))
                return
            }
            when (newState) {
                BluetoothProfile.STATE_CONNECTED -> {
                    // Discover services immediately: a connected link with no tree is
                    // useless to the PC, and the discovery is what almost always fails
                    // on a flaky peripheral.
                    if (!gatt.discoverServices()) {
                        failLink(link, "the peripheral refused service discovery", close = true)
                        return
                    }
                    // 23 is the ATT minimum; asking for more is what makes a long
                    // characteristic readable in one round trip. Failure is fine.
                    runCatching { gatt.requestMtu(REQUESTED_MTU) }
                    link.state = "connected"
                }

                BluetoothProfile.STATE_DISCONNECTED -> {
                    val wasConnected = link.state == "connected"
                    // Any operation still waiting on this link will never be answered.
                    abortOperations(link.address)
                    link.state = "disconnected"
                    link.gatt = null
                    links.remove(link.address)
                    runCatching { gatt.close() }
                    if (wasConnected) {
                        emit(
                            "bluetooth.gatt",
                            mapOf("address" to link.address, "state" to "disconnected", "error" to ""),
                        )
                        log("INFO", "GATT ${link.address} disconnected by the peripheral")
                    }
                }
            }
        }

        override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                link.state = "failed"
                link.error = "service discovery failed (${gattStatus(status)})"
                log("WARN", "service discovery on ${link.address} failed: ${link.error}")
                return
            }
            val tree = ArrayList<Map<String, Any?>>()
            for (service in gatt.services) {
                val chars = ArrayList<Map<String, Any?>>()
                for (characteristic in service.characteristics) {
                    chars.add(
                        linkedMapOf(
                            "uuid" to characteristic.uuid.toString(),
                            "short" to shortUuid(characteristic.uuid),
                            "properties" to propertiesOf(characteristic),
                            "descriptors" to characteristic.descriptors.map { it.uuid.toString() },
                        )
                    )
                }
                tree.add(
                    linkedMapOf(
                        "uuid" to service.uuid.toString(),
                        "short" to shortUuid(service.uuid),
                        "characteristics" to chars,
                    )
                )
            }
            synchronized(link) {
                link.services.clear()
                link.services.addAll(tree)
            }
            link.mtu = 23
            log("INFO", "discovered ${tree.size} service(s) on ${link.address}")
        }

        override fun onCharacteristicRead(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray,
            status: Int,
        ) {
            // Hand the platform's status to the waiting caller, success or not: a
            // failure reported as "timed out" fifteen seconds later is a lie.
            completeOperation(
                link.address,
                characteristic.uuid.toString(),
                "read",
                status,
                value.copyOf(),
            )
        }

        @Deprecated("Deprecated in Java")
        override fun onCharacteristicRead(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            status: Int,
        ) {
            @Suppress("DEPRECATION")
            val value = characteristic.value
            completeOperation(
                link.address,
                characteristic.uuid.toString(),
                "read",
                status,
                value?.copyOf(),
            )
        }

        override fun onCharacteristicWrite(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            status: Int,
        ) {
            completeOperation(
                link.address,
                characteristic.uuid.toString(),
                "write",
                status,
            )
        }

        override fun onMtuChanged(gatt: BluetoothGatt, mtu: Int, status: Int) {
            if (status == BluetoothGatt.GATT_SUCCESS && mtu > 0) {
                link.mtu = mtu
                log("INFO", "GATT ${link.address} negotiated a ${mtu}-byte MTU")
            }
        }

        override fun onCharacteristicChanged(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray,
        ) {
            recordNotification(link, characteristic, value)
        }

        @Deprecated("Deprecated in Java")
        override fun onCharacteristicChanged(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
        ) {
            @Suppress("DEPRECATION")
            val value = characteristic.value ?: return
            recordNotification(link, characteristic, value)
        }

        override fun onDescriptorWrite(
            gatt: BluetoothGatt,
            descriptor: BluetoothGattDescriptor,
            status: Int,
        ) {
            // The subscription is only real once this arrives; `subscribe()` waits
            // for it instead of reporting success as soon as the write is queued.
            completeOperation(
                link.address,
                descriptor.characteristic.uuid.toString(),
                "descriptor",
                status,
            )
            if (status != BluetoothGatt.GATT_SUCCESS) {
                log("WARN", "CCCD write on ${link.address} failed (${gattStatus(status)})")
            }
        }
    }

    private fun recordNotification(link: Link, characteristic: BluetoothGattCharacteristic, value: ByteArray) {
        val bytes = value.copyOf()
        val payload = linkedMapOf<String, Any?>(
            "address" to link.address,
            "service" to (findServiceUuid(link, characteristic.uuid.toString()) ?: ""),
            "characteristic" to characteristic.uuid.toString(),
            "value_b64" to if (bytes.size <= MAX_VALUE_BYTES) {
                Base64.encodeToString(bytes, Base64.NO_WRAP)
            } else {
                ""
            },
            "hex" to hex(bytes.copyOf(minOf(bytes.size, MAX_VALUE_BYTES))),
            "bytes" to bytes.size,
            "ts_ms" to android.os.SystemClock.elapsedRealtime(),
        )
        synchronized(link) {
            link.notifications.addLast(payload)
            while (link.notifications.size > MAX_NOTIFICATIONS) link.notifications.removeFirst()
        }
        emit("bluetooth.notification", payload)
    }

    private fun findServiceUuid(link: Link, characteristic: String): String? {
        val gatt = link.gatt ?: return null
        for (service in gatt.services) {
            if (service.characteristics.any { it.uuid.toString() == characteristic }) {
                return service.uuid.toString()
            }
        }
        return null
    }

    /**
     * Run one GATT operation on one link, serialised and cancelled cleanly.
     *
     * The monitor is held for the whole exchange — issue *and* callback — because
     * `BluetoothGatt` accepts one operation at a time. Holding it while waiting does
     * not deadlock: the callback only ever touches the operation's own monitor.
     */
    private fun <T> runOperation(
        link: Link,
        kind: String,
        characteristic: BluetoothGattCharacteristic,
        block: (Op) -> T,
    ): T {
        val key = Key(link.address, characteristic.uuid.toString(), kind)
        val op = Op(kind, link.address, characteristic.uuid.toString())
        synchronized(linkLock(link.address)) {
            operations[key] = op
            try {
                return block(op)
            } finally {
                operations.remove(key)
            }
        }
    }

    /** Wait for the platform's answer, bounded; the operation is always discarded. */
    private fun awaitOperation(op: Op, timeoutMs: Long = GATT_TIMEOUT_MS): Op {
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        synchronized(op) {
            while (!op.done) {
                val remaining = deadline - SystemClock.elapsedRealtime()
                if (remaining <= 0L) break
                try {
                    (op as Object).wait(remaining)
                } catch (e: InterruptedException) {
                    Thread.currentThread().interrupt()
                    break
                }
            }
        }
        if (!op.done) {
            log("WARN", "GATT ${op.kind} on ${shortUuidStr(op.uuid)} timed out after ${timeoutMs}ms")
            throw BleFailure(
                "gatt_failed",
                "the peripheral did not answer the ${op.kind} on ${shortUuidStr(op.uuid)} in time",
            )
        }
        return op
    }

    private fun completeOperation(
        address: String,
        uuid: String,
        kind: String,
        status: Int,
        value: ByteArray? = null,
    ) {
        val op = operations[Key(address, uuid, kind)]
        if (op == null) {
            // A stale callback: the operation already timed out or the link was torn
            // down. Dropping it is what keeps a late answer from completing a newer
            // request for the same characteristic.
            log("DEBUG", "ignoring a late $kind callback for $uuid on $address")
            return
        }
        op.complete(status, value)
    }

    private fun abortOperations(address: String) {
        for ((key, op) in operations) {
            if (key.address == address) op.abort()
        }
    }

    /** Fail a link: wake every waiter, close the client, forget the entry. */
    private fun failLink(link: Link, why: String, close: Boolean) {
        abortOperations(link.address)
        link.state = "failed"
        link.error = why
        val gatt = link.gatt
        link.gatt = null
        synchronized(link) { link.subscriptions.clear() }
        links.remove(link.address)
        if (close && gatt != null) {
            runCatching { gatt.disconnect() }
            runCatching { gatt.close() }
        }
    }

    private fun shortUuidStr(text: String): String = runCatching { shortUuid(UUID.fromString(text)) }
        .getOrDefault(text)

    private fun writeCccd(
        gatt: BluetoothGatt,
        characteristic: BluetoothGattCharacteristic,
        enable: Boolean,
    ): Boolean {
        val descriptor = characteristic.getDescriptor(CCCD) ?: return true
        val value = if (enable) {
            if (characteristic.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY != 0) {
                BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            } else {
                BluetoothGattDescriptor.ENABLE_INDICATION_VALUE
            }
        } else {
            BluetoothGattDescriptor.DISABLE_NOTIFICATION_VALUE
        }
        @Suppress("DEPRECATION")
        return run {
            descriptor.value = value
            gatt.writeDescriptor(descriptor)
        }
    }

    private fun requireLink(params: Map<*, *>): Link {
        val address = (params["address"] as? String)?.trim()?.uppercase()
            ?: throw BleFailure("bad_params", "address is required")
        val link = links[address]
        if (link == null || link.state != "connected") {
            throw BleFailure("not_connected", "$address is not connected")
        }
        return link
    }

    private fun requireCharacteristic(
        link: Link,
        params: Map<*, *>,
    ): Pair<String, BluetoothGattCharacteristic> {
        val serviceUuid = parseUuid(params["service"]) ?: throw BleFailure("bad_params", "service is required")
        val charUuid = parseUuid(params["characteristic"])
            ?: throw BleFailure("bad_params", "characteristic is required")
        val gatt = link.gatt ?: throw BleFailure("not_connected", "${link.address} is not connected")
        val service = gatt.getService(serviceUuid)
            ?: throw BleFailure("gatt_failed", "service ${shortUuid(serviceUuid)} is not on ${link.address}")
        val characteristic = service.getCharacteristic(charUuid)
            ?: throw BleFailure(
                "gatt_failed",
                "characteristic ${shortUuid(charUuid)} is not in ${shortUuid(serviceUuid)}",
            )
        return shortUuid(serviceUuid) to characteristic
    }

    private fun disconnectLink(link: Link) {
        val gatt = link.gatt
        link.state = "disconnected"
        link.gatt = null
        synchronized(link) {
            link.subscriptions.clear()
        }
        if (gatt != null) {
            try {
                gatt.disconnect()
            } catch (e: Exception) {
                log("WARN", "disconnect on ${link.address} failed: ${e.message}")
            }
            runCatching { gatt.close() }
        }
    }

    /**
     * The last session ended: stop scanning and drop every link.
     *
     * A GATT link held open for a PC that is gone is a privacy and battery bug, so
     * this happens on the same edge as the audio and camera release.
     */
    fun onSessionClosed(why: String) {
        val hadWork = scanning || links.isNotEmpty()
        runCatching { stopScan() }
        for (link in links.values.toList()) {
            disconnectLink(link)
        }
        links.clear()
        devices.clear()
        if (hadWork) log("INFO", "Bluetooth released ($why)")
    }

    fun release() {
        onSessionClosed("the bridge stopped")
    }

    // ------------------------------------------------------------------ //
    // helpers
    // ------------------------------------------------------------------ //
    private fun parseUuid(value: Any?): UUID? {
        val text = (value as? String)?.trim() ?: return null
        return try {
            when {
                text.length == 4 -> UUID.fromString("0000$text-0000-1000-8000-00805f9b34fb")
                text.length == 8 -> UUID.fromString("$text-0000-1000-8000-00805f9b34fb")
                else -> UUID.fromString(text)
            }
        } catch (e: IllegalArgumentException) {
            null
        }
    }

    private fun shortUuid(uuid: UUID): String {
        val text = uuid.toString().lowercase()
        return if (text.startsWith("0000") && text.endsWith("-0000-1000-8000-00805f9b34fb")) {
            text.substring(4, 8)
        } else {
            text
        }
    }

    private fun propertiesOf(characteristic: BluetoothGattCharacteristic): List<String> {
        val properties = characteristic.properties
        val out = ArrayList<String>()
        if (properties and BluetoothGattCharacteristic.PROPERTY_READ != 0) out.add("read")
        if (properties and BluetoothGattCharacteristic.PROPERTY_WRITE != 0) out.add("write")
        if (properties and BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE != 0) {
            out.add("write_no_response")
        }
        if (properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY != 0) out.add("notify")
        if (properties and BluetoothGattCharacteristic.PROPERTY_INDICATE != 0) out.add("indicate")
        if (properties and BluetoothGattCharacteristic.PROPERTY_SIGNED_WRITE != 0) out.add("signed_write")
        if (properties and BluetoothGattCharacteristic.PROPERTY_BROADCAST != 0) out.add("broadcast")
        return out
    }

    private fun gattStatus(status: Int): String = when (status) {
        BluetoothGatt.GATT_SUCCESS -> "success"
        133 -> "GATT_ERROR (the peripheral is out of range or refused the link)"
        257 -> "GATT_CONNECTION_CONGESTED"
        8 -> "GATT_CONN_TIMEOUT"
        else -> "GATT status $status"
    }

    private fun hex(bytes: ByteArray): String {
        val builder = StringBuilder(bytes.size * 3)
        for ((index, byte) in bytes.withIndex()) {
            if (index > 0) builder.append(' ')
            builder.append("%02x".format(byte))
        }
        return builder.toString()
    }

    private fun isMac(text: String): Boolean = Regex("^([0-9A-F]{2}:){5}[0-9A-F]{2}$").matches(text)
}

/** Small helper kept next to the engine: permissions that resolve per API level. */
object PermissionsCompat {
    fun bluetoothScan(context: Context): String = Permissions.effective(context, Manifest.permission.BLUETOOTH_SCAN)
    fun bluetoothConnect(context: Context): String =
        Permissions.effective(context, Manifest.permission.BLUETOOTH_CONNECT)

    fun grantedScan(context: Context): Boolean = Permissions.granted(context, bluetoothScan(context))
    fun grantedConnect(context: Context): Boolean = Permissions.granted(context, bluetoothConnect(context))
}
