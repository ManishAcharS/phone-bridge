package dev.phonebridge.net

import android.content.Context
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Build
import android.os.SystemClock
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * A sensor refusal the PC can show verbatim. It *is* an [RpcFailure], so
 * `BridgeServer`'s existing handler turns it into the documented error code
 * (`sensor_denied`, `sensor_failed`, `bad_params`) with no extra plumbing.
 */
class SensorFailure(code: String, message: String) : RpcFailure(code, message)

/**
 * Phase 5 sensors: `SensorManager` -> `MSG_SENSOR`.
 *
 * The rules are the same ones that shaped [AudioEngine] and [CameraEngine], with
 * one addition that only a sensor plane needs:
 *
 * 1. **Nothing starts by itself.** Listeners are registered only when an
 *    authenticated PC asks with `sensor.start`, for the ids it named. There is no
 *    background sampling: a phone whose screen says "idle" must not be measuring.
 * 2. **One frame per event, bounded.** Readings go through a small queue drained
 *    by a single sender thread. When the queue is full the *oldest* reading is
 *    dropped and counted, so a slow link delays nothing and the PC sees a
 *    truthful `dropped` counter rather than a growing backlog.
 * 3. **Sequence numbers are per sensor** and timestamps come from
 *    `SystemClock.elapsedRealtimeNanos`, converted to milliseconds since the
 *    stream started. The PC never has to guess either.
 * 4. **Everything is released.** [stop] unregisters every listener and stops the
 *    sender; [release] does the same. It runs on `sensor.stop`, on session close,
 *    on session error and in `BridgeService.onDestroy`, so no sensor outlives the
 *    window that started it.
 * 5. **The device decides what is possible.** A sensor the platform refuses is
 *    reported with the platform's own words; a sensor that exists but cannot be
 *    streamed (a text sensor, a camera-dependent pose) is listed with the reason
 *    instead of silently missing.
 */
class SensorEngine(
    private val context: Context,
    private val sendFrame: (streamId: Int, body: ByteArray) -> Unit,
    private val log: (level: String, message: String) -> Unit,
) {

    companion object {
        /** Half a second of 100 Hz data: enough slack, small enough to bound memory. */
        const val QUEUE_FRAMES = 64

        /** Ids are assigned once per process so the PC's list stays stable. */
        const val MAX_SENSORS = 64

        /** How often the phone's own card is refreshed while streaming. */
        const val STATE_PUBLISH_MS = 250L
    }

    data class Entry(
        val id: Int,
        val kind: Int,
        val name: String,
        val vendor: String,
        val minDelayUs: Int,
        val maxDelayUs: Int,
        val maxRange: Float,
        val resolution: Float,
        val powerMa: Float,
        val streamable: Boolean,
        val reason: String = "",
    ) {
        fun asMap(): Map<String, Any> = linkedMapOf(
            "id" to id,
            "kind" to kind,
            "name" to name,
            "label" to name.ifBlank { SensorFrames.nameOf(kind) },
            "vendor" to vendor,
            "axes" to SensorFrames.axesOf(kind),
            "unit" to SensorFrames.unitOf(kind),
            "min_delay_us" to minDelayUs,
            "max_delay_us" to maxDelayUs,
            "max_range" to maxRange,
            "resolution" to resolution,
            "power_ma" to powerMa,
            // Five-state honesty: a sensor that is present but not streamable says
            // so here, rather than vanishing from the list.
            "available" to streamable,
            "present" to true,
            "implemented" to streamable,
            "active" to false,
            "detail" to if (streamable) "available" else reason,
        )
    }

    /** What the phone screen shows. */
    data class State(
        val active: List<Int> = emptyList(),
        val intervalUs: Int = SensorFrames.DEFAULT_INTERVAL_US,
        val framesSent: Long = 0,
        val dropped: Long = 0,
        val registered: Int = 0,
        val permitted: Boolean = true,
        val lastError: String = "",
    )

    private val _state = MutableStateFlow(State())

    /** Reasons that mean "the stream ended as intended", never "it broke". */
    private val EXPECTED_STOPS = setOf(
        "stopped",
        "restarted",
        "released",
        "last session ended",
        "nothing started",
        "refused",
    )
    val state: StateFlow<State> = _state.asStateFlow()

    private val lock = Any()
    private val manager: SensorManager? =
        context.getSystemService(Context.SENSOR_SERVICE) as? SensorManager

    private val byId = LinkedHashMap<Int, Sensor>()
    // `Sensor` has no stable equality and getSensorList() hands back fresh objects,
    // so the id table is keyed by *identity*: SensorEvent.sensor is one of the exact
    // objects we registered, and identity is the only thing that is true of it.
    private val idBySensor = java.util.IdentityHashMap<Sensor, Int>()
    private val entries = LinkedHashMap<Int, Entry>()
    private val seqById = HashMap<Int, Int>()

    private val running = AtomicBoolean(false)
    private val framesSent = AtomicLong(0)
    private val framesDropped = AtomicLong(0)
    private val activeCount = AtomicInteger(0)

    private var startedAtNanos = 0L
    private var statePublishedAt = 0L
    private var sender: Thread? = null
    private var queue: ArrayBlockingQueue<ByteArray>? = null
    private var listener: SensorEventListener? = null

    // ------------------------------------------------------------------ //
    // discovery
    // ------------------------------------------------------------------ //
    /** Every sensor the platform reports, with the reason when it is not streamable. */
    fun entries(): List<Entry> {
        synchronized(lock) {
            if (entries.isEmpty()) refreshCatalogue()
            return entries.values.toList()
        }
    }

    private fun refreshCatalogue() {
        val service = manager ?: return
        val assigned = HashMap<Int, Sensor>()
        val built = LinkedHashMap<Int, Entry>()
        val sensors = try {
            service.getSensorList(Sensor.TYPE_ALL)
        } catch (e: Exception) {
            log("WARN", "sensor enumeration failed: ${e.javaClass.simpleName}: ${e.message}")
            emptyList()
        }
        for (sensor in sensors) {
            if (built.size >= MAX_SENSORS) break
            val id = built.size + 1
            val known = SensorFrames.KINDS.containsKey(sensor.type)
            val reason = when {
                // `Sensor.TYPE_STRING` was never part of the public SDK; the
                // supported way to recognise a text sensor is its string type.
                sensor.stringType != Sensor.STRING_TYPE_NONE ->
                    "text sensors cannot be sent as numbers"
                requiresBiometricPermission(sensor.type) ->
                    "needs the BODY_SENSORS permission, which this build does not request"
                SensorFrames.UNSUPPORTED_REASONS.containsKey(sensor.type) ->
                    SensorFrames.UNSUPPORTED_REASONS[sensor.type] ?: "not streamed"
                !known -> "PhoneBridge has no name for sensor type ${sensor.type}"
                else -> ""
            }
            assigned[id] = sensor
            built[id] = Entry(
                id = id,
                kind = sensor.type,
                name = sensor.name?.trim() ?: "",
                vendor = sensor.vendor?.trim() ?: "",
                minDelayUs = sensor.minDelay.coerceAtLeast(0),
                maxDelayUs = sensor.maxDelay.coerceAtLeast(0),
                maxRange = sensor.maximumRange,
                resolution = sensor.resolution,
                powerMa = sensor.power,
                streamable = reason.isEmpty(),
                reason = reason,
            )
        }
        byId.clear()
        byId.putAll(assigned)
        idBySensor.clear()
        assigned.forEach { (id, sensor) -> idBySensor[sensor] = id }
        entries.clear()
        entries.putAll(built)
        log("INFO", "sensors: ${built.size} reported, ${built.values.count { it.streamable }} streamable")
    }

    fun hasHardware(): Boolean = !entries().isEmpty()

    /**
     * Whether sensor reads are allowed right now.
     *
     * Answered from the platform, never assumed: the previous release returned a
     * constant `true`, so a phone that could not sample anything still told the PC
     * it was permitted and the refusal only surfaced at `sensor.start`.
     */
    fun permitted(): Boolean {
        val gate = highRatePermission() ?: return true
        return try {
            context.checkSelfPermission(gate) == PackageManager.PERMISSION_GRANTED
        } catch (e: Exception) {
            false
        }
    }

    /** Why sampling is refused, or "" when it is allowed. */
    fun permissionDetail(): String =
        if (permitted()) {
            ""
        } else {
            "the high sampling rate permission is not granted on this phone"
        }

    fun minDelayUsFor(ids: List<Int>): Int {
        val delays = ids.mapNotNull { id -> byId[id]?.minDelay?.takeIf { it > 0 } }
        return delays.maxOrNull() ?: 0
    }

    // ------------------------------------------------------------------ //
    // start / stop
    // ------------------------------------------------------------------ //
    /**
     * Register listeners for [ids] at [intervalUs].
     *
     * @return the interval the platform actually accepted, which may be slower
     *   than asked because a sensor floors it at its own `minDelay`.
     * @throws SensorFailure with `bad_params` (unknown or unstreamable id) or
     *   `sensor_failed` (the platform refused to register).
     */
    fun start(ids: List<Int>, intervalUs: Int): Int {
        val service = manager ?: throw SensorFailure("sensor_failed", "this phone has no sensor service")
        if (!permitted()) {
            throw SensorFailure(
                "sensor_denied",
                "this phone has not granted the high sampling rate permission " +
                    "needed for sensor streaming",
            )
        }
        if (running.get()) stop("restarted")
        val catalogue = entries()
        if (catalogue.isEmpty()) {
            throw SensorFailure("sensor_failed", "this phone reported no sensors")
        }
        val wanted = if (ids.isEmpty()) catalogue.filter { it.streamable }.map { it.id } else ids
        val unknown = wanted.filter { id -> catalogue.none { it.id == id && it.streamable } }
        if (unknown.isNotEmpty()) {
            throw SensorFailure("bad_params", "this phone cannot stream sensor(s) $unknown")
        }
        val interval = SensorFrames.clampInterval(intervalUs)

        queue = ArrayBlockingQueue(QUEUE_FRAMES)
        framesSent.set(0)
        framesDropped.set(0)
        seqById.clear()
        startedAtNanos = SystemClock.elapsedRealtimeNanos()
        running.set(true)
        activeCount.set(0)

        val callback = object : SensorEventListener {
            override fun onSensorChanged(event: SensorEvent) {
                onEvent(event)
            }

            override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {
                // Accuracy is reported by the platform and carried in the values of
                // some sensors; a change alone is not a reading to forward.
            }
        }
        listener = callback

        val applied = IntArray(1) { interval }
        val registered = ArrayList<Int>(wanted.size)
        for (id in wanted) {
            val sensor = byId[id] ?: continue
            val accepted = try {
                service.registerListener(callback, sensor, interval, 0)
            } catch (e: SecurityException) {
                stop("denied")
                throw SensorFailure("sensor_denied", "the sensor permission is not granted: ${e.message}")
            } catch (e: Exception) {
                stop("failed")
                throw SensorFailure("sensor_failed", "the platform refused sensor $id: ${e.message}")
            }
            if (!accepted) {
                stop("refused")
                throw SensorFailure(
                    "sensor_failed",
                    "the platform would not register ${sensor.name ?: "sensor $id"}",
                )
            }
            activeCount.incrementAndGet()
            registered.add(id)
            // The sensor may silently clamp the period; report the effective one.
            val floor = sensor.minDelay.coerceAtLeast(0)
            if (floor > applied[0]) applied[0] = floor
        }
        if (activeCount.get() == 0) {
            stop("nothing started")
            throw SensorFailure("sensor_failed", "no sensor could be registered")
        }

        sender = Thread({ drain() }, "phonebridge-sensors").apply {
            isDaemon = true
            start()
        }
        _state.value = State(
            active = registered,
            intervalUs = applied[0],
            permitted = true,
        )
        log("INFO", "streaming ${registered.size} sensor(s) at ${applied[0]}us")
        return applied[0]
    }

    fun stop(reason: String = "stopped") {
        val service = manager
        val activeListener = listener
        listener = null
        queue?.offer(ByteArray(0)) // wake the sender so it can exit
        val thread = sender
        sender = null
        running.set(false)
        if (service != null && activeListener != null) {
            try {
                service.unregisterListener(activeListener)
            } catch (e: Exception) {
                log("WARN", "unregistering sensors failed: ${e.message}")
            }
        }
        if (thread != null) {
            thread.interrupt()
        }
        queue = null
        val wasActive = activeCount.get()
        activeCount.set(0)
        _state.value = _state.value.copy(
            active = emptyList(),
            registered = 0,
            permitted = permitted(),
            // A stream that ends because the session ended, or because the service
            // is going away, is not an error: it is the expected end. Only a real
            // failure is recorded as one.
            lastError = if (reason in EXPECTED_STOPS) "" else reason,
        )
        if (wasActive > 0) log("INFO", "sensor stream stopped ($reason)")
    }

    /**
     * Stop because the last session ended, or because the service is going away.
     *
     * Separate from [stop] so the log can say *why*: a stream that ends because
     * the PC disconnected looks like a bug otherwise.
     */
    fun onSessionClosed(why: String) {
        if (running.get() || _state.value.active.isNotEmpty()) {
            stop(why)
        }
    }

    fun release() {
        stop("released")
        synchronized(lock) {
            byId.clear()
            entries.clear()
        }
    }

    // ------------------------------------------------------------------ //
    // the hot path
    // ------------------------------------------------------------------ //
    private fun onEvent(event: SensorEvent) {
        if (!running.get()) return
        val id = idFor(event.sensor) ?: return
        val values = event.values
        if (values == null || values.isEmpty() || values.size > SensorFrames.MAX_VALUES) return
        for (value in values) {
            if (!value.isFinite()) {
                framesDropped.incrementAndGet()
                return
            }
        }
        val seq = seqById[id] ?: 0
        seqById[id] = (seq + 1) and 0xFFFF
        val elapsedMs = ((SystemClock.elapsedRealtimeNanos() - startedAtNanos) / 1_000_000L)
        val body = try {
            SensorFrames.Frame(
                sensorId = id,
                seq = seq,
                timestampMs = (elapsedMs.coerceIn(0, Int.MAX_VALUE.toLong())).toInt(),
                kind = event.sensor.type,
                // A copy: SensorEvent reuses its array for the next callback.
                values = values.copyOf(),
            ).encode()
        } catch (e: Exception) {
            framesDropped.incrementAndGet()
            log("WARN", "could not encode a sensor frame: ${e.message}")
            return
        }
        val target = queue ?: return
        if (!target.offer(body)) {
            // Bounded, drop-oldest: a stale reading is worth less than a current one.
            target.poll()
            target.offer(body)
            val dropped = framesDropped.incrementAndGet()
            _state.value = _state.value.copy(dropped = dropped)
        }
    }

    /**
     * The id we handed out for this sensor.
     *
     * Identity first (the listener was registered with one of these objects), then
     * the sensor *type* as a fallback — two accelerometers would be ambiguous there,
     * which is why the fallback is last and the identity table is authoritative.
     */
    private fun idFor(sensor: Sensor): Int? {
        idBySensor[sensor]?.let { return it }
        return byId.entries.firstOrNull { it.value.type == sensor.type }?.key
    }

    private fun drain() {
        val target = queue ?: return
        while (true) {
            val body = try {
                target.take()
            } catch (e: InterruptedException) {
                return
            }
            if (body.isEmpty()) return // sentinel from stop()
            if (!running.get()) return
            try {
                sendFrame(SensorFrames.SENSOR_STREAM, body)
                val sent = framesSent.incrementAndGet()
                // Publish the counter at most four times a second. At 3 x 50 Hz the
                // per-frame version re-emitted a StateFlow 150 times a second, and
                // every emission recomposed the phone's Sensors card.
                val now = SystemClock.elapsedRealtime()
                if (now - statePublishedAt >= STATE_PUBLISH_MS) {
                    statePublishedAt = now
                    _state.value = _state.value.copy(framesSent = sent)
                }
            } catch (e: Exception) {
                // A send failure means the session is closing; stop rather than spin.
                framesDropped.incrementAndGet()
                log("WARN", "dropping sensor frames: ${e.message}")
                return
            }
        }
    }

    /** What `sensor.status` answers, built next to the method name in BridgeServer. */
    fun status(): Map<String, Any?> = linkedMapOf(
        "active" to _state.value.active,
        "interval_us" to _state.value.intervalUs,
        "frames_sent" to _state.value.framesSent,
        "permitted" to permitted(),
        "detail" to permissionDetail(),
    )
}

/**
 * Android 12+ gates sampling faster than 200 Hz behind a normal permission that a
 * managed profile, a policy or a restricted device can still withhold.
 */
private fun highRatePermission(): String? =
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        android.Manifest.permission.HIGH_SAMPLING_RATE_SENSORS
    } else {
        null
    }

/** Kinds that only work with a runtime permission this build never requests. */
internal fun requiresBiometricPermission(kind: Int): Boolean =
    kind == Sensor.TYPE_HEART_RATE || kind == Sensor.TYPE_HEART_BEAT
