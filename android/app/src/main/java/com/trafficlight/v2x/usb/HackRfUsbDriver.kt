package com.trafficlight.v2x.usb

import android.content.Context
import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Direct Android USB-OTG driver for HackRF One (VID 0x1D50, PID 0x6089).
 * Uses standard libhackrf USB vendor control transfers — no root required.
 */
class HackRfUsbDriver(private val context: Context) {

    companion object {
        const val HACKRF_VID = 0x1D50
        const val HACKRF_ONE_PID = 0x6089
        const val HACKRF_JAWBREAKER_PID = 0x604B

        // libhackrf vendor requests
        private const val REQ_SET_TRANSCEIVER_MODE = 1
        private const val REQ_SAMPLE_RATE_SET = 6
        private const val REQ_BASEBAND_FILTER_BANDWIDTH_SET = 7
        private const val REQ_SET_FREQ = 16
        private const val REQ_AMP_ENABLE = 17
        private const val REQ_SET_LNA_GAIN = 19
        private const val REQ_SET_VGA_GAIN = 20
        private const val REQ_ANTENNA_ENABLE = 23

        private const val MODE_OFF = 0
        private const val MODE_RECEIVE = 1

        private const val USB_DIR_OUT = UsbConstants.USB_DIR_OUT or UsbConstants.USB_TYPE_VENDOR
        private const val USB_DIR_IN = UsbConstants.USB_DIR_IN or UsbConstants.USB_TYPE_VENDOR
    }

    private val usbManager = context.getSystemService(Context.USB_SERVICE) as UsbManager
    private var device: UsbDevice? = null
    private var connection: UsbDeviceConnection? = null
    private var usbInterface: UsbInterface? = null
    private var bulkInEndpoint: UsbEndpoint? = null

    val isOpen: Boolean get() = connection != null && bulkInEndpoint != null

    fun findHackRf(): UsbDevice? {
        return usbManager.deviceList.values.firstOrNull {
            it.vendorId == HACKRF_VID && (it.productId == HACKRF_ONE_PID || it.productId == HACKRF_JAWBREAKER_PID)
        }
    }

    fun hasPermission(dev: UsbDevice): Boolean = usbManager.hasPermission(dev)

    fun open(dev: UsbDevice): Boolean {
        close()
        val conn = usbManager.openDevice(dev) ?: return false
        val intf = dev.getInterface(0)
        if (!conn.claimInterface(intf, true)) {
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
            conn.releaseInterface(intf)
            conn.close()
            return false
        }
        device = dev
        connection = conn
        usbInterface = intf
        bulkInEndpoint = inEp
        return true
    }

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
    }

    private fun controlOut(request: Int, value: Int = 0, index: Int = 0, data: ByteArray? = null): Boolean {
        val conn = connection ?: return false
        val len = data?.size ?: 0
        return conn.controlTransfer(USB_DIR_OUT, request, value, index, data, len, 1000) >= 0
    }

    private fun controlIn(request: Int, value: Int = 0, index: Int = 0, length: Int = 1): ByteArray? {
        val conn = connection ?: return null
        val buf = ByteArray(length)
        val n = conn.controlTransfer(USB_DIR_IN, request, value, index, buf, length, 1000)
        return if (n >= 0) buf else null
    }

    fun setFrequency(freqHz: Long): Boolean {
        val mhz = (freqHz / 1_000_000L).toInt()
        val hz = (freqHz % 1_000_000L).toInt()
        val payload = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN)
            .putInt(mhz)
            .putInt(hz)
            .array()
        return controlOut(REQ_SET_FREQ, 0, 0, payload)
    }

    fun setSampleRate(sampleRateHz: Int): Boolean {
        val payload = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN)
            .putInt(sampleRateHz)
            .putInt(1)
            .array()
        val ok = controlOut(REQ_SAMPLE_RATE_SET, 0, 0, payload)
        // Also set baseband filter <= 0.75 * sampleRate
        val bw = if (sampleRateHz >= 20_000_000) 15_000_000 else 7_000_000
        setBasebandFilterBandwidth(bw)
        return ok
    }

    fun setBasebandFilterBandwidth(bwHz: Int): Boolean {
        val low = bwHz and 0xFFFF
        val high = (bwHz ushr 16) and 0xFFFF
        return controlOut(REQ_BASEBAND_FILTER_BANDWIDTH_SET, low, high, null)
    }

    fun setLnaGain(gainDb: Int): Boolean {
        val clamped = (gainDb.coerceIn(0, 40) / 8) * 8
        return controlIn(REQ_SET_LNA_GAIN, 0, clamped, 1) != null
    }

    fun setVgaGain(gainDb: Int): Boolean {
        val clamped = (gainDb.coerceIn(0, 62) / 2) * 2
        return controlIn(REQ_SET_VGA_GAIN, 0, clamped, 1) != null
    }

    fun setAmpEnable(enable: Boolean): Boolean {
        return controlOut(REQ_AMP_ENABLE, if (enable) 1 else 0, 0, null)
    }

    fun setAntennaEnable(enable: Boolean): Boolean {
        return controlOut(REQ_ANTENNA_ENABLE, if (enable) 1 else 0, 0, null)
    }

    fun setTransceiverMode(mode: Int): Boolean {
        return controlOut(REQ_SET_TRANSCEIVER_MODE, mode, 0, null)
    }

    /**
     * Configure HackRF and capture [totalBytes] of raw int8 interleaved I/Q data.
     */
    fun captureIqChunk(
        freqHz: Long,
        sampleRateHz: Int,
        lnaGain: Int,
        vgaGain: Int,
        ampEnable: Boolean,
        totalBytes: Int
    ): ByteArray? {
        val conn = connection ?: return null
        val ep = bulkInEndpoint ?: return null

        setSampleRate(sampleRateHz)
        setFrequency(freqHz)
        setLnaGain(lnaGain)
        setVgaGain(vgaGain)
        setAmpEnable(ampEnable)
        setAntennaEnable(false) // Always off for passive 5.8 GHz antenna

        if (!setTransceiverMode(MODE_RECEIVE)) return null

        val out = ByteArray(totalBytes)
        val chunkBuf = ByteArray(16384)
        var offset = 0
        try {
            while (offset < totalBytes) {
                val n = conn.bulkTransfer(ep, chunkBuf, chunkBuf.size, 1500)
                if (n <= 0) break
                val toCopy = minOf(n, totalBytes - offset)
                System.arraycopy(chunkBuf, 0, out, offset, toCopy)
                offset += toCopy
            }
        } finally {
            setTransceiverMode(MODE_OFF)
        }
        return if (offset > 65536) out.copyOf(offset) else null
    }
}
