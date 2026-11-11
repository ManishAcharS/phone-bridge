package dev.phonebridge.net

import android.content.Context
import android.net.Uri
import android.os.SystemClock
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

        /**
         * How long the session reader waits for the writer before failing.
         *
         * Deliberately short: this runs on the connection's read thread, so every
         * millisecond here is a millisecond in which heartbeats, acknowledgements
         * and control replies are not being read. A slow card is a reason to fail
         * a transfer with a sentence, not to stall the session.
         */
        const val INBOUND_WAIT_MS = 250L

        /**
         * How long the sender holds its first chunk waiting for the PC's resume
         * decision. A phone->PC download starts streaming the moment it answers
         * `file.request`, which used to make a resume impossible; waiting briefly
         * makes the handshake deterministic, and the window is short enough that a
         * PC which never asks (an older build) sees no delay worth noticing.
         */
        const val RESUME_GRACE_MS = 1_000L

        /** Files the user picked for sending, announced to the PC on request. */
        const val MAX_SELECTION = 16

        const val RESUME_PREFS = "phonebridge_resume"
        const val KEY_NAME = "name"
        const val KEY_SIZE = "size"
        const val KEY_DIGEST = "digest"
        const val KEY_PART = "part_uri"
        const val KEY_BYTES = "bytes"
        const val KEY_CHUNK = "chunk_size"
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

    /**
     * The phone->PC resume handshake.
     *
     * `requested` holds the offset the PC asked for before the sender committed to
     * one; `started` remembers the offset it actually began at, so a *late*
     * `file.resume` is answered with the truth instead of a number the receiver
     * would use to truncate good bytes. `decided` is the "the sender has committed"
     * marker that makes both answers deterministic.
     */
    private val resumeLock = Object()
    private val requested = HashMap<Int, Long>()
    private val started = HashMap<Int, Long>()
    private val decided = HashSet<Int>()

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
        val (resumeFrom, partUri) = openPart(name, size, digest, chunkSize)
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
        // Record the identity before a single chunk arrives: a transfer that dies
        // during the first window must still be findable on the next attempt.
        rememberResume(transfer)
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
     * A record may only be used when *everything* that identifies the transfer
     * matches: the name, the size, the digest and the chunk size. The chunk size is
     * part of the identity because a resume offset is a chunk boundary — bytes
     * written as 64 KiB chunks cannot be continued as a 16 KiB-chunk transfer.
     */
    private data class ResumeRecord(
        val name: String,
        val size: Long,
        val sha256: String,
        val partUri: String,
        val bytes: Long,
        val chunkSize: Int,
    )

    private fun resumeRecord(): ResumeRecord? {
        val prefs = context.getSharedPreferences(RESUME_PREFS, Context.MODE_PRIVATE)
        val name = prefs.getString(KEY_NAME, null) ?: return null
        val digest = prefs.getString(KEY_DIGEST, null) ?: return null
        val partUri = prefs.getString(KEY_PART, null) ?: return null
        val size = prefs.getLong(KEY_SIZE, -1L)
        val bytes = prefs.getLong(KEY_BYTES, 0L)
        if (size <= 0L || bytes <= 0L) return null
        return ResumeRecord(name, size, digest, partUri, bytes, prefs.getInt(KEY_CHUNK, 0))
    }

    /**
     * Choose the document this transfer writes into, and where in it.
     *
     * A resumed transfer must continue in the **same** document. Creating a second
     * `.part` and walking away from the first is how the previous release lost
     * every resumed file: the metadata described bytes that were no longer being
     * written to. The offset is aligned to the *negotiated* chunk size, and the
     * document must hold exactly that many bytes, or the transfer starts over
     * rather than appending to something that does not match.
     */
    private fun openPart(name: String, size: Long, digest: String, chunkSize: Int): Pair<Long, Uri> {
        val previous = resumeRecord()
        if (previous != null &&
            previous.name == name &&
            previous.size == size &&
            previous.sha256 == digest &&
            (previous.chunkSize == 0 || previous.chunkSize == chunkSize)
        ) {
            val aligned = if (chunkSize > 0) previous.bytes / chunkSize * chunkSize else 0L
            val uri = runCatching { Uri.parse(previous.partUri) }.getOrNull()
            val actual = if (uri != null) SafStore.sizeOf(context, uri) else -1L
            if (uri != null && aligned > 0L && aligned < size && actual == aligned) {
                log("INFO", "resuming $name at $aligned B in $uri")
                return aligned to uri
            }
            log(
                "WARN",
                "not resuming $name: metadata says $aligned B, the part holds $actual B",
            )
            // The partial document is deliberately left alone: it is the user's
            // data, and deleting it here would destroy a resume that a later
            // attempt (with the right chunk size, say) could still use.
        }
        val created = SafStore.createDocument(
            context,
            FileFrames.partName(name),
            SafStore.mimeFor(name),
        )
        log("INFO", "receiving $name into a new .part document")
        return 0L to created
    }

    private fun rememberResume(transfer: Transfer) {
        context.getSharedPreferences(RESUME_PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_NAME, transfer.name)
            .putLong(KEY_SIZE, transfer.size)
            .putString(KEY_DIGEST, transfer.sha256)
            .putString(KEY_PART, transfer.partUri)
            .putLong(KEY_BYTES, transfer.bytes)
            .putInt(KEY_CHUNK, transfer.chunkSize)
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
                // A paused transfer must not write, but the chunk is not lost. It is
                // put back with a *blocking* put, not an offer: dropping it here
                // would silently corrupt the file, and the writer is this queue's
                // only consumer, so the put cannot deadlock.
                try {
                    Thread.sleep(100)
                    queue.put(chunk)
                } catch (e: InterruptedException) {
                    return
                }
                continue
            }
            if (!writeChunk(transfer, chunk)) return
        }
    }

    private fun writeChunk(transfer: Transfer, chunk: FileFrames.Chunk): Boolean {
        val id = transfer.id
        // Resume: the sender may continue at the offset we reported, so anything
        // before that offset is already on disk.
        val startIndex = (transfer.resumedFrom / transfer.chunkSize).toInt()
        val expectedIndex = transfer.chunks + startIndex
        if (chunk.chunkIndex < expectedIndex) {
            // A duplicate — a retry, or a sender that restarted at zero because it
            // could not honour the offset. Acknowledge it and never write it twice:
            // appending it again would corrupt the file and fail the hash at the end.
            sendAck(id, chunk.chunkIndex)
            log("DEBUG", "ignoring duplicate chunk ${chunk.chunkIndex} of transfer $id")
            return true
        }
        if (chunk.chunkIndex > expectedIndex) {
            cancelInbound(id, "chunk ${chunk.chunkIndex} arrived before $expectedIndex")
            return true
        }
        val uri = Uri.parse(transfer.partUri)
        try {
            // Append for every chunk of a resumed transfer (the prefix is already in
            // the document) and for every chunk after the first. `"wt"` truncates,
            // so it is only correct for a brand-new part.
            val append = transfer.resumedFrom > 0L || transfer.chunks > 0
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
        if (chunk.chunkIndex == startIndex && transfer.resumedFrom > 0L) {
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
            rememberResume(updated)
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
            // A short file is an *incomplete* one, not a wrong one: keep the part
            // and its metadata so the next attempt resumes from where this stopped.
            return failInbound(
                transfer,
                "the phone received ${transfer.bytes} of ${transfer.size} bytes",
                keepPart = true,
            )
        }
        val actual = digest?.let { FileFrames.hex(it) } ?: ""
        if (actual.isEmpty() || actual != transfer.sha256) {
            // Wrong bytes are not resumable, so the part and its metadata go: a
            // later attempt must start from zero rather than trust this prefix.
            return failInbound(
                transfer,
                "the SHA-256 did not match the offer",
                keepPart = false,
            )
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

    /**
     * Fail a transfer, deciding honestly whether its partial data survives.
     *
     * `keepPart = true` is the incomplete case (a dropped link, a short file): the
     * bytes are a valid prefix, so deleting them would throw away the only thing
     * that makes resume possible. `false` is the corrupt case (a hash mismatch):
     * those bytes can never be completed, so they go — as does the metadata that
     * describes them — and the next attempt starts clean.
     */
    private fun failInbound(transfer: Transfer, message: String, keepPart: Boolean) {
        if (!keepPart) {
            SafStore.deleteDocument(context, Uri.parse(transfer.partUri))
            clearResume()
        }
        update(transfer.id) {
            it.copy(
                state = FAILED,
                error = message,
                verified = false,
                finishedAtMs = System.currentTimeMillis(),
            )
        }
        log(
            "WARN",
            "file transfer ${transfer.id} failed: $message" +
                if (keepPart) " (the partial file is kept for a resume)" else "",
        )
        sendCatching(
            FileFrames.Chunk(transfer.id, 0, ByteArray(0), FileFrames.FLAG_CANCEL)
        )
        _state.value = _state.value.copy(lastError = message)
    }

    private fun cancelInbound(id: Int, reason: String) {
        val transfer = transfer(id) ?: return
        synchronized(lock) {
            cancelled.add(id)
            paused.remove(id)
        }
        digests.remove(id)
        synchronized(resumeLock) { resumeLock.notifyAll() }
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
        synchronized(resumeLock) {
            requested.remove(id)
            started.remove(id)
            decided.remove(id)
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
            "resume_from" to 0L,
        )
    }

    private fun sender(id: Int) {
        val transfer = transfer(id) ?: return
        val uri = Uri.parse(transfer.sourceUri)
        val chunkSize = transfer.chunkSize
        val total = ((transfer.size + chunkSize - 1) / chunkSize).toInt().coerceAtLeast(1)
        // Hold the first chunk until the PC has said where to start. Without this
        // the phone is already streaming while the PC is still deciding, which is
        // what made a resumed download impossible to negotiate.
        val from = awaitResumeOffset(id, transfer)
        if (from < 0L) {
            log("INFO", "transfer $id was cancelled before it started")
            publish()
            return
        }
        val startIndex = if (chunkSize > 0) (from / chunkSize).toInt() else 0
        synchronized(ackLock) { ackedUpTo[id] = startIndex - 1 }
        update(id) {
            it.copy(state = ACTIVE, bytes = from, chunks = startIndex, resumedFrom = from)
        }
        try {
            context.contentResolver.openInputStream(uri)?.use { input ->
                if (from > 0L) skipFully(input, from)
                streamOut(id, input, total, chunkSize, startIndex)
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

    private fun streamOut(id: Int, input: InputStream, total: Int, chunkSize: Int, startIndex: Int) {
        var index = startIndex
        var sent = startIndex.toLong() * chunkSize
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

    /**
     * Wait for the PC's resume decision, bounded by [RESUME_GRACE_MS].
     *
     * Returns the byte offset to start at, or `-1` when the transfer was cancelled
     * or the session ended before it began. A PC that never asks (an older build)
     * gets offset zero after the grace, i.e. exactly the old behaviour.
     */
    private fun awaitResumeOffset(id: Int, transfer: Transfer): Long {
        val requestedOffset: Long
        synchronized(resumeLock) {
            val deadline = SystemClock.elapsedRealtime() + RESUME_GRACE_MS
            while (id !in requested) {
                if (synchronized(lock) { cancelled.contains(id) }) {
                    decided.add(id)
                    return -1L
                }
                val remaining = deadline - SystemClock.elapsedRealtime()
                if (remaining <= 0L) break
                try {
                    resumeLock.wait(remaining.coerceAtMost(50L))
                } catch (e: InterruptedException) {
                    Thread.currentThread().interrupt()
                    decided.add(id)
                    return -1L
                }
            }
            requestedOffset = requested.remove(id) ?: 0L
            decided.add(id)
            val offset = sanitiseOffset(transfer, requestedOffset)
            started[id] = offset
            return offset
        }
    }

    /** A resume offset is only usable on a chunk boundary inside the file. */
    private fun sanitiseOffset(transfer: Transfer, offset: Long): Long {
        if (offset <= 0L) return 0L
        if (offset >= transfer.size) {
            log("WARN", "resume offset $offset is at or past the end of ${transfer.name}")
            return 0L
        }
        val chunkSize = transfer.chunkSize
        if (chunkSize <= 0) return 0L
        val aligned = offset / chunkSize * chunkSize
        if (aligned != offset) {
            log("WARN", "resume offset $offset is not a multiple of $chunkSize; using $aligned")
        }
        return aligned
    }

    /** Skip exactly [count] bytes, or throw: a short skip would corrupt the file. */
    private fun skipFully(input: InputStream, count: Long) {
        var remaining = count
        while (remaining > 0L) {
            val skipped = input.skip(remaining)
            if (skipped > 0L) {
                remaining -= skipped
                continue
            }
            // `skip` may return 0 at a stream boundary; read one byte to progress.
            if (input.read() < 0) {
                throw java.io.IOException("the file ended $remaining bytes before the resume offset")
            }
            remaining -= 1L
        }
    }

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
        update(id) {
            if (pcVerified == false) {
                // The PC read the bytes back and they were wrong: the card must not
                // keep saying "complete" while the only machine that verified it
                // says otherwise.
                it.copy(
                    state = FAILED,
                    verified = false,
                    error = "the PC could not verify the file",
                    finishedAtMs = System.currentTimeMillis(),
                )
            } else {
                it.copy(verified = pcVerified ?: it.verified)
            }
        }
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
            // A sender still holding its first chunk must wake up and stop, or the
            // cancel would be honoured only after the grace period.
            synchronized(resumeLock) { resumeLock.notifyAll() }
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

    /**
     * Resume a transfer, optionally from a byte offset the receiver already holds.
     *
     * For a phone->PC download the offset is the negotiation: the sender holds its
     * first chunk until this call (or the grace period) decides where to start, so
     * the receiver never has to discard a prefix it asked us to skip. The reply
     * always states the offset the sender will *actually* use — for a transfer that
     * has already begun, that is where it began, not what was asked for, because
     * the receiver uses this number to decide how much of its part file to keep.
     */
    fun resume(params: Map<*, *>): Map<String, Any?> {
        val id = (params["transfer_id"] as? Number)?.toInt()
            ?: throw FileFailure("bad_params", "transfer_id is required")
        val transfer = transfer(id)
            ?: throw FileFailure("unknown_transfer", "this phone has no transfer $id")
        if (transfer.state == COMPLETE || transfer.state == CANCELLED) {
            throw FileFailure("bad_params", "transfer $id is ${transfer.state}; it cannot resume")
        }
        val asked = (params["from_bytes"] as? Number)?.toLong() ?: 0L
        if (asked < 0L || asked > transfer.size) {
            throw FileFailure(
                "bad_params",
                "from_bytes $asked is outside this ${transfer.size} byte transfer",
            )
        }
        if (transfer.direction == FROM_PHONE && transfer.chunkSize > 0 &&
            asked % transfer.chunkSize != 0L
        ) {
            throw FileFailure(
                "bad_params",
                "from_bytes must be a multiple of the ${transfer.chunkSize} byte chunk size",
            )
        }
        var applied = asked
        if (transfer.direction == FROM_PHONE) {
            synchronized(resumeLock) {
                if (id in decided) {
                    // Too late to change the start: report where it really began.
                    applied = started[id] ?: 0L
                } else {
                    requested[id] = asked
                    resumeLock.notifyAll()
                }
            }
        } else {
            applied = transfer.resumedFrom
        }
        synchronized(lock) {
            paused.remove(id)
            cancelled.remove(id)
        }
        update(id) { it.copy(state = ACTIVE) }
        log("INFO", "transfer $id resumes at $applied B (asked for $asked)")
        return linkedMapOf("ok" to true, "from_bytes" to applied, "state" to ACTIVE)
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
        synchronized(resumeLock) { resumeLock.notifyAll() }
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
        synchronized(resumeLock) {
            requested.clear()
            started.clear()
            decided.clear()
            resumeLock.notifyAll()
        }
        digests.clear()
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
