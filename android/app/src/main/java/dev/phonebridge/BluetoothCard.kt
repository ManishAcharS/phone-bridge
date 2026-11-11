package dev.phonebridge

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp

/**
 * The Bluetooth card: what the radio is, what the user allowed, and how to stop it.
 *
 * The three facts are kept apart on purpose — a radio that exists is not a radio
 * that is switched on, and a scan is not a connection:
 *
 *   supported / enabled / permitted  ->  can the PC ask anything at all?
 *   scanning devices                 ->  is the radio airtime the user agreed to?
 *   connected links                  ->  is this phone talking to a peripheral?
 *
 * The card never starts a scan by itself and never connects to anything: both are
 * requested by the PC over an authenticated session, and the stop button here is
 * the phone owner's veto.
 */
@Composable
fun BluetoothCard(
    supported: Boolean,
    enabled: Boolean,
    permitted: Boolean?,
    detail: String,
    scanning: Boolean,
    links: List<Map<String, Any?>>,
    onGrantScan: () -> Unit,
    onStop: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Card(modifier = modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier.padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text("Bluetooth (BLE)", style = MaterialTheme.typography.titleMedium)
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                KeyValue("Radio", if (supported) "present" else "absent")
                KeyValue("Power", if (enabled) "on" else "off")
                KeyValue(
                    "Scan permission",
                    when (permitted) {
                        true -> "granted"
                        false -> "not granted"
                        null -> "unknown"
                    },
                )
            }
            if (detail.isNotBlank()) {
                Text(detail, style = MaterialTheme.typography.bodySmall)
            }
            Text(
                if (scanning) "scanning right now (asked for by the PC)" else "not scanning",
                style = MaterialTheme.typography.bodySmall,
                fontWeight = if (scanning) FontWeight.Bold else FontWeight.Normal,
            )
            if (links.isEmpty()) {
                Text(
                    "No peripheral is connected. The PC must name a device before a " +
                        "link is opened; this app never connects on its own.",
                    style = MaterialTheme.typography.bodySmall,
                )
            } else {
                for (link in links) {
                    val address = link["address"] as? String ?: "?"
                    val state = link["state"] as? String ?: "?"
                    val subscriptions = (link["subscriptions"] as? List<*>)?.size ?: 0
                    Text(
                        "$address — $state, $subscriptions subscription(s)",
                        style = MaterialTheme.typography.bodySmall,
                        fontFamily = FontFamily.Monospace,
                    )
                }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (permitted == false) {
                    OutlinedButton(onClick = onGrantScan) { Text("Grant scan permission") }
                }
                if (scanning || links.isNotEmpty()) {
                    OutlinedButton(onClick = onStop) { Text("Stop Bluetooth work") }
                }
            }
        }
    }
}
