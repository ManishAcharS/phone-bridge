package dev.phonebridge.net

import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * The Phase 5 sensor frame that rides inside `MSG_SENSOR` (0x60).
 *
 * Wire format, identical to `windows/phonebridge/sensors/frames.py`:
 *
 * ```
 * u16 sensor_id   the id SensorEngine assigned in sensor.list
 * u16 seq         per-sensor counter, wraps at 65535
 * u32 ts_ms       monotonic milliseconds since this stream started
 * u8  kind        Android Sensor.TYPE_*
 * u8  count       1..16 float32 values
 * u16 reserved    must be zero
 * ..  count * f32 big-endian values
 * ```
 *
 * One message is one reading from one sensor. Two decisions explain the shape:
 *
 *  * **the kind is on the wire.** A sensor the PC has no name for is still
 *    useful: the header says what Android calls it, and the PC shows the numbers
 *    under the vendor name the phone reported rather than hiding the sensor.
 *  * **the timestamp is the phone's**, in milliseconds since the stream started.
 *    A PC whose clock is wrong then still shows correct *intervals*, and the PC
 *    never invents a sample time of its own.
 */
object SensorFrames {

    const val MSG_SENSOR = 0x60

    /** Fixed by docs/03: 4 = sensors. */
    const val SENSOR_STREAM = 4

    const val HEADER_SIZE = 12
    const val MAX_VALUES = 16

    /**
     * Android `Sensor.TYPE_*` -> (wire name, axis names, unit).
     *
     * Kept as numbers on the wire so a new phone sensor needs no protocol change
     * to be reported usefully. `Sensor.TYPE_STRING` (19) is deliberately not
     * streamed: it is a text sensor and a float32 frame would mangle it.
     */
    val KINDS: Map<Int, Triple<String, List<String>, String>> = linkedMapOf(
        1 to Triple("accelerometer", listOf("x", "y", "z"), "m/s^2"),
        2 to Triple("magnetometer", listOf("x", "y", "z"), "uT"),
        3 to Triple("orientation", listOf("azimuth", "pitch", "roll"), "deg"),
        4 to Triple("gyroscope", listOf("x", "y", "z"), "rad/s"),
        5 to Triple("light", listOf("lux"), "lx"),
        6 to Triple("pressure", listOf("hPa"), "hPa"),
        8 to Triple("proximity", listOf("distance"), "cm"),
        9 to Triple("gravity", listOf("x", "y", "z"), "m/s^2"),
        10 to Triple("linear_acceleration", listOf("x", "y", "z"), "m/s^2"),
        11 to Triple("rotation_vector", listOf("x", "y", "z", "w"), ""),
        12 to Triple("relative_humidity", listOf("percent"), "%"),
        13 to Triple("ambient_temperature", listOf("celsius"), "C"),
        14 to Triple("magnetic_field_uncalibrated", listOf("x", "y", "z"), "uT"),
        15 to Triple("game_rotation_vector", listOf("x", "y", "z", "w"), ""),
        16 to Triple("gyroscope_uncalibrated", listOf("x", "y", "z"), "rad/s"),
        18 to Triple("step_detector", listOf("event"), ""),
        19 to Triple("step_counter", listOf("steps"), "steps"),
        20 to Triple("geomagnetic_rotation_vector", listOf("x", "y", "z", "w"), ""),
        21 to Triple("heart_rate", listOf("bpm"), "bpm"),
        29 to Triple("hinge_angle", listOf("degrees"), "deg"),
    )

    /** The kinds the app offers first, in the order the PC lists them. */
    val PREFERRED_ORDER = listOf(1, 4, 2, 9, 10, 11, 15, 3, 8, 5, 19, 18)

    /** Why a sensor is on the device but cannot be streamed. */
    val UNSUPPORTED_REASONS: Map<Int, String> = mapOf(
        0 to "unknown sensor type",
        7 to "ambient temperature is deprecated on modern Android",
        13 to "ambient temperature is often a placeholder value",
        17 to "significant motion is an event, not a reading",
        19 to "step counter and detector are reported as they happen",
        22 to "accelerometer uncalibrated is not streamed in this release",
        28 to "pose sensors are camera-dependent",
    )

    data class Frame(
        val sensorId: Int,
        val seq: Int,
        val timestampMs: Int,
        val kind: Int,
        val values: FloatArray,
    ) {
        fun encode(): ByteArray {
            require(sensorId in 0..0xFFFF) { "sensor id out of range: $sensorId" }
            require(seq in 0..0xFFFF) { "sequence out of range: $seq" }
            require(kind in 0..0xFF) { "sensor kind does not fit the header: $kind" }
            require(values.isNotEmpty() && values.size <= MAX_VALUES) {
                "a sensor frame carries 1-$MAX_VALUES values, not ${values.size}"
            }
            val out = ByteArray(HEADER_SIZE + 4 * values.size)
            out[0] = ((sensorId ushr 8) and 0xFF).toByte()
            out[1] = (sensorId and 0xFF).toByte()
            out[2] = ((seq ushr 8) and 0xFF).toByte()
            out[3] = (seq and 0xFF).toByte()
            val ts = timestampMs.toLong() and 0xFFFFFFFFL
            out[4] = ((ts ushr 24) and 0xFF).toByte()
            out[5] = ((ts ushr 16) and 0xFF).toByte()
            out[6] = ((ts ushr 8) and 0xFF).toByte()
            out[7] = (ts and 0xFF).toByte()
            out[8] = kind.toByte()
            out[9] = values.size.toByte()
            out[10] = 0
            out[11] = 0
            val buffer = ByteBuffer.wrap(out, HEADER_SIZE, 4 * values.size)
                .order(ByteOrder.BIG_ENDIAN)
            for (value in values) buffer.putFloat(value)
            return out
        }
    }

    /** Sampling intervals the PC may ask for, in microseconds. */
    val INTERVAL_CHOICES_US: List<Pair<String, Int>> = listOf(
        "5 ms (200 Hz)" to 5_000,
        "10 ms (100 Hz)" to 10_000,
        "20 ms (50 Hz)" to 20_000,
        "67 ms (15 Hz)" to 66_667,
        "200 ms (5 Hz)" to 200_000,
        "1000 ms (1 Hz)" to 1_000_000,
    )

    const val DEFAULT_INTERVAL_US = 20_000
    const val MIN_INTERVAL_US = 1_000
    const val MAX_INTERVAL_US = 10_000_000

    fun clampInterval(intervalUs: Int): Int = intervalUs.coerceIn(MIN_INTERVAL_US, MAX_INTERVAL_US)

    fun nameOf(kind: Int): String = KINDS[kind]?.first ?: "sensor_$kind"

    fun axesOf(kind: Int): List<String> = KINDS[kind]?.second ?: listOf("value")

    fun unitOf(kind: Int): String = KINDS[kind]?.third ?: ""

    /** Parse `{"ids":[...]}` defensively; the caller validates the ids. */
    fun parseIds(params: Map<*, *>?): List<Int> =
        (params?.get("ids") as? List<*>)
            ?.mapNotNull { (it as? Number)?.toInt() }
            ?.distinct()
            ?: emptyList()
}
