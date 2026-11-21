package com.trafficlight.v2x.storage

import android.content.Context
import java.io.File
import java.io.FileOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class CaptureLogger(private val context: Context) {
    private val dir: File by lazy {
        File(context.getExternalFilesDir(null), "v2x_captures").apply { mkdirs() }
    }
    private val pcapFile: File by lazy { File(dir, "dsrc_decoded.pcap") }
    private val gpsCsvFile: File by lazy { File(dir, "hits_gps.csv") }
    private val tsFmt = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US)
    private val isoFmt = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)

    val captureDirPath: String get() = dir.absolutePath

    @Synchronized
    fun clearAllCaptures(): Int {
        var deleted = 0
        dir.listFiles()?.forEach { f ->
            if (f.isFile && f.delete()) deleted++
        }
        return deleted
    }

    @Synchronized
    fun logGpsHit(
        lat: Double?,
        lon: Double?,
        freqMhz: Double,
        protocol: String,
        peakDbfs: Float,
        summary: String,
        iqFileName: String = ""
    ) {
        val writeHeader = !gpsCsvFile.exists() || gpsCsvFile.length() == 0L
        FileOutputStream(gpsCsvFile, true).use { fos ->
            if (writeHeader) {
                fos.write("timestamp,latitude,longitude,freq_mhz,protocol,peak_dbfs,summary,iq_file\n".toByteArray())
            }
            val safeSum = summary.replace(",", ";").replace("\n", " ")
            val latStr = lat?.let { "%.6f".format(it) } ?: ""
            val lonStr = lon?.let { "%.6f".format(it) } ?: ""
            val line = "${isoFmt.format(Date())},$latStr,$lonStr,${"%.2f".format(freqMhz)},$protocol,${"%.1f".format(peakDbfs)},$safeSum,$iqFileName\n"
            fos.write(line.toByteArray())
        }
    }

    fun saveIqBurst(iqBytes: ByteArray, freqMhz: Double): File {
        val name = "burst_${tsFmt.format(Date())}_${freqMhz.toInt()}MHz.iq"
        val f = File(dir, name)
        f.writeBytes(iqBytes)
        return f
    }

    @Synchronized
    fun appendPcapFrame(frame: ByteArray) {
        val writeHeader = !pcapFile.exists() || pcapFile.length() == 0L
        FileOutputStream(pcapFile, true).use { fos ->
            if (writeHeader) {
                // Global PCAP header (LINKTYPE_IEEE802_11 = 105)
                val hdr = ByteBuffer.allocate(24).order(ByteOrder.LITTLE_ENDIAN)
                    .putInt(0xA1B2C3D4.toInt())
                    .putShort(2)
                    .putShort(4)
                    .putInt(0)
                    .putInt(0)
                    .putInt(65535)
                    .putInt(105)
                    .array()
                fos.write(hdr)
            }
            val nowMs = System.currentTimeMillis()
            val sec = (nowMs / 1000L).toInt()
            val usec = ((nowMs % 1000L) * 1000L).toInt()
            val pktHdr = ByteBuffer.allocate(16).order(ByteOrder.LITTLE_ENDIAN)
                .putInt(sec)
                .putInt(usec)
                .putInt(frame.size)
                .putInt(frame.size)
                .array()
            fos.write(pktHdr)
            fos.write(frame)
        }
    }
}
