package dev.phonebridge.net

import android.util.Log
import dev.phonebridge.protocol.Json
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * Broadcasts a discovery beacon so the PC can find this phone without typing an
 * IP address.  Matches `windows/phonebridge/transport/discovery.py`.
 *
 * The datagram carries no keys and grants no trust: it only advertises
 * (device id, name, port).  The handshake decides whether the PC is allowed in.
 *
 * Broadcast does not cross subnets and some Wi-Fi access points block it, so
 * discovery is a convenience, never a requirement.  Manual IP entry and the
 * cached last endpoint always work.
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
                val payload = JSONObject(
                    mapOf(
                        "app" to "phonebridge",
                        "v" to 1,
                        "id" to deviceId,
                        "name" to deviceName,
                        "port" to port,
                        "method" to "wifi",
                    )
                ).toString().toByteArray(Charsets.UTF_8)
                val target = InetAddress.getByName("255.255.255.255")
                while (!myCancel.get()) {
                    try {
                        sock.send(DatagramPacket(payload, payload.size, target, DISCOVERY_PORT))
                    } catch (e: Exception) {
                        // Normal while the Wi-Fi interface is going away; the
                        // beacon keeps running so it resumes when it returns.
                        Log.w(TAG, "beacon send failed: ${e.message}")
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
        Json.encodeToBytes(
            mapOf(
                "app" to "phonebridge", "v" to 1, "id" to deviceId,
                "name" to deviceName, "port" to port, "method" to "wifi",
            )
        ),
        Charsets.UTF_8,
    )
}
