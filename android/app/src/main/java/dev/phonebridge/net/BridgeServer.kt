package dev.phonebridge.net

import android.content.Context
import android.util.Log
import dev.phonebridge.Permissions
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
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
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

        /**
         * How long a peer gets to finish HELLO -> AUTH -> READY.
         *
         * A socket that opens and then says nothing used to hold a PeerSession
         * (and a WebSocket) forever; whoever can reach port 8765 could exhaust
         * the process just by connecting and staying silent.
         */
        private const val SETUP_TIMEOUT_S = 20L

        /** Hard ceilings so one host cannot consume the bridge. */
        private const val MAX_SESSIONS = 8
        private const val MAX_SESSIONS_PER_DEVICE = 4

        /** A device id is the first 16 hex characters of SHA-256(pubkey). */
        private val DEVICE_ID = Regex("^[0-9a-f]{16}$")

        /**
         * How many un-answered pairing requests may exist at once.
         *
         * Every pending request holds a session, and an attacker on the LAN can
         * produce them faster than a human can read them. Beyond this the phone
         * refuses the request outright instead of queueing it.
         */
        private const val MAX_PENDING_PAIRINGS = 4

        /** ANSI escapes: they can move a cursor and retitle a window. */
        private val ANSI_ESCAPE = Regex("\u001B\\[[0-9;?]*[ -/]*[@-~]")

        /**
         * Peer-supplied text, made safe for the log, the UI and the pin store.
         *
         * The PC picks its own name. Control characters would let it forge log
         * lines and escapes would let it repaint the screen the user is reading,
         * so anything that is not printable text is dropped here, once, at the
         * boundary.
         */
        internal fun safeLabel(value: Any?, limit: Int = 64, fallback: String = "pc"): String {
            val raw = (value as? String) ?: value?.toString() ?: return fallback
            val cleaned = ANSI_ESCAPE.replace(raw, "")
                .filter { !it.isISOControl() }
                .replace(Regex("[ \\t\\u00a0]+"), " ")
                .trim()
                .take(limit)
            return cleaned.ifBlank { fallback }
        }
    }

    data class PeerInfo(
        val deviceId: String,
        val name: String,
        val remoteAddress: String,
        val connectedAt: Long,
    )

    /**
     * One pending pairing, identified by [token].
     *
     * The decision must name the request it answers: approving "whatever is
     * pending" would grant every concurrent pairing request on the strength of
     * one code the user only looked at once.
     */
    data class PairingRequest(
        val deviceId: String,
        val name: String,
        val sas: String,
        val token: Long = 0L,
    )

    data class LogLine(val at: String, val level: String, val message: String)

    data class Stats(
        val framesIn: Long = 0,
        val framesOut: Long = 0,
        val bytesIn: Long = 0,
        val bytesOut: Long = 0,
        val handshakes: Long = 0,
    )

    /**
     * Phase 2 audio. One engine for the phone: it is refused until a session is
     * authenticated (there is nobody to send frames to otherwise), and it is
     * released when the last session ends so no microphone outlives a window.
     */
    private val audio = AudioEngine(
        context = context,
        sendFrame = { streamId, body -> sendAudioFrame(streamId, body) },
        log = { level, message -> addLog(level, message) },
    )

    private fun sendAudioFrame(streamId: Int, body: ByteArray) {
        val session = sessions.firstOrNull { it.isReady }
        if (session == null) {
            addLog("WARN", "dropping audio frame: no authenticated session")
            return
        }
        session.sendSecure(Messages.MSG_AUDIO, streamId, body)
    }

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

    /** Enforces [SETUP_TIMEOUT_S] for connections that never reach READY. */
    private var setupTimer: ScheduledExecutorService? = null

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

    /**
     * True while a capability probe is queued or running.
     *
     * The executor has one thread, so without this a PC (or a looping script)
     * could queue probe after probe: each one can hold the microphone for five
     * minutes, and every queued request is a promised answer that will be sent
     * long after the caller gave up.
     */
    private val probeInFlight = AtomicBoolean(false)

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
            } finally {
                // Released before the answer is sent: the next probe may start as
                // soon as this one stopped touching the audio path.
                probeInFlight.set(false)
            }
            peer.sendControl(payload)
        }
    }

    @Volatile
    private var pendingPairing: MutableList<PendingPairing> =
        Collections.synchronizedList(mutableListOf())

    private val pairingTokens = AtomicLong()

    private data class PendingPairing(
        val token: Long,
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
            setupTimer = Executors.newSingleThreadScheduledExecutor { runnable ->
                Thread(runnable, "pb-setup-timeout").apply { isDaemon = true }
            }
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
        denyAllPairings("server stopping")
        sessions.toList().forEach { it.close("server stopping", bye = true) }
        sessions.clear()
        pendingPairing.clear()
        _pairingRequest.value = null
        setupTimer?.shutdownNow()
        setupTimer = null
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

    /**
     * Answer exactly the pairing request the user is looking at.
     *
     * [token] must be the token of that request. An unknown or stale token is
     * refused loudly rather than applied to a different PC's session.
     */
    fun answerPairing(token: Long, approve: Boolean) {
        val target = pendingPairing.firstOrNull { it.token == token }
        if (target == null) {
            addLog("WARN", "pairing decision for unknown/stale request $token ignored")
            _pairingRequest.value = pendingPairing.firstOrNull()?.toRequest()
            return
        }
        pendingPairing.remove(target)
        // Show the next waiter (one SAS on screen at a time), or clear it.
        _pairingRequest.value = pendingPairing.firstOrNull()?.toRequest()
        target.session.onUserPairingDecision(approve)
    }

    /** Refuse everything pending: used when the server is going down. */
    fun denyAllPairings(reason: String = "bridge stopping") {
        val pending = pendingPairing.toList()
        pendingPairing.clear()
        _pairingRequest.value = null
        pending.forEach { it.session.onUserPairingDecision(false) }
        if (pending.isNotEmpty()) addLog("WARN", "refused ${pending.size} pending pairing(s): $reason")
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

    /**
     * Admit a READY session, enforcing the per-device and global ceilings.
     *
     * Returns the refusal reason when the session must be refused; the caller
     * closes it with that reason so the PC can show it. Bounded because an attacker on the LAN can
     * otherwise open sessions until the process runs out of sockets.
     */
    internal fun admitSession(session: PeerSession, deviceId: String): String? {
        val ready = sessions.count { it.isReady }
        val forDevice = sessions.count { it.isReady && it.peerDeviceId == deviceId }
        val refusal = when {
            forDevice >= MAX_SESSIONS_PER_DEVICE -> "too many connections from this device"
            ready >= MAX_SESSIONS -> "server connection limit reached"
            else -> null
        }
        if (refusal != null) {
            addLog("WARN", "refusing session from '$deviceId': $refusal")
            return refusal
        }
        sessions.add(session)
        publishPeers()
        return null
    }

    internal fun unregisterSession(session: PeerSession) {
        sessions.remove(session)
        publishPeers()
        // The last PC leaving must not leave a microphone or an AudioTrack open.
        releaseAudioIfIdle()
    }

    private fun publishPeers() {
        // One row per PC, however many sessions it holds (a `watch` and a
        // `ping` at the same time are two sessions, one peer).
        _peers.value = sessions
            .filter { it.isReady }
            .sortedByDescending { it.toPeerInfo().connectedAt }
            .distinctBy { it.toPeerInfo().deviceId }
            .map { it.toPeerInfo() }
    }

    internal fun requestPairingApproval(
        session: PeerSession,
        deviceId: String,
        name: String,
        sas: String,
    ): Boolean {
        if (pendingPairing.size >= MAX_PENDING_PAIRINGS) {
            addLog(
                "WARN",
                "refusing pairing request from '$name': " +
                    "${pendingPairing.size} requests are already waiting for an answer",
            )
            return false
        }
        val token = pairingTokens.incrementAndGet()
        val pending = PendingPairing(token, deviceId, name, sas, session)
        pendingPairing.add(pending)
        _pairingRequest.value = pending.toRequest()
        addLog(
            "WARN",
            "pairing request #$token from '$name' - the code is shown on screen only",
        )
        return true
    }

    /**
     * Forget the pairing request that belongs to a session that just ended.
     *
     * Without this the entry outlived its socket: the prompt stayed on screen
     * and the list grew for as long as the process ran, because nothing ever
     * removed a request whose PC had gone away.
     */
    private fun dropPendingPairing(session: PeerSession) {
        synchronized(pendingPairing) {
            val removed = pendingPairing.filter { it.session === session }
            if (removed.isEmpty()) return
            pendingPairing.removeAll(removed)
            addLog(
                "INFO",
                "dropped ${removed.size} pairing request(s) from a closed session",
            )
            _pairingRequest.value = pendingPairing.firstOrNull()?.toRequest()
        }
    }

    private fun PendingPairing.toRequest(): PairingRequest =
        PairingRequest(deviceId, name, Crypto.formatSas(sas), token)

    /** Broadcast an event to every ready PC (Phase 2+ telemetry, headset changes). */
    fun broadcastEvent(name: String, data: Map<String, Any?> = emptyMap()) {
        val payload = Messages.event(name, data)
        sessions.filter { it.isReady }.forEach { it.sendControl(payload) }
    }

    // ------------------------------------------------------------------ //
    // RPC surface
    // ------------------------------------------------------------------ //

    /**
     * What this phone can do *through PhoneBridge v1 today*.
     *
     * Every value is measured by [DeviceFacts] and gated on the feature actually
     * existing. The version this replaced returned `true` for camera, sensors and
     * file transfer without checking anything, so a PC had no way to tell a real
     * capability from a placeholder. Hardware facts are still published, under
     * `hardware.*`, and a per-key explanation under `detail.*`.
     */
    private fun capabilities(): Map<String, Any?> = DeviceFacts.capabilities(context)

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
                "system.info" -> {
                    // One source for the device block, shared with the app's
                    // "This device" card, so the two cannot disagree.
                    val info = linkedMapOf<String, Any?>(
                        "device_id" to identity.deviceId,
                        "name" to identity.name,
                    )
                    info.putAll(DeviceFacts.deviceInfo(context))
                    info["capabilities"] = capabilities()
                    info["permissions"] = Permissions.wireSnapshot(context)
                    Messages.responseOk(id, info)
                }

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

                // --- Phase 2 audio ------------------------------------- //
                // The result maps are built here, not inside AudioEngine, so the
                // wire contract stays readable next to the method name (and the
                // protocol contract test can check it against docs/03).
                "audio.playback.start" -> {
                    val state = audio.startPlayback(params)
                    Messages.responseOk(
                        id,
                        mapOf(
                            "ok" to true,
                            "format" to state["format"],
                            "detail" to (state["detail"] ?: "playing"),
                        ),
                    )
                }

                "audio.playback.stop" -> {
                    audio.stopPlayback()
                    Messages.responseOk(id, mapOf("ok" to true, "playback" to "idle"))
                }

                "audio.playback.mute" -> {
                    val mutedNow = audio.setMuted(params["muted"] as? Boolean ?: true)
                    Messages.responseOk(
                        id,
                        mapOf("ok" to true, "muted" to mutedNow["muted"]),
                    )
                }

                "audio.playback.volume" -> {
                    val applied = audio.setVolume((params["percent"] as? Number)?.toInt() ?: 40)
                    Messages.responseOk(
                        id,
                        mapOf("ok" to true, "percent" to applied["percent"]),
                    )
                }

                "audio.mic.start" -> {
                    val state = audio.startMic(params)
                    Messages.responseOk(id, mapOf("ok" to true, "format" to state["format"]))
                }

                "audio.mic.stop" -> {
                    audio.stopMic()
                    Messages.responseOk(id, mapOf("ok" to true, "mic" to "idle"))
                }

                "audio.status" -> {
                    val s = audio.status()
                    Messages.responseOk(
                        id,
                        mapOf(
                            "playback" to s["playback"],
                            "mic" to s["mic"],
                            "volume" to s["volume"],
                            "muted" to s["muted"],
                            "permitted" to s["permitted"],
                            "mic_muted" to s["mic_muted"],
                            "format" to s["format"],
                            "frames_in" to s["frames_in"],
                            "frames_out" to s["frames_out"],
                            "dropped" to s["dropped"],
                            "last_error" to s["last_error"],
                        ),
                    )
                }

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
                    if (!probeInFlight.compareAndSet(false, true)) {
                        // Refusing is honest: the answer would otherwise arrive
                        // minutes later, after the PC had already timed out.
                        Messages.responseErr(
                            id,
                            "busy",
                            "a capability probe is already running on this phone",
                        )
                    } else {
                        val seconds = ((params["seconds"] as? Number)?.toInt() ?: 30)
                            .coerceIn(5, 300)
                        val acoustic = params["acoustic"] as? Boolean ?: false
                        runCapabilityProbeAsync(id, seconds, acoustic, peer)
                        null // answered from the probe thread when it finishes
                    }
                }

                "audio.capabilities.last" -> Messages.responseOk(
                    id,
                    lastCapabilityReport?.let { mapOf("available" to true, "report" to it) }
                        ?: mapOf("available" to false, "reason" to "no probe has run yet"),
                )

                else -> Messages.responseErr(id, "method_not_found", "no handler for '$method'")
            }
        } catch (e: RpcFailure) {
            // A refusal with a documented code (docs/03 §7) and a sentence the PC
            // shows verbatim: "microphone permission is not granted", not "bad_params".
            addLog("INFO", "RPC $method refused: ${e.code} - ${e.message}")
            Messages.responseErr(id, e.code, e.message ?: e.code)
        } catch (e: Exception) {
            addLog("ERROR", "RPC $method failed: ${e.message}")
            Messages.responseErr(id, "internal", "${e.javaClass.simpleName}: ${e.message}")
        }
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
        private var setupTimeoutTask: ScheduledFuture<*>? = null

        init {
            // Armed before the first frame arrives: a silent socket must not be
            // able to sit in the setup states indefinitely.
            setupTimeoutTask = setupTimer?.schedule({
                val state = synchronized(lock) { stage }
                if (state != Stage.READY && state != Stage.DEAD) {
                    if (state == Stage.AWAIT_PAIR_CONFIRM) {
                        // A prompt nobody answered is an expired prompt: it must
                        // not stay on screen (or stay answerable) any longer.
                        addLog(
                            "WARN",
                            "pairing request from $remote expired after ${SETUP_TIMEOUT_S}s " +
                                "with no decision",
                        )
                    } else {
                        addLog(
                            "WARN",
                            "connection from $remote did not finish setup in ${SETUP_TIMEOUT_S}s",
                        )
                    }
                    close("setup timeout", code = 1002)
                }
            }, SETUP_TIMEOUT_S, TimeUnit.SECONDS)
        }

        private fun cancelSetupTimeout() {
            setupTimeoutTask?.cancel(false)
            setupTimeoutTask = null
        }
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
                close("expected handshake", code = 1002)
                return
            }
            val hello = env.asJson()
            val claimedId = hello["id"] as? String ?: ""
            val claimedStatic = hello["static"] as? String ?: ""

            // The device id is SHA-256(pubkey)[:16]. Anything else cannot be
            // pinned, cannot be looked up, and must not reach the pairing path
            // (an empty id used to be stored as a pin for "").
            if (!DEVICE_ID.matches(claimedId)) {
                addLog("WARN", "rejecting hello with malformed device id '$claimedId'")
                close("bad hello", code = 1002)
                return
            }

            // Known device id with a different key: refuse, do not fall back to
            // pairing. That is the "phone got reflashed" signal.
            if (pairingStore.isPaired(claimedId) && !pairingStore.verify(claimedId, claimedStatic)) {
                addLog("WARN", "device $claimedId presented a different identity key; refusing")
                close("identity key changed for known device", code = 1008)
                return
            }

            val r = Crypto.HandshakeResponder(identity)
            val known = pairingStore.isPaired(claimedId)
            val ack = r.onHello(env, known)
            responder = r
            codec = r.keys?.let { Crypto.SecureCodec.forServer(it) }
            peerDeviceId = r.peerDeviceId ?: ""
            peerName = safeLabel(r.peerName)
            bumpHandshake()
            // The six digits are for the screen the human compares, not for a
            // log file that outlives the pairing.
            addLog("INFO", "handshake with '$peerName' (${r.peerDeviceId}) known=$known")
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
                close("version", code = 1002)
                return
            }
            val pcId = msg["id"] as? String ?: ""
            val pcName = msg["name"] as? String ?: "pc"
            val peerStatic = responder?.peerStaticKey?.let { Crypto.b64e(it) } ?: ""

            if (pcId != peerDeviceId) {
                sendControl(Messages.handshake("auth_fail", "reason" to "identity mismatch"))
                close("identity mismatch", code = 1008)
                return
            }
            if (pairingStore.isPaired(pcId) && !pairingStore.verify(pcId, peerStatic)) {
                sendControl(Messages.handshake("auth_fail", "reason" to "identity key changed"))
                close("identity key changed", code = 1008)
                return
            }

            if (pairingStore.isPaired(pcId)) {
                peerName = safeLabel(pcName)
                // markReady() closes the session itself when we are at a limit.
                if (!markReady()) return
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
            if (!requestPairingApproval(this, pcId, peerName, sas)) {
                sendControl(
                    Messages.handshake(
                        "pair_fail",
                        "reason" to "too many pending pairing requests",
                    ),
                )
                close("too many pending pairing requests", code = 1008)
            }
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
                close("SAS mismatch", code = 1008)
                return
            }
            if (!decision) {
                sendControl(Messages.handshake("pair_fail", "reason" to "declined on phone"))
                close("declined", code = 1008)
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
            if (!markReady()) {
                addLog("WARN", "paired with '$peerName' but the session was refused")
                return
            }
            sendControl(
                Messages.handshake("pair_ok", "id" to identity.deviceId, "name" to identity.name)
            )
            addLog("INFO", "paired with '$peerName'; identity key pinned")
        }

        private fun responseSasNormalised(formatted: String) = formatted.replace(" ", "")

        /** @return true when this session was admitted; false means it was closed. */
        /** Called whenever a session leaves the ready set. */
    private fun releaseAudioIfIdle() {
        if (sessions.none { it.isReady }) {
            audio.release()
        }
    }

    private fun markReady(): Boolean {
            cancelSetupTimeout()
            val refusal = admitSession(this, peerDeviceId)
            if (refusal != null) {
                close(refusal, code = 1008)
                return false
            }
            stage = Stage.READY
            connectedAt = System.currentTimeMillis()
            // admitSession() published the peer list while this session was
            // still in a setup stage, so it was filtered out: republish now that
            // the session counts as ready, or the UI would not show the PC until
            // some unrelated event changed the list.
            publishPeers()
            return true
        }

        // --- steady state ---------------------------------------------- //
        private fun onSecure(env: Framing.Envelope) {
            if (env.kind == Framing.KIND_CLOSE) {
                close("PC closed")
                return
            }
            val opened = codec?.unseal(env)
            if (opened == null) {
                close("no session keys")
                return
            }
            when (opened.msgType) {
                Messages.MSG_CONTROL -> onControl(opened.payload)
                Messages.MSG_PING -> {
                    val echoed = Messages.parsePingEcho(opened.payload)
                    sendSecure(Messages.MSG_PONG, Messages.STREAM_CONTROL, Messages.pong(echoed))
                }

                Messages.MSG_BYE -> {
                    val reason = String(opened.payload, Charsets.UTF_8).trim()
                    addLog("INFO", "PC said bye${if (reason.isEmpty()) "" else ": $reason"}")
                    close("PC said bye")
                }

                Messages.MSG_AUDIO -> audio.onFrame(opened.streamId, opened.payload)

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
            val c = codec
            if (c == null) {
                // Unreachable in a correct run: nothing is sent before the
                // handshake produced keys. Loud, because silently dropping a
                // frame the caller believes was sent is how sessions hang.
                addLog("WARN", "dropping msg 0x%02x: no session keys yet".format(msgType))
                return
            }
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
                // A failed send means the socket is gone; keeping the session
                // "open" would keep answering RPCs that can never be delivered.
                addLog("ERROR", "send failed: ${e.javaClass.simpleName}: ${e.message}")
                close("send failed")
            }
        }

        /**
         * Tear the session down.
         *
         * [bye] sends the protocol-v1 BYE frame first. Use it for intentional
         * shutdowns (user pressed Stop, service torn down) so the PC records
         * "the phone stopped" instead of "the link died" and does not treat a
         * planned stop as a network failure worth reconnecting to.
         */
        fun close(reason: String, bye: Boolean = false, code: Int = 1000) {
            cancelSetupTimeout()
            // Only the first caller gets to tear down (and to send BYE).
            synchronized(lock) {
                if (stage == Stage.DEAD) return
                stage = Stage.DEAD
            }
            if (bye) {
                // Best effort: the socket may already be gone.
                runCatching {
                    codec?.let { c ->
                        sendRaw(c.seal(Messages.MSG_BYE, Messages.STREAM_CONTROL, reason.toByteArray()))
                    }
                }.onFailure { addLog("WARN", "could not send BYE: ${it.message}") }
            }
            try {
                if (conn.isOpen) conn.close(code, reason.take(120))
            } catch (e: Exception) {
                Log.w(TAG, "close failed", e)
            }
            dropPendingPairing(this)
            unregisterSession(this)
        }

        fun onClosed() {
            cancelSetupTimeout()
            synchronized(lock) { stage = Stage.DEAD }
            dropPendingPairing(this)
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
            // Only browsers send Origin, and a browser must never drive the
            // bridge: WebSocket bypasses the same-origin policy entirely, so a
            // page the user visits could otherwise open ws://<phone-ip>:8765,
            // complete a handshake with its own keys and provoke a pairing
            // prompt. Native clients (our PC app, the simulator) send no Origin.
            val origin = handshake.getFieldValue("Origin")
            if (!origin.isNullOrBlank()) {
                addLog(
                    "WARN",
                    "refusing connection from $remote: browser Origin " +
                        "'${safeLabel(origin, limit = 80)}' (only native clients may connect)",
                )
                runCatching { conn.close(1008, "browser origins are not accepted") }
                return
            }
            if (byConnection.size >= MAX_SESSIONS) {
                addLog("WARN", "refusing connection from $remote: server connection limit reached")
                runCatching { conn.close(1008, "server connection limit reached") }
                return
            }
            addLog("INFO", "connection from $remote")
            byConnection[conn] = PeerSession(conn, remote)
        }

        override fun onClose(conn: WebSocket, code: Int, reason: String?, remote: Boolean) {
            byConnection.remove(conn)?.onClosed()
            addLog("INFO", "connection closed ($code ${reason ?: ""})")
            publishPeers()
        }

        override fun onMessage(conn: WebSocket, message: ByteBuffer) {
            // Checked before the copy: the 8 MiB ceiling is enforced again in
            // Framing.decode, but by then the whole hostile payload has already
            // been copied into a fresh array -- repeatedly, once per frame.
            if (message.remaining() > Framing.MAX_ENVELOPE_BODY) {
                addLog(
                    "WARN",
                    "refusing oversized frame from " +
                        "${conn.remoteSocketAddress?.address?.hostAddress ?: "?"}: " +
                        "${message.remaining()} bytes (limit ${Framing.MAX_ENVELOPE_BODY})",
                )
                runCatching { conn.close(1009, "frame too large") }
                return
            }
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
