package dev.phonebridge.service

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.ServiceCompat
import dev.phonebridge.MainActivity
import dev.phonebridge.R
import dev.phonebridge.net.BridgeServer
import dev.phonebridge.net.DiscoveryBeacon
import dev.phonebridge.net.IdentityStore
import dev.phonebridge.net.PairingStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch

/**
 * Foreground service that keeps the bridge alive while the screen is off.
 *
 * A foreground service is mandatory here: Android will otherwise freeze or kill
 * the process and the PC's audio stream would die the moment the phone locks.
 *
 * The types are the accurate description of what the service does: it holds a
 * connection to the PC (`connectedDevice`) and, through it, streams the camera,
 * streams the microphone and plays the PC's audio. Android 11+ silences a
 * microphone that is used from a foreground service without the `microphone`
 * type once the app is in the background, and Android 14+ refuses to start a
 * service whose types it was not granted the runtime permission for - so the
 * camera and microphone types are only claimed while their runtime permission
 * is actually held. See [foregroundTypes].
 */
class BridgeService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var server: BridgeServer? = null
    private var beacon: DiscoveryBeacon? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    private var startAttempts = 0

    /**
     * The notification-text collectors started for the current server.
     *
     * They used to be launched per `startBridge()` and never cancelled, so every
     * restart (Settings → Save, ACTION_RESTART, a retry after a failed bind) left
     * the previous pair running: N collectors all calling notify() on the same
     * notification, forever.
     */
    private var stateJobs: List<Job> = emptyList()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Android 8+ requires startForeground() shortly after
        // startForegroundService(), which is how this service is normally
        // entered. That applies to the stop and restart paths below too: doing
        // them before startForeground() got the process killed on Android 12+
        // (ForegroundServiceDidNotStartInTimeException) instead of stopped.
        promoteToForeground(
            buildNotification(if (server == null) "Starting…" else "Listening…")
        )

        when (intent?.action) {
            ACTION_STOP -> {
                stopBridge()
                stopSelf()
                return START_NOT_STICKY
            }

            ACTION_RESTART -> {
                stopBridge()
                startBridge()
                return START_STICKY
            }
        }

        // A null intent is the system re-creating a START_STICKY service after
        // it killed the process: rebuild everything from disk.
        if (server == null) startBridge()
        return START_STICKY
    }

    /**
     * Become a foreground service, claiming only the work this phone can do.
     *
     * `startForeground(id, notification)` would claim *every* type declared in the
     * manifest, and on Android 14+ a camera/microphone type without its runtime
     * permission is a SecurityException that kills the service. So the types are
     * computed from `checkSelfPermission` instead, and a refusal (a vendor ROM, or
     * a permission revoked between the check and the call) falls back to the
     * connection-only type, which needs no runtime permission at all: the bridge
     * always comes up.
     */
    private fun promoteToForeground(notification: Notification) {
        val types = foregroundTypes()
        try {
            ServiceCompat.startForeground(this, NOTIFICATION_ID, notification, types)
            return
        } catch (e: RuntimeException) {
            android.util.Log.w("BridgeService", "startForeground(type=$types) refused: ${e.message}")
        }
        ServiceCompat.startForeground(this, NOTIFICATION_ID, notification, connectionType())
    }

    /** The types this phone has the runtime permissions for, right now. */
    private fun foregroundTypes(): Int {
        var types = connectionType()
        if (types == 0) return 0 // below API 29 the platform has no typed services
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && hasPermission(Manifest.permission.RECORD_AUDIO)) {
            types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        }
        if (hasPermission(Manifest.permission.CAMERA)) {
            types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
        }
        return types
    }

    /**
     * connectedDevice (the connection) plus mediaPlayback (the AudioTrack). Both
     * are normal permissions granted at install, so this pair can never be
     * refused; `0` on API < 29, where typed services do not exist.
     */
    private fun connectionType(): Int {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return 0
        return ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE or
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
    }

    private fun hasPermission(permission: String): Boolean =
        checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED

    private fun startBridge() {
        if (server != null) return
        val identityStore = IdentityStore(this)
        val pairingStore = PairingStore(this)
        val identity = identityStore.loadOrCreate(defaultDeviceName())
        val port = getSharedPreferences("phonebridge_settings", MODE_PRIVATE)
            .getInt("port", BridgeServer.DEFAULT_PORT)

        val srv = BridgeServer(this, identity, pairingStore, port)
        server = srv
        BridgeState.bind(srv, scope, applicationContext)
        BridgeState.setPort(port)

        val started = srv.start()
        if (started.isFailure) {
            retryStart(srv, port, started.exceptionOrNull())
            return
        }
        startAttempts = 0

        beacon = DiscoveryBeacon(identity.deviceId, identity.name, port).also { it.start() }
        acquireWakeLock()
        watchNetwork()
        updateNotification("Listening on port $port")

        // Keep the notification text roughly in sync with reality.
        stateJobs = listOf(
            scope.launch {
                BridgeState.peers.collectLatest { peers ->
                    val text = when {
                        peers.isEmpty() -> "Listening on port $port · no PC connected"
                        peers.size == 1 -> "Connected: ${peers[0].name}"
                        else -> "${peers.size} PCs connected"
                    }
                    updateNotification(text)
                }
            },
            scope.launch {
                BridgeState.pairingRequest.collectLatest { request ->
                    if (request != null) {
                        updateNotification("Pairing request from ${request.name} — open the app")
                    }
                }
            },
        )
    }

    /**
     * Handle a failed bind.
     *
     * The common cause is a socket that the previous run has not released yet
     * (restart from the notification, or the system re-creating the service), so
     * a short retry wins where an immediate stopSelf() would leave the user with
     * a bridge that never came up and no explanation. Retries are bounded; after
     * that the error is shown and the service stops honestly.
     */
    private fun retryStart(srv: BridgeServer, port: Int, error: Throwable?) {
        val detail = error?.message ?: "unknown error"
        startAttempts++
        val wait = START_RETRY_DELAYS_MS.getOrNull(startAttempts - 1)
        if (wait == null) {
            BridgeState.denyAllPairings()
            updateNotification("Cannot listen on port $port: $detail")
            stopBridge()
            stopSelf()
            return
        }
        updateNotification("Port $port unavailable ($detail) — retrying in ${wait / 1000}s")
        scope.launch {
            delay(wait)
            if (server === srv) {
                stopBridge()
                startBridge()
            }
        }
    }

    /**
     * Follow network changes.
     *
     * The listening socket survives a Wi-Fi dropout, but the discovery beacon's
     * datagram socket is bound to the old interface and its broadcasts go
     * nowhere after the phone rejoins a network. Rebinding it keeps discovery
     * working; the PC still reconnects to the same port on the new address.
     */
    private fun watchNetwork() {
        if (networkCallback != null) return
        val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager ?: return
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                scope.launch {
                    val b = beacon
                    if (b != null) {
                        b.restart()
                        updateNotification("Network changed — re-advertising on ${BridgeState.port.value}")
                    }
                }
            }

            override fun onLost(network: Network) {
                scope.launch { updateNotification("Network lost — waiting for a network") }
            }
        }
        networkCallback = callback
        try {
            cm.registerDefaultNetworkCallback(callback)
        } catch (e: Exception) {
            // Not fatal: discovery is a convenience, and the PC can still
            // connect to the address it used before.
            networkCallback = null
            updateNotification("Network monitoring unavailable: ${e.message}")
        }
    }

    private fun stopBridge() {
        stateJobs.forEach { it.cancel() }
        stateJobs = emptyList()
        networkCallback?.let { callback ->
            val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            try {
                cm?.unregisterNetworkCallback(callback)
            } catch (e: Exception) {
                // Already unregistered (or the process is going away).
                android.util.Log.w("BridgeService", "unregisterNetworkCallback: ${e.message}")
            }
        }
        networkCallback = null
        beacon?.stop()
        beacon = null
        server?.stop()
        server = null
        BridgeState.unbind()
        releaseWakeLock()
    }

    override fun onDestroy() {
        stopBridge()
        scope.cancel()
        // Without this the "Listening on port …" notification can outlive the
        // service when the system kills and re-creates it.
        @Suppress("DEPRECATION")
        stopForeground(true)
        super.onDestroy()
    }

    // ------------------------------------------------------------------ //
    private fun defaultDeviceName(): String =
        "${Build.MANUFACTURER.replaceFirstChar { it.uppercase() }} ${Build.MODEL}".trim()

    private fun acquireWakeLock() {
        // A partial wake lock is what keeps the socket alive with the screen off.
        // It is held only while the service runs and released in stopBridge().
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "PhoneBridge::bridge").apply {
            setReferenceCounted(false)
            // Bounded so a crash cannot leak the lock forever. 12 h covers a
            // working day; the service is restarted long before that in practice.
            acquire(12L * 60 * 60 * 1000)
        }
    }

    private fun releaseWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            "PhoneBridge bridge",
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = "Keeps the phone connected to your PC"
            setShowBadge(false)
        }
        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
            .createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, BridgeService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setContentTitle("PhoneBridge")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(
                Notification.Action.Builder(
                    android.graphics.drawable.Icon.createWithResource(
                        this, android.R.drawable.ic_menu_close_clear_cancel
                    ),
                    "Stop",
                    stop,
                ).build()
            )
            .build()
    }

    private fun updateNotification(text: String) {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.notify(NOTIFICATION_ID, buildNotification(text))
    }

    companion object {
        private const val CHANNEL_ID = "phonebridge_bridge"
        private const val NOTIFICATION_ID = 4711
        const val ACTION_START = "dev.phonebridge.action.START"
        const val ACTION_STOP = "dev.phonebridge.action.STOP"
        const val ACTION_RESTART = "dev.phonebridge.action.RESTART"

        /** Backoff for a bind that failed because the old socket was still closing. */
        private val START_RETRY_DELAYS_MS = longArrayOf(1_000L, 2_000L, 4_000L)

        fun start(context: Context) {
            val intent = Intent(context, BridgeService::class.java).setAction(ACTION_START)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, BridgeService::class.java).setAction(ACTION_STOP)
            )
        }

        /**
         * Restart in place.
         *
         * A stop-then-start with a sleep between them (what the UI used to do)
         * raced the old socket's teardown: the rebind could hit EADDRINUSE and
         * the bridge silently stayed down. The service now owns the ordering.
         */
        fun restart(context: Context) {
            val intent = Intent(context, BridgeService::class.java).setAction(ACTION_RESTART)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }
    }
}
