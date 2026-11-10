package dev.phonebridge.net

import android.util.Log
import dev.phonebridge.protocol.Json
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.NetworkInterface
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * Broadcasts a discovery beacon so the PC can find this phone without typing an
 * IP address.  Matches `windows/phonebridge/transport/discovery.py`.
 *
 * The datagram carries no keys and grants no trust: it only advertises
 * (device id, name, port, addresses).  The handshake decides whether the PC is
 * allowed in.
 *
 * **Why it also broadcasts to each interface's own subnet address.** Sending
 * only to `255.255.255.255` lets Android route the datagram out of the *default*
 * network interface. When the phone is a hotspot and mobile data or Wi-Fi is
 * also up, the default route is not the access point, so the beacon never
 * reaches the PC that just joined the hotspot - which looks exactly like "hotspot
 * discovery does not work". Sending to each interface's directed broadcast
 * address (e.g. 192.168.43.255) pins the datagram to that interface's route, so
 * the PC sees it no matter what the default route is. The generic
 * `255.255.255.255` broadcast is kept for access points that forward it.
 *
 * The payload also lists every local IPv4 address, so a PC that hears the beacon
 * over one interface can try the others. Both additions are backward compatible:
 * an older PC ignores the extra field, and an older phone simply sends no list.
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

        /** Every IPv4 address this phone holds, loopback excluded. */
        fun localAddresses(): List<String> = try {
            NetworkInterface.getNetworkInterfaces().toList()
                .filter { it.isUp && !it.isLoopback }
                .flatMap { it.inetAddresses.toList() }
                .filterIsInstance<Inet4Address>()
                .filterNot { it.isLoopbackAddress }
                .map { it.hostAddress ?: "" }
                .filter { it.isNotEmpty() }
                .distinct()
        } catch (e: Exception) {
            Log.w(TAG, "could not enumerate interfaces: ${e.message}")
            emptyList()
        }

        /**
         * Directed broadcast addresses for every up, non-loopback interface.
         *
         * These are what make a hotspot work: each one belongs to an interface, so
         * the kernel routes the datagram out of *that* interface regardless of the
         * default route.
         */
        fun broadcastAddresses(): List<InetAddress> = try {
            NetworkInterface.getNetworkInterfaces().toList()
                .filter { it.isUp && !it.isLoopback }
                .flatMap { it.interfaceAddresses }
                .mapNotNull { it.broadcast }
                .distinct()
        } catch (e: Exception) {
            Log.w(TAG, "could not enumerate broadcast addresses: ${e.message}")
            emptyList()
        }

        /** The beacon body, built fresh so the address list is never stale. */
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

    private val running = AtomicBoolean(false)

    /**
     * Cancellation flag for the *current* worker.
     *
     * A shared [running] flag is not enough: `stop(); start()` used to flip it
     * back to true before the old worker woke from its sleep, so the old thread
     * never exited and the phone broadcast two beacons every interval (and leaked
     * a thread per network change). Each run gets its own flag.
     */
    @Volatile
    private var cancelled = AtomicBoolean(true)

    @Volatile
    private var worker: Thread? = null

    @Volatile
    private var socket: DatagramSocket? = null

    fun start() {
        if (!running.compareAndSet(false, true)) return
        val myCancel = AtomicBoolean(false)
        cancelled = myCancel
        worker = thread(name = "phonebridge-beacon", isDaemon = true) {
            try {
                val sock = DatagramSocket().apply { broadcast = true }
                socket = sock
                val generic = InetAddress.getByName("255.255.255.255")
                while (!myCancel.get()) {
                    val body = payload(deviceId, deviceName, port, localAddresses())
                    // Directed broadcasts first: they cannot be stolen by the
                    // default route the way 255.255.255.255 can.
                    val targets = broadcastAddresses() + generic
                    for (target in targets.distinct()) {
                        try {
                            sock.send(DatagramPacket(body, body.size, target, DISCOVERY_PORT))
                        } catch (e: Exception) {
                            // Normal while an interface is going away; keep sending
                            // to the others so one dead interface cannot blind the PC.
                            Log.w(TAG, "beacon send to $target failed: ${e.message}")
                        }
                    }
                    Thread.sleep(INTERVAL_MS)
                }
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
            } catch (e: Exception) {
                Log.w(TAG, "beacon stopped: ${e.message}")
            } finally {
                socket?.close()
                socket = null
            }
        }
    }

    fun stop() {
        running.set(false)
        cancelled.set(true)
        // Closing the socket unblocks a send() that is in flight; interrupt()
        // unblocks the sleep between beacons.
        socket?.close()
        worker?.interrupt()
        worker = null
    }

    /**
     * Rebind after the network changed (Wi-Fi rejoined, hotspot switched).
     *
     * The old datagram socket is bound to an interface that may no longer exist,
     * and its broadcasts would silently go nowhere.
     */
    fun restart() {
        stop()
        start()
    }

    /** JSON the PC's discovery listener expects, exposed for tests/debugging. */
    fun beaconJson(): String = String(
        payload(deviceId, deviceName, port, localAddresses()),
        Charsets.UTF_8,
    )
}
