package dev.phonebridge.net

import android.content.Context
import android.net.Uri
import java.io.InputStream
import java.io.OutputStream
import java.security.MessageDigest
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Phase 5 file transfer: chunked, SHA-256 verified, resumable, and only ever
 * inside a folder the user picked.
 *
 * What this class refuses to do is as important as what it does:
 *
 *  * **it never browses storage.** The receive destination is a SAF tree the
 *    user chose; the send source is a file the user picked with
 *    `ACTION_OPEN_DOCUMENT`. There is no path walking and no
 *    `READ_EXTERNAL_STORAGE`.
 *  * **it never reports success early.** A file is `complete` only after the
 *    SHA-256 of what was written matches the digest in the offer. A mismatch
 *    deletes the part file and answers `hash_mismatch`.
 *  * **it never writes outside the tree.** Incoming names go through
 *    [FileFrames.safeName]; a traversal, a separator, a reserved device name or a
 *    control character is refused before a document exists.
 *  * **it never buffers a whole file.** Both directions stream through 64 KiB
 *    chunks; memory use is a chunk plus the window, not the file size.
 *
 * Two flows share one engine because they share one wire format, one id space and
 * one set of failure sentences:
 *
 * ```
 * PC  --file.offer-->  phone creates <name>.part in the chosen folder
 * PC  --MSG_FILE(5)->  chunks; phone acks on stream 6 every 4th chunk
 * PC  --file.complete-> phone verifies SHA-256 and renames .part -> name
 *
 * PC  --file.request->  phone hashes the file the user picked and starts sending
 * PC  <-MSG_FILE(6)--   chunks; PC acks on stream 5
 * PC  --file.complete-> phone records that the PC verified it
 * ```
 */
class FileTransferEngine(
    private val context: Context,
    private val send: (streamId: Int, body: ByteArray) -> Unit,
    private val log: (level: String, message: String) -> Unit,
) {

    companion object {
        const val PENDING = "pending"
        const val ACTIVE = "active"
        const val PAUSED = "paused"
        const val COMPLETE = "complete"
        const val FAILED = "failed"
        const val CANCELLED = "cancelled"

        const val TO_PHONE = "to_phone"
        const val FROM_PHONE = "from_phone"

        /** Chunks buffered from the reader thread before the writer catches up. */
        const val INBOUND_QUEUE = 8

        /** How long the reader waits for the writer before failing the transfer. */
        const val INBOUND_WAIT_MS = 5_000L

        /** Files the user picked for sending, announced to the PC on request. */
        const val MAX_SELECTION = 16

        const val RESUME_PREFS = "phonebridge_resume"
        const val KEY_NAME = "name"
        const val KEY_SIZE = "size"
        const val KEY_DIGEST = "digest"
        const val KEY_PART = "part_uri"
        const val KEY_BYTES = "bytes"
        const val APPEND_HINT = " (the folder permission may have been revoked)"
    }

    /** One transfer, as the phone screen and the PC both see it. */
    data class Transfer(
        val id: Int,
        val direction: String,
        val name: String,
        val size: Long,
        val chunkSize: Int,
        val sha256: String,
        val state: String = PENDING,
        val bytes: Long = 0,
        val chunks: Int = 0,
        val verified: Boolean? = null,
        val error: String = "",
        val resumedFrom: Long = 0,
        val startedAtMs: Long = System.currentTimeMillis(),
        val finishedAtMs: Long = 0,
        val partUri: String = "",
        val targetUri: String = "",
        val sourceUri: String = "",
    ) {
        val fraction: Float
            get() = when {
                state == COMPLETE -> 1f
                size <= 0 -> 0f
                else -> (bytes.toDouble() / size).toFloat().coerceIn(0f, 1f)
            }

        fun speedBps(nowMs: Long): Long {
            val elapsed = ((if (finishedAtMs > 0) finishedAtMs else nowMs) - startedAtMs)
            return if (elapsed <= 0) 0 else (bytes * 1000 / elapsed)
        }

        fun asMap(): Map<String, Any?> = linkedMapOf(
            "transfer_id" to id,
            "direction" to direction,
            "name" to name,
            "size" to size,
            "chunk_size" to chunkSize,
            "sha256" to sha256,
            "state" to state,
            "bytes" to bytes,
            "chunks" to chunks,
            "verified" to verified,
            "error" to error,
            "resumed_from" to resumedFrom,
        )
    }

    data class State(
        val transfers: List<Transfer> = emptyList(),
        val selection: List<String> = emptyList(),
        val destination: String = "",
        val busy: Boolean = false,
        val lastError: String = "",
    )

    private val _state = MutableStateFlow(State())
    val state: StateFlow<State> = _state.asStateFlow()

    private val lock = Any()
    private val nextId = AtomicInteger(1)
    private val transfers = LinkedHashMap<Int, Transfer>()
    private val running = AtomicBoolean(false)

    /** Transfer ids the user paused, so the sender waits instead of failing. */
    private val paused = HashSet<Int>()
    private val cancelled = HashSet<Int>()

    /** Acked chunk index per sending transfer, guarded by [ackLock]. */
    private val ackLock = Object()
    private val ackedUpTo = HashMap<Int, Int>()

    /** Picked source files, in pick order; the first is what `file.request` sends. */
    private val selection = ArrayList<Uri>()

    /** Inbound writes happen on one thread so chunk order can never be reordered. */
    @Volatile
    private var writer: Thread? = null
    @Volatile
    private var inboundQueue: LinkedBlockingQueue<FileFrames.Chunk>? = null

    // ------------------------------------------------------------------ //
    // observation
    // ------------------------------------------------------------------ //
    fun transfers(): List<Transfer> = synchronized(lock) { transfers.values.toList() }

    fun transfer(id: Int): Transfer? = synchronized(lock) { transfers[id] }

    private fun publish() {
        _state.value = State(
            transfers = transfers(),
            selection = synchronized(lock) { selection.mapNotNull { SafStore.displayName(context, it) } },
            destination = SafStore.destinationLabel(context),
            busy = synchronized(lock) {
                transfers.values.any { it.state == ACTIVE || it.state == PENDING }
            },
            lastError = _state.value.lastError,
        )
    }

    private fun update(id: Int, block: (Transfer) -> Transfer) {
        synchronized(lock) {
            val current = transfers[id] ?: return
            transfers[id] = block(current)
        }
        publish()
    }

    // ------------------------------------------------------------------ //
    // the user's side: what may be sent, and where it lands
    // ------------------------------------------------------------------ //
    fun setDestination(label: String) {
        log("INFO", "receiving folder: $label")
        publish()
    }

    fun setSelection(uris: List<Uri>) {
        synchronized(lock) {
            selection.clear()
            selection.addAll(uris.take(MAX_SELECTION))
        }
        for (uri in uris.take(MAX_SELECTION)) {
            log("INFO", "file selected to send: ${SafStore.displayName(context, uri) ?: uri}")
        }
        publish()
    }

    fun appendedSelection(uris: List<Uri>) {
        synchronized(lock) {
            for (uri in uris) {
                if (selection.size >= MAX_SELECTION) break
                if (selection.none { it == uri }) selection.add(uri)
            }
        }
        publish()
    }

    fun clearSelection() {
        synchronized(lock) { selection.clear() }
        publish()
    }

    fun selectionNames(): List<String> = synchronized(lock) {
        selection.mapNotNull { SafStore.displayName(context, it) }
    }

    private fun nextSelected(): Uri? = synchronized(lock) {
        val first = selection.firstOrNull() ?: return null
        selection.removeAt(0)
        first
    }

    // ------------------------------------------------------------------ //
    // receiving (PC -> phone)
    // ------------------------------------------------------------------ //
    /**
     * Accept an offer and create the `.part` document.
     *
     * @throws FileFailure `bad_params` for an impossible offer,
     *   `storage_denied` when no folder has been chosen, `busy` when another
     *   transfer is already running.
     */
    fun offer(params: Map<*, *>): Map<String, Any?> {
        val name = FileFrames.safeName(params["name"] as? String)
            ?: throw FileFailure("bad_params", "that file name is not safe to write")
        val size = (params["size"] as? Number)?.toLong() ?: -1L
        if (size < 0 || size > FileFrames.MAX_FILE_BYTES) {
            throw FileFailure("bad_params", "file size $size is outside the 4 GiB limit")
        }
        val digest = (params["sha256"] as? String)?.lowercase() ?: ""
        if (!FileFrames.isHexDigest(digest)) {
            throw FileFailure("bad_params", "sha256 must be 64 hex characters")
        }
        val chunkSize = FileFrames.chunkSize(params["chunk_size"])
        if (!SafStore.canReceive(context)) {
            throw FileFailure(
                "storage_denied",
                "no folder has been chosen on the phone (Files card -> Choose folder)",
            )
        }
        synchronized(lock) {
            val busy = transfers.values.firstOrNull {
                it.direction == TO_PHONE && (it.state == ACTIVE || it.state == PENDING)
            }
            if (busy != null) {
                throw FileFailure("busy", "transfer ${busy.id} is still running")
            }
        }

        val id = nextId.getAndIncrement()
        val resumeFrom = resumeOffsetFor(name, size, digest)
        val partName = FileFrames.partName(name)
        val partUri = SafStore.createDocument(context, partName, SafStore.mimeFor(name))
        val transfer = Transfer(
            id = id,
            direction = TO_PHONE,
            name = name,
            size = size,
            chunkSize = chunkSize,
            sha256 = digest,
            state = PENDING,
            resumedFrom = resumeFrom,
            partUri = partUri.toString(),
        )
        synchronized(lock) { transfers[id] = transfer }
        ensureWriter()
        publish()
        log("INFO", "offered $name ($size B) as transfer $id")
        return linkedMapOf(
            "transfer_id" to id,
            "chunk_size" to chunkSize,
            "window" to FileFrames.WINDOW_CHUNKS,
            "resume_from" to resumeFrom,
        )
    }

    /**
     * Resume metadata, kept in preferences rather than in a sidecar document.
     *
     * Only a transfer that died mid-flight with the *same* name, size and digest
     * may resume: resuming anything else would append new bytes to an old file.
     * The part document's actual size is checked too, so a part the user deleted
     * (or a folder that was revoked) restarts from zero instead of lying.
     */
    private fun resumeOffsetFor(name: String, size: Long, digest: String): Long {
        val prefs = context.getSharedPreferences(RESUME_PREFS, Context.MODE_PRIVATE)
        if (prefs.getString(KEY_NAME, null) != name) return 0
        if (prefs.getLong(KEY_SIZE, -1) != size) return 0
        if (prefs.getString(KEY_DIGEST, null) != digest) return 0
        val storedUri = prefs.getString(KEY_PART, null) ?: return 0
        val bytes = prefs.getLong(KEY_BYTES, 0)
        if (bytes <= 0) return 0
        return try {
            val uri = Uri.parse(storedUri)
            val actual = SafStore.sizeOf(context, uri)
            // Chunk-aligned: a partial chunk would be written twice otherwise.
            val aligned = (bytes / FileFrames.CHUNK_SIZE) * FileFrames.CHUNK_SIZE
            if (actual >= 0 && actual >= aligned && aligned > 0) aligned else 0
        } catch (e: Exception) {
            log("WARN", "resume metadata could not be used: ${e.message}")
            0
        }
    }

    private fun rememberResume(transfer: Transfer) {
        context.getSharedPreferences(RESUME_PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_NAME, transfer.name)
            .putLong(KEY_SIZE, transfer.size)
            .putString(KEY_DIGEST, transfer.sha256)
            .putString(KEY_PART, transfer.partUri)
            .putLong(KEY_BYTES, transfer.bytes)
            .apply()
    }

    private fun clearResume() {
        context.getSharedPreferences(RESUME_PREFS, Context.MODE_PRIVATE).edit().clear().apply()
    }

    private fun ensureWriter() {
        if (writer?.isAlive == true) return
        inboundQueue = LinkedBlockingQueue(INBOUND_QUEUE)
        writer = Thread({ writeLoop() }, "phonebridge-files-writer").apply {
            isDaemon = true
            start()
        }
    }

    /**
     * Handle one `MSG_FILE` body from the PC.
     *
     * Called from the session's reader thread, so it must not block for long: data
     * chunks go onto a bounded queue and the writer does the I/O. Acks and control
     * frames are handled inline because they are tiny and order-sensitive.
     */
    fun onChunk(body: ByteArray) {
        val chunk = try {
            FileFrames.decode(body)
        } catch (e: FileFrames.BadChunk) {
            log("WARN", "dropping a bad file chunk: ${e.message}")
            return
        }
        if (chunk.isAck) {
            synchronized(ackLock) {
                val current = ackedUpTo[chunk.transferId] ?: -1
                if (chunk.chunkIndex > current) {
                    ackedUpTo[chunk.transferId] = chunk.chunkIndex
                    ackLock.notifyAll()
                }
            }
            return
        }
        val transfer = transfer(chunk.transferId) ?: run {
            // Unknown transfer: tell the sender to stop instead of discarding a
            // file that can never complete.
            if (!chunk.isCancel) {
                sendCatching(
                    FileFrames.Chunk(chunk.transferId, 0, ByteArray(0), FileFrames.FLAG_CANCEL)
                )
            }
            null
        } ?: return

        if (chunk.isCancel) {
            cancelInbound(chunk.transferId, "cancelled by the PC")
            return
        }
        if (chunk.isPause) {
            synchronized(lock) { paused.add(chunk.transferId) }
            update(chunk.transferId) { it.copy(state = PAUSED) }
            return
        }
        if (chunk.isResume) {
            synchronized(lock) {
                paused.remove(chunk.transferId)
                cancelled.remove(chunk.transferId)
            }
            update(chunk.transferId) { it.copy(state = ACTIVE) }
            return
        }
        if (transfer.direction != TO_PHONE) {
            log("WARN", "ignoring file data for an outbound transfer ${chunk.transferId}")
            return
        }
        if (transfer.chunkSize > 0 && chunk.payload.size > transfer.chunkSize) {
            cancelInbound(chunk.transferId, "a chunk was larger than the agreed size")
            return
        }
        val queue = inboundQueue
        if (queue == null) {
            cancelInbound(chunk.transferId, "no writer was running for this transfer")
            return
        }
        // Backpressure: wait for the writer, but never for ever. Dropping a chunk
        // would corrupt the file, so the honest options are "wait" and "fail".
        if (!queue.offer(chunk)) {
            val queued = try {
                queue.offer(chunk, INBOUND_WAIT_MS, java.util.concurrent.TimeUnit.MILLISECONDS)
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
                false
            }
            if (!queued) {
                cancelInbound(chunk.transferId, "the phone could not keep up with the transfer")
            }
        }
    }

    private fun writeLoop() {
        val queue = inboundQueue ?: return
        while (true) {
            val chunk = try {
                queue.take()
            } catch (e: InterruptedException) {
                return
            }
            if (chunk.payload.isEmpty() && !chunk.isEnd) continue
            val transfer = transfer(chunk.transferId) ?: continue
            if (transfer.direction != TO_PHONE) continue
            if (transfer.state == CANCELLED || transfer.state == FAILED) continue
            if (synchronized(lock) { paused.contains(chunk.transferId) }) {
                // A paused transfer must not write, but the chunk is not lost: it is
                // re-queued so resume continues exactly where it stopped.
                try {
                    Thread.sleep(100)
                } catch (e: InterruptedException) {
                    return
                }
                queue.offer(chunk)
                continue
            }
            if (!writeChunk(transfer, chunk)) return
        }
    }

    private fun writeChunk(transfer: Transfer, chunk: FileFrames.Chunk): Boolean {
        val id = transfer.id
        // Resume: the sender may continue at the offset we reported, so anything
        // before that offset has already been written.
        val startIndex = (transfer.resumedFrom / transfer.chunkSize).toInt()
        if (chunk.chunkIndex < startIndex) {
            sendAck(id, chunk.chunkIndex)
            return true
        }
        val expectedIndex = transfer.chunks + startIndex
        if (chunk.chunkIndex > expectedIndex) {
            cancelInbound(id, "chunk ${chunk.chunkIndex} arrived before $expectedIndex")
            return true
        }
        val uri = Uri.parse(transfer.partUri)
        try {
            val append = chunk.chunkIndex > startIndex
            val stream = openPart(uri, if (append) "wa" else "wt")
                ?: return failWrite(id, "the phone could not open ${transfer.name}$APPEND_HINT")
            stream.use { out ->
                out.write(chunk.payload)
                out.flush()
            }
        } catch (e: Exception) {
            return failWrite(id, "writing ${transfer.name} failed: ${e.message}")
        }
        val digest = digests[id] ?: MessageDigest.getInstance("SHA-256").also { digests[id] = it }
        if (chunk.chunkIndex == startIndex && transfer.resumedFrom > 0) {
            // Continuing an earlier part: those bytes are already on disk, so the
            // digest has to be rebuilt from them *by streaming*, not by loading the
            // file into memory.
            digest.reset()
            try {
                feedDigest(digest, uri, transfer.resumedFrom)
            } catch (e: Exception) {
                return failWrite(id, "could not re-read the part file: ${e.message}")
            }
        }
        digest.update(chunk.payload)
        val written = transfer.bytes + chunk.payload.size
        val updated = transfer.copy(
            state = ACTIVE,
            bytes = written.coerceAtMost(transfer.size),
            chunks = transfer.chunks + 1,
        )
        synchronized(lock) { transfers[id] = updated }
        if (chunk.chunkIndex % FileFrames.ACK_EVERY_CHUNKS == 0) rememberResume(updated)
        if (chunk.isEnd || written >= transfer.size) {
            finishInbound(updated)
            publish()
            return true
        }
        if (chunk.chunkIndex % FileFrames.ACK_EVERY_CHUNKS == 0) {
            sendAck(id, chunk.chunkIndex)
        }
        publish()
        return true
    }

    private fun failWrite(id: Int, message: String): Boolean {
        update(id) {
            it.copy(state = FAILED, error = message, finishedAtMs = System.currentTimeMillis())
        }
        log("WARN", "file transfer $id stopped: $message")
        sendCatching(FileFrames.Chunk(id, 0, ByteArray(0), FileFrames.FLAG_CANCEL))
        return false
    }

    /** Verify and publish. Success exists only after the hash matches. */
    private fun finishInbound(transfer: Transfer) {
        val digest = digests.remove(transfer.id)?.digest()
        val uri = Uri.parse(transfer.partUri)
        if (transfer.bytes != transfer.size) {
            return failInbound(
                transfer,
                "the phone received ${transfer.bytes} of ${transfer.size} bytes",
                "hash_mismatch",
            )
        }
        val actual = digest?.let { FileFrames.hex(it) } ?: ""
        if (actual.isEmpty() || actual != transfer.sha256) {
            return failInbound(transfer, "the SHA-256 did not match the offer", "hash_mismatch")
        }
        // Atomic completion: rename only after the bytes are known good.
        val renamed = SafStore.renameDocument(context, uri, transfer.name)
        val target = renamed ?: uri
        clearResume()
        val done = transfer.copy(
            state = COMPLETE,
            verified = true,
            finishedAtMs = System.currentTimeMillis(),
            partUri = target.toString(),
            targetUri = target.toString(),
        )
        synchronized(lock) { transfers[transfer.id] = done }
        log("INFO", "received ${transfer.name} (${transfer.size} B), sha256 verified")
        sendAck(transfer.id, transfer.chunks - 1)
        publish()
    }

    private fun failInbound(transfer: Transfer, message: String, code: String) {
        SafStore.deleteDocument(context, Uri.parse(transfer.partUri))
        clearResume()
        update(transfer.id) {
            it.copy(
                state = FAILED,
                error = message,
                verified = false,
                finishedAtMs = System.currentTimeMillis(),
            )
        }
        log("WARN", "file transfer ${transfer.id} failed verification: $message")
        sendCatching(
            FileFrames.Chunk(transfer.id, 0, ByteArray(0), FileFrames.FLAG_CANCEL)
        )
        _state.value = _state.value.copy(lastError = message)
        log("DEBUG", "verification failure reported as $code")
    }

    private fun cancelInbound(id: Int, reason: String) {
        val transfer = transfer(id) ?: return
        synchronized(lock) {
            cancelled.add(id)
            paused.remove(id)
        }
        if (transfer.partUri.isNotEmpty()) {
            SafStore.deleteDocument(context, Uri.parse(transfer.partUri))
        }
        clearResume()
        update(id) {
            it.copy(state = CANCELLED, error = reason, finishedAtMs = System.currentTimeMillis())
        }
        log("INFO", "file transfer $id cancelled: $reason")
    }

    private fun openPart(uri: Uri, mode: String): OutputStream? =
        context.contentResolver.openOutputStream(uri, mode)

    /** Stream the first [limit] bytes of a document into [digest]; never buffers it. */
    private fun feedDigest(digest: MessageDigest, uri: Uri, limit: Long) {
        val buffer = ByteArray(FileFrames.CHUNK_SIZE)
        var remaining = limit
        context.contentResolver.openInputStream(uri)?.use { input ->
            while (remaining > 0) {
                val want = minOf(buffer.size.toLong(), remaining).toInt()
                val read = input.read(buffer, 0, want)
                if (read <= 0) break
                digest.update(buffer, 0, read)
                remaining -= read
            }
        } ?: throw IllegalStateException("the part file could not be read")
    }

    // ------------------------------------------------------------------ //
    // sending (phone -> PC)
    // ------------------------------------------------------------------ //
    /**
     * Announce the file the user picked and start streaming it to the PC.
     *
     * The digest is computed before the reply: the PC needs the expected hash to
     * verify what it receives, and a hash invented while streaming would be
     * worthless. That costs one extra read of the file, which docs/23 states.
     */
    fun request(params: Map<*, *>): Map<String, Any?> {
        val uri = nextSelected()
            ?: throw FileFailure(
                "no_file",
                "nothing is selected on the phone (Files card -> Choose files to send)",
            )
        val name = FileFrames.safeName(SafStore.displayName(context, uri))
            ?: throw FileFailure("bad_params", "that file has no usable name")
        val size = SafStore.sizeOf(context, uri)
        if (size < 0) throw FileFailure("storage_denied", "the phone could not read the file size")
        if (size > FileFrames.MAX_FILE_BYTES) {
            throw FileFailure("bad_params", "that file is larger than the 4 GiB limit")
        }
        val digest = digestOf(uri)
            ?: throw FileFailure("storage_denied", "the phone could not read '$name'")
        synchronized(lock) {
            val busy = transfers.values.firstOrNull {
                it.direction == FROM_PHONE && (it.state == ACTIVE || it.state == PENDING)
            }
            if (busy != null) throw FileFailure("busy", "transfer ${busy.id} is still running")
        }

        val id = nextId.getAndIncrement()
        val transfer = Transfer(
            id = id,
            direction = FROM_PHONE,
            name = name,
            size = size,
            chunkSize = FileFrames.CHUNK_SIZE,
            sha256 = digest,
            state = PENDING,
            sourceUri = uri.toString(),
        )
        synchronized(lock) {
            transfers[id] = transfer
            ackedUpTo[id] = -1
            paused.remove(id)
            cancelled.remove(id)
        }
        publish()
        Thread({ sender(id) }, "phonebridge-files-sender").apply {
            isDaemon = true
            start()
        }
        return linkedMapOf(
            "transfer_id" to id,
            "name" to name,
            "size" to size,
            "sha256" to digest,
            "chunk_size" to FileFrames.CHUNK_SIZE,
        )
    }

    private fun sender(id: Int) {
        val transfer = transfer(id) ?: return
        val uri = Uri.parse(transfer.sourceUri)
        val chunkSize = transfer.chunkSize
        val total = ((transfer.size + chunkSize - 1) / chunkSize).toInt().coerceAtLeast(1)
        update(id) { it.copy(state = ACTIVE) }
        try {
            context.contentResolver.openInputStream(uri)?.use { input ->
                streamOut(id, input, total, chunkSize)
            } ?: run {
                update(id) {
                    it.copy(
                        state = FAILED,
                        error = "the phone could not open '${transfer.name}'",
                        finishedAtMs = System.currentTimeMillis(),
                    )
                }
                log("WARN", "could not open the selected file for transfer $id")
            }
        } catch (e: Exception) {
            update(id) {
                it.copy(
                    state = FAILED,
                    error = "sending failed: ${e.message}",
                    finishedAtMs = System.currentTimeMillis(),
                )
            }
            log("WARN", "sending transfer $id failed: ${e.message}")
        }
        publish()
    }

    private fun streamOut(id: Int, input: InputStream, total: Int, chunkSize: Int) {
        var index = 0
        var sent = 0L
        while (index < total) {
            if (synchronized(lock) { cancelled.contains(id) }) {
                update(id) {
                    it.copy(
                        state = CANCELLED,
                        error = "cancelled on the phone",
                        finishedAtMs = System.currentTimeMillis(),
                    )
                }
                return
            }
            if (synchronized(lock) { paused.contains(id) }) {
                update(id) { it.copy(state = PAUSED) }
                Thread.sleep(100)
                continue
            }
            // Window: never more than WINDOW_CHUNKS unacknowledged.
            if (index >= FileFrames.WINDOW_CHUNKS &&
                !waitForAck(id, index - FileFrames.WINDOW_CHUNKS)
            ) {
                return
            }
            val buffer = ByteArray(chunkSize)
            var read = 0
            while (read < chunkSize) {
                val count = input.read(buffer, read, chunkSize - read)
                if (count <= 0) break
                read += count
            }
            if (read == 0 && index > 0) break
            val payload = if (read == chunkSize) buffer else buffer.copyOf(read)
            var flags = 0
            if (index == 0) flags = flags or FileFrames.FLAG_START
            val last = index == total - 1 || read < chunkSize
            if (last) flags = flags or FileFrames.FLAG_END
            if (!sendCatching(FileFrames.Chunk(id, index, payload, flags))) {
                update(id) {
                    it.copy(
                        state = FAILED,
                        error = "the session closed mid-transfer",
                        finishedAtMs = System.currentTimeMillis(),
                    )
                }
                return
            }
            sent += payload.size
            val progress = sent
            update(id) {
                it.copy(state = ACTIVE, bytes = progress, chunks = index + 1)
            }
            index += 1
            if (last) break
        }
        // The receiver confirms verification with file.complete; until then the
        // transfer is complete-but-unverified, which is exactly what it is.
        update(id) {
            it.copy(
                state = if (it.state == PAUSED) PAUSED else COMPLETE,
                verified = null,
                finishedAtMs = System.currentTimeMillis(),
            )
        }
        log("INFO", "sent ${nameOf(id)} ($sent B); waiting for the PC's verification")
        publish()
    }

    private fun nameOf(id: Int): String = transfer(id)?.name ?: "transfer $id"

    /** Wait until [index] has been acknowledged, or fail with a sentence. */
    private fun waitForAck(id: Int, index: Int): Boolean {
        synchronized(ackLock) {
            val deadline = System.currentTimeMillis() + FileFrames.ACK_TIMEOUT_MS
            while ((ackedUpTo[id] ?: -1) < index) {
                val remaining = deadline - System.currentTimeMillis()
                if (remaining <= 0) {
                    update(id) {
                        it.copy(
                            state = FAILED,
                            error = "the PC stopped acknowledging after chunk ${ackedUpTo[id] ?: -1}",
                            finishedAtMs = System.currentTimeMillis(),
                        )
                    }
                    log("WARN", "transfer $id failed: no acknowledgement for chunk $index")
                    return false
                }
                try {
                    ackLock.wait(remaining.coerceAtMost(250))
                } catch (e: InterruptedException) {
                    Thread.currentThread().interrupt()
                    return false
                }
                if (synchronized(lock) { cancelled.contains(id) }) return false
            }
        }
        return true
    }

    // ------------------------------------------------------------------ //
    // RPC surface
    // ------------------------------------------------------------------ //
    fun progress(params: Map<*, *>): Map<String, Any?> {
        val id = (params["transfer_id"] as? Number)?.toInt()
            ?: throw FileFailure("bad_params", "transfer_id is required")
        val transfer = transfer(id)
            ?: throw FileFailure("unknown_transfer", "this phone has no transfer $id")
        return linkedMapOf(
            "transfer_id" to transfer.id,
            "state" to transfer.state,
            "bytes" to transfer.bytes,
            "chunks" to transfer.chunks,
            "sha256" to transfer.sha256,
            "error" to transfer.error,
        )
    }

    /**
     * The PC says it finished. For an inbound transfer this is the *verification*
     * answer; for an outbound one it carries the PC's verdict, which is recorded
     * rather than assumed.
     */
    fun complete(params: Map<*, *>): Map<String, Any?> {
        val id = (params["transfer_id"] as? Number)?.toInt()
            ?: throw FileFailure("bad_params", "transfer_id is required")
        val transfer = transfer(id)
            ?: throw FileFailure("unknown_transfer", "this phone has no transfer $id")
        val verified = transfer.state == COMPLETE && transfer.verified == true
        if (transfer.direction == TO_PHONE) {
            if (!verified) {
                throw FileFailure(
                    "hash_mismatch",
                    "the phone has not verified transfer $id (${transfer.state})",
                )
            }
            return linkedMapOf(
                "ok" to true,
                "verified" to true,
                "sha256" to transfer.sha256,
                "bytes" to transfer.size,
                "path_hint" to transfer.name,
            )
        }
        val pcVerified = params["verified"] as? Boolean
        update(id) { it.copy(verified = pcVerified ?: it.verified) }
        return linkedMapOf(
            "ok" to true,
            "verified" to (pcVerified ?: false),
            "sha256" to transfer.sha256,
            "bytes" to transfer.bytes,
            "path_hint" to "",
        )
    }

    fun cancel(params: Map<*, *>): Map<String, Any?> {
        val id = (params["transfer_id"] as? Number)?.toInt()
            ?: throw FileFailure("bad_params", "transfer_id is required")
        val transfer = transfer(id)
            ?: throw FileFailure("unknown_transfer", "this phone has no transfer $id")
        if (transfer.direction == TO_PHONE) {
            cancelInbound(id, "cancelled by the PC")
        } else {
            synchronized(lock) { cancelled.add(id) }
            update(id) {
                it.copy(state = CANCELLED, finishedAtMs = System.currentTimeMillis())
            }
            sendCatching(FileFrames.Chunk(id, 0, ByteArray(0), FileFrames.FLAG_CANCEL))
        }
        return linkedMapOf("ok" to true, "state" to CANCELLED)
    }

    fun pause(params: Map<*, *>): Map<String, Any?> {
        val id = (params["transfer_id"] as? Number)?.toInt()
            ?: throw FileFailure("bad_params", "transfer_id is required")
        if (transfer(id) == null) throw FileFailure("unknown_transfer", "no transfer $id")
        synchronized(lock) { paused.add(id) }
        update(id) { it.copy(state = PAUSED) }
        return linkedMapOf("ok" to true, "state" to PAUSED)
    }

    fun resume(params: Map<*, *>): Map<String, Any?> {
        val id = (params["transfer_id"] as? Number)?.toInt()
            ?: throw FileFailure("bad_params", "transfer_id is required")
        val transfer = transfer(id)
            ?: throw FileFailure("unknown_transfer", "this phone has no transfer $id")
        if (transfer.state == COMPLETE || transfer.state == CANCELLED) {
            throw FileFailure("bad_params", "transfer $id is ${transfer.state}; it cannot resume")
        }
        synchronized(lock) {
            paused.remove(id)
            cancelled.remove(id)
        }
        update(id) { it.copy(state = ACTIVE) }
        return linkedMapOf("ok" to true, "from_bytes" to transfer.bytes, "state" to ACTIVE)
    }

    // ------------------------------------------------------------------ //
    // housekeeping
    // ------------------------------------------------------------------ //
    fun onSessionClosed(why: String) {
        val active = transfers().filter { it.state == ACTIVE || it.state == PENDING }
        if (active.isEmpty()) return
        for (transfer in active) {
            synchronized(lock) { cancelled.add(transfer.id) }
            update(transfer.id) {
                it.copy(state = CANCELLED, error = why, finishedAtMs = System.currentTimeMillis())
            }
        }
        log("INFO", "file transfers ended with the session ($why)")
    }

    fun release() {
        onSessionClosed("the bridge stopped")
        synchronized(lock) {
            transfers.clear()
            selection.clear()
            paused.clear()
            cancelled.clear()
        }
        synchronized(ackLock) {
            ackedUpTo.clear()
            ackLock.notifyAll()
        }
        inboundQueue = null
        writer?.interrupt()
        writer = null
        publish()
    }

    private val digests = HashMap<Int, MessageDigest>()

    private fun digestOf(uri: Uri): String? = try {
        val digest = MessageDigest.getInstance("SHA-256")
        context.contentResolver.openInputStream(uri)?.use { input ->
            val buffer = ByteArray(FileFrames.CHUNK_SIZE)
            while (true) {
                val read = input.read(buffer)
                if (read <= 0) break
                digest.update(buffer, 0, read)
            }
        }
        FileFrames.hex(digest.digest())
    } catch (e: Exception) {
        log("WARN", "could not hash a selected file: ${e.message}")
        null
    }

    private fun sendAck(id: Int, index: Int) {
        sendCatching(FileFrames.Chunk(id, index, ByteArray(0), FileFrames.FLAG_ACK))
    }

    /** Send a chunk, reporting failure instead of throwing at a caller that cannot act. */
    private fun sendCatching(chunk: FileFrames.Chunk): Boolean = try {
        send(FileFrames.STREAM_TO_PC, chunk.encode())
        true
    } catch (e: Exception) {
        log("WARN", "could not send a file frame: ${e.message}")
        false
    }

}

/** A file refusal, reported to the PC with the given documented code. */
class FileFailure(code: String, message: String) : RpcFailure(code, message)
