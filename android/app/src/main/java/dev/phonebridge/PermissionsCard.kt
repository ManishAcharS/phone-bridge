package dev.phonebridge

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.phonebridge.service.BridgeState
import kotlinx.coroutines.delay

/**
 * The permission panel: state, the reason, and the one action that can help.
 *
 * * **Granted** — shown as granted; nothing to press.
 * * **Denied** — "Allow" re-opens the system dialog, with the reason above it.
 * * **Blocked (permanently denied)** — the system will not show a dialog again,
 *   so the button becomes "Open app settings". This is the case the old UI could
 *   not handle: it kept launching a request that silently did nothing.
 * * **Not applicable** — e.g. BLUETOOTH_CONNECT below Android 12.
 *
 * The state is re-read on a timer, which is how a revocation from the system
 * settings (or an auto-reset of an unused app's permissions) shows up while the
 * app is still open. A revoked microphone permission is also written to the
 * bridge log, because the PC side needs to know why its capture tests stop
 * working.
 */
@Composable
fun PermissionsCard(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    var rows by remember { mutableStateOf(Permissions.snapshot(context)) }
    var lastError by remember { mutableStateOf<String?>(null) }

    val launcher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { result ->
        // Record that we asked: that is what separates "denied" from "blocked".
        result.keys.forEach { Permissions.markAsked(context, it) }
        rows = Permissions.snapshot(context)
        lastError = result.entries
            .filterValues { !it }
            .mapNotNull { (permission, _) ->
                Permissions.runtime.firstOrNull { it.permission == permission }
                    ?.let { item ->
                        Permissions.explainDenial(item, Permissions.status(context, item))
                    }
            }
            .joinToString("\n")
            .ifBlank { null }
    }

    LaunchedEffect(Unit) {
        var previous = rows
        while (true) {
            val now = Permissions.snapshot(context)
            for ((item, status) in now) {
                val before = previous.firstOrNull { it.first.permission == item.permission }?.second
                if (before == Permissions.Status.GRANTED && status != Permissions.Status.GRANTED) {
                    BridgeState.note(
                        "WARN",
                        "${item.shortName} was revoked while running — " +
                            "audio.capture is now reported as unavailable",
                    )
                }
            }
            previous = now
            rows = now
            delay(1500)
        }
    }

    Card(modifier = modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp)) {
            Text("Permissions", fontWeight = FontWeight.Bold, fontSize = 15.sp)
            Text(
                "Each one is requested only when you start the feature that needs " +
                    "it — never on launch.",
                fontSize = 12.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 4.dp, bottom = 8.dp),
            )

            rows.forEach { (item, status) ->
                PermissionRow(
                    item = item,
                    status = status,
                    onRequest = {
                        Permissions.markAsked(context, item.permission)
                        launcher.launch(arrayOf(item.permission))
                    },
                )
            } 

            lastError?.let { message ->
                Spacer(Modifier.height(6.dp))
                Text(
                    message,
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.error,
                )
            }

            Spacer(Modifier.height(8.dp))
            Text(
                "Not requested yet, because nothing uses them: " +
                    Permissions.futurePermissions.joinToString("; ") { (name, why) ->
                        "${name.substringAfterLast('.')} ($why)"
                    } + ".",
                fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun PermissionRow(
    item: Permissions.Item,
    status: Permissions.Status,
    onRequest: () -> Unit,
) {
    val context = LocalContext.current
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(item.title, fontSize = 13.sp, fontWeight = FontWeight.Medium)
            Text(
                when (status) {
                    Permissions.Status.GRANTED -> "Granted"
                    Permissions.Status.NOT_APPLICABLE -> "Not needed on this Android version"
                    Permissions.Status.DENIED -> "Not granted — ${item.why}"
                    Permissions.Status.PERMANENTLY_DENIED ->
                        "Blocked by Android. ${item.withoutIt}"
                },
                fontSize = 11.sp,
                color = when (status) {
                    Permissions.Status.GRANTED -> Color(0xFF2E7D32)
                    Permissions.Status.DENIED,
                    Permissions.Status.PERMANENTLY_DENIED,
                    -> MaterialTheme.colorScheme.error
                    Permissions.Status.NOT_APPLICABLE -> MaterialTheme.colorScheme.onSurfaceVariant
                },
            )
        }
        Spacer(Modifier.width(8.dp))
        when (status) {
            Permissions.Status.DENIED -> Button(onClick = onRequest) { Text("Allow") }
            Permissions.Status.PERMANENTLY_DENIED -> OutlinedButton(
                onClick = { Permissions.openAppSettings(context) }
            ) { Text("Settings") }
            else -> Unit
        }
    }
}
