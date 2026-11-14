package dev.phonebridge.net

import java.nio.ByteBuffer

/**
 * Pack a `YUV_420_888` frame into NV21, for `YuvImage.compressToJpeg`.
 *
 * This is deliberately *not* an `ImageProxy` extension: it takes three
 * [Plane] descriptions (a buffer, a row stride and a pixel stride), so it can be
 * unit-tested on the JVM with synthetic planes. The layout rules are the part
 * that broke on a real device, and they are exactly the part a test can pin.
 *
 * Three things are true of real devices and false of the naive implementation:
 *
 * 1. **Rows are padded.** `rowStride` is usually larger than `width` (the buffer
 *    is 16- or 32-byte aligned), so a frame copied with a single
 *    `buffer.get(dst, 0, width * height)` is skewed a little more with every
 *    row: the picture tears diagonally and the JPEG stops compressing.
 * 2. **Chroma has a pixel stride.** NV21/NV12 phones interleave U and V, so
 *    each plane's `pixelStride` is 2 and the V plane's buffer is exactly the
 *    V,U,V,U sequence NV21 wants. Fully planar devices use stride 1. Both
 *    exist, and some devices report a mix, so every plane is read with its own
 *    stride rather than assuming the pair agree.
 * 3. **There is a crop rectangle.** CameraX may hand over a frame whose valid
 *    region is smaller than the buffer; honouring `cropRect` is the difference
 *    between a picture and a picture with a strip of the previous resolution
 *    along one edge.
 *
 * The output is `width * height * 3 / 2` bytes: the Y plane as-is, followed by
 * interleaved **V then U** (NV21, not NV12). Both crop dimensions must be even,
 * because 4:2:0 chroma has no half sample; an odd crop is a caller bug and is
 * refused rather than silently rounded (rounding would shift every row).
 */
object Nv21 {

    /** One `ImageProxy.PlaneProxy`, reduced to what the conversion needs. */
    class Plane(val buffer: ByteBuffer, val rowStride: Int, val pixelStride: Int) {
        init {
            require(rowStride > 0) { "row stride must be positive, was $rowStride" }
            require(pixelStride > 0) { "pixel stride must be positive, was $pixelStride" }
        }
    }

    /**
     * NV21 bytes for the crop region of a `YUV_420_888` frame.
     *
     * @param width full frame width in pixels
     * @param height full frame height in pixels
     * @param y the luma plane
     * @param u the U (Cb) plane
     * @param v the V (Cr) plane
     * @param cropLeft left edge of the region to encode, in pixels
     * @param cropTop top edge of the region to encode, in pixels
     * @param cropWidth width of that region; must be even
     * @param cropHeight height of that region; must be even
     */
    fun from(
        width: Int,
        height: Int,
        y: Plane,
        u: Plane,
        v: Plane,
        cropLeft: Int = 0,
        cropTop: Int = 0,
        cropWidth: Int = width,
        cropHeight: Int = height,
    ): ByteArray {
        require(width > 0 && height > 0) { "frame is ${width}x$height" }
        require(cropWidth > 0 && cropHeight > 0) { "crop is ${cropWidth}x$cropHeight" }
        require(cropLeft >= 0 && cropTop >= 0) { "crop origin is negative: $cropLeft,$cropTop" }
        require(cropLeft + cropWidth <= width && cropTop + cropHeight <= height) {
            "crop ${cropLeft},${cropTop} ${cropWidth}x$cropHeight is outside ${width}x$height"
        }
        require(cropWidth % 2 == 0 && cropHeight % 2 == 0) {
            "4:2:0 chroma needs an even crop, got ${cropWidth}x$cropHeight"
        }

        val lumaSize = cropWidth * cropHeight
        val out = ByteArray(lumaSize + lumaSize / 2)
        copyLuma(y, out, cropLeft, cropTop, cropWidth, cropHeight, width, height)
        copyChroma(u, v, out, lumaSize, cropLeft, cropTop, cropWidth, cropHeight, width, height)
        return out
    }

    /** Y plane: one row at a time, so `rowStride > width` costs padding and nothing else. */
    private fun copyLuma(
        y: Plane,
        out: ByteArray,
        cropLeft: Int,
        cropTop: Int,
        cropWidth: Int,
        cropHeight: Int,
        width: Int,
        height: Int,
    ) {
        var write = 0
        for (row in 0 until cropHeight) {
            val srcRow = (cropTop + row) * y.rowStride
            if (y.pixelStride == 1) {
                val start = srcRow + cropLeft
                checkRange(y, start, cropWidth, width, height, "Y row $row")
                val view = y.buffer.duplicate()
                view.position(start)
                view.get(out, write, cropWidth)
            } else {
                for (column in 0 until cropWidth) {
                    val index = srcRow + (cropLeft + column) * y.pixelStride
                    checkIndex(y, index, width, height, "Y row $row")
                    out[write + column] = y.buffer.get(index)
                }
            }
            write += cropWidth
        }
    }

    /**
     * Chroma: interleaved V,U pairs.
     *
     * The bulk path is the semi-planar one (both strides 2): NV21 wants exactly
     * the byte sequence the V plane already holds, so a row is one copy. Any
     * other layout - fully planar, or a device where U and V disagree about
     * their stride - falls back to reading each sample at its own stride, which
     * is slower and always correct.
     */
    private fun copyChroma(
        u: Plane,
        v: Plane,
        out: ByteArray,
        lumaSize: Int,
        cropLeft: Int,
        cropTop: Int,
        cropWidth: Int,
        cropHeight: Int,
        width: Int,
        height: Int,
    ) {
        val chromaWidth = cropWidth / 2
        val chromaHeight = cropHeight / 2
        val left = cropLeft / 2
        val top = cropTop / 2
        var write = lumaSize
        if (u.pixelStride == 2 && v.pixelStride == 2) {
            val view = v.buffer.duplicate()
            for (row in 0 until chromaHeight) {
                val start = (top + row) * v.rowStride + left * 2
                val bytes = chromaWidth * 2
                if (start >= 0 && start + bytes <= v.buffer.limit()) {
                    view.position(start)
                    view.get(out, write, bytes)
                    write += bytes
                    continue
                }
                // A buffer shorter than its stride suggests: read sample by sample.
                for (column in 0 until chromaWidth) {
                    val index = (top + row) * v.rowStride + (left + column) * 2
                    out[write++] = v.buffer.get(index)
                    out[write++] = u.buffer.get((top + row) * u.rowStride + (left + column) * 2)
                }
            }
            return
        }
        for (row in 0 until chromaHeight) {
            val vRow = (top + row) * v.rowStride
            val uRow = (top + row) * u.rowStride
            for (column in 0 until chromaWidth) {
                out[write++] = v.buffer.get(vRow + (left + column) * v.pixelStride)
                out[write++] = u.buffer.get(uRow + (left + column) * u.pixelStride)
            }
        }
    }

    private fun checkRange(plane: Plane, start: Int, length: Int, width: Int, height: Int, what: String) {
        checkIndex(plane, start, width, height, what)
        require(start + length <= plane.buffer.limit()) {
            "$what: reading $length bytes at $start needs ${start + length}, " +
                "buffer holds ${plane.buffer.limit()}"
        }
    }

    private fun checkIndex(plane: Plane, index: Int, width: Int, height: Int, what: String) {
        require(index >= 0) { "$what: negative index $index" }
        require(index < plane.buffer.limit()) {
            "$what: index $index is beyond the ${plane.buffer.limit()}-byte plane " +
                "(frame ${width}x$height)"
        }
    }
}
