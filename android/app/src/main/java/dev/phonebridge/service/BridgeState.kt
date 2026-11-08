package dev.phonebridge.service

import dev.phonebridge.net.BridgeServer
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
    }

    private fun unbindMirrors() {
        mirrors.forEach { it.cancel() }
        mirrors = emptyList()
    }

    fun answerPairing(approve: Boolean) {
        server?.answerPairing(approve)
    }

    fun forgetAllPairings(store: dev.phonebridge.net.PairingStore) {
        store.list().forEach { store.unpair(it.deviceId) }
    }
}
