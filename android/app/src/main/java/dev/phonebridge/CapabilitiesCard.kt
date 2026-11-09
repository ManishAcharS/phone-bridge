package dev.phonebridge

import android.Manifest
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.phonebridge.net.DeviceFacts
import kotlinx.coroutines.delay

/**
 * What this phone can do — separated into "the hardware exists" and "the bridge
 * can use it today".
 *
 * The old dashboard had no such card: the PC received a capability map with
 * `camera.back`, `camera.front`, `sensors` and `file_transfer` hard-coded to
 * `true`, and nothing on the phone contradicted it. Every row here comes from
 * [DeviceFacts], the same object `system.info` reports, so the two cannot drift.
 */
@Composable
fun CapabilitiesCard(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    // Hardware does not change while the app runs, so it is measured once: the
    // camera/sensor enumeration is not something to repeat every two seconds.
    val rows = remember { DeviceFacts.capabilityRows(context) }
    var micGranted by remember {
        mutableStateOf(Permissions.granted(context, Manifest.permission.RECORD_AUDIO))
    }

    // Permission state does change, and it is the difference between "present"
    // and "available".
    LaunchedEffect(Unit) {
        while (true) {
            micGranted = Permissions.granted(context, Manifest.permission.RECORD_AUDIO)
            delay(1500)
        }
    }

    Card(modifier = modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp)) {
            Text("Capabilities", fontWeight = FontWeight.Bold, fontSize = 15.sp)
            Text(
                "Measured on this device. \"Present\" means the hardware is there; " +
                    "\"available\" means PhoneBridge can use it today.",
                fontSize = 12.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 4.dp, bottom = 8.dp),
            )
            rows.forEach { row ->
                KeyValue(
                    row.label.replaceFirstChar { it.uppercase() },
                    row.summary(micGranted),
                    valueColor = if (row.available(micGranted)) Color(0xFF2E7D32)
                    else MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Spacer(Modifier.height(6.dp))
            Text(
                "Nothing here is claimed as working before it is. The PC sees the " +
                    "same values via system.info, plus a hardware.* key per row.",
                fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
