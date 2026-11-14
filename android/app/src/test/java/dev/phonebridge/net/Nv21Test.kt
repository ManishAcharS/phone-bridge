package dev.phonebridge.net

import java.nio.ByteBuffer
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The plane layouts here are the ones that broke encoding on a real phone, so
 * they are pinned as bytes: a "picture still looks roughly right" assertion
 * would have passed on a skewed frame.
 *
 * Covered: fully planar YUV (pixel stride 1), semi-planar NV12/NV21 buffers
 * (pixel stride 2, U and V interleaved), both with row padding, with and
 * without a crop rectangle, plus the layouts a device can report that we refuse.
 */
class Nv21Test {

    private fun buffer(size: Int): ByteBuffer = ByteBuffer.allocateDirect(size)

    private fun yValue(x: Int, y: Int): Byte = (x * 3 + y * 7 + 11).toByte()

    private fun uValue(x: Int, y: Int): Byte = (200 - x * 5 - y * 3).toByte()

    private fun vValue(x: Int, y: Int): Byte = (100 + x * 9 + y * 2).toByte()

    /** Fully planar frame: one byte per sample, rows optionally padded. */
    private fun planar(
        width: Int,
        height: Int,
        rowStride: Int,
        value: (Int, Int) -> Byte,
    ): ByteBuffer = strided(width, height, rowStride, 1, value)

    /** A plane whose samples are [pixelStride] bytes apart, for the odd layouts. */
    private fun strided(
        width: Int,
        height: Int,
        rowStride: Int,
        pixelStride: Int,
        value: (Int, Int) -> Byte,
    ): ByteBuffer {
        val plane = buffer(rowStride * height)
        for (row in 0 until height) {
            for (column in 0 until width) {
                plane.put(row * rowStride + column * pixelStride, value(column, row))
            }
        }
        return plane
    }

    /**
     * Semi-planar chroma: one interleaved buffer, V first then U, `rowStride`
     * bytes per chroma row, two bytes per pair, both planes reporting stride 2.
     * The two plane views start one byte apart, exactly as CameraX reports them.
     */
    private fun semiPlanar(
        width: Int,
        height: Int,
        rowStride: Int,
    ): Triple<ByteBuffer, ByteBuffer, ByteBuffer> {
        val chromaWidth = width / 2
        val chromaHeight = height / 2
        val interleaved = buffer(rowStride * chromaHeight)
        for (row in 0 until chromaHeight) {
            for (pair in 0 until chromaWidth) {
                interleaved.put(row * rowStride + pair * 2, vValue(pair, row))
                interleaved.put(row * rowStride + pair * 2 + 1, uValue(pair, row))
            }
        }
        val v = interleaved.duplicate().apply { position(0); limit(rowStride * chromaHeight) }.slice()
        val u = interleaved.duplicate().apply { position(1); limit(rowStride * chromaHeight) }.slice()
        return Triple(interleaved, u, v)
    }

    private fun expectedLuma(
        cropLeft: Int,
        cropTop: Int,
        cropWidth: Int,
        cropHeight: Int,
    ): ByteArray {
        val out = ByteArray(cropWidth * cropHeight)
        for (row in 0 until cropHeight) {
            for (column in 0 until cropWidth) {
                out[row * cropWidth + column] = yValue(cropLeft + column, cropTop + row)
            }
        }
        return out
    }

    private fun expectedChroma(
        cropLeft: Int,
        cropTop: Int,
        cropWidth: Int,
        cropHeight: Int,
    ): ByteArray {
        val chromaWidth = cropWidth / 2
        val chromaHeight = cropHeight / 2
        val out = ByteArray(chromaWidth * chromaHeight * 2)
        var write = 0
        for (row in 0 until chromaHeight) {
            for (column in 0 until chromaWidth) {
                out[write++] = vValue(cropLeft / 2 + column, cropTop / 2 + row)
                out[write++] = uValue(cropLeft / 2 + column, cropTop / 2 + row)
            }
        }
        return out
    }

    private fun assertFrame(
        frame: ByteArray,
        cropWidth: Int,
        cropHeight: Int,
        cropLeft: Int = 0,
        cropTop: Int = 0,
    ) {
        val lumaSize = cropWidth * cropHeight
        assertEquals("NV21 frame size", lumaSize + lumaSize / 2, frame.size)
        assertArrayEquals(
            "Y plane",
            expectedLuma(cropLeft, cropTop, cropWidth, cropHeight),
            frame.copyOfRange(0, lumaSize),
        )
        assertArrayEquals(
            "V,U pairs",
            expectedChroma(cropLeft, cropTop, cropWidth, cropHeight),
            frame.copyOfRange(lumaSize, frame.size),
        )
    }

    @Test
    fun planarUnpaddedFrameIsCopiedLineByLine() {
        val width = 8
        val height = 6
        val y = planar(width, height, width, ::yValue)
        val chromaWidth = width / 2
        val u = planar(chromaWidth, height / 2, chromaWidth, ::uValue)
        val v = planar(chromaWidth, height / 2, chromaWidth, ::vValue)

        val frame = Nv21.from(
            width, height,
            Nv21.Plane(y, width, 1),
            Nv21.Plane(u, chromaWidth, 1),
            Nv21.Plane(v, chromaWidth, 1),
        )

        assertFrame(frame, width, height)
    }

    /** The regression: a padded row stride used to skew every row by the padding. */
    @Test
    fun paddedRowsDoNotSkewTheFrame() {
        val width = 8
        val height = 6
        val rowStride = 20
        val chromaWidth = width / 2
        val chromaStride = 12
        val y = planar(width, height, rowStride, ::yValue)
        val u = planar(chromaWidth, height / 2, chromaStride, ::uValue)
        val v = planar(chromaWidth, height / 2, chromaStride, ::vValue)

        val frame = Nv21.from(
            width, height,
            Nv21.Plane(y, rowStride, 1),
            Nv21.Plane(u, chromaStride, 1),
            Nv21.Plane(v, chromaStride, 1),
        )

        assertFrame(frame, width, height)
    }

    @Test
    fun cropRectangleSelectsTheRequestedRegion() {
        val width = 12
        val height = 8
        val rowStride = 16
        val cropLeft = 2
        val cropTop = 2
        val cropWidth = 8
        val cropHeight = 4
        val y = planar(width, height, rowStride, ::yValue)
        val u = planar(width / 2, height / 2, rowStride / 2, ::uValue)
        val v = planar(width / 2, height / 2, rowStride / 2, ::vValue)

        val frame = Nv21.from(
            width, height,
            Nv21.Plane(y, rowStride, 1),
            Nv21.Plane(u, rowStride / 2, 1),
            Nv21.Plane(v, rowStride / 2, 1),
            cropLeft = cropLeft,
            cropTop = cropTop,
            cropWidth = cropWidth,
            cropHeight = cropHeight,
        )

        assertFrame(frame, cropWidth, cropHeight, cropLeft, cropTop)
    }

    /** NV21/NV12 phones: chroma planes interleaved, both reporting pixel stride 2. */
    @Test
    fun semiPlanarChromaIsRepackedAsVThenU() {
        val width = 8
        val height = 6
        val y = planar(width, height, width, ::yValue)
        val (_, u, v) = semiPlanar(width, height, width)

        val frame = Nv21.from(
            width, height,
            Nv21.Plane(y, width, 1),
            Nv21.Plane(u, width, 2),
            Nv21.Plane(v, width, 2),
        )

        assertFrame(frame, width, height)
    }

    @Test
    fun semiPlanarChromaWithPaddingAndCrop() {
        val width = 12
        val height = 8
        val lumaStride = 32
        val chromaStride = 16
        val y = planar(width, height, lumaStride, ::yValue)
        val (_, u, v) = semiPlanar(width, height, chromaStride)
        val cropLeft = 2
        val cropTop = 2
        val cropWidth = 8
        val cropHeight = 6

        val frame = Nv21.from(
            width, height,
            Nv21.Plane(y, lumaStride, 1),
            Nv21.Plane(u, chromaStride, 2),
            Nv21.Plane(v, chromaStride, 2),
            cropLeft = cropLeft,
            cropTop = cropTop,
            cropWidth = cropWidth,
            cropHeight = cropHeight,
        )

        assertFrame(frame, cropWidth, cropHeight, cropLeft, cropTop)
    }

    /** A stride-2 luma plane is odd but legal; it must not be read as packed. */
    @Test
    fun stridedLumaPlaneIsReadSampleBySample() {
        val width = 6
        val height = 4
        val rowStride = width * 2
        val y = strided(width, height, rowStride, 2, ::yValue)
        val u = planar(width / 2, height / 2, width / 2, ::uValue)
        val v = planar(width / 2, height / 2, width / 2, ::vValue)

        val frame = Nv21.from(
            width, height,
            Nv21.Plane(y, rowStride, 2),
            Nv21.Plane(u, width / 2, 1),
            Nv21.Plane(v, width / 2, 1),
        )

        assertFrame(frame, width, height)
    }

    /** Planes that disagree about their stride take the generic path. */
    @Test
    fun mixedChromaStridesAreHandled() {
        val width = 8
        val height = 4
        val y = planar(width, height, width, ::yValue)
        val u = planar(width / 2, height / 2, width / 2, ::uValue)
        val (_, _, v) = semiPlanar(width, height, width)

        val frame = Nv21.from(
            width, height,
            Nv21.Plane(y, width, 1),
            Nv21.Plane(u, width / 2, 1),
            Nv21.Plane(v, width, 2),
        )

        assertFrame(frame, width, height)
    }

    @Test
    fun oddCropIsRefused() {
        val y = planar(8, 6, 8, ::yValue)
        val u = planar(4, 3, 4, ::uValue)
        val v = planar(4, 3, 4, ::vValue)
        val error = runCatching {
            Nv21.from(
                8, 6,
                Nv21.Plane(y, 8, 1),
                Nv21.Plane(u, 4, 1),
                Nv21.Plane(v, 4, 1),
                cropWidth = 7,
                cropHeight = 4,
            )
        }.exceptionOrNull()
        assertTrue("expected a refusal, got $error", error is IllegalArgumentException)
        assertTrue(
            "the message should name the crop, was ${error?.message}",
            error?.message?.contains("even") == true,
        )
    }

    @Test
    fun cropOutsideTheFrameIsRefused() {
        val y = planar(8, 6, 8, ::yValue)
        val u = planar(4, 3, 4, ::uValue)
        val v = planar(4, 3, 4, ::vValue)
        val error = runCatching {
            Nv21.from(
                8, 6,
                Nv21.Plane(y, 8, 1),
                Nv21.Plane(u, 4, 1),
                Nv21.Plane(v, 4, 1),
                cropLeft = 4,
                cropTop = 0,
                cropWidth = 8,
                cropHeight = 4,
            )
        }.exceptionOrNull()
        assertTrue("expected a refusal, got $error", error is IllegalArgumentException)
    }

    @Test
    fun zeroStridePlaneIsRefused() {
        val buffer = buffer(16)
        val error = runCatching { Nv21.Plane(buffer, 0, 1) }.exceptionOrNull()
        assertTrue("expected a refusal, got $error", error is IllegalArgumentException)
    }
}
