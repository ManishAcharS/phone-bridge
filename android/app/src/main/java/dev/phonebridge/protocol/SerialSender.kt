package dev.phonebridge.protocol

/**
 * Seal a frame and write it to the socket as one step, one frame at a time.
 *
 * This is a correctness primitive, not a convenience. Two facts make it
 * necessary:
 *
 * 1. **The counter is assigned inside the seal.** `SecureCodec.seal` takes the
 *    next value of `sendCounter` and puts it in the frame, and the receiver
 *    rejects anything whose counter is not strictly greater than the last one it
 *    accepted (that is the replay defence doing its job).
 * 2. **A session has more than one sender.** The RPC reader answers a request
 *    from the socket thread, the camera has its own sender thread, and audio has
 *    another. If sealing is not serialised, two threads can take 301 and 300 and
 *    then write 300 second, and the receiver kills the session: a real camera
 *    session died 27 seconds in with `replayed frame: counter 300 <= 301`.
 *
 * Holding the lock across the write is what makes the counter order and the wire
 * order the same thing. Writers therefore block on each other for the duration
 * of one `conn.send` (which hands the buffer to the WebSocket library's outgoing
 * queue), not for the duration of a network round trip.
 *
 * [serialised] exists for the two frames that carry no counter but must still not
 * interleave with a sealed frame: the plaintext handshake reply and BYE.
 */
class SerialSender(
    private val seal: (msgType: Int, streamId: Int, payload: ByteArray) -> ByteArray,
    private val write: (ByteArray) -> Unit,
) {

    private val lock = Any()

    /**
     * Seal [payload] and write the result, under the lock.
     *
     * Exceptions propagate to the caller, which is what closes the session: they
     * are thrown from inside the `synchronized` block but reach the caller only
     * after the lock has been released, so a send failure cannot deadlock the
     * thread that is about to tear the session down.
     */
    fun send(msgType: Int, streamId: Int, payload: ByteArray) {
        synchronized(lock) {
            write(seal(msgType, streamId, payload))
        }
    }

    /** Run [block] under the same lock as [send], for unsealed control frames. */
    fun <T> serialised(block: () -> T): T = synchronized(lock) { block() }
}
