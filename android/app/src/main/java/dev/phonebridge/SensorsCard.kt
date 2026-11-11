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
import dev.phonebridge.net.SensorEngine
import dev.phonebridge.net.SensorFrames

/**
 * The sensor card: what this phone has, and — while a PC is streaming — exactly
 * which sensors are registered and how many readings have left the device.
 *
 * Presenting a sensor as "on" when no listener is registered is the failure this
 * card exists to prevent: [state.active] comes from the engine, not from a wish.
 */
@Composable
fun SensorsCard(
    state: SensorEngine.State,
    catalogue: List<SensorEngine.Entry>,
    connected: Boolean,
    onStop: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Card(modifier = modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier.padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text("Sensors", style = MaterialTheme.typography.titleMedium)
            if (catalogue.isEmpty()) {
                Text(
                    "This phone reports no sensors.",
                    style = MaterialTheme.typography.bodySmall,
                )
            } else {
                val streamable = catalogue.count { it.streamable }
                Text(
                    "$streamable of ${catalogue.size} reported sensor(s) can be streamed",
                    style = MaterialTheme.typography.bodySmall,
                )
                for (entry in catalogue.take(8)) {
                    val rate = if (entry.minDelayUs > 0) {
                        "${1_000_000 / entry.minDelayUs} Hz max"
                    } else {
                        "rate not advertised"
                    }
                    Text(
                        "• ${entry.name.ifBlank { SensorFrames.nameOf(entry.kind) }} " +
                            "(${rate})" + if (!entry.streamable) " — ${entry.reason}" else "",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
                if (catalogue.size > 8) {
                    Text(
                        "… and ${catalogue.size - 8} more (the PC lists them all)",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                KeyValue("Streaming", if (state.active.isEmpty()) "no" else "yes")
                KeyValue("Interval", "${state.intervalUs / 1000} ms")
                KeyValue("Frames sent", state.framesSent.toString())
                KeyValue("Dropped", state.dropped.toString())
            }
            if (state.active.isNotEmpty()) {
                Text(
                    "Active sensor ids: " + state.active.joinToString(", "),
                    style = MaterialTheme.typography.bodySmall,
                    fontWeight = FontWeight.Bold,
                    fontFamily = FontFamily.Monospace,
                )
                Text(
                    "Readings are measured by this phone's sensors and sent only to " +
                        "the paired PC.",
                    style = MaterialTheme.typography.bodySmall,
                )
                OutlinedButton(onClick = onStop) { Text("Stop sensor streaming") }
            } else {
                Text(
                    if (connected) {
                        "No sensor is being read. A paired PC can start a stream with " +
                            "sensors of its choosing."
                    } else {
                        "No sensor is being read. Connect a PC to stream readings."
                    },
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            if (state.lastError.isNotBlank()) {
                Text(
                    "Last error: ${state.lastError}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }
        }
    }
}
