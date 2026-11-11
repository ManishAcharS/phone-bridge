package dev.phonebridge.service

import dev.phonebridge.net.BridgeServer
import dev.phonebridge.net.CameraEngine
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

    @Volatile
    private var server: BridgeServer? = null

    private var mirrors: List<Job> = emptyList()

    fun bind(server: BridgeServer, scope: CoroutineScope) {
        this.server = server
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
        )
    }

    /** Stop the camera from the phone's own UI. */
    fun stopCameraLocally() {
        server?.stopCameraLocally()
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
        // The engine was released with the server; the badge must not linger.
        _camera.value = CameraEngine.State()
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
