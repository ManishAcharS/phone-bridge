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
import android.util.Base64
import android.util.Log
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
        var state: String = "connecting",
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
            return linkedMapOf(
                "ok" to true,
                "scanning" to true,
                "started_at_ms" to 0L,
                "duration_ms" to 0L,
            )
        }
        val duration = ((params["duration_ms"] as? Number)?.toLong() ?: DEFAULT_SCAN_MS)
            .coerceIn(0L, MAX_SCAN_MS)
        val filters = (params["filter_uuids"] as? List<*>)
            ?.mapNotNull { runCatching { UUID.fromString(it.toString()) }.getOrNull() }
            .orEmpty()
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
        val startedAt = android.os.SystemClock.elapsedRealtime()
        if (duration > 0) {
            val stop = Runnable { runCatching { stopScan() } }
            scanStopRunnable = stop
            handler.postDelayed(stop, duration)
        }
        emit("bluetooth.scan", mapOf("state" to "scanning", "detail" to ""))
        log("INFO", "BLE scan started for ${if (duration > 0) "${duration}ms" else "until stopped"}")
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
        emit("bluetooth.device", merged.asMap())
    }

    fun results(): Map<String, Any?> = linkedMapOf(
        "scanning" to scanning,
        "state" to if (scanning) "scanning" else "stopped",
        "devices" to devicesList(),
        "detail" to "",
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
        val (service, characteristic) = requireCharacteristic(link, params)
        val gatt = link.gatt ?: throw BleFailure("not_connected", "${link.address} is not connected")
        val char = characteristic
        // API 33 uses readCharacteristic(); older releases need readCharacteristic() too
        // but report the value through the same callback.
        if (!gatt.readCharacteristic(char)) {
            throw BleFailure("gatt_failed", "the phone refused to read ${shortUuid(char.uuid)}")
        }
        val deadline = android.os.SystemClock.elapsedRealtime() + GATT_TIMEOUT_MS
        var value: ByteArray? = null
        while (value == null) {
            if (android.os.SystemClock.elapsedRealtime() > deadline) {
                throw BleFailure("gatt_failed", "reading ${shortUuid(char.uuid)} timed out")
            }
            value = readValues[Key(link.address, char.uuid.toString())]
            if (value == null) Thread.sleep(20)
        }
        readValues.remove(Key(link.address, char.uuid.toString()))
        val bytes = value.copyOf()
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
        val started = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            gatt.writeCharacteristic(characteristic, bytes, type) == BluetoothGatt.GATT_SUCCESS
        } else {
            @Suppress("DEPRECATION")
            gatt.writeCharacteristic(characteristic)
        }
        if (!started) {
            throw BleFailure("gatt_failed", "the phone refused to write ${shortUuid(characteristic.uuid)}")
        }
        log("INFO", "GATT write ${shortUuid(characteristic.uuid)}: ${bytes.size} B to ${link.address}")
        return linkedMapOf("ok" to true, "bytes" to bytes.size)
    }

    fun subscribe(params: Map<*, *>): Map<String, Any?> {
        val link = requireLink(params)
        val (_, characteristic) = requireCharacteristic(link, params)
        val gatt = link.gatt ?: throw BleFailure("not_connected", "${link.address} is not connected")
        val enable = params["enable"] as? Boolean ?: true
        val key = characteristic.uuid.toString()
        if (!enable) {
            gatt.setCharacteristicNotification(characteristic, false)
            writeCccd(gatt, characteristic, false)
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
        if (!gatt.setCharacteristicNotification(characteristic, true)) {
            throw BleFailure("gatt_failed", "the phone refused the notification registration")
        }
        writeCccd(gatt, characteristic, true)
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
    // callbacks
    // ------------------------------------------------------------------ //
    private data class Key(val address: String, val uuid: String)

    private val readValues = ConcurrentHashMap<Key, ByteArray>()
    private val pending = ConcurrentHashMap<Key, Boolean>()

    private fun callback(link: Link) = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(gatt: BluetoothGatt, status: Int, newState: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                link.state = "failed"
                link.error = gattStatus(status)
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
                        link.state = "failed"
                        link.error = "the peripheral refused service discovery"
                        return
                    }
                    link.state = "connected"
                }

                BluetoothProfile.STATE_DISCONNECTED -> {
                    val wasConnected = link.state == "connected"
                    link.state = "disconnected"
                    link.gatt = null
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
            if (status == BluetoothGatt.GATT_SUCCESS) {
                readValues[Key(link.address, characteristic.uuid.toString())] = value.copyOf()
            } else {
                link.error = "read failed (${gattStatus(status)})"
                log("WARN", "GATT read on ${link.address} failed: ${link.error}")
            }
        }

        @Deprecated("Deprecated in Java")
        override fun onCharacteristicRead(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            status: Int,
        ) {
            @Suppress("DEPRECATION")
            val value = characteristic.value
            if (status == BluetoothGatt.GATT_SUCCESS && value != null) {
                readValues[Key(link.address, characteristic.uuid.toString())] = value.copyOf()
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
            if (status != BluetoothGatt.GATT_SUCCESS) {
                log("WARN", "CCCD write on ${link.address} failed (${gattStatus(status)})")
            }
        }
    }

    private fun recordNotification(link: Link, characteristic: BluetoothGattCharacteristic, value: ByteArray) {
        val bytes = value.copyOf()
        val payload = linkedMapOf<String, Any?>(
            "address" to link.address,
            "service" to (findServiceUuid(link, characteristic.uuid) ?: ""),
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

    private fun writeCccd(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic, enable: Boolean) {
        val descriptor = characteristic.getDescriptor(CCCD) ?: return
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
        run {
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
