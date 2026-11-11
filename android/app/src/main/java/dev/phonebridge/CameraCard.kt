package dev.phonebridge

import android.graphics.BitmapFactory
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import dev.phonebridge.net.CameraEngine

/**
 * The camera card: what the phone has, what the user allowed, and — while the PC
 * is streaming — an unmistakable red "camera active" banner with a self-view of
 * the frames actually leaving the device.
 *
 * Four facts are kept apart on purpose, because conflating them is how a
 * "camera ready" badge ends up on a phone that is sending nothing:
 *
 *   hardware  →  the lens exists (DeviceFacts, read-only)
 *   permitted →  the user granted CAMERA
 *   streaming →  a paired PC asked and frames are flowing right now
 *
 * Only the last one is allowed to look active, and it is loud.
 */
@Composable
fun CameraCard(
    state: CameraEngine.State,
    permitted: Boolean,
    hardware: Boolean,
    onGrant: () -> Unit,
    onStop: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Card(modifier = modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier.padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Camera", style = MaterialTheme.typography.titleMedium)
                if (state.active) {
                    Text(
                        "  ● STREAMING TO PC",
                        color = Color.White,
                        fontWeight = FontWeight.Bold,
                        style = MaterialTheme.typography.labelLarge,
                        modifier = Modifier
                            .padding(start = 8.dp)
                            .clip(RoundedCornerShape(6.dp))
                            .background(Color(0xFFB3261E))
                            .padding(horizontal = 8.dp, vertical = 3.dp),
                    )
                }
            }

            when {
                !hardware -> Text(
                    "This phone reports no camera. Nothing will stream, and the PC " +
                        "will be told `no_camera` rather than shown a black picture.",
                    style = MaterialTheme.typography.bodySmall,
                )

                !permitted -> {
                    Text(
                        "Permission not granted. The camera is never opened until you " +
                            "allow it — the PC's start button can only report the refusal.",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Button(onClick = onGrant) { Text("Allow camera") }
                }

                state.active -> {
                    Text(
                        "${lensName(state.facing)} · ${state.width}x${state.height} · " +
                            "${state.fps} fps · ${state.framesSent} frames" +
                            if (state.dropped > 0) " · ${state.dropped} dropped" else "",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    SelfView(state)
                    Text(
                        "The PC started this stream. Stop ends capture immediately and " +
                            "frees the lens for other apps.",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    OutlinedButton(onClick = onStop) { Text("Stop streaming") }
                }

                else -> Text(
                    "Ready: a paired PC can start ${lensName("back")} or " +
                        "${lensName("front")} and pick resolution and frame rate. " +
                        "Nothing is captured until it asks.",
                    style = MaterialTheme.typography.bodySmall,
                )
            }

            if (state.lastError.isNotEmpty()) {
                Text(
                    state.lastError,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }
        }
    }
}

/** Decode only when the bytes change, and never at full size in the card. */
@Composable
private fun SelfView(state: CameraEngine.State) {
    val bytes = state.lastJpeg
    // Decode off the composition's critical path and only when the bytes change:
    // this is the phone showing its own frames, not a decoder under test.
    val image by produceState<ImageBitmap?>(initialValue = null, bytes) {
        value = bytes?.let { BitmapFactory.decodeByteArray(it, 0, it.size)?.asImageBitmap() }
    }
    if (image != null) {
        Image(
            bitmap = image!!,
            contentDescription = "Preview of the frames being sent to the PC",
            contentScale = ContentScale.Fit,
            modifier = Modifier
                .fillMaxWidth()
                .height(150.dp)
                .clip(RoundedCornerShape(8.dp))
                .background(Color.Black),
        )
    } else {
        Text(
            "Waiting for the first frame…",
            style = MaterialTheme.typography.bodySmall,
        )
    }
}

private fun lensName(facing: String): String = if (facing == "front") "front camera" else "rear camera"

/** A one-line summary for the diagnostics list. */
@Composable
fun CameraSummaryLine(state: CameraEngine.State, permitted: Boolean, hardware: Boolean) {
    val text = when {
        !hardware -> "camera: no hardware"
        !permitted -> "camera: permission not granted"
        state.active -> "camera: streaming ${state.facing} ${state.width}x${state.height}"
        else -> "camera: idle (ready)"
    }
    Text(text, style = MaterialTheme.typography.bodySmall)
}
