package dev.phonebridge.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import dev.phonebridge.MainActivity
import dev.phonebridge.R
import dev.phonebridge.net.BridgeServer
import dev.phonebridge.net.DiscoveryBeacon
import dev.phonebridge.net.IdentityStore
import dev.phonebridge.net.PairingStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch

/**
 * Foreground service that keeps the bridge alive while the screen is off.
 *
 * A foreground service is mandatory here: Android will otherwise freeze or kill
 * the process and the PC's audio stream would die the moment the phone locks.
 * The service type is `connectedDevice`, which is the accurate declaration for
 * "maintaining a connection to an external device" over USB/Wi-Fi.
 */
class BridgeService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var server: BridgeServer? = null
    private var beacon: DiscoveryBeacon? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
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

        startForeground(NOTIFICATION_ID, buildNotification("Starting…"))
        if (server == null) startBridge()
        return START_STICKY
    }

    private fun startBridge() {
        val identityStore = IdentityStore(this)
        val pairingStore = PairingStore(this)
        val identity = identityStore.loadOrCreate(defaultDeviceName())
        val port = getSharedPreferences("phonebridge_settings", MODE_PRIVATE)
            .getInt("port", BridgeServer.DEFAULT_PORT)

        val srv = BridgeServer(this, identity, pairingStore, port)
        server = srv
        BridgeState.bind(srv, scope)
        BridgeState.setPort(port)

        srv.start().onFailure { error ->
            BridgeState.answerPairing(false)
            updateNotification("Error: ${error.message}")
            stopBridge()
            stopSelf()
        }

        if (srv.running.value) {
            beacon = DiscoveryBeacon(identity.deviceId, identity.name, port).also { it.start() }
            acquireWakeLock()
            updateNotification("Listening on port $port")
        }

        // Keep the notification text roughly in sync with reality.
        scope.launch {
            BridgeState.peers.collectLatest { peers ->
                val text = when {
                    peers.isEmpty() -> "Listening on port $port · no PC connected"
                    peers.size == 1 -> "Connected: ${peers[0].name}"
                    else -> "${peers.size} PCs connected"
                }
                updateNotification(text)
            }
        }
        scope.launch {
            BridgeState.pairingRequest.collectLatest { request ->
                if (request != null) {
                    updateNotification("Pairing request from ${request.name} — open the app")
                }
            }
        }
    }

    private fun stopBridge() {
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
    }
}
