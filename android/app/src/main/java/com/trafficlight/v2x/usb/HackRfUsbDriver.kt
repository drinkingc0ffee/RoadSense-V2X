package com.trafficlight.v2x.usb

import android.content.Context
import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager
import android.hardware.usb.UsbRequest
import android.util.Log
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.TimeoutException

/**
 * Direct Android USB-OTG driver for HackRF One (VID 0x1D50, PID 0x6089).
 * Uses standard libhackrf USB vendor control transfers and queued async UsbRequest
 * bulk transfers — no root required.
 */
class HackRfUsbDriver(private val context: Context) {

    companion object {
        private const val TAG = "HackRfUsbDriver"
        const val HACKRF_VID = 0x1D50
        const val HACKRF_ONE_PID = 0x6089
        const val HACKRF_JAWBREAKER_PID = 0x604B

        // libhackrf vendor requests
        private const val REQ_SET_TRANSCEIVER_MODE = 1
        private const val REQ_SAMPLE_RATE_SET = 6
        private const val REQ_BASEBAND_FILTER_BANDWIDTH_SET = 7
        private const val REQ_BOARD_ID_READ = 14
        private const val REQ_VERSION_STRING_READ = 15
        private const val REQ_SET_FREQ = 16
        private const val REQ_AMP_ENABLE = 17
        private const val REQ_SET_LNA_GAIN = 19
        private const val REQ_SET_VGA_GAIN = 20
        private const val REQ_ANTENNA_ENABLE = 23

        private const val MODE_OFF = 0
        private const val MODE_RECEIVE = 1

        private const val USB_DIR_OUT = UsbConstants.USB_DIR_OUT or UsbConstants.USB_TYPE_VENDOR
        private const val USB_DIR_IN = UsbConstants.USB_DIR_IN or UsbConstants.USB_TYPE_VENDOR

        private const val QUEUE_DEPTH = 8
        private const val TRANSFER_SIZE = 16384
    }

    private val usbManager = context.getSystemService(Context.USB_SERVICE) as UsbManager
    private var device: UsbDevice? = null
    private var connection: UsbDeviceConnection? = null
    private var usbInterface: UsbInterface? = null
    private var bulkInEndpoint: UsbEndpoint? = null

    // Cached RF configuration so we don't re-program PLL/clock on every chunk
    private var cachedSampleRateHz: Int = -1
    private var cachedFreqHz: Long = -1L
    private var cachedLnaGain: Int = -1
    private var cachedVgaGain: Int = -1
    private var cachedAmpEnable: Boolean? = null
    private var cachedAntennaOff: Boolean = false

    @Volatile
    var lastError: String = ""
        private set

    @Volatile
    var firmwareVersion: String = ""
        private set

    val isOpen: Boolean get() = connection != null && bulkInEndpoint != null

    fun findHackRf(): UsbDevice? {
        return usbManager.deviceList.values.firstOrNull {
            it.vendorId == HACKRF_VID && (it.productId == HACKRF_ONE_PID || it.productId == HACKRF_JAWBREAKER_PID)
        }
    }

    fun hasPermission(dev: UsbDevice): Boolean = usbManager.hasPermission(dev)

    @Synchronized
    fun open(dev: UsbDevice): Boolean {
        close()
        lastError = ""
        val conn = usbManager.openDevice(dev)
        if (conn == null) {
            lastError = "openDevice returned null"
            return false
        }
        val intf = dev.getInterface(0)
        if (!conn.claimInterface(intf, true)) {
            lastError = "claimInterface(0) failed"
            conn.close()
            return false
        }
        var inEp: UsbEndpoint? = null
        for (i in 0 until intf.endpointCount) {
            val ep = intf.getEndpoint(i)
            if (ep.type == UsbConstants.USB_ENDPOINT_XFER_BULK && ep.direction == UsbConstants.USB_DIR_IN) {
                inEp = ep
                break
            }
        }
        if (inEp == null) {
            lastError = "No bulk-IN endpoint found"
            conn.releaseInterface(intf)
            conn.close()
            return false
        }
        device = dev
        connection = conn
        usbInterface = intf
        bulkInEndpoint = inEp
        resetConfigCache()

        // Ensure clean initial USB state
        setTransceiverMode(MODE_OFF)
        clearEndpointHalt(inEp)
        firmwareVersion = readVersionString() ?: "unknown fw"
        Log.i(TAG, "Opened HackRF One: fw=$firmwareVersion, ep=0x${inEp.address.toString(16)}, maxPkt=${inEp.maxPacketSize}")
        return true
    }

    @Synchronized
    fun close() {
        try {
            if (connection != null) {
                setTransceiverMode(MODE_OFF)
            }
        } catch (_: Exception) {}
        try {
            usbInterface?.let { connection?.releaseInterface(it) }
            connection?.close()
        } catch (_: Exception) {}
        device = null
        connection = null
        usbInterface = null
        bulkInEndpoint = null
        resetConfigCache()
    }

    private fun resetConfigCache() {
        cachedSampleRateHz = -1
        cachedFreqHz = -1L
        cachedLnaGain = -1
        cachedVgaGain = -1
        cachedAmpEnable = null
        cachedAntennaOff = false
    }

    private fun controlOut(request: Int, value: Int = 0, index: Int = 0, data: ByteArray? = null): Int {
        val conn = connection ?: return -99
        val len = data?.size ?: 0
        return conn.controlTransfer(USB_DIR_OUT, request, value, index, data, len, 1000)
    }

    private fun controlIn(request: Int, value: Int = 0, index: Int = 0, length: Int = 1): Pair<Int, ByteArray> {
        val conn = connection ?: return -99 to ByteArray(0)
        val buf = ByteArray(length)
        val n = conn.controlTransfer(USB_DIR_IN, request, value, index, buf, length, 1000)
        return n to buf
    }

    private fun clearEndpointHalt(ep: UsbEndpoint): Int {
        val conn = connection ?: return -99
        // Standard USB CLEAR_FEATURE (0x01) -> ENDPOINT_HALT (0x00) on recipient ENDPOINT (0x02)
        return conn.controlTransfer(
            UsbConstants.USB_DIR_OUT or UsbConstants.USB_TYPE_STANDARD or 0x02,
            0x01,
            0x00,
            ep.address,
            null,
            0,
            500
        )
    }

    fun readVersionString(): String? {
        val (n, buf) = controlIn(REQ_VERSION_STRING_READ, 0, 0, 255)
        if (n <= 0) return null
        return String(buf, 0, n, Charsets.US_ASCII).trim('\u0000', ' ', '\r', '\n')
    }

    fun setFrequency(freqHz: Long): Boolean {
        val mhz = (freqHz / 1_000_000L).toInt()
        val hz = (freqHz % 1_000_000L).toInt()
        val payload = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN)
            .putInt(mhz)
            .putInt(hz)
            .array()
        val rc = controlOut(REQ_SET_FREQ, 0, 0, payload)
        if (rc < 0) {
            lastError = "setFreq(${mhz}MHz) rc=$rc"
            return false
        }
        return true
    }

    fun setSampleRate(sampleRateHz: Int): Boolean {
        val payload = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN)
            .putInt(sampleRateHz)
            .putInt(1)
            .array()
        val rc = controlOut(REQ_SAMPLE_RATE_SET, 0, 0, payload)
        if (rc < 0) {
            lastError = "setSampleRate($sampleRateHz) rc=$rc"
            return false
        }
        // Also set baseband filter <= 0.75 * sampleRate
        val bw = if (sampleRateHz >= 20_000_000) 15_000_000 else 7_000_000
        if (!setBasebandFilterBandwidth(bw)) return false
        return true
    }

    fun setBasebandFilterBandwidth(bwHz: Int): Boolean {
        val low = bwHz and 0xFFFF
        val high = (bwHz ushr 16) and 0xFFFF
        val rc = controlOut(REQ_BASEBAND_FILTER_BANDWIDTH_SET, low, high, null)
        if (rc < 0) {
            lastError = "setBBFilter($bwHz) rc=$rc"
            return false
        }
        return true
    }

    fun setLnaGain(gainDb: Int): Boolean {
        val clamped = (gainDb.coerceIn(0, 40) / 8) * 8
        val (rc, _) = controlIn(REQ_SET_LNA_GAIN, 0, clamped, 1)
        if (rc < 0) {
            lastError = "setLna($clamped) rc=$rc"
            return false
        }
        return true
    }

    fun setVgaGain(gainDb: Int): Boolean {
        val clamped = (gainDb.coerceIn(0, 62) / 2) * 2
        val (rc, _) = controlIn(REQ_SET_VGA_GAIN, 0, clamped, 1)
        if (rc < 0) {
            lastError = "setVga($clamped) rc=$rc"
            return false
        }
        return true
    }

    fun setAmpEnable(enable: Boolean): Boolean {
        val rc = controlOut(REQ_AMP_ENABLE, if (enable) 1 else 0, 0, null)
        if (rc < 0) {
            lastError = "setAmp($enable) rc=$rc"
            return false
        }
        return true
    }

    fun setAntennaEnable(enable: Boolean): Boolean {
        val rc = controlOut(REQ_ANTENNA_ENABLE, if (enable) 1 else 0, 0, null)
        if (rc < 0) {
            lastError = "setAntenna($enable) rc=$rc"
            return false
        }
        return true
    }

    fun setTransceiverMode(mode: Int): Boolean {
        val rc = controlOut(REQ_SET_TRANSCEIVER_MODE, mode, 0, null)
        if (rc < 0) {
            lastError = "setMode($mode) rc=$rc"
            return false
        }
        return true
    }

    /**
     * Configure HackRF (only sending changed parameters) and capture [totalBytes]
     * of raw int8 interleaved I/Q data using queued asynchronous UsbRequest URBs
     * to prevent USB FIFO overruns at 10–20 MS/s.
     */
    @Synchronized
    fun captureIqChunk(
        freqHz: Long,
        sampleRateHz: Int,
        lnaGain: Int,
        vgaGain: Int,
        ampEnable: Boolean,
        totalBytes: Int
    ): ByteArray? {
        val conn = connection
        val ep = bulkInEndpoint
        if (conn == null || ep == null) {
            lastError = "USB not open"
            return null
        }

        if (sampleRateHz != cachedSampleRateHz) {
            if (!setSampleRate(sampleRateHz)) {
                resetConfigCache()
                return null
            }
            cachedSampleRateHz = sampleRateHz
        }
        if (freqHz != cachedFreqHz) {
            if (!setFrequency(freqHz)) {
                resetConfigCache()
                return null
            }
            cachedFreqHz = freqHz
        }
        if (lnaGain != cachedLnaGain) {
            if (!setLnaGain(lnaGain)) {
                resetConfigCache()
                return null
            }
            cachedLnaGain = lnaGain
        }
        if (vgaGain != cachedVgaGain) {
            if (!setVgaGain(vgaGain)) {
                resetConfigCache()
                return null
            }
            cachedVgaGain = vgaGain
        }
        if (ampEnable != cachedAmpEnable) {
            if (!setAmpEnable(ampEnable)) {
                resetConfigCache()
                return null
            }
            cachedAmpEnable = ampEnable
        }
        if (!cachedAntennaOff) {
            if (!setAntennaEnable(false)) { // Always off for passive 5.8 GHz antenna
                resetConfigCache()
                return null
            }
            cachedAntennaOff = true
        }

        // Start RX mode
        if (!setTransceiverMode(MODE_RECEIVE)) {
            resetConfigCache()
            return null
        }

        val out = ByteArray(totalBytes)
        var offset = captureAsync(conn, ep, out, totalBytes)
        if (offset < 0) {
            // Fallback to synchronous bulkTransfer if UsbRequest initialization/queuing failed
            Log.w(TAG, "Async UsbRequest failed ($lastError), falling back to bulkTransfer")
            clearEndpointHalt(ep)
            if (setTransceiverMode(MODE_RECEIVE)) {
                offset = captureSync(conn, ep, out, totalBytes)
            } else {
                offset = 0
            }
        }

        if (offset > 65536) {
            lastError = ""
            return if (offset == totalBytes) out else out.copyOf(offset)
        }
        if (lastError.isEmpty()) {
            lastError = "Short USB read: $offset / $totalBytes B"
        }
        Log.w(TAG, "captureIqChunk failed: $lastError")
        clearEndpointHalt(ep)
        resetConfigCache()
        return null
    }

    /**
     * Queued asynchronous UsbRequest capture (8 x 16 KB direct buffers in flight).
     * Returns bytes read (>= 0), or -1 if UsbRequest could not be initialized/queued.
     */
    private fun captureAsync(
        conn: UsbDeviceConnection,
        ep: UsbEndpoint,
        out: ByteArray,
        totalBytes: Int
    ): Int {
        val requests = ArrayList<UsbRequest>(QUEUE_DEPTH)
        val inFlight = HashSet<UsbRequest>(QUEUE_DEPTH)
        var offset = 0
        try {
            for (i in 0 until QUEUE_DEPTH) {
                val req = UsbRequest()
                if (!req.initialize(conn, ep)) {
                    lastError = "UsbRequest.initialize failed"
                    req.close()
                    return -1
                }
                req.clientData = ByteBuffer.allocateDirect(TRANSFER_SIZE)
                requests.add(req)
            }

            // Prime the USB URB pipeline
            for (req in requests) {
                val buf = req.clientData as ByteBuffer
                buf.clear()
                if (!req.queue(buf)) {
                    lastError = "UsbRequest.queue failed"
                    return -1
                }
                inFlight.add(req)
            }

            while (offset < totalBytes && inFlight.isNotEmpty()) {
                val done = try {
                    conn.requestWait(1500)
                } catch (_: TimeoutException) {
                    lastError = "USB timeout @ $offset/$totalBytes B"
                    break
                } catch (e: Exception) {
                    lastError = "requestWait err: ${e.message}"
                    break
                }

                if (done == null) {
                    lastError = "requestWait null @ $offset/$totalBytes B"
                    break
                }

                inFlight.remove(done)
                val buf = done.clientData as? ByteBuffer ?: continue
                val n = buf.position()
                if (n > 0) {
                    val toCopy = minOf(n, totalBytes - offset)
                    buf.flip()
                    buf.get(out, offset, toCopy)
                    offset += toCopy
                }

                // Re-queue if we still need more data
                if (offset + inFlight.size * TRANSFER_SIZE < totalBytes) {
                    buf.clear()
                    if (done.queue(buf)) {
                        inFlight.add(done)
                    }
                }
            }
            return offset
        } finally {
            setTransceiverMode(MODE_OFF)
            for (r in inFlight) {
                try { r.cancel() } catch (_: Exception) {}
            }
            // Drain cancelled URBs so they don't pollute the next capture
            var drain = inFlight.size
            while (drain > 0) {
                val r = try { conn.requestWait(50) } catch (_: Exception) { null } ?: break
                inFlight.remove(r)
                drain--
            }
            for (r in requests) {
                try { r.close() } catch (_: Exception) {}
            }
        }
    }

    /**
     * Synchronous bulkTransfer fallback.
     */
    private fun captureSync(
        conn: UsbDeviceConnection,
        ep: UsbEndpoint,
        out: ByteArray,
        totalBytes: Int
    ): Int {
        val chunkBuf = ByteArray(TRANSFER_SIZE)
        var offset = 0
        try {
            while (offset < totalBytes) {
                val n = conn.bulkTransfer(ep, chunkBuf, chunkBuf.size, 1500)
                if (n <= 0) {
                    lastError = "bulkTransfer rc=$n @ $offset/$totalBytes B"
                    break
                }
                val toCopy = minOf(n, totalBytes - offset)
                System.arraycopy(chunkBuf, 0, out, offset, toCopy)
                offset += toCopy
            }
        } finally {
            setTransceiverMode(MODE_OFF)
        }
        return offset
    }
}
