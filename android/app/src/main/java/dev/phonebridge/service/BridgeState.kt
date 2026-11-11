package dev.phonebridge.service

import dev.phonebridge.net.BridgeServer
import dev.phonebridge.net.CameraEngine
import dev.phonebridge.net.FileTransferEngine
import dev.phonebridge.net.SafStore
import dev.phonebridge.net.SensorEngine
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * Process-wide view of the bridge, so the UI can be recreated (rotation, process
 * backgrounding) without owning or restarting the server.
 *
 * The service owns [BridgeServer]; this object only mirrors its state flows.
 */
object BridgeState {

    private val _running = MutableStateFlow(false)
    val running: StateFlow<Boolean> = _running.asStateFlow()

    private val _peers = MutableStateFlow<List<BridgeServer.PeerInfo>>(emptyList())
    val peers: StateFlow<List<BridgeServer.PeerInfo>> = _peers.asStateFlow()

    private val _pairingRequest = MutableStateFlow<BridgeServer.PairingRequest?>(null)
    val pairingRequest: StateFlow<BridgeServer.PairingRequest?> = _pairingRequest.asStateFlow()

    private val _log = MutableStateFlow<List<BridgeServer.LogLine>>(emptyList())
    val log: StateFlow<List<BridgeServer.LogLine>> = _log.asStateFlow()

    private val _stats = MutableStateFlow(BridgeServer.Stats())
    val stats: StateFlow<BridgeServer.Stats> = _stats.asStateFlow()

    private val _lastError = MutableStateFlow<String?>(null)
    val lastError: StateFlow<String?> = _lastError.asStateFlow()

    private val _deviceId = MutableStateFlow("")
    val deviceId: StateFlow<String> = _deviceId.asStateFlow()

    private val _deviceName = MutableStateFlow("")
    val deviceName: StateFlow<String> = _deviceName.asStateFlow()

    private val _port = MutableStateFlow(BridgeServer.DEFAULT_PORT)
    val port: StateFlow<Int> = _port.asStateFlow()

    /**
     * Phase 4. The single source for "is the camera on right now", mirrored from
     * the engine so the badge and the red banner cannot disagree with reality.
     */
    private val _camera = MutableStateFlow(CameraEngine.State())
    val camera: StateFlow<CameraEngine.State> = _camera.asStateFlow()

    /**
     * Phase 5. Three more mirrors, for the same reason: the phone's own screen must
     * never guess whether a sensor listener, a file transfer or a GATT link is
     * running. Each flow is fed by the engine that owns the resource.
     */
    private val _sensors = MutableStateFlow(SensorEngine.State())
    val sensors: StateFlow<SensorEngine.State> = _sensors.asStateFlow()

    private val _files = MutableStateFlow(FileTransferEngine.State())
    val files: StateFlow<FileTransferEngine.State> = _files.asStateFlow()

    private val _bluetooth = MutableStateFlow<Map<String, Any?>>(emptyMap())
    val bluetooth: StateFlow<Map<String, Any?>> = _bluetooth.asStateFlow()

    @Volatile
    private var server: BridgeServer? = null

    /** Set while the service is bound, so the SAF helpers can resolve names. */
    @Volatile
    private var appContext: android.content.Context? = null

    private var mirrors: List<Job> = emptyList()

    fun bind(
        server: BridgeServer,
        scope: CoroutineScope,
        context: android.content.Context? = null,
    ) {
        this.server = server
        this.appContext = context
        unbindMirrors()
        _deviceId.value = server.identity.deviceId
        _deviceName.value = server.identity.name
        mirrors = listOf(
            scope.launch { server.running.collect { _running.value = it } },
            scope.launch { server.peers.collect { _peers.value = it } },
            scope.launch { server.pairingRequest.collect { _pairingRequest.value = it } },
            scope.launch { server.log.collect { _log.value = it } },
            scope.launch { server.stats.collect { _stats.value = it } },
            scope.launch { server.lastError.collect { _lastError.value = it } },
            scope.launch { server.cameraState().collect { _camera.value = it } },
            scope.launch { server.sensorState().collect { _sensors.value = it } },
            scope.launch { server.fileState().collect { _files.value = it } },
        )
        refreshBluetooth()
    }

    /** Stop the camera from the phone's own UI. */
    fun stopCameraLocally() {
        server?.stopCameraLocally()
    }

    /** Stop every sensor listener from the phone's own UI. */
    fun stopSensorsLocally() {
        server?.stopSensorsLocally()
    }

    /** Cancel every running transfer (the file on the phone is not written). */
    fun cancelTransfersLocally() {
        server?.cancelTransfersLocally()
    }

    /** Drop every GATT link and stop scanning. */
    fun disconnectBluetoothLocally() {
        server?.disconnectBluetoothLocally()
        refreshBluetooth()
    }

    /** Remember the SAF folder the user picked (label only; the URI is in SafStore). */
    fun setDestination(label: String) {
        if (label.isBlank()) {
            server?.filesClearDestination()
        } else {
            server?.filesDestination(label)
        }
        _files.value = _files.value.copy(destination = label)
    }

    /** Add files the user picked to what the PC may pull. */
    fun addSelection(uris: List<android.net.Uri>) {
        server?.filesAddSelection(uris)
        refreshSelections(uris)
    }

    private fun refreshSelections(uris: List<android.net.Uri>) {
        if (uris.isEmpty()) return
        _files.value = _files.value.copy(
            selection = _files.value.selection + uris.mapNotNull { resolveName(it) },
        )
    }

    private fun resolveName(uri: android.net.Uri): String? = try {
        appContext?.let { SafStore.displayName(it, uri) }
    } catch (e: Exception) {
        null
    }

    /** The phone's own sensor catalogue, for the card (never a PC's report). */
    fun sensorCatalogue(context: android.content.Context): List<SensorEngine.Entry> =
        server?.sensorCatalogue() ?: SensorEngine(context, { _, _ -> }, { _, _ -> }).entries()

    /** Connected GATT links, for the card. */
    fun bluetoothLinks(): List<Map<String, Any?>> = server?.bluetoothLinks() ?: emptyList()

    /**
     * Re-read the adapter state.
     *
     * The platform has no callback for "the user turned Bluetooth off in Settings",
     * so the UI polls this while the Bluetooth card is visible. It is a cheap
     * query: no scan, no connection, no permission prompt.
     */
    fun refreshBluetooth() {
        _bluetooth.value = server?.bluetoothStatus() ?: mapOf(
            "supported" to false,
            "enabled" to false,
            "le_supported" to false,
            "name" to "",
            "address" to "",
            "permitted" to null,
            "detail" to "the bridge is not running",
        )
    }

    fun setPort(port: Int) {
        _port.value = port
    }

    fun unbind() {
        unbindMirrors()
        server = null
        _running.value = false
        _peers.value = emptyList()
        _pairingRequest.value = null
        // The engines were released with the server; no badge may linger.
        _camera.value = CameraEngine.State()
        _sensors.value = SensorEngine.State()
        _files.value = FileTransferEngine.State()
    }

    private fun unbindMirrors() {
        mirrors.forEach { it.cancel() }
        mirrors = emptyList()
    }

    /**
     * Hand a probe report to the running server so the PC can fetch it later
     * with `audio.capabilities.last`. With no server running there is no PC to
     * fetch it, and the report stays in the UI (and on the clipboard).
     */
    fun publishProbe(report: Map<String, Any?>) {
        server?.publishCapabilityReport(report)
    }

    /**
     * Answer the pairing request that is currently displayed.
     *
     * [request] is passed explicitly (rather than a bare boolean) so the
     * decision can only apply to the SAS the user actually looked at.
     */
    fun answerPairing(request: BridgeServer.PairingRequest?, approve: Boolean) {
        val srv = server
        if (srv == null || request == null) return
        srv.answerPairing(request.token, approve)
    }

    /** Refuse everything pending (service teardown / failure). */
    fun denyAllPairings() {
        server?.denyAllPairings()
    }

    /**
     * Record a local event in the bridge log (and therefore in the UI).
     *
     * Used for things only the UI can observe, such as a permission being
     * revoked from the system settings while the app is open. With no server
     * running there is no log to append to, so it goes to logcat only.
     */
    fun note(level: String, message: String) {
        val srv = server
        if (srv != null) {
            srv.addLog(level, message)
        } else {
            android.util.Log.println(
                if (level == "ERROR") android.util.Log.ERROR else android.util.Log.WARN,
                "BridgeState",
                message,
            )
        }
    }

    fun forgetAllPairings(store: dev.phonebridge.net.PairingStore) {
        store.list().forEach { store.unpair(it.deviceId) }
    }
}
