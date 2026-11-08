package dev.phonebridge.net

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTimestamp
import android.media.AudioTrack
import android.media.MediaRecorder
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.bluetooth.BluetoothA2dp
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothHeadset
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Measures what this handset can actually do with a Bluetooth headset.
 *
 * This is **not** the audio relay. It is the instrument that decides whether the
 * relay is worth building on this device, and in which mode. It touches the
 * audio path for a few seconds at a time, records facts, and leaves everything
 * as it found it.
 *
 * Ground rules, because guessing here is worse than useless:
 *
 *  - A platform declaration is recorded, never trusted. `isBluetoothScoAvailableOffCall()`
 *    returning true says what the API thinks; the probe separately verifies that
 *    audio actually flows in both directions.
 *  - Routing is verified twice: what the track/record says it is routed to
 *    (`routedDevice`) and whether samples were non-silent.
 *  - Every field may be null. Null means "not measured", which the PC-side report
 *    renders as `unmeasured` instead of inventing an answer.
 *  - The probe never changes a system setting it does not restore, and never
 *    needs root.
 *
 * Output shape is documented in `docs/08-multi-device-test-strategy.md` and
 * validated by `windows/phonebridge/core/compat.py`.
 */
object CapabilityProbe {

    const val SCHEMA_VERSION = 1

    /** Sample rate for every test. SCO negotiates 8 or 16 kHz; 16 kHz covers mSBC. */
    private const val RATE = 16000
    private const val SCO_ROUTE_TIMEOUT_MS = 3000L
    private const val READ_TIMEOUT_MS = 5000
    private const val CHUNK = 320  // 20 ms at 16 kHz

    /** Peak amplitude below which a capture is treated as silence (of 32767). */
    private const val SIGNAL_PEAK_THRESHOLD = 300

    data class Config(
        val seconds: Int = 30,
        val acoustic: Boolean = false,
    )

    // ------------------------------------------------------------------ //
    // entry point
    // ------------------------------------------------------------------ //
    fun run(context: Context, config: Config = Config()): Map<String, Any?> {
        val started = SystemClock.elapsedRealtime()
        val errors = mutableListOf<String>()
        val interruptions = mutableListOf<Map<String, Any?>>()
        val testsRun = mutableListOf<String>()
        val audio = context.getSystemService(Context.AUDIO_SERVICE) as? AudioManager

        val report = linkedMapOf<String, Any?>(
            "schema_version" to SCHEMA_VERSION,
            "source" to "device",
            "captured_at" to isoNow(),
        )

        testsRun += "device"
        val device = deviceFacts(context)
        report["device"] = device
        report["device_id"] = device["probe_id"]

        report["bluetooth"] = bluetoothFacts(context, errors)
        testsRun += "bluetooth"

        if (audio == null) {
            errors += "AudioManager unavailable"
            report["sco"] = mapOf("path" to "unknown", "declared_offcall_available" to null)
            report["route_output"] = mapOf("status" to "error", "detail" to "no AudioManager")
            report["route_input"] = mapOf("status" to "error", "detail" to "no AudioManager")
            report["duplex"] = mapOf("status" to "error", "detail" to "no AudioManager")
            report["latency"] = mapOf("method" to "none")
            report["errors"] = errors.toList()
            report["interruptions"] = interruptions.toList()
            report["tests_run"] = testsRun.toList()
            report["duration_ms"] = SystemClock.elapsedRealtime() - started
            return report
        }

        if (!hasPermission(context, Manifest.permission.RECORD_AUDIO)) {
            errors += "RECORD_AUDIO was not granted, so no capture test could run"
        }

        // --- bring the headset route up once, measure everything on it ---- //
        val route = RouteSession(context, audio, interruptions)
        var sco: Map<String, Any?>
        try {
            sco = route.request()
            testsRun += "sco"
            report["sco"] = sco

            val routed = sco["request_succeeded"] == true
            report["route_output"] = if (routed) {
                testsRun += "route_output"
                guarded(errors) { testOutput() } ?: errorBlock("playback")
            } else {
                skipped("no headset route: ${sco["detail"]}")
            }

            report["route_input"] = if (routed && hasPermission(context, Manifest.permission.RECORD_AUDIO)) {
                testsRun += "route_input"
                guarded(errors) { testInput() } ?: errorBlock("capture")
            } else if (!routed) {
                skipped("no headset route: ${sco["detail"]}")
            } else {
                skipped("RECORD_AUDIO not granted")
            }

            report["duplex"] = if (routed && hasPermission(context, Manifest.permission.RECORD_AUDIO)) {
                testsRun += "duplex"
                guarded(errors) { testDuplex(config.seconds, route) } ?: errorBlock("duplex")
            } else {
                skipped(if (!routed) "no headset route" else "RECORD_AUDIO not granted")
            }

            report["latency"] = when {
                config.acoustic && routed && hasPermission(context, Manifest.permission.RECORD_AUDIO) -> {
                    testsRun += "latency_acoustic"
                    guarded(errors) { acousticLatency() }
                        ?: mapOf("method" to "none", "detail" to "acoustic test failed")
                }
                routed -> mapOf(
                    "method" to "timestamp_estimate",
                    "estimate_ms" to (route.captureBufferMs + route.playbackBufferMs).takeIf { it > 0 },
                    "note" to "OS buffering only; excludes Bluetooth radio and codec time",
                )
                else -> mapOf("method" to "none")
            }
        } catch (e: Exception) {
            errors += "${e.javaClass.simpleName}: ${e.message}"
            sco = mapOf("path" to route.pathName, "detail" to "route attempt threw")
            report["sco"] = sco
            report["route_output"] = errorBlock("playback")
            report["route_input"] = errorBlock("capture")
            report["duplex"] = errorBlock("duplex")
            report["latency"] = mapOf("method" to "none")
        } finally {
            route.release()
        }

        report["interruptions"] = interruptions.toList()
        report["errors"] = errors.toList()
        report["tests_run"] = testsRun.toList()
        report["duration_ms"] = SystemClock.elapsedRealtime() - started
        return report
    }

    // ------------------------------------------------------------------ //
    // device facts
    // ------------------------------------------------------------------ //
    private fun deviceFacts(context: Context): Map<String, Any?> {
        val power = context.getSystemService(Context.POWER_SERVICE) as? PowerManager
        val pm = context.packageManager
        val romProps = systemProperties(
            "ro.miui.ui.version.name",      // MIUI
            "ro.mi.os.version.name",        // HyperOS
            "ro.build.version.incremental",
        )
        val rom = when {
            romProps["ro.mi.os.version.name"] != null -> "HyperOS"
            romProps["ro.miui.ui.version.name"] != null -> "MIUI"
            Build.MANUFACTURER.equals("Xiaomi", ignoreCase = true) -> "MIUI/HyperOS (unresolved)"
            else -> "unknown"
        }
        val romVersion = romProps["ro.mi.os.version.name"]
            ?: romProps["ro.miui.ui.version.name"]

        return linkedMapOf(
            "manufacturer" to Build.MANUFACTURER,
            "brand" to Build.BRAND,
            "model" to Build.MODEL,
            "codename" to Build.DEVICE,
            "product" to Build.PRODUCT,
            "android_release" to Build.VERSION.RELEASE,
            "sdk_int" to Build.VERSION.SDK_INT,
            "incremental" to Build.VERSION.INCREMENTAL,
            "fingerprint" to Build.FINGERPRINT,
            "rom" to rom,
            "rom_version" to romVersion,
            "is_emulator" to isProbablyEmulator(),
            "package" to context.packageName,
            "app_sdk_target" to pm.applicationInfo?.targetSdkVersion,
            "power_save_mode" to power?.isPowerSaveMode,
            "ignoring_battery_optimizations" to
                power?.isIgnoringBatteryOptimizations(context.packageName),
            "screen_interactive" to power?.isInteractive,
            "probe_id" to probeId(),
        )
    }

    /**
     * A stable-ish identifier for the report without exposing the identity key.
     *
     * The PC stores records by `device_id`; the probe cannot read the bridge
     * identity (it lives in [IdentityStore]), so it derives a short hash from
     * build fields. The PC overrides this with the phone's real device id when it
     * receives the report.
     */
    private fun probeId(): String {
        val material = "${Build.MANUFACTURER}|${Build.MODEL}|${Build.DEVICE}|${Build.FINGERPRINT}"
        val digest = java.security.MessageDigest.getInstance("SHA-256")
            .digest(material.toByteArray(Charsets.UTF_8))
        return digest.joinToString("") { "%02x".format(java.util.Locale.ROOT, it) }.substring(0, 16)
    }

    private fun isProbablyEmulator(): Boolean {
        val fp = Build.FINGERPRINT.lowercase()
        return fp.startsWith("generic") || fp.contains("emulator") ||
            Build.MODEL.contains("sdk", ignoreCase = true) ||
            Build.PRODUCT.contains("sdk", ignoreCase = true) ||
            Build.HARDWARE.contains("goldfish") || Build.HARDWARE.contains("ranchu")
    }

    /** Reflection on SystemProperties is best-effort: it is blocked on newer builds. */
    private fun systemProperties(vararg keys: String): Map<String, String?> {
        val out = mutableMapOf<String, String?>()
        val clazz = try {
            Class.forName("android.os.SystemProperties")
        } catch (e: Throwable) {
            keys.forEach { out[it] = null }
            return out
        }
        val getter = try {
            clazz.getMethod("get", String::class.java)
        } catch (e: Throwable) {
            keys.forEach { out[it] = null }
            return out
        }
        keys.forEach { key ->
            out[key] = try {
                (getter.invoke(null, key) as? String)?.takeIf { it.isNotBlank() }
            } catch (e: Throwable) {
                null
            }
        }
        return out
    }

    // ------------------------------------------------------------------ //
    // bluetooth facts
    // ------------------------------------------------------------------ //
    private fun bluetoothFacts(context: Context, errors: MutableList<String>): Map<String, Any?> {
        val manager = context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager
        val adapter = manager?.adapter
        if (adapter == null) {
            return linkedMapOf(
                "supported" to false,
                "enabled" to false,
                "hfp_connected" to emptyList<String>(),
                "a2dp_connected" to emptyList<String>(),
                "bonded_count" to 0,
                "detail" to "no BluetoothAdapter on this device",
            )
        }

        val hfp = safe(errors, "HFP") { headsetDevices(context, adapter) } ?: emptyList()
        val a2dp = safe(errors, "A2DP") { a2dpDevices(context, adapter) } ?: emptyList()
        val bonded = safe(errors, "bondedDevices") { adapter.bondedDevices?.size } ?: 0

        return linkedMapOf(
            "supported" to true,
            "enabled" to safe(errors, "adapter.isEnabled") { adapter.isEnabled },
            "adapter_name" to safe(errors, "adapter.name") { adapter.name },
            "adapter_address" to safe(errors, "adapter.address") { adapter.address },
            "bonded_count" to bonded,
            "hfp_connected" to hfp,
            "a2dp_connected" to a2dp,
            "le_audio_supported" to
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    safe(errors, "isLeAudioSupported") { adapter.isLeAudioSupported }
                } else {
                    null
                },
            // The public API does not expose the Bluetooth specification version.
            // Recorded as null on purpose - and it is never used for a verdict.
            "version" to null,
            "version_note" to "not exposed by the public Android API",
        )
    }

    private fun headsetDevices(context: Context, adapter: BluetoothAdapter): List<String> =
        profileDevices(context, adapter, BluetoothProfile.HEADSET) { proxy ->
            (proxy as BluetoothHeadset).connectedDevices
        }

    private fun a2dpDevices(context: Context, adapter: BluetoothAdapter): List<String> =
        profileDevices(context, adapter, BluetoothProfile.A2DP) { proxy ->
            (proxy as BluetoothA2dp).connectedDevices
        }

    private fun profileDevices(
        context: Context,
        adapter: BluetoothAdapter,
        profile: Int,
        extract: (BluetoothProfile) -> List<android.bluetooth.BluetoothDevice>,
    ): List<String> {
        val latch = java.util.concurrent.CountDownLatch(1)
        var found: List<String> = emptyList()
        val listener = object : BluetoothProfile.ServiceListener {
            override fun onServiceConnected(profileId: Int, proxy: BluetoothProfile?) {
                try {
                    if (proxy != null) {
                        found = extract(proxy).mapNotNull { device ->
                            runCatching { device.address }.getOrNull()
                                ?: runCatching { device.name }.getOrNull()
                        }
                    }
                } catch (e: SecurityException) {
                    // BLUETOOTH_CONNECT not granted: names/addresses are hidden.
                } finally {
                    runCatching { adapter.closeProfileProxy(profileId, proxy) }
                    latch.countDown()
                }
            }

            override fun onServiceDisconnected(profileId: Int) {
                latch.countDown()
            }
        }
        if (!adapter.getProfileProxy(context, listener, profile)) return emptyList()
        latch.await(2, java.util.concurrent.TimeUnit.SECONDS)
        return found
    }

    // ------------------------------------------------------------------ //
    // route session: bring SCO up, observe it, tear it down
    // ------------------------------------------------------------------ //
    private class RouteSession(
        private val context: Context,
        private val audio: AudioManager,
        private val interruptions: MutableList<Map<String, Any?>>,
    ) {
        val pathName: String =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) "modern" else "legacy"

        var captureBufferMs: Double = 0.0
        var playbackBufferMs: Double = 0.0

        private val handler = Handler(Looper.getMainLooper())
        private val events = mutableListOf<Map<String, Any?>>()
        private val callback = object : AudioDeviceCallback() {
            override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>) {
                addedDevices.forEach { events += deviceEvent("added", it) }
            }

            override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>) {
                removedDevices.forEach {
                    events += deviceEvent("removed", it)
                    if (it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO) {
                        interruptions += mapOf(
                            "at_ms" to SystemClock.elapsedRealtime(),
                            "what" to "sco_device_removed",
                            "detail" to "the platform withdrew the SCO device during the probe",
                        )
                    }
                }
            }
        }

        private fun deviceEvent(what: String, info: AudioDeviceInfo): Map<String, Any?> = mapOf(
            "what" to what,
            "type" to AudioProbe.typeName(info.type),
            "at_ms" to SystemClock.elapsedRealtime(),
        )

        private var routed = false

        fun request(): Map<String, Any?> {
            audio.registerAudioDeviceCallback(callback, handler)
            val started = SystemClock.elapsedRealtime()
            val detail = StringBuilder()

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                val target = audio.availableCommunicationDevices
                    .firstOrNull { it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO }
                if (target == null) {
                    detail.append("no TYPE_BLUETOOTH_SCO in availableCommunicationDevices")
                    return result(started, detail.toString(), target = null, accepted = false)
                }
                val accepted = audio.setCommunicationDevice(target)
                if (!accepted) detail.append("setCommunicationDevice() returned false; ")
                routed = awaitRoute(started) {
                    audio.communicationDevice?.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO
                }
                detail.append(
                    if (routed) "communicationDevice reports bluetooth_sco"
                    else "communicationDevice never reported bluetooth_sco"
                )
                return result(started, detail.toString(), target, accepted)
            }

            // Legacy path (< API 31): MODE_IN_COMMUNICATION + startBluetoothSco.
            audio.mode = AudioManager.MODE_IN_COMMUNICATION
            val declared = audio.isBluetoothScoAvailableOffCall
            if (!declared) detail.append("isBluetoothScoAvailableOffCall() is false; ")
            val accepted = try {
                audio.startBluetoothSco()
                true
            } catch (e: Throwable) {
                detail.append("startBluetoothSco() threw ${e.javaClass.simpleName}; ")
                false
            }
            routed = awaitRoute(started) { audio.isBluetoothScoOn }
            detail.append(if (routed) "isBluetoothScoOn=true" else "isBluetoothScoOn stayed false")
            return result(started, detail.toString(), target = null, accepted = accepted)
        }

        private fun result(
            started: Long,
            detail: String,
            target: AudioDeviceInfo?,
            accepted: Boolean,
        ): Map<String, Any?> = linkedMapOf(
            "path" to pathName,
            "declared_offcall_available" to declaredOffCall(),
            "request_accepted" to accepted,
            "request_succeeded" to routed,
            "route_observed_ms" to if (routed) SystemClock.elapsedRealtime() - started else null,
            "communication_device_after" to communicationDeviceName(),
            "target_device" to target?.let { AudioProbe.typeName(it.type) },
            "audio_device_events" to events.toList(),
            "detail" to detail.trim(),
        )

        private fun declaredOffCall(): Boolean? = try {
            audio.isBluetoothScoAvailableOffCall
        } catch (e: Throwable) {
            null
        }

        private fun communicationDeviceName(): String? =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                audio.communicationDevice?.let { AudioProbe.typeName(it.type) }
            } else {
                null
            }

        private fun awaitRoute(started: Long, predicate: () -> Boolean): Boolean {
            while (SystemClock.elapsedRealtime() - started < SCO_ROUTE_TIMEOUT_MS) {
                if (predicate()) return true
                SystemClock.sleep(50)
            }
            return predicate()
        }

        fun release() {
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    audio.clearCommunicationDevice()
                } else {
                    runCatching { audio.stopBluetoothSco() }
                    audio.isBluetoothScoOn = false
                    audio.mode = AudioManager.MODE_NORMAL
                }
            } catch (e: Throwable) {
                // Nothing to restore failure-wise; the route is released by the OS anyway.
            } finally {
                runCatching { audio.unregisterAudioDeviceCallback(callback) }
            }
        }
    }

    // ------------------------------------------------------------------ //
    // tests
    // ------------------------------------------------------------------ //
    private fun testOutput(): Map<String, Any?> {
        val track = buildTrack()
        try {
            val tone = tone(FREQ, 0.2)
            track.play()
            val started = SystemClock.elapsedRealtime()
            var written = 0
            while (SystemClock.elapsedRealtime() - started < 1500) {
                written += track.write(tone, 0, tone.size)
            }
            SystemClock.sleep(120)
            val routed = track.routedDevice
            val routedName = routed?.let { AudioProbe.typeName(it.type) }
            val ok = routed?.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO
            return linkedMapOf(
                "status" to if (ok) "pass" else "fail",
                "routed_device" to routedName,
                "expected" to "bluetooth_sco",
                "underruns" to track.underrunCount,
                "frames_written" to written,
                "track_rate_hz" to RATE,
                "detail" to if (ok) "playback routed to the headset"
                else "playback routed to ${routedName ?: "nothing"}",
            )
        } finally {
            runCatching { track.stop() }
            runCatching { track.release() }
        }
    }

    private fun testInput(): Map<String, Any?> {
        val record = buildRecord()
        try {
            val buffer = ShortArray(CHUNK)
            var peak = 0
            var sumSquares = 0.0
            var samples = 0
            var shortReads = 0
            record.startRecording()
            val started = SystemClock.elapsedRealtime()
            while (SystemClock.elapsedRealtime() - started < INPUT_TEST_MS) {
                val read = record.read(buffer, 0, buffer.size, AudioRecord.READ_BLOCKING)
                if (read <= 0) {
                    shortReads++
                    continue
                }
                for (i in 0 until read) {
                    val value = buffer[i].toInt()
                    peak = maxOf(peak, abs(value))
                    sumSquares += value.toDouble() * value
                }
                samples += read
            }
            val rms = if (samples > 0) sqrt(sumSquares / samples) else 0.0
            val routed = record.routedDevice
            val routedName = routed?.let { AudioProbe.typeName(it.type) }
            val routedOk = routed?.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO
            val signal = peak > SIGNAL_PEAK_THRESHOLD
            return linkedMapOf(
                "status" to if (routedOk && samples > 0) "pass" else "fail",
                "routed_device" to routedName,
                "expected" to "bluetooth_sco",
                "record_rate_hz" to RATE,
                "samples" to samples,
                "rms" to rms,
                "peak" to peak,
                "signal_detected" to signal,
                "short_reads" to shortReads,
                "detail" to when {
                    !routedOk -> "microphone routed to ${routedName ?: "nothing"}"
                    !signal -> "routed, but every sample was below the silence threshold " +
                        "(speak during the test, or the headset mic is muted)"
                    else -> "captured ${samples} samples with peak $peak"
                },
            )
        } finally {
            runCatching { record.stop() }
            runCatching { record.release() }
        }
    }

    private fun testDuplex(seconds: Int, route: RouteSession): Map<String, Any?> {
        val clamped = seconds.coerceIn(5, 300)
        val track = buildTrack()
        val record = buildRecord()
        val playBuffer = tone(FREQ, 0.15)
        val micBuffer = ShortArray(CHUNK)
        var underrunsBefore = 0
        var readFailures = 0
        var lateChunks = 0
        var micPeak = 0
        var chunks = 0

        try {
            record.startRecording()
            track.play()
            underrunsBefore = track.underrunCount
            val iterations = clamped * 1000 / 20
            var index = 0
            while (index < iterations) {
                val loopStart = SystemClock.elapsedRealtime()
                track.write(playBuffer, 0, playBuffer.size)
                val read = record.read(micBuffer, 0, micBuffer.size, AudioRecord.READ_BLOCKING)
                if (read <= 0) {
                    readFailures++
                } else {
                    for (i in 0 until read) micPeak = maxOf(micPeak, abs(micBuffer[i].toInt()))
                }
                chunks++
                val elapsed = SystemClock.elapsedRealtime() - loopStart
                if (elapsed > 100) lateChunks++
                if (elapsed < 20) SystemClock.sleep(20 - elapsed)
                index++
                if (index % 50 == 0) sampleTimestamps(record, track, route)
            }
            sampleTimestamps(record, track, route)

            val underruns = track.underrunCount - underrunsBefore
            val ok = readFailures == 0 && underruns == 0 && chunks > 0 &&
                lateChunks * 100 / maxOf(chunks, 1) < 5
            return linkedMapOf(
                "status" to if (ok) "pass" else "fail",
                "seconds" to clamped,
                "chunks" to chunks,
                "underruns" to underruns,
                "read_failures" to readFailures,
                "late_chunks" to lateChunks,
                "mic_peak" to micPeak,
                "detail" to if (ok) "duplex held for ${clamped}s"
                else "underruns=$underruns read_failures=$readFailures late_chunks=$lateChunks",
            )
        } finally {
            runCatching { record.stop() }
            runCatching { record.release() }
            runCatching { track.stop() }
            runCatching { track.release() }
        }
    }

    private fun sampleTimestamps(record: AudioRecord, track: AudioTrack, route: RouteSession) {
        val now = System.nanoTime()
        try {
            val recordTs = AudioTimestamp()
            if (record.getTimestamp(recordTs, AudioTimestamp.TIMEBASE_MONOTONIC) ==
                AudioRecord.SUCCESS
            ) {
                route.captureBufferMs = maxOf(0.0, (now - recordTs.nanoTime) / 1_000_000.0)
            }
        } catch (e: Throwable) {
            // Timestamps are advisory; absence is reported as a missing estimate.
        }
        try {
            val trackTs = AudioTimestamp()
            if (track.getTimestamp(trackTs)) {
                route.playbackBufferMs = maxOf(0.0, (now - trackTs.nanoTime) / 1_000_000.0)
            }
        } catch (e: Throwable) {
            // Same as above.
        }
    }

    /**
     * Acoustic round trip: play a chirp out of the headset while recording from
     * its microphone, then cross-correlate what came back against what was sent.
     *
     * This is the only measurement here that includes the Bluetooth radio, the
     * codecs and the air path - i.e. the number that decides whether the relay
     * feels usable. It needs the earpiece to be audible to its own microphone:
     * wear the headset, or rest the earpiece against the boom.
     */
    private fun acousticLatency(): Map<String, Any?> {
        val track = buildTrack()
        val record = buildRecord()
        val totalSamples = ACOUSTIC_CAPTURE_MS * RATE / 1000
        val chirp = chirp(400.0, 3600.0, CHIRP_MS)
        val playback = ShortArray(totalSamples)
        var offset = 0
        while (offset + chirp.size < playback.size) {
            chirp.copyInto(playback, offset)
            offset += CHIRP_PERIOD_MS * RATE / 1000
        }
        val captured = ShortArray(totalSamples)

        try {
            record.startRecording()
            track.play()
            var read = 0
            var written = 0
            while (read < totalSamples) {
                val want = minOf(CHUNK, totalSamples - read)
                // Interleave: keep the playback queue fed, then take a chunk in.
                if (written < totalSamples) {
                    val w = minOf(CHUNK, totalSamples - written)
                    val n = track.write(playback, written, w)
                    if (n > 0) written += n
                }
                val n = record.read(captured, read, want, AudioRecord.READ_BLOCKING)
                if (n > 0) read += n else break
            }
        } finally {
            runCatching { record.stop() }
            runCatching { record.release() }
            runCatching { track.stop() }
            runCatching { track.release() }
        }

        val peak = captured.maxOfOrNull { abs(it.toInt()) } ?: 0
        val match = bestLag(captured, chirp, RATE)
        if (peak < SIGNAL_PEAK_THRESHOLD) {
            return mapOf(
                "method" to "acoustic_loopback",
                "round_trip_ms" to null,
                "confidence" to 0.0,
                "detail" to "recorded signal was silent (peak $peak); the earpiece is probably " +
                    "not audible to the microphone",
            )
        }
        if (match == null || match.second < 3.0) {
            return mapOf(
                "method" to "acoustic_loopback",
                "round_trip_ms" to null,
                "confidence" to (match?.second ?: 0.0),
                "detail" to "chirp not identifiable in the recording; result inconclusive",
            )
        }
        return mapOf(
            "method" to "acoustic_loopback",
            "round_trip_ms" to match.first,
            "confidence" to match.second,
            "peak" to peak,
            "detail" to "cross-correlation peak at ${"%.1f".format(java.util.Locale.ROOT, match.first)} ms",
        )
    }

    /**
     * Cross-correlation of [captured] against [template], decimated 4x to keep it
     * cheap. Returns (lag_ms, confidence) where confidence is peak/mean over all
     * lags; a confident peak is several times the mean.
     */
    private fun bestLag(captured: ShortArray, template: ShortArray, rate: Int): Pair<Double, Double>? {
        val decim = 4
        val maxLagSamples = minOf(captured.size, ACOUSTIC_MAX_LAG_MS * rate / 1000)
        val x = decimate(captured, decim, 0, captured.size)
        val t = decimate(template, decim, 0, template.size)
        if (x.isEmpty() || t.isEmpty()) return null
        val templateEnergy = t.sumOf { it * it }
        if (templateEnergy <= 0.0) return null

        var bestLag = 0
        var bestScore = 0.0
        var sum = 0.0
        var count = 0
        val lagStep = 1
        var lag = 0
        while (lag < maxLagSamples / decim && lag + t.size <= x.size) {
            var dot = 0.0
            for (i in t.indices) dot += x[lag + i] * t[i]
            val score = dot / sqrt(templateEnergy)
            sum += abs(score)
            count++
            if (score > bestScore) {
                bestScore = score
                bestLag = lag
            }
            lag += lagStep
        }
        if (count == 0) return null
        val mean = sum / count
        val confidence = if (mean > 0.0) bestScore / mean else 0.0
        return Pair(bestLag * decim * 1000.0 / rate, confidence)
    }

    private fun decimate(source: ShortArray, factor: Int, from: Int, to: Int): DoubleArray {
        val out = ArrayList<Double>((to - from) / factor + 1)
        var i = from
        while (i < to) {
            var sum = 0.0
            var n = 0
            var j = i
            while (j < minOf(i + factor, to)) {
                sum += source[j]
                n++
                j++
            }
            out.add(sum / maxOf(n, 1))
            i += factor
        }
        val values = out.toDoubleArray()
        if (values.isEmpty()) return values
        val mean = values.average()
        for (k in values.indices) values[k] -= mean
        return values
    }

    // ------------------------------------------------------------------ //
    // audio plumbing
    // ------------------------------------------------------------------ //
    private fun buildTrack(): AudioTrack {
        val minBuffer = AudioTrack.getMinBufferSize(
            RATE,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        return AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(RATE)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .build(),
            )
            .setBufferSizeInBytes(maxOf(minBuffer, RATE))  // >= 0.5 s of audio
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
    }

    private fun buildRecord(): AudioRecord {
        val minBuffer = AudioRecord.getMinBufferSize(
            RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        return AudioRecord.Builder()
            .setAudioSource(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
            .setAudioFormat(
                AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(RATE)
                    .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                    .build(),
            )
            .setBufferSizeInBytes(maxOf(minBuffer, RATE))
            .build()
    }

    private fun tone(frequency: Double, amplitude: Double): ShortArray {
        val out = ShortArray(CHUNK)
        for (i in out.indices) {
            out[i] = (sin(2 * PI * frequency * i / RATE) * amplitude * Short.MAX_VALUE).toInt()
                .toShort()
        }
        return out
    }

    private fun chirp(fromHz: Double, toHz: Double, ms: Int): ShortArray {
        val samples = ms * RATE / 1000
        val out = ShortArray(samples)
        var phase = 0.0
        for (i in 0 until samples) {
            val progress = i.toDouble() / samples
            val frequency = fromHz + (toHz - fromHz) * progress
            phase += 2 * PI * frequency / RATE
            // Hann window keeps the chirp band-limited enough to correlate cleanly.
            val window = 0.5 * (1 - kotlin.math.cos(2 * PI * i / (samples - 1)))
            out[i] = (sin(phase) * window * 0.6 * Short.MAX_VALUE).toInt().toShort()
        }
        return out
    }

    // ------------------------------------------------------------------ //
    // small helpers
    // ------------------------------------------------------------------ //
    private fun hasPermission(context: Context, permission: String): Boolean =
        context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED

    private fun skipped(detail: String): Map<String, Any?> =
        mapOf("status" to "skipped", "detail" to detail)

    private fun errorBlock(what: String): Map<String, Any?> =
        mapOf("status" to "error", "detail" to "$what test threw")

    private fun <T> guarded(errors: MutableList<String>, block: () -> T): T? = try {
        block()
    } catch (e: Throwable) {
        errors += "${e.javaClass.simpleName}: ${e.message}"
        null
    }

    private fun <T> safe(errors: MutableList<String>, what: String, block: () -> T): T? = try {
        block()
    } catch (e: SecurityException) {
        errors += "$what: SecurityException (${e.message}) - a runtime Bluetooth permission " +
            "is probably missing"
        null
    } catch (e: Throwable) {
        errors += "$what: ${e.javaClass.simpleName}: ${e.message}"
        null
    }

    private fun isoNow(): String {
        val format = java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", java.util.Locale.ROOT)
        format.timeZone = java.util.TimeZone.getTimeZone("UTC")
        return format.format(java.util.Date())
    }

    private const val FREQ = 440.0
    private const val INPUT_TEST_MS = 3000L
    private const val ACOUSTIC_CAPTURE_MS = 6000
    private const val ACOUSTIC_MAX_LAG_MS = 600
    private const val CHIRP_MS = 120
    private const val CHIRP_PERIOD_MS = 600
}
