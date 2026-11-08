package dev.phonebridge.net

import android.content.Context
import android.os.Build
import android.util.Log
import dev.phonebridge.protocol.Crypto
import dev.phonebridge.protocol.Framing
import dev.phonebridge.protocol.Json
import dev.phonebridge.protocol.Messages
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.java_websocket.WebSocket
import org.java_websocket.handshake.ClientHandshake
import org.java_websocket.server.WebSocketServer
import java.net.InetSocketAddress
import java.nio.ByteBuffer
import java.text.SimpleDateFormat
import java.util.Collections
import java.util.Date
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

/**
 * The phone side of PhoneBridge: a WebSocket listener that speaks protocol v1.
 *
 * The phone is always the server and the PC always dials in, so the same code
 * serves USB (adb forward or USB tethering), Wi-Fi and hotspot without a
 * branch anywhere.
 *
 * Threading: Java-WebSocket delivers every frame as a callback on its own
 * selector/worker threads.  Each connection is therefore an *event-driven*
 * state machine rather than a blocking read loop, and shared state is guarded
 * by a per-connection lock.
 */
class BridgeServer(
    private val context: Context,
    val identity: Crypto.Identity,
    private val pairingStore: PairingStore,
    private val port: Int = DEFAULT_PORT,
) {
    companion object {
        private const val TAG = "BridgeServer"
        const val DEFAULT_PORT = 8765
        private const val MAX_LOG_LINES = 200
        private const val BIND_TIMEOUT_S = 5L
    }

    data class PeerInfo(
        val deviceId: String,
        val name: String,
        val remoteAddress: String,
        val connectedAt: Long,
    )

    data class PairingRequest(val deviceId: String, val name: String, val sas: String)

    data class LogLine(val at: String, val level: String, val message: String)

    data class Stats(
        val framesIn: Long = 0,
        val framesOut: Long = 0,
        val bytesIn: Long = 0,
        val bytesOut: Long = 0,
        val handshakes: Long = 0,
    )

    private val _running = MutableStateFlow(false)
    val running: StateFlow<Boolean> = _running.asStateFlow()

    private val _peers = MutableStateFlow<List<PeerInfo>>(emptyList())
    val peers: StateFlow<List<PeerInfo>> = _peers.asStateFlow()

    private val _pairingRequest = MutableStateFlow<PairingRequest?>(null)
    val pairingRequest: StateFlow<PairingRequest?> = _pairingRequest.asStateFlow()

    private val _log = MutableStateFlow<List<LogLine>>(emptyList())
    val log: StateFlow<List<LogLine>> = _log.asStateFlow()

    private val _stats = MutableStateFlow(Stats())
    val stats: StateFlow<Stats> = _stats.asStateFlow()

    private val _lastError = MutableStateFlow<String?>(null)
    val lastError: StateFlow<String?> = _lastError.asStateFlow()

    private val framesIn = AtomicLong()
    private val framesOut = AtomicLong()
    private val bytesIn = AtomicLong()
    private val bytesOut = AtomicLong()
    private val handshakes = AtomicLong()

    private val sessions: MutableList<PeerSession> =
        Collections.synchronizedList(mutableListOf())

    private var impl: ServerImpl? = null

    // WebSocketServer.start() returns before the socket is bound, so the bind
    // result arrives on the server thread via onStart()/onError(). These two
    // fields let start() wait for the real answer instead of assuming success.
    @Volatile
    private var bindLatch: CountDownLatch? = null

    @Volatile
    private var bindError: Exception? = null

    /** Last capability probe report, kept so a reconnect can fetch it. */
    @Volatile
    private var lastCapabilityReport: Map<String, Any?>? = null

    private val probeLock = Any()

    /** Store a report measured in the UI, so a later PC connection can read it. */
    fun publishCapabilityReport(report: Map<String, Any?>) {
        lastCapabilityReport = report
        addLog("INFO", "capability probe report stored (${report["duration_ms"]} ms)")
    }

    // One probe at a time: the audio path is a shared resource.
    private val probeExecutor = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "capability-probe").apply { isDaemon = true }
    }

    /**
     * Run [CapabilityProbe] off the connection thread and answer when it is done.
     *
     * The reply goes out on the probe thread through the peer's normal send path,
     * which is thread-safe; the connection keeps reading (and answering
     * heartbeats) meanwhile.
     */
    private fun runCapabilityProbeAsync(
        rpcId: Any?,
        seconds: Int,
        acoustic: Boolean,
        peer: PeerSession,
    ) {
        addLog("INFO", "capability probe requested (soak ${seconds}s, acoustic=$acoustic)")
        probeExecutor.execute {
            val payload = try {
                val report = synchronized(probeLock) {
                    CapabilityProbe
                        .run(context, CapabilityProbe.Config(seconds = seconds, acoustic = acoustic))
                        .also { lastCapabilityReport = it }
                }
                addLog("INFO", "capability probe finished (${report["duration_ms"]} ms)")
                Messages.responseOk(rpcId, report)
            } catch (e: Exception) {
                addLog("ERROR", "capability probe failed: ${e.message}")
                Messages.responseErr(rpcId, "probe_failed", "${e.javaClass.simpleName}: ${e.message}")
            }
            peer.sendControl(payload)
        }
    }

    @Volatile
    private var pendingPairing: MutableList<PendingPairing> =
        Collections.synchronizedList(mutableListOf())

    private data class PendingPairing(
        val deviceId: String,
        val name: String,
        val sas: String,
        val session: PeerSession,
    )

    // ------------------------------------------------------------------ //
    // lifecycle
    // ------------------------------------------------------------------ //
    fun start(): Result<Unit> {
        if (_running.value) return Result.success(Unit)
        return try {
            val server = ServerImpl(InetSocketAddress(port))
            server.isReuseAddr = true
            bindError = null
            val latch = CountDownLatch(1)
            bindLatch = latch
            server.start() // returns immediately; binding happens on its own thread

            val settled = latch.await(BIND_TIMEOUT_S, TimeUnit.SECONDS)
            bindLatch = null
            if (!settled) {
                runCatching { server.stop(300) }
                throw IllegalStateException(
                    "timed out after ${BIND_TIMEOUT_S}s waiting for port $port to bind"
                )
            }
            bindError?.let { throw it }
            impl = server
            _lastError.value = null
            addLog("INFO", "listening on 0.0.0.0:$port (device ${identity.deviceId})")
            Result.success(Unit)
        } catch (e: Exception) {
            _lastError.value = "${e.javaClass.simpleName}: ${e.message}"
            addLog("ERROR", "cannot start server: ${e.message}")
            _running.value = false
            impl = null
            Result.failure(e)
        }
    }

    fun stop() {
        sessions.toList().forEach { it.close("server stopping") }
        sessions.clear()
        pendingPairing.clear()
        _pairingRequest.value = null
        impl?.let {
            try {
                it.stop(1000)
            } catch (e: Exception) {
                Log.w(TAG, "stop failed", e)
            }
        }
        impl = null
        _running.value = false
        _peers.value = emptyList()
        addLog("INFO", "server stopped")
    }

    fun answerPairing(approve: Boolean) {
        val pending = pendingPairing.toList()
        _pairingRequest.value = null
        pending.forEach { it.session.onUserPairingDecision(approve) }
        pendingPairing.clear()
    }

    // ------------------------------------------------------------------ //
    // helpers used by sessions
    // ------------------------------------------------------------------ //
    internal fun addLog(level: String, message: String) {
        Log.println(
            when (level) {
                "ERROR" -> Log.ERROR
                "WARN" -> Log.WARN
                else -> Log.INFO
            },
            TAG, message,
        )
        val stamp = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())
        _log.value = (_log.value + LogLine(stamp, level, message)).takeLast(MAX_LOG_LINES)
    }

    internal fun bumpIn(bytes: Int) {
        framesIn.incrementAndGet()
        bytesIn.addAndGet(bytes.toLong())
        refreshStats()
    }

    internal fun bumpOut(bytes: Int) {
        framesOut.incrementAndGet()
        bytesOut.addAndGet(bytes.toLong())
        refreshStats()
    }

    internal fun bumpHandshake() {
        handshakes.incrementAndGet()
        refreshStats()
    }

    private fun refreshStats() {
        _stats.value = Stats(
            framesIn = framesIn.get(),
            framesOut = framesOut.get(),
            bytesIn = bytesIn.get(),
            bytesOut = bytesOut.get(),
            handshakes = handshakes.get(),
        )
    }

    internal fun registerSession(session: PeerSession) {
        sessions.add(session)
        publishPeers()
    }

    internal fun unregisterSession(session: PeerSession) {
        sessions.remove(session)
        publishPeers()
    }

    private fun publishPeers() {
        _peers.value = sessions
            .filter { it.isReady }
            .map { it.toPeerInfo() }
    }

    internal fun requestPairingApproval(session: PeerSession, deviceId: String, name: String, sas: String) {
        pendingPairing.add(PendingPairing(deviceId, name, sas, session))
        _pairingRequest.value = PairingRequest(deviceId, name, Crypto.formatSas(sas))
        addLog("WARN", "pairing request from '$name' - compare SAS ${Crypto.formatSas(sas)}")
    }

    /** Broadcast an event to every ready PC (Phase 2+ telemetry, headset changes). */
    fun broadcastEvent(name: String, data: Map<String, Any?> = emptyMap()) {
        val payload = Messages.event(name, data)
        sessions.filter { it.isReady }.forEach { it.sendControl(payload) }
    }

    // ------------------------------------------------------------------ //
    // RPC surface
    // ------------------------------------------------------------------ //
    private fun capabilities(): Map<String, Any?> = mapOf(
        "audio.playback" to true,
        "audio.capture" to true,
        // Reported as a probe result, not a promise: the real answer depends on
        // the headset and on whether a SCO link can be established off-call.
        "audio.bluetooth_sco" to AudioProbe.headsetConnected(context),
        "audio.bluetooth_profile" to AudioProbe.bluetoothProfileName(context),
        "camera.back" to true,
        "camera.front" to true,
        "sensors" to true,
        "file_transfer" to true,
        "bluetooth_scan" to false, // Phase 5, needs runtime permission
        "protocol_version" to Framing.PROTOCOL_VERSION,
    )

    /**
     * Handle one control request.
     *
     * Returns the bytes to send back, or **null** when the answer will be sent
     * later from another thread. That escape hatch exists for the capability
     * probe, which can run for minutes: blocking the connection's read loop that
     * long would starve the WebSocket heartbeats and drop the very link the
     * probe is reporting on.
     */
    private fun dispatchRpc(msg: Map<String, Any?>, peer: PeerSession): ByteArray? {
        val id = msg["id"]
        val method = msg["method"] as? String
            ?: return Messages.responseErr(id, "bad_request", "missing method")
        @Suppress("UNCHECKED_CAST")
        val params = (msg["params"] as? Map<String, Any?>) ?: emptyMap()

        return try {
            when (method) {
                "system.info" -> Messages.responseOk(
                    id,
                    mapOf(
                        "device_id" to identity.deviceId,
                        "name" to identity.name,
                        "model" to Build.MODEL,
                        "manufacturer" to Build.MANUFACTURER,
                        "android_version" to Build.VERSION.RELEASE,
                        "sdk_int" to Build.VERSION.SDK_INT,
                        "protocol_version" to Framing.PROTOCOL_VERSION,
                        "capabilities" to capabilities(),
                        "app_version" to appVersion(),
                    ),
                )

                "system.echo" -> Messages.responseOk(
                    id,
                    mapOf("echo" to params["value"], "at_ns" to System.nanoTime()),
                )

                "session.stats" -> Messages.responseOk(
                    id,
                    mapOf(
                        "frames_in" to framesIn.get(),
                        "frames_out" to framesOut.get(),
                        "bytes_in" to bytesIn.get(),
                        "bytes_out" to bytesOut.get(),
                        "connections" to sessions.count { it.isReady },
                    ),
                )

                "bluetooth.headset.status" -> Messages.responseOk(
                    id,
                    mapOf(
                        "connected" to AudioProbe.headsetConnected(context),
                        "profile" to AudioProbe.bluetoothProfileName(context),
                        "devices" to AudioProbe.describeCommunicationDevices(context),
                    ),
                )

                "audio.route.bluetooth" -> Messages.responseOk(
                    id,
                    mapOf(
                        "supported" to AudioProbe.headsetConnected(context),
                        "detail" to "Phase 2: SCO routing is exercised by the audio relay, " +
                            "not by this probe",
                    ),
                )

                "audio.capabilities.run" -> {
                    val seconds = ((params["seconds"] as? Number)?.toInt() ?: 30)
                        .coerceIn(5, 300)
                    val acoustic = params["acoustic"] as? Boolean ?: false
                    runCapabilityProbeAsync(id, seconds, acoustic, peer)
                    null // answered from the probe thread when it finishes
                }

                "audio.capabilities.last" -> Messages.responseOk(
                    id,
                    lastCapabilityReport?.let { mapOf("available" to true, "report" to it) }
                        ?: mapOf("available" to false, "reason" to "no probe has run yet"),
                )

                else -> Messages.responseErr(id, "method_not_found", "no handler for '$method'")
            }
        } catch (e: Exception) {
            addLog("ERROR", "RPC $method failed: ${e.message}")
            Messages.responseErr(id, "internal", "${e.javaClass.simpleName}: ${e.message}")
        }
    }

    private fun appVersion(): String = try {
        context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: "?"
    } catch (e: Exception) {
        "?"
    }

    // ------------------------------------------------------------------ //
    // per-connection state machine
    // ------------------------------------------------------------------ //

    /**
     * Handshake state of one [PeerSession].
     *
     * Declared here, in [BridgeServer], rather than inside [PeerSession] because
     * Kotlin forbids *any* nested class-like declaration (`enum class`, `object`,
     * `interface`, `companion object`, even a plain `class`) inside an `inner
     * class` — the compiler says `enum class is not allowed here`. An `inner`
     * class only exists together with an instance of its enclosing class, while
     * enum entries are static singletons, so the nesting has no valid meaning.
     * [PeerSession] must stay `inner` (it reaches this instance's sockets, log,
     * stats and pairing callbacks), so the enum moves out one level instead.
     *
     * `private` to this file's class and never used in a public signature, so it
     * stays an implementation detail exactly as before.
     */
    private enum class Stage { AWAIT_HELLO, AWAIT_AUTH, AWAIT_PAIR_CONFIRM, READY, DEAD }

    internal inner class PeerSession(
        private val conn: WebSocket,
        private val remote: String,
    ) {
        private val lock = Any()
        private var stage = Stage.AWAIT_HELLO
        private var responder: Crypto.HandshakeResponder? = null
        private var codec: Crypto.SecureCodec? = null

        var peerDeviceId: String = ""
            private set
        var peerName: String = "pc"
            private set
        private var connectedAt: Long = System.currentTimeMillis()

        private var pcConfirmedSas: String? = null
        private var userDecision: Boolean? = null

        val isReady: Boolean get() = synchronized(lock) { stage == Stage.READY }

        fun toPeerInfo(): PeerInfo =
            PeerInfo(peerDeviceId, peerName, remote, connectedAt)

        // --- inbound -------------------------------------------------- //
        fun onMessage(bytes: ByteArray) {
            try {
                handleEnvelope(Framing.decode(bytes))
            } catch (e: Crypto.HandshakeException) {
                addLog("ERROR", "protocol error from $peerName: ${e.message}")
                close("protocol error")
            } catch (e: Crypto.ReplayException) {
                addLog("ERROR", "replay/ordering violation from $peerName: ${e.message}")
                close("replay")
            } catch (e: Framing.FramingException) {
                addLog("ERROR", "framing error: ${e.message}")
                close("framing")
            } catch (e: Exception) {
                addLog("ERROR", "unexpected error: ${e.javaClass.simpleName}: ${e.message}")
                close("error")
            }
        }

        private fun handleEnvelope(env: Framing.Envelope) {
            synchronized(lock) {
                when (stage) {
                    Stage.AWAIT_HELLO -> onHello(env)
                    Stage.AWAIT_AUTH -> onAuth(env)
                    Stage.AWAIT_PAIR_CONFIRM -> onPairConfirm(env)
                    Stage.READY -> onSecure(env)
                    Stage.DEAD -> Unit
                }
            }
        }

        private fun onHello(env: Framing.Envelope) {
            if (env.kind != Framing.KIND_HANDSHAKE) {
                close("expected handshake")
                return
            }
            val hello = env.asJson()
            val claimedId = hello["id"] as? String ?: ""
            val claimedStatic = hello["static"] as? String ?: ""

            // Known device id with a different key: refuse, do not fall back to
            // pairing. That is the "phone got reflashed" signal.
            if (pairingStore.isPaired(claimedId) && !pairingStore.verify(claimedId, claimedStatic)) {
                addLog("WARN", "device $claimedId presented a different identity key; refusing")
                close("identity key changed for known device")
                return
            }

            val r = Crypto.HandshakeResponder(identity)
            val known = pairingStore.isPaired(claimedId)
            val ack = r.onHello(env, known)
            responder = r
            codec = r.keys?.let { Crypto.SecureCodec.forServer(it) }
            peerDeviceId = r.peerDeviceId ?: ""
            peerName = r.peerName ?: "pc"
            bumpHandshake()
            addLog(
                "INFO",
                "handshake with '$peerName' (${r.peerDeviceId}) known=$known " +
                    "SAS=${r.keys?.sasDigits}",
            )
            sendRaw(ack)
            stage = Stage.AWAIT_AUTH
        }

        private fun onAuth(env: Framing.Envelope) {
            val opened = codec?.unseal(env) ?: run { close("no keys"); return }
            if (opened.msgType != Messages.MSG_CONTROL) {
                close("expected auth")
                return
            }
            val msg = Json.toMap(opened.payload)
            if (msg["k"] != "auth") {
                close("expected auth")
                return
            }
            if ((msg["proto"] as? Number)?.toInt() != Framing.PROTOCOL_VERSION) {
                sendControl(Messages.handshake("auth_fail", "reason" to "protocol version"))
                close("version")
                return
            }
            val pcId = msg["id"] as? String ?: ""
            val pcName = msg["name"] as? String ?: "pc"
            val peerStatic = responder?.peerStaticKey?.let { Crypto.b64e(it) } ?: ""

            if (pcId != peerDeviceId) {
                sendControl(Messages.handshake("auth_fail", "reason" to "identity mismatch"))
                close("identity mismatch")
                return
            }
            if (pairingStore.isPaired(pcId) && !pairingStore.verify(pcId, peerStatic)) {
                sendControl(Messages.handshake("auth_fail", "reason" to "identity key changed"))
                close("identity key changed")
                return
            }

            if (pairingStore.isPaired(pcId)) {
                peerName = pcName
                markReady()
                sendControl(
                    Messages.handshake(
                        "auth_ok",
                        "id" to identity.deviceId,
                        "name" to identity.name,
                        "capabilities" to capabilities(),
                    )
                )
                return
            }

            // ---- pairing -------------------------------------------------- //
            val sas = responder?.keys?.sas ?: run { close("no sas"); return }
            sendControl(
                Messages.handshake(
                    "pair_required",
                    "id" to identity.deviceId,
                    "name" to identity.name,
                    "sas" to Crypto.formatSas(sas),
                    "capabilities" to capabilities(),
                )
            )
            stage = Stage.AWAIT_PAIR_CONFIRM
            requestPairingApproval(this, pcId, pcName, sas)
        }

        private fun onPairConfirm(env: Framing.Envelope) {
            val opened = codec?.unseal(env) ?: run { close("no keys"); return }
            if (opened.msgType != Messages.MSG_CONTROL) return
            val msg = Json.toMap(opened.payload)
            when (msg["k"]) {
                "pair_cancel" -> {
                    addLog("INFO", "PC cancelled pairing")
                    close("pairing cancelled")
                }

                "pair_confirm" -> {
                    pcConfirmedSas = (msg["sas"] as? String)?.replace(" ", "")
                    maybeFinishPairing()
                }

                else -> addLog("WARN", "unexpected message during pairing: ${msg["k"]}")
            }
        }

        fun onUserPairingDecision(approve: Boolean) {
            synchronized(lock) {
                if (stage != Stage.AWAIT_PAIR_CONFIRM) return
                userDecision = approve
                maybeFinishPairing()
            }
        }

        /** Completes only when *both* the PC confirmed and the user decided. */
        private fun maybeFinishPairing() {
            val pcSas = pcConfirmedSas ?: return
            val decision = userDecision ?: return
            val expectedSas = responder?.keys?.sas ?: return
            val expectedFormatted = Crypto.formatSas(expectedSas)

            if (pcSas != responseSasNormalised(expectedFormatted)) {
                addLog("WARN", "PC confirmed a mismatched SAS; refusing pairing")
                sendControl(Messages.handshake("pair_fail", "reason" to "SAS mismatch"))
                close("SAS mismatch")
                return
            }
            if (!decision) {
                sendControl(Messages.handshake("pair_fail", "reason" to "declined on phone"))
                close("declined")
                return
            }
            val staticKey = responder?.peerStaticKey
            if (staticKey == null) {
                close("internal")
                return
            }
            try {
                pairingStore.pin(peerDeviceId, peerName, Crypto.b64e(staticKey))
            } catch (e: IllegalStateException) {
                sendControl(Messages.handshake("pair_fail", "reason" to "already paired differently"))
                close("pin failed")
                return
            }
            markReady()
            sendControl(
                Messages.handshake("pair_ok", "id" to identity.deviceId, "name" to identity.name)
            )
            addLog("INFO", "paired with '$peerName'; identity key pinned")
        }

        private fun responseSasNormalised(formatted: String) = formatted.replace(" ", "")

        private fun markReady() {
            stage = Stage.READY
            connectedAt = System.currentTimeMillis()
            registerSession(this)
        }

        // --- steady state ---------------------------------------------- //
        private fun onSecure(env: Framing.Envelope) {
            if (env.kind == Framing.KIND_CLOSE) {
                close("PC closed")
                return
            }
            val opened = codec?.unseal(env) ?: return
            when (opened.msgType) {
                Messages.MSG_CONTROL -> onControl(opened.payload)
                Messages.MSG_PING -> {
                    val echoed = Messages.parsePingEcho(opened.payload)
                    sendSecure(Messages.MSG_PONG, Messages.STREAM_CONTROL, Messages.pong(echoed))
                }

                Messages.MSG_BLOB -> sendSecure(Messages.MSG_BLOB, opened.streamId, opened.payload)
                else -> addLog("WARN", "ignoring msg type 0x%02x".format(opened.msgType))
            }
        }

        private fun onControl(payload: ByteArray) {
            val msg = Json.toMap(payload)
            when (msg["k"]) {
                Messages.K_REQUEST -> dispatchRpc(msg, this)?.let { sendControl(it) }
                Messages.K_EVENT -> addLog("DEBUG", "event from PC: ${msg["event"]}")
                Messages.K_RESPONSE -> addLog("DEBUG", "unexpected response ${msg["id"]}")
                else -> Unit
            }
        }

        // --- outbound --------------------------------------------------- //
        fun sendControl(payload: ByteArray) =
            sendSecure(Messages.MSG_CONTROL, Messages.STREAM_CONTROL, payload)

        fun sendSecure(msgType: Int, streamId: Int, payload: ByteArray) {
            val c = codec ?: return
            try {
                val env = c.seal(msgType, streamId, payload)
                sendRaw(env)
            } catch (e: Exception) {
                addLog("ERROR", "send failed: ${e.message}")
                close("send failed")
            }
        }

        private fun sendRaw(bytes: ByteArray) {
            try {
                conn.send(bytes)
                bumpOut(bytes.size)
            } catch (e: Exception) {
                Log.w(TAG, "sendRaw failed", e)
            }
        }

        fun close(reason: String) {
            synchronized(lock) {
                if (stage == Stage.DEAD) return
                stage = Stage.DEAD
            }
            try {
                if (conn.isOpen) conn.close(1000, reason.take(120))
            } catch (e: Exception) {
                Log.w(TAG, "close failed", e)
            }
            unregisterSession(this)
        }

        fun onClosed() {
            synchronized(lock) { stage = Stage.DEAD }
            unregisterSession(this)
        }
    }

    // ------------------------------------------------------------------ //
    // Java-WebSocket plumbing
    // ------------------------------------------------------------------ //
    private inner class ServerImpl(address: InetSocketAddress) : WebSocketServer(address) {
        private val byConnection = Collections.synchronizedMap(mutableMapOf<WebSocket, PeerSession>())

        override fun onStart() {
            _running.value = true
            addLog("INFO", "WebSocket server listening on $address")
            bindLatch?.countDown()
        }

        override fun onOpen(conn: WebSocket, handshake: ClientHandshake) {
            val remote = conn.remoteSocketAddress?.address?.hostAddress ?: "?"
            addLog("INFO", "connection from $remote")
            byConnection[conn] = PeerSession(conn, remote)
        }

        override fun onClose(conn: WebSocket, code: Int, reason: String?, remote: Boolean) {
            byConnection.remove(conn)?.onClosed()
            addLog("INFO", "connection closed ($code ${reason ?: ""})")
            publishPeers()
        }

        override fun onMessage(conn: WebSocket, message: ByteBuffer) {
            val bytes = ByteArray(message.remaining())
            message.get(bytes)
            bumpIn(bytes.size)
            byConnection[conn]?.onMessage(bytes)
        }

        override fun onMessage(conn: WebSocket, message: String) {
            addLog("WARN", "text WebSocket frame ignored (protocol requires binary)")
            conn.close(1003, "binary only")
        }

        override fun onError(conn: WebSocket?, ex: Exception) {
            if (conn == null) {
                // Server-level failure (typically "address already in use"):
                // record it and release start() so it can report honestly.
                bindError = ex
                _lastError.value = "${ex.javaClass.simpleName}: ${ex.message}"
                addLog("ERROR", "server error: ${ex.message}")
                _running.value = false
                bindLatch?.countDown()
            } else {
                addLog("ERROR", "connection error: ${ex.message}")
            }
        }
    }
}
