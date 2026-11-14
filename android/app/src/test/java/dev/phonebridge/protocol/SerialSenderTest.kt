package dev.phonebridge.protocol

import java.nio.ByteBuffer
import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The failure this pins down is not hypothetical: a camera session was killed by
 * the PC with `replayed frame: counter 300 <= 301` because two sender threads
 * sealed 301 and 300 and then wrote them the other way round.
 *
 * The fake `seal` below is deliberately *not* atomic - a plain `var` incremented
 * in three steps, with a yield in between - because that is exactly what
 * `SecureCodec.seal` does with `sendCounter++`. If the lock in [SerialSender]
 * ever stops covering the write, the counters in [written] stop being 1, 2, 3 …
 */
class SerialSenderTest {

    @Test
    fun countersArriveStrictlyIncreasingFromManyThreads() {
        val threadCount = 8
        val perThread = 250
        val written = Collections.synchronizedList(mutableListOf<Int>())

        // SecureCodec's counter: a plain field, read, incremented, written back.
        var counter = 0
        val sender = SerialSender(
            seal = { _, _, _ ->
                Thread.yield()
                val next = counter + 1
                Thread.yield()
                counter = next
                Thread.yield()
                ByteBuffer.allocate(4).putInt(next).array()
            },
            write = { bytes ->
                // A slow write widens the window between sealing and writing,
                // which is where the frames used to overtake each other.
                Thread.yield()
                written.add(ByteBuffer.wrap(bytes).int)
            },
        )

        val start = CountDownLatch(1)
        val finished = CountDownLatch(threadCount)
        repeat(threadCount) {
            thread {
                start.await()
                repeat(perThread) { sender.send(1, 1, ByteArray(0)) }
                finished.countDown()
            }
        }
        start.countDown()
        assertTrue(
            "the senders did not finish in 30 s: a frame is stuck behind the lock",
            finished.await(30, TimeUnit.SECONDS),
        )

        assertEquals(threadCount * perThread, written.size)
        var previous = 0
        written.toList().forEachIndexed { index, value ->
            assertTrue(
                "frame $index carried counter $value after $previous: the wire order " +
                    "and the counter order disagree",
                value > previous,
            )
            previous = value
        }
        // No gaps either: every seal that happened was written, in order.
        assertEquals(threadCount * perThread, previous)
    }

    @Test
    fun aFailedSealReleasesTheLockAndKeepsTheCounterFree() {
        var fail = true
        val written = mutableListOf<ByteArray>()
        val sender = SerialSender(
            seal = { _, _, _ ->
                if (fail) throw IllegalStateException("boom") else byteArrayOf(1)
            },
            write = { bytes -> written.add(bytes) },
        )

        val error = runCatching { sender.send(1, 1, ByteArray(0)) }.exceptionOrNull()
        assertEquals("the seal failure must reach the caller", "boom", error?.message)
        assertEquals("nothing may be written for a frame that failed to seal", 0, written.size)

        fail = false
        sender.send(1, 1, ByteArray(0))
        assertEquals("the lock must not stay held after a failure", 1, written.size)
    }

    @Test
    fun unsealedFramesShareTheSameLock() {
        val order = mutableListOf<String>()
        val sender = SerialSender(
            seal = { _, _, _ ->
                order.add("seal")
                byteArrayOf(0)
            },
            write = { order.add("write") },
        )

        sender.serialised { order.add("handshake") }
        sender.send(1, 1, ByteArray(0))

        assertEquals(listOf("handshake", "seal", "write"), order)
    }
}
