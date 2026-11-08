package dev.phonebridge

import android.content.Context
import android.os.Build
import android.os.Bundle
import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Divider
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.phonebridge.net.AudioProbe
import dev.phonebridge.net.IdentityStore
import dev.phonebridge.net.PairingStore
import dev.phonebridge.protocol.SelfTest
import dev.phonebridge.service.BridgeService
import dev.phonebridge.service.BridgeState
import kotlinx.coroutines.delay

/**
 * The phone-side dashboard.
 *
 * Every control here does something real: the switch starts/stops the foreground
 * service, the pairing buttons accept or refuse the pending PC, the self-test
 * runs the protocol known-answer vectors, and the status cards read live state
 * from [BridgeState] and [AudioProbe].
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            PhoneBridgeTheme {
                Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                    DashboardScreen()
                }
            }
        }
    }
}

@Composable
private fun PhoneBridgeTheme(content: @Composable () -> Unit) {
    val context = LocalContext.current
    val colors = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S -> {
            if (isSystemDark(context)) dynamicDarkColorScheme(context)
            else dynamicLightColorScheme(context)
        }

        isSystemDark(context) -> darkColorScheme()
        else -> lightColorScheme()
    }
    MaterialTheme(colorScheme = colors, content = content)
}

private fun isSystemDark(context: Context): Boolean =
    (context.resources.configuration.uiMode and
        android.content.res.Configuration.UI_MODE_NIGHT_MASK) ==
        android.content.res.Configuration.UI_MODE_NIGHT_YES

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DashboardScreen() {
    val context = LocalContext.current
    val running by BridgeState.running.collectAsState()
    val peers by BridgeState.peers.collectAsState()
    val pairing by BridgeState.pairingRequest.collectAsState()
    val stats by BridgeState.stats.collectAsState()
    val log by BridgeState.log.collectAsState()
    val deviceId by BridgeState.deviceId.collectAsState()
    val deviceName by BridgeState.deviceName.collectAsState()
    val port by BridgeState.port.collectAsState()
    val lastError by BridgeState.lastError.collectAsState()

    val identityStore = remember { IdentityStore(context) }
    val pairingStore = remember { PairingStore(context) }

    // Android 13+ needs an explicit grant before the foreground-service
    // notification is visible. Asked once, at first launch, with no nagging.
    val notificationPermission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { /* the service still runs; only the notification stays hidden */ }
    LaunchedEffect(Unit) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    var selfTestResults by remember { mutableStateOf<List<SelfTest.Check>?>(null) }
    var headset by remember { mutableStateOf(AudioProbe.headsetConnected(context)) }
    var profile by remember { mutableStateOf(AudioProbe.bluetoothProfileName(context)) }
    var devices by remember { mutableStateOf(AudioProbe.describeCommunicationDevices(context)) }

    // Poll the audio route: AudioManager has no callback for "headset changed
    // profile", and this is a 2-second read of a small list.
    LaunchedEffect(Unit) {
        while (true) {
            headset = AudioProbe.headsetConnected(context)
            profile = AudioProbe.bluetoothProfileName(context)
            devices = AudioProbe.describeCommunicationDevices(context)
            delay(2000)
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("PhoneBridge") },
                actions = {
                    Text(
                        when {
                            lastError != null -> "error"
                            running -> "listening"
                            else -> "stopped"
                        },
                        color = when {
                            lastError != null -> MaterialTheme.colorScheme.error
                            running -> Color(0xFF2E7D32)
                            else -> MaterialTheme.colorScheme.onSurfaceVariant
                        },
                        modifier = Modifier.padding(end = 16.dp),
                        fontWeight = FontWeight.Medium,
                    )
                },
            )
        },
    ) { padding ->
        LazyColumn(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(horizontal = 12.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item {
                Section("Bridge") {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Switch(
                            checked = running,
                            onCheckedChange = { on ->
                                if (on) BridgeService.start(context) else BridgeService.stop(context)
                            },
                        )
                        Spacer(Modifier.width(12.dp))
                        Text(if (running) "Service running on port $port" else "Service stopped")
                    }
                    if (lastError != null) {
                        Text(
                            "Error: $lastError",
                            color = MaterialTheme.colorScheme.error,
                            fontSize = 13.sp,
                        )
                    }
                    Text(
                        "Reachable as ws://<this phone's IP>:$port  ·  " +
                            "USB: run adb forward tcp:$port tcp:$port",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }

            item {
                Section("This device") {
                    KeyValue("Device id", deviceId.ifBlank { "-" })
                    KeyValue("Bluetooth headset", if (headset) "connected (HFP/SCO)" else "not connected")
                    KeyValue("Audio profile", profile)
                    devices.forEach { d ->
                        KeyValue(
                            "  · ${d["type"]}",
                            "${d["product"] ?: "unknown"}  sink=${d["is_sink"]}",
                            small = true,
                        )
                    }
                    if (!headset) {
                        Text(
                            "Connect your Bluetooth headphones to the phone before " +
                                "starting the audio relay (Phase 2).",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }

            val request = pairing
            if (request != null) {
                item {
                    Card(
                        colors = CardDefaults.cardColors(
                            containerColor = MaterialTheme.colorScheme.errorContainer
                        ),
                    ) {
                        Column(Modifier.padding(14.dp)) {
                            Text("Pairing request", fontWeight = FontWeight.Bold)
                            Text("From: ${request.name}")
                            Text(
                                request.sas,
                                fontSize = 30.sp,
                                fontFamily = FontFamily.Monospace,
                                fontWeight = FontWeight.Bold,
                                modifier = Modifier.padding(vertical = 8.dp),
                            )
                            Text(
                                "This code must match the one shown on your PC. " +
                                    "If it does not, refuse: someone may be intercepting.",
                                fontSize = 12.sp,
                            )
                            Row(Modifier.padding(top = 10.dp)) {
                                Button(onClick = { BridgeState.answerPairing(true) }) {
                                    Text("Codes match — allow")
                                }
                                Spacer(Modifier.width(10.dp))
                                OutlinedButton(onClick = { BridgeState.answerPairing(false) }) {
                                    Text("Refuse")
                                }
                            }
                        }
                    }
                }
            }

            item {
                Section("Connected PCs") {
                    if (peers.isEmpty()) {
                        Text(
                            "No PC connected yet.",
                            fontSize = 13.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    } else {
                        peers.forEach { peer ->
                            KeyValue(peer.name, "${peer.remoteAddress}  since " +
                                java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.US)
                                    .format(java.util.Date(peer.connectedAt)))
                        }
                    }
                    Divider(Modifier.padding(vertical = 6.dp))
                    val pinned = pairingStore.list()
                    KeyValue("Paired PCs", pinned.size.toString())
                    pinned.forEach { pc -> KeyValue("  · ${pc.name}", pc.deviceId, small = true) }
                    if (pinned.isNotEmpty()) {
                        OutlinedButton(
                            onClick = {
                                pinned.forEach { pairingStore.unpair(it.deviceId) }
                            },
                            modifier = Modifier.padding(top = 6.dp),
                        ) { Text("Forget all paired PCs") }
                    }
                }
            }

            item {
                Section("Traffic") {
                    KeyValue("Frames in / out", "${stats.framesIn} / ${stats.framesOut}")
                    KeyValue(
                        "Bytes in / out",
                        "${stats.bytesIn / 1024} KiB / ${stats.bytesOut / 1024} KiB",
                    )
                    KeyValue("Handshakes", stats.handshakes.toString())
                }
            }

            item { CapabilityProbeCard() }

            item {
                Section("Protocol self-test") {
                    Text(
                        "Checks this phone's crypto against the PC's known-answer " +
                            "vectors: ECDH, HKDF, SAS, AES-GCM framing.",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Button(
                        onClick = { selfTestResults = SelfTest.run(context) },
                        modifier = Modifier.padding(top = 8.dp),
                    ) { Text("Run protocol self-test") }

                    selfTestResults?.let { results ->
                        val failures = results.count { !it.passed }
                        Text(
                            if (failures == 0) "All ${results.size} checks passed"
                            else "$failures of ${results.size} checks FAILED",
                            color = if (failures == 0) Color(0xFF2E7D32)
                            else MaterialTheme.colorScheme.error,
                            fontWeight = FontWeight.Bold,
                            modifier = Modifier.padding(top = 8.dp),
                        )
                        results.forEach { check ->
                            KeyValue(
                                (if (check.passed) "PASS  " else "FAIL  ") + check.name,
                                check.detail,
                                small = true,
                            )
                        }
                    }
                }
            }

            item {
                Section("Settings") {
                    var nameDraft by remember(deviceName) { mutableStateOf(deviceName) }
                    var portDraft by remember(port) { mutableStateOf(port.toString()) }
                    OutlinedTextField(
                        value = nameDraft,
                        onValueChange = { nameDraft = it },
                        label = { Text("Device name") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Spacer(Modifier.height(8.dp))
                    OutlinedTextField(
                        value = portDraft,
                        onValueChange = { portDraft = it.filter(Char::isDigit).take(5) },
                        label = { Text("Port") },
                        singleLine = true,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Row(Modifier.padding(top = 8.dp)) {
                        Button(onClick = {
                            val newPort = portDraft.toIntOrNull()
                            if (newPort != null && newPort in 1024..65535) {
                                context.getSharedPreferences(
                                    "phonebridge_settings", Context.MODE_PRIVATE
                                ).edit().putInt("port", newPort).apply()
                            }
                            val identity = identityStore.loadOrCreate(nameDraft.ifBlank { deviceName })
                            identityStore.rename(identity, nameDraft.ifBlank { deviceName })
                            restartService(context)
                        }) { Text("Save and restart") }
                    }
                    Text(
                        "Changing the name or port restarts the bridge. Ports below 1024 " +
                            "are refused by Android.",
                        fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 6.dp),
                    )
                }
            }

            item {
                Section("Log") {
                    if (log.isEmpty()) {
                        Text(
                            "Nothing logged yet.",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    log.takeLast(25).reversed().forEach { line ->
                        Text(
                            "${line.at} ${line.level} ${line.message}",
                            fontSize = 11.sp,
                            fontFamily = FontFamily.Monospace,
                            color = if (line.level == "ERROR") MaterialTheme.colorScheme.error
                            else MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }

            item { Spacer(Modifier.height(24.dp)) }
        }
    }
}

@Composable
private fun Section(title: String, content: @Composable () -> Unit) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp)) {
            Text(
                title,
                fontWeight = FontWeight.Bold,
                fontSize = 15.sp,
                modifier = Modifier.padding(bottom = 8.dp),
            )
            content()
        }
    }
}

@Composable
private fun KeyValue(key: String, value: String, small: Boolean = false) {
    Row(modifier = Modifier.fillMaxWidth().padding(vertical = 1.dp)) {
        Text(
            key,
            fontSize = if (small) 11.sp else 13.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.weight(0.42f),
        )
        Text(value, fontSize = if (small) 11.sp else 13.sp, modifier = Modifier.weight(0.58f))
    }
}

private fun restartService(context: Context) {
    BridgeService.stop(context)
    // Give the service a moment to tear down the socket before rebinding.
    android.os.Handler(android.os.Looper.getMainLooper()).postDelayed(
        { BridgeService.start(context) },
        350,
    )
}
