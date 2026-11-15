package dev.phonebridge.net

import android.util.Log
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.NetworkInterface
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * Broadcasts the same unauthenticated convenience beacon as
 * `windows/phonebridge/transport/discovery.py`. The handshake still decides trust.
 *
 * Directed per-interface broadcasts are sent first so a hotspot beacon is not
 * stolen by the mobile-data default route; 255.255.255.255 is kept as a fallback.
 *
 * Lifecycle bug fixed here: `stop(); start()` used to let the old worker's
 * `finally` close the *new* worker's socket through a shared field. The phone
 * then logged `beacon send to /255.255.255.255 failed: Socket is closed` forever.
 * Each worker now closes only the socket it created, and only clears the shared
 * field if that field still points at its own socket. Public lifecycle calls are
 * serialized, and stop joins the old worker briefly before restart creates another.
 */
class DiscoveryBeacon(
    private val deviceId: String,
    private val deviceName: String,
    private val port: Int = BridgeServer.DEFAULT_PORT,
) {
    companion object {
        private const val TAG = "DiscoveryBeacon"
        const val DISCOVERY_PORT = 47823
        private const val INTERVAL_MS = 2000L
        private const val JOIN_TIMEOUT_MS = 250L

        /** Every usable IPv4 address this phone holds. */
        fun localAddresses(): List<String> = try {
            NetworkInterface.getNetworkInterfaces().toList()
                .filter { it.isUp && !it.isLoopback }
                .flatMap { it.inetAddresses.toList() }
                .filterIsInstance<Inet4Address>()
                .filterNot {
                    it.isLoopbackAddress || it.isLinkLocalAddress ||
                        it.isAnyLocalAddress || it.isMulticastAddress
                }
                .map { it.hostAddress ?: "" }
                .filter { it.isNotEmpty() }
                .distinct()
        } catch (e: Exception) {
            Log.w(TAG, "could not enumerate interfaces: ${e.message}")
            emptyList()
        }

        /** Directed broadcast addresses for every up, non-loopback IPv4 interface. */
        fun broadcastAddresses(): List<InetAddress> = try {
            NetworkInterface.getNetworkInterfaces().toList()
                .filter { it.isUp && !it.isLoopback }
                .flatMap { it.interfaceAddresses }
                .mapNotNull { it.broadcast }
                .filterIsInstance<Inet4Address>()
                .filterNot { it.isLinkLocalAddress }
                .distinct()
        } catch (e: Exception) {
            Log.w(TAG, "could not enumerate broadcast addresses: ${e.message}")
            emptyList()
        }

        fun payload(
            deviceId: String,
            deviceName: String,
            port: Int,
            hosts: List<String>,
        ): ByteArray = JSONObject(
            mapOf(
                "app" to "phonebridge",
                "v" to 1,
                "id" to deviceId,
                "name" to deviceName,
                "port" to port,
                "method" to "wifi",
                "hosts" to hosts,
            )
        ).toString().toByteArray(Charsets.UTF_8)
    }

    private val lifecycleLock = Any()
    private val stateLock = Any()
    private val running = AtomicBoolean(false)

    @Volatile
    private var cancelled = AtomicBoolean(true)

    private var worker: Thread? = null
    private var socket: DatagramSocket? = null

    fun start() {
        synchronized(lifecycleLock) {
            if (running.get() && worker?.isAlive == true) return
            val myCancel = AtomicBoolean(false)
            val newWorker = thread(name = "phonebridge-beacon", isDaemon = true, start = false) {
                var ownSocket: DatagramSocket? = null
                try {
                    while (!myCancel.get()) {
                        try {
                            val sock = DatagramSocket().apply { broadcast = true }
                            ownSocket = sock
                            val published = synchronized(stateLock) {
                                if (myCancel.get() || cancelled !== myCancel) {
                                    false
                                } else {
                                    socket = sock
                                    true
                                }
                            }
                            if (!published) {
                                sock.close()
                                return@thread
                            }
                            val generic = InetAddress.getByName("255.255.255.255")
                            var lastWarning = ""
                            while (!myCancel.get() && !sock.isClosed) {
                                val body = payload(deviceId, deviceName, port, localAddresses())
                                val targets = (broadcastAddresses() + generic).distinct()
                                for (target in targets) {
                                    if (myCancel.get()) break
                                    try {
                                        sock.send(
                                            DatagramPacket(body, body.size, target, DISCOVERY_PORT)
                                        )
                                    } catch (e: Exception) {
                                        if (myCancel.get()) break
                                        // One dead interface must not blind the others. Repeat
                                        // the same failure once per socket, not every 2 seconds.
                                        val warning = "beacon send to $target failed: ${e.message}"
                                        if (warning != lastWarning) {
                                            Log.w(TAG, warning)
                                            lastWarning = warning
                                        }
                                    }
                                }
                                if (!myCancel.get() && !sock.isClosed) Thread.sleep(INTERVAL_MS)
                            }
                        } catch (e: InterruptedException) {
                            throw e
                        } catch (e: Exception) {
                            if (!myCancel.get()) {
                                Log.w(TAG, "beacon worker will retry: ${e.message}")
                            }
                        } finally {
                            // Ownership is local: an old worker can never close the
                            // replacement socket created by start() after a restart.
                            ownSocket?.close()
                            synchronized(stateLock) {
                                if (socket === ownSocket) socket = null
                            }
                            ownSocket = null
                        }
                        if (!myCancel.get()) Thread.sleep(INTERVAL_MS)
                    }
                } catch (e: InterruptedException) {
                    Thread.currentThread().interrupt()
                } finally {
                    ownSocket?.close()
                    synchronized(stateLock) {
                        if (socket === ownSocket) socket = null
                        if (worker === Thread.currentThread()) worker = null
                        if (cancelled === myCancel) running.set(false)
                    }
                }
            }
            synchronized(stateLock) {
                cancelled = myCancel
                worker = newWorker
                running.set(true)
            }
            newWorker.start()
        }
    }

    fun stop() {
        synchronized(lifecycleLock) {
            val myCancel = cancelled
            var oldWorker: Thread? = null
            var oldSocket: DatagramSocket? = null
            synchronized(stateLock) {
                myCancel.set(true)
                running.set(false)
                oldWorker = worker
                worker = null
                oldSocket = socket
                socket = null
            }
            oldSocket?.close()
            oldWorker?.interrupt()
            if (oldWorker !== Thread.currentThread()) {
                try {
                    oldWorker?.join(JOIN_TIMEOUT_MS)
                } catch (e: InterruptedException) {
                    Thread.currentThread().interrupt()
                }
            }
        }
    }

    /** Rebind after a network change without leaking or closing the new worker. */
    fun restart() {
        synchronized(lifecycleLock) {
            stop()
            start()
        }
    }

    /** JSON the PC's discovery listener expects, exposed for tests/debugging. */
    fun beaconJson(): String = String(
        payload(deviceId, deviceName, port, localAddresses()),
        Charsets.UTF_8,
    )
}
