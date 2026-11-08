package dev.phonebridge

import android.Manifest
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.view.WindowManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.phonebridge.net.CapabilityProbe
import dev.phonebridge.protocol.Json
import dev.phonebridge.service.BridgeState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Runs the capability probe and shows what it measured.
 *
 * This is the phone side of the multi-device test: the result can be read here,
 * copied as JSON, or fetched from the PC with `phonebridge compat run`.
 *
 * Rules that shaped the UI:
 *  - the probe is started explicitly, never in the background;
 *  - the screen is kept awake while it runs, because Android 14 stops capture
 *    from an app that is no longer visible;
 *  - nothing is summarised as "supported" that was not measured.
 */
@Composable
fun CapabilityProbeCard(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var running by remember { mutableStateOf(false) }
    var acoustic by remember { mutableStateOf(true) }
    var report by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }

    val micPermission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { }
    val bluetoothPermission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { }

    // The probe records audio; a locked screen stops recording on Android 14+.
    KeepScreenOn(enabled = running)

    fun startProbe() {
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            micPermission.launch(Manifest.permission.RECORD_AUDIO)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
            context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            bluetoothPermission.launch(Manifest.permission.BLUETOOTH_CONNECT)
        }
        running = true
        error = null
        scope.launch {
            val outcome = withContext(Dispatchers.IO) {
                runCatching {
                    CapabilityProbe.run(
                        context,
                        CapabilityProbe.Config(seconds = 30, acoustic = acoustic),
                    )
                }
            }
            outcome.onSuccess {
                report = it
                BridgeState.publishProbe(it)
            }
            outcome.onFailure {
                error = "${it.javaClass.simpleName}: ${it.message}"
            }
            running = false
        }
    }

    Card(modifier = modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp)) {
            Text(
                "Capability probe",
                fontWeight = FontWeight.Bold,
                fontSize = 15.sp,
                modifier = Modifier.padding(bottom = 8.dp),
            )
            Text(
                "Measures what this phone can really do with a Bluetooth headset: " +
                    "routing, microphone, duplex, latency. Connect the headset first.",
                fontSize = 13.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.padding(top = 8.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Switch(checked = acoustic, onCheckedChange = { if (!running) acoustic = it })
                Spacer(Modifier.width(10.dp))
                Text("Acoustic latency (play a chirp, record it)", fontSize = 13.sp)
            }
            Spacer(Modifier.padding(top = 8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { startProbe() }, enabled = !running) {
                    Text(if (running) "Probing…" else "Run probe (30 s)")
                }
                OutlinedButton(
                    onClick = {
                        val data = report ?: return@OutlinedButton
                        val json = Json.encodeToBytes(data).decodeToString()
                        val clipboard =
                            context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                        clipboard.setPrimaryClip(ClipData.newPlainText("phonebridge-probe", json))
                    },
                    enabled = report != null && !running,
                ) { Text("Copy JSON") }
            }
            if (error != null) {
                Spacer(Modifier.padding(top = 8.dp))
                Text(
                    error ?: "",
                    color = MaterialTheme.colorScheme.error,
                    fontSize = 13.sp,
                )
            }
            report?.let { ProbeSummary(it) }
        }
    }
}

@Composable
private fun ProbeSummary(report: Map<String, Any?>) {
    val device = report.block("device")
    val bluetooth = report.block("bluetooth")
    val sco = report.block("sco")
    val output = report.block("route_output")
    val input = report.block("route_input")
    val duplex = report.block("duplex")
    val latency = report.block("latency")

    Column(Modifier.padding(top = 10.dp)) {
        ProbeRow("Device", "${device["manufacturer"]} ${device["model"]} (${device["codename"]})")
        ProbeRow("Android", "${device["android_release"]} / SDK ${device["sdk_int"]}")
        ProbeRow("ROM", "${device["rom"] ?: "unknown"} ${device["rom_version"] ?: ""}".trim())
        ProbeRow("Bluetooth", if (bluetooth["enabled"] == true) "enabled" else "disabled/unavailable")
        ProbeRow("HFP connected", (bluetooth["hfp_connected"] as? List<*>)?.joinToString(", ")
            ?.ifBlank { "none" } ?: "none")
        ProbeRow("SCO path", "${sco["path"]}")
        ProbeRow("Declared off-call", "${sco["declared_offcall_available"]}")
        ProbeRow("Route established", "${sco["request_succeeded"]}" +
            (sco["route_observed_ms"]?.let { " after ${it} ms" } ?: ""))
        ProbeRow("Playback → headset", statusLine(output))
        ProbeRow("Microphone ← headset", statusLine(input) +
            (input["signal_detected"]?.let { " (signal: $it)" } ?: ""))
        ProbeRow("Duplex", statusLine(duplex))
        ProbeRow("Latency", latencyLine(latency))
        val errors = report["errors"] as? List<*>
        if (!errors.isNullOrEmpty()) {
            ProbeRow("Problems", errors.joinToString("; "))
        }
        Text(
            "Copy the JSON (or run `phonebridge compat run --host <ip>`) to add this " +
                "device to the compatibility report.",
            fontSize = 12.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 8.dp),
        )
    }
}

@Composable
private fun ProbeRow(key: String, value: String) {
    Row(Modifier.fillMaxWidth().padding(vertical = 1.dp)) {
        Text(
            key,
            fontSize = 13.sp,
            modifier = Modifier.width(150.dp),
            fontWeight = FontWeight.Medium,
        )
        Text(
            value,
            fontSize = 13.sp,
            fontFamily = FontFamily.Monospace,
        )
    }
}

@Suppress("UNCHECKED_CAST")
private fun Map<String, Any?>.block(key: String): Map<String, Any?> =
    (this[key] as? Map<String, Any?>) ?: emptyMap()

private fun statusLine(block: Map<String, Any?>): String {
    val status = block["status"] as? String ?: "not measured"
    val detail = block["detail"] as? String
    return if (detail.isNullOrBlank()) status else "$status — $detail"
}

private fun latencyLine(latency: Map<String, Any?>): String {
    val value = latency["round_trip_ms"] ?: latency["estimate_ms"]
    val unit = if (latency["round_trip_ms"] != null) "ms round trip" else "ms buffering estimate"
    val method = latency["method"] as? String ?: "none"
    return if (value is Number) "${value.toInt()} $unit ($method)" else method
}

@Composable
private fun KeepScreenOn(enabled: Boolean) {
    val context = LocalContext.current
    DisposableEffect(enabled) {
        val window = (context as? Activity)?.window
        if (enabled) {
            window?.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
        onDispose {
            if (enabled) {
                window?.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            }
        }
    }
}
