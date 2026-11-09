package dev.phonebridge

import android.content.Context
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.enableEdgeToEdge
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
import androidx.compose.material3.AlertDialog
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
import androidx.compose.material3.TextButton
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
import dev.phonebridge.net.DeviceFacts
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
 * service, the pairing buttons accept or refuse the pending PC, the capability
 * probe measures the device, the self-test runs the protocol known-answer
 * vectors, and the status/capability/permission cards read live state rather than
 * restating a hard-coded wish list.
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Android 15+ draws every app edge-to-edge; asking for it explicitly
        // means the layout is identical on 8-14 instead of switching shape at
        // the OS upgrade. Scaffold below consumes the insets, so the content
        // never lands under the status or navigation bar.
        enableEdgeToEdge()
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
    val facts = remember { DeviceFacts.deviceInfo(context) }

    val settings = remember {
        context.getSharedPreferences("phonebridge_settings", Context.MODE_PRIVATE)
    }
    // First-run guidance: shown until dismissed, and never again once the user
    // has started the bridge or paired a PC (either means they found their way).
    var onboardingDone by remember {
        mutableStateOf(settings.getBoolean("onboarding_done", false))
    }
    var confirmForgetAll by remember { mutableStateOf(false) }
    var portError by remember { mutableStateOf<String?>(null) }

    var selfTestResults by remember { mutableStateOf<List<SelfTest.Check>?>(null) }
    var headset by remember { mutableStateOf(AudioProbe.headsetConnected(context)) }
    var profile by remember { mutableStateOf(AudioProbe.bluetoothProfileName(context)) }
    var devices by remember { mutableStateOf(AudioProbe.describeCommunicationDevices(context)) }
    var lanIp by remember { mutableStateOf(DeviceFacts.lanIpv4()) }

    // The notification permission is only worth asking for at the moment the
    // bridge starts: that is when Android needs it to show the service
    // notification. A refusal is not fatal and never blocks the start.
    val notificationPermission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { /* the bridge runs either way; only the notification is hidden */ }

    // Poll the audio route and the LAN address: AudioManager has no callback for
    // "headset changed profile", and the address changes when Wi-Fi does.
    LaunchedEffect(Unit) {
        while (true) {
            headset = AudioProbe.headsetConnected(context)
            profile = AudioProbe.bluetoothProfileName(context)
            devices = AudioProbe.describeCommunicationDevices(context)
            lanIp = DeviceFacts.lanIpv4()
            delay(2000)
        }
    }

    val request = pairing

    val status = statusOf(running, peers.size, lastError)

    fun startBridge() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            !Permissions.granted(context, android.Manifest.permission.POST_NOTIFICATIONS)
        ) {
            // Marked as asked so the permission card can tell "denied" from
            // "blocked by Android" later.
            Permissions.markAsked(context, android.Manifest.permission.POST_NOTIFICATIONS)
            notificationPermission.launch(android.Manifest.permission.POST_NOTIFICATIONS)
        }
        BridgeService.start(context)
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("PhoneBridge") },
                actions = {
                    Row(Modifier.padding(end = 14.dp)) { StatusPill(status, peers.size) }
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
            if (!onboardingDone && !running && peers.isEmpty() && request == null) {
                item {
                    OnboardingCard(
                        onDismiss = {
                            settings.edit().putBoolean("onboarding_done", true).apply()
                            onboardingDone = true
                        },
                    )
                }
            }

            item {
                Section("Bridge") {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Switch(
                            checked = running,
                            onCheckedChange = { on ->
                                if (on) startBridge() else BridgeService.stop(context)
                            },
                        )
                        Spacer(Modifier.width(12.dp))
                        Text(
                            if (running) "Running on port $port" else "Stopped",
                            fontWeight = FontWeight.Medium,
                        )
                    }
                    Spacer(Modifier.height(6.dp))
                    KeyValue(
                        "Connect from the PC",
                        when {
                            peers.isNotEmpty() -> "connected"
                            lanIp != null -> "ws://$lanIp:$port"
                            else -> "no LAN address — join Wi-Fi or use USB"
                        },
                    )
                    KeyValue("USB", "adb forward tcp:$port tcp:$port  →  ws://127.0.0.1:$port")
                    Text(
                        "The bridge keeps running with the screen off; a network change " +
                            "does not stop it, but the PC must reconnect to the address above.",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 6.dp),
                    )
                    if (lastError != null) {
                        Spacer(Modifier.height(8.dp))
                        Text(
                            "Error: $lastError",
                            color = MaterialTheme.colorScheme.error,
                            fontSize = 13.sp,
                        )
                        explainBridgeError(lastError, port)?.let { hint ->
                            Text(
                                hint,
                                color = MaterialTheme.colorScheme.error,
                                fontSize = 12.sp,
                                modifier = Modifier.padding(top = 3.dp),
                            )
                        }
                        OutlinedButton(
                            onClick = { restartService(context) },
                            modifier = Modifier.padding(top = 6.dp),
                        ) { Text("Restart bridge") }
                    }
                }
            }

            item { PermissionsCard() }

            item {
                Section("This device") {
                    KeyValue("Model", "${facts["manufacturer"]} ${facts["model"]}")
                    KeyValue("Codename", "${facts["device"]} (${facts["product"]})")
                    KeyValue(
                        "Android",
                        "${facts["android_release"]} · SDK ${facts["sdk_int"]} · " +
                            "target ${facts["target_sdk"]}",
                    )
                    KeyValue(
                        "App",
                        "${facts["app_version"]} (build ${facts["app_version_code"]})",
                    )
                    KeyValue("ROM", "${facts["rom"]} ${facts["rom_version"] ?: ""}".trim())
                    KeyValue("Protocol", "v${facts["protocol_version"]}")
                    KeyValue("Device id", deviceId.ifBlank { "-" })
                    KeyValue("Name", deviceName.ifBlank { "-" })
                    Divider(Modifier.padding(vertical = 6.dp))
                    KeyValue(
                        "Bluetooth headset",
                        if (headset) "connected (HFP/SCO)" else "not connected",
                    )
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
                                "running the capability probe.",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(top = 4.dp),
                        )
                    }
                }
            }

            item { CapabilitiesCard() }

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
                                Button(onClick = { BridgeState.answerPairing(request, true) }) {
                                    Text("Codes match — allow")
                                }
                                Spacer(Modifier.width(10.dp))
                                OutlinedButton(onClick = { BridgeState.answerPairing(request, false) }) {
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
                            KeyValue(
                                peer.name,
                                "${peer.remoteAddress}  since " +
                                    java.text.SimpleDateFormat(
                                        "HH:mm:ss",
                                        java.util.Locale.US,
                                    ).format(java.util.Date(peer.connectedAt)),
                            )
                        }
                    }
                    Divider(Modifier.padding(vertical = 6.dp))
                    val pinned = pairingStore.list()
                    KeyValue("Paired PCs", pinned.size.toString())
                    pinned.forEach { pc -> KeyValue("  · ${pc.name}", pc.deviceId, small = true) }
                    if (pinned.isNotEmpty()) {
                        OutlinedButton(
                            onClick = { confirmForgetAll = true },
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
                            if (newPort == null || newPort !in 1024..65535) {
                                // Refusing silently was the old behaviour: the
                                // user pressed Save and nothing told them why
                                // the port was unchanged.
                                portError = "Port must be a whole number between 1024 and 65535."
                            } else {
                                portError = null
                                settings.edit().putInt("port", newPort).apply()
                                val chosen = nameDraft.ifBlank { deviceName }
                                val identity = identityStore.loadOrCreate(chosen)
                                identityStore.rename(identity, chosen)
                                restartService(context)
                            }
                        }) { Text("Save and restart") }
                    }
                    portError?.let { message ->
                        Text(
                            message,
                            color = MaterialTheme.colorScheme.error,
                            fontSize = 12.sp,
                            modifier = Modifier.padding(top = 6.dp),
                        )
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

    if (confirmForgetAll) {
        AlertDialog(
            onDismissRequest = { confirmForgetAll = false },
            title = { Text("Forget every paired PC?") },
            text = {
                Text(
                    "Each PC will have to pair again with a new six-digit code. " +
                        "Use this if you no longer trust a computer that has access to " +
                        "this phone.",
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    pairingStore.list().forEach { pairingStore.unpair(it.deviceId) }
                    confirmForgetAll = false
                    BridgeState.note("WARN", "all paired PCs were forgotten from the UI")
                }) { Text("Forget all") }
            },
            dismissButton = {
                TextButton(onClick = { confirmForgetAll = false }) { Text("Cancel") }
            },
        )
    }
}

/**
 * First-run guidance.
 *
 * Three steps, in the order a new user needs them, and a way to make it go away
 * for good. A dashboard full of counters is not onboarding: without this, the
 * first thing a new install shows is a switch labelled "Stopped".
 */
@Composable
private fun OnboardingCard(onDismiss: () -> Unit) {
    Card {
        Column(Modifier.padding(14.dp)) {
            Text("Start here", fontWeight = FontWeight.Bold, fontSize = 16.sp)
            Spacer(Modifier.height(6.dp))
            Text(
                "1.  Turn the bridge on with the switch below.",
                fontSize = 13.sp,
            )
            Text(
                "2.  On your Windows PC:  pip install phonebridge  then  phonebridge discover",
                fontSize = 13.sp,
                fontFamily = FontFamily.Monospace,
            )
            Text(
                "3.  Compare the six-digit code shown on both screens. If the codes " +
                    "match, allow the pairing on the phone.",
                fontSize = 13.sp,
            )
            Spacer(Modifier.height(6.dp))
            Text(
                "The PC never needs the internet, an account, or a cable. USB, Wi-Fi and " +
                    "the phone's hotspot all work.",
                fontSize = 12.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            OutlinedButton(onClick = onDismiss, modifier = Modifier.padding(top = 8.dp)) {
                Text("Got it")
            }
        }
    }
}

private fun restartService(context: Context) {
    // The service sequences stop-then-start itself: sleeping in the UI and
    // hoping the old socket had closed was a race that could leave the bridge
    // down with no error shown.
    BridgeService.restart(context)
}
