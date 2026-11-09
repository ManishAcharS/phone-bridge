package dev.phonebridge

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * Small shared pieces of the dashboard.
 *
 * They live here rather than inside `MainActivity` so the cards in
 * `CapabilitiesCard.kt` / `PermissionsCard.kt` / `ProbeCard.kt` use exactly the
 * same layout instead of three near-copies.
 */

/** What the bridge is actually doing, derived from state the service publishes. */
enum class BridgeStatus { STOPPED, LISTENING, CONNECTED, ERROR }

/**
 * Single source of truth for the status line.
 *
 * An error outranks "running": a bind failure leaves `running == false`, but a
 * session-level error while listening is still something the user must see.
 */
internal fun statusOf(running: Boolean, peerCount: Int, lastError: String?): BridgeStatus = when {
    lastError != null -> BridgeStatus.ERROR
    running && peerCount > 0 -> BridgeStatus.CONNECTED
    running -> BridgeStatus.LISTENING
    else -> BridgeStatus.STOPPED
}

internal fun statusLabel(status: BridgeStatus, peerCount: Int): String = when (status) {
    BridgeStatus.STOPPED -> "Stopped"
    BridgeStatus.LISTENING -> "Listening — no PC connected"
    BridgeStatus.CONNECTED -> if (peerCount == 1) "Connected to 1 PC" else "Connected to $peerCount PCs"
    BridgeStatus.ERROR -> "Error"
}

@Composable
internal fun StatusPill(status: BridgeStatus, peerCount: Int) {
    val color = when (status) {
        BridgeStatus.CONNECTED -> Color(0xFF2E7D32)
        BridgeStatus.LISTENING -> Color(0xFF1565C0)
        BridgeStatus.ERROR -> MaterialTheme.colorScheme.error
        BridgeStatus.STOPPED -> MaterialTheme.colorScheme.onSurfaceVariant
    }
    Surface(color = color.copy(alpha = 0.12f), shape = RoundedCornerShape(50)) {
        Row(Modifier.padding(horizontal = 10.dp, vertical = 5.dp)) {
            Text(
                statusLabel(status, peerCount),
                color = color,
                fontSize = 12.sp,
                fontWeight = FontWeight.Medium,
            )
        }
    }
}

@Composable
internal fun Section(title: String, content: @Composable () -> Unit) {
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
internal fun KeyValue(key: String, value: String, small: Boolean = false, valueColor: Color? = null) {
    Row(modifier = Modifier.fillMaxWidth().padding(vertical = 1.dp)) {
        Text(
            key,
            fontSize = if (small) 11.sp else 13.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.weight(0.42f),
        )
        Text(
            value,
            fontSize = if (small) 11.sp else 13.sp,
            color = valueColor ?: MaterialTheme.colorScheme.onSurface,
            modifier = Modifier.weight(0.58f),
        )
    }
}

/**
 * Turn the service's raw failure into something a user can act on.
 *
 * The raw string is still shown underneath: guessing must never replace the
 * actual error.
 */
internal fun explainBridgeError(raw: String?, port: Int): String? {
    if (raw == null) return null
    val lower = raw.lowercase()
    return when {
        "eaddrinuse" in lower || "address already in use" in lower ->
            "Port $port is already used by another app. Pick a different port in " +
                "Settings (1024–65535), then restart the bridge."

        "timed out" in lower && "bind" in lower ->
            "Android did not let the app bind port $port. A VPN or another " +
                "instance of this app can do that; try another port."

        "permission" in lower || "securityexception" in lower ->
            "A required permission or foreground-service declaration is missing, so " +
                "Android refused to start the bridge."

        "foregroundservice" in lower ->
            "Android refused to start the foreground service. Reopen the app from " +
                "the launcher and start the bridge again."

        "starting" in lower || "cannot start" in lower ->
            "The bridge could not start. Check the log below for the exact failure."

        else -> null
    }
}
