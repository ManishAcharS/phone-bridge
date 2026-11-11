package dev.phonebridge

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import dev.phonebridge.net.FileFrames
import dev.phonebridge.net.FileTransferEngine

/**
 * The file card: where received files land, which files may be sent, and what is
 * running right now.
 *
 * Both directions go through the user, never around them:
 *
 *  * **receiving** needs a folder the user picked with the system picker
 *    (`ACTION_OPEN_DOCUMENT_TREE`); without one the PC is told `storage_denied`
 *    rather than the app inventing a path;
 *  * **sending** needs files the user picked (`ACTION_OPEN_DOCUMENT`); the PC asks
 *    with `file.request` and this app sends only what is in that selection.
 *
 * A transfer is only shown as complete when its SHA-256 verified.
 */
@Composable
fun FilesCard(
    state: FileTransferEngine.State,
    onChooseFolder: () -> Unit,
    onChooseFiles: () -> Unit,
    onForgetFolder: () -> Unit,
    onCancelAll: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Card(modifier = modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier.padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text("File transfer", style = MaterialTheme.typography.titleMedium)
            KeyValue(
                "Receiving folder",
                state.destination.ifBlank { "not chosen (receiving is unavailable)" },
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = onChooseFolder) {
                    Text(if (state.destination.isBlank()) "Choose folder" else "Change folder")
                }
                if (state.destination.isNotBlank()) {
                    OutlinedButton(onClick = onForgetFolder) { Text("Forget folder") }
                }
            }

            Text(
                if (state.selection.isEmpty()) {
                    "Nothing is selected to send. The PC cannot pull a file until you " +
                        "pick one."
                } else {
                    "Ready to send: " + state.selection.joinToString(", ")
                },
                style = MaterialTheme.typography.bodySmall,
            )
            OutlinedButton(onClick = onChooseFiles) { Text("Choose files to send") }

            if (state.transfers.isEmpty()) {
                Text(
                    "No transfers in this session. Chunks are hashed with SHA-256 and " +
                        "the file on the phone is only renamed after the hash matches.",
                    style = MaterialTheme.typography.bodySmall,
                )
            } else {
                for (transfer in state.transfers.takeLast(6)) {
                    // material3 1.2.1: the Float overload is the current one; the
                    // lambda form only exists from 1.3.0.
                    LinearProgressIndicator(
                        progress = transfer.fraction,
                        modifier = Modifier.fillMaxWidth(),
                    )
                    val direction = if (transfer.direction == FileTransferEngine.TO_PHONE) {
                        "from PC"
                    } else {
                        "to PC"
                    }
                    Text(
                        "${transfer.name} ($direction) — ${transfer.state} · " +
                            "${FileFrames.humanBytes(transfer.bytes)} of " +
                            "${FileFrames.humanBytes(transfer.size)}",
                        style = MaterialTheme.typography.bodySmall,
                        fontFamily = FontFamily.Monospace,
                    )
                    transfer.verified?.let { verified ->
                        Text(
                            if (verified) "SHA-256 verified" else "SHA-256 did NOT verify",
                            style = MaterialTheme.typography.bodySmall,
                            fontWeight = FontWeight.Bold,
                            color = if (verified) {
                                MaterialTheme.colorScheme.primary
                            } else {
                                MaterialTheme.colorScheme.error
                            },
                        )
                    }
                    if (transfer.error.isNotBlank()) {
                        Text(
                            transfer.error,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.error,
                        )
                    }
                }
            }
            if (state.transfers.any {
                    it.state == FileTransferEngine.ACTIVE ||
                        it.state == FileTransferEngine.PENDING
                }
            ) {
                OutlinedButton(onClick = onCancelAll) { Text("Cancel transfers") }
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
