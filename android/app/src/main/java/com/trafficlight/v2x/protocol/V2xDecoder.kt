package com.trafficlight.v2x.protocol

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

enum class LightColor {
    RED, YELLOW, GREEN, PROTECTED_GREEN, FLASHING_RED, DARK
}

data class SignalPhaseState(
    val signalGroup: Int,
    val label: String,
    val color: LightColor,
    val rawEventState: String,
    val countdownSec: Float?
)

data class SpatMessage(
    val intersectionId: Int,
    val intersectionName: String,
    val phases: List<SignalPhaseState>
)

data class DecodedPacket(
    val id: Long,
    val timestamp: String,
    val freqMhz: Double,
    val rssiDbfs: Float,
    val protocol: String,      // "SPaT", "MAP", "BSM", "CAM", "DENM", "SPATEM", "MAPEM", "WSMP", "GN", "802.11"
    val summary: String,
    val details: Map<String, String>,
    val hexPreview: String,
    val spat: SpatMessage? = null,
    val gpsLat: Double? = null,
    val gpsLon: Double? = null
)

/**
 * Unaligned Packed Encoding Rules (ASN.1 UPER) bit-level reader.
 */
class UperBitReader(private val data: ByteArray, startByte: Int = 0) {
    var bitPos: Int = startByte * 8
        private set
    val totalBits: Int = data.size * 8
    val remainingBits: Int get() = (totalBits - bitPos).coerceAtLeast(0)

    fun readBit(): Int {
        if (bitPos >= totalBits) return 0
        val b = data[bitPos ushr 3].toInt() and 0xFF
        val shift = 7 - (bitPos and 7)
        bitPos++
        return (b ushr shift) and 1
    }

    fun readBits(count: Int): Long {
        if (count <= 0) return 0L
        var v = 0L
        for (i in 0 until count) {
            v = (v shl 1) or readBit().toLong()
        }
        return v
    }

    fun readInt(count: Int): Int = readBits(count).toInt()

    fun readSignedInt(count: Int, offset: Long): Long = readBits(count) + offset

    fun readNormallySmallLength(): Int {
        val flag = readBit()
        return if (flag == 0) readInt(6) else readLengthDeterminant()
    }

    fun readLengthDeterminant(): Int {
        val b0 = readInt(8)
        return when {
            (b0 and 0x80) == 0 -> b0
            (b0 and 0xC0) == 0x80 -> ((b0 and 0x3F) shl 8) or readInt(8)
            else -> 0
        }
    }

    fun skip(bits: Int) {
        bitPos = (bitPos + bits).coerceAtMost(totalBits)
    }
}

/**
 * Simple UPER bit-level writer (used for building genuine UPER test/demo frames).
 */
class UperBitWriter {
    private val bytes = ArrayList<Byte>()
    private var curByte = 0
    private var bitCount = 0

    fun writeBit(bit: Int) {
        curByte = (curByte shl 1) or (bit and 1)
        bitCount++
        if (bitCount == 8) {
            bytes.add(curByte.toByte())
            curByte = 0
            bitCount = 0
        }
    }

    fun writeBits(value: Long, count: Int) {
        for (i in count - 1 downTo 0) {
            writeBit(((value ushr i) and 1L).toInt())
        }
    }

    fun toByteArray(): ByteArray {
        val out = ArrayList(bytes)
        if (bitCount > 0) {
            out.add((curByte shl (8 - bitCount)).toByte())
        }
        return out.toByteArray()
    }
}

object V2xDecoder {

    private val timeFmt = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)
    private var nextId = 1L

    /**
     * Decode a CRC-verified IEEE 802.11 MAC frame into:
     *  - US DSRC: 802.11p -> LLC/SNAP 0x88DC -> WSMP (IEEE 1609.3) -> SAE J2735 UPER (SPaT, MAP, BSM)
     *  - EU ITS-G5: 802.11p -> LLC/SNAP 0x8947 -> GeoNetworking -> BTP -> ETSI ITS (CAM, DENM, SPATEM, MAPEM)
     *  - General 802.11: Beacons (SSID), Data, Control frames
     */
    fun decode80211Frame(
        frame: ByteArray,
        freqMhz: Double,
        rssiDbfs: Float,
        gpsLat: Double? = null,
        gpsLon: Double? = null
    ): DecodedPacket {
        val ts = timeFmt.format(Date())
        val hex = frame.take(56).joinToString(" ") { "%02X".format(it) }
        val details = linkedMapOf<String, String>()
        details["Length"] = "${frame.size} bytes"
        details["Frequency"] = "%.2f MHz".format(freqMhz)
        details["RSSI"] = "%.1f dBFS".format(rssiDbfs)
        if (gpsLat != null && gpsLon != null) {
            details["Receiver GPS"] = "%.6f, %.6f".format(gpsLat, gpsLon)
        }

        if (frame.size < 24) {
            return DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "802.11", "Short 802.11 frame (${frame.size}B)", details, hex, null, gpsLat, gpsLon)
        }

        val fc0 = frame[0].toInt() and 0xFF
        val type = (fc0 shr 2) and 0x03
        val subtype = (fc0 shr 4) and 0x0F
        val dstMac = formatMac(frame, 4)
        val srcMac = formatMac(frame, 10)
        val bssid = formatMac(frame, 16)
        details["802.11 Type"] = when (type) {
            0 -> "Management (sub=$subtype)"
            1 -> "Control (sub=$subtype)"
            2 -> "Data (sub=$subtype)"
            else -> "Other ($type/$subtype)"
        }
        details["Src MAC"] = srcMac
        details["Dst MAC"] = dstMac
        details["BSSID"] = bssid

        // Beacon frame (Mgmt subtype 8)
        if (type == 0 && subtype == 8 && frame.size > 38) {
            val ssid = extractBeaconSsid(frame)
            if (ssid != null) {
                details["SSID"] = ssid
                return DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "802.11", "Beacon SSID=\"$ssid\" ($srcMac)", details, hex, null, gpsLat, gpsLon)
            }
        }

        // Data frame: inspect LLC/SNAP EtherType
        if (type == 2) {
            val hdrLen = if ((subtype and 0x08) != 0) 26 else 24
            if (frame.size > hdrLen + 8 &&
                (frame[hdrLen].toInt() and 0xFF) == 0xAA &&
                (frame[hdrLen + 1].toInt() and 0xFF) == 0xAA
            ) {
                val etherType = ((frame[hdrLen + 6].toInt() and 0xFF) shl 8) or (frame[hdrLen + 7].toInt() and 0xFF)
                val payload = frame.copyOfRange(hdrLen + 8, frame.size)
                details["EtherType"] = "0x%04X".format(etherType)
                return when (etherType) {
                    0x88DC -> decodeWsmp(payload, ts, freqMhz, rssiDbfs, details, hex, gpsLat, gpsLon)
                    0x8947 -> decodeGeoNetworking(payload, ts, freqMhz, rssiDbfs, details, hex, gpsLat, gpsLon)
                    else -> DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "802.11", "802.11 Data EtherType=0x%04X ($srcMac)".format(etherType), details, hex, null, gpsLat, gpsLon)
                }
            }
        }

        return DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "802.11", "802.11 ${details["802.11 Type"]} ($srcMac)", details, hex, null, gpsLat, gpsLon)
    }

    // =========================================================================
    // US DSRC: WSMP (IEEE 1609.3) + SAE J2735 UPER (SPaT, MAP, BSM)
    // =========================================================================
    private fun decodeWsmp(
        wsmp: ByteArray,
        ts: String,
        freqMhz: Double,
        rssiDbfs: Float,
        details: MutableMap<String, String>,
        hex: String,
        gpsLat: Double?,
        gpsLon: Double?
    ): DecodedPacket {
        if (wsmp.size < 4) {
            return DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "WSMP", "Short WSMP (${wsmp.size}B)", details, hex, null, gpsLat, gpsLon)
        }

        var idx = 0
        val subtypeVer = wsmp[idx++].toInt() and 0xFF
        val version = subtypeVer and 0x07
        details["WSMP Version"] = "$version"

        if ((subtypeVer and 0x08) != 0 && idx < wsmp.size) {
            val extCount = wsmp[idx++].toInt() and 0xFF
            repeat(extCount) {
                if (idx + 1 < wsmp.size) {
                    idx++
                    val extLen = wsmp[idx++].toInt() and 0xFF
                    idx += extLen
                }
            }
        }
        if (idx >= wsmp.size) {
            return DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "WSMP", "WSMP (header only)", details, hex, null, gpsLat, gpsLon)
        }

        idx++ // TPID
        var psid = 0
        if (idx < wsmp.size) {
            val b0 = wsmp[idx].toInt() and 0xFF
            when {
                (b0 and 0x80) == 0 -> { psid = b0; idx += 1 }
                (b0 and 0xC0) == 0x80 && idx + 1 < wsmp.size -> {
                    psid = ((b0 and 0x3F) shl 8) or (wsmp[idx + 1].toInt() and 0xFF); idx += 2
                }
                (b0 and 0xE0) == 0xC0 && idx + 2 < wsmp.size -> {
                    psid = ((b0 and 0x1F) shl 16) or ((wsmp[idx + 1].toInt() and 0xFF) shl 8) or (wsmp[idx + 2].toInt() and 0xFF); idx += 3
                }
                idx + 3 < wsmp.size -> {
                    psid = ((b0 and 0x0F) shl 24) or ((wsmp[idx + 1].toInt() and 0xFF) shl 16) or
                        ((wsmp[idx + 2].toInt() and 0xFF) shl 8) or (wsmp[idx + 3].toInt() and 0xFF); idx += 4
                }
            }
        }
        details["WSMP PSID"] = "0x%X".format(psid)

        if (idx < wsmp.size) {
            val lenByte = wsmp[idx++].toInt() and 0xFF
            if ((lenByte and 0x80) != 0 && idx < wsmp.size) idx++
        }

        if (idx < wsmp.size) {
            val j2735 = unwrapIeee16092(wsmp.copyOfRange(idx, wsmp.size), details)
            val parsed = tryParseJ2735Uper(j2735, details)
            if (parsed != null) {
                val (proto, summary, spat) = parsed
                return DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, proto, summary, details, hex, spat, gpsLat, gpsLon)
            }
        }

        return DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "WSMP", "WSMP PSID=0x%X (${wsmp.size}B)".format(psid), details, hex, null, gpsLat, gpsLon)
    }

    /**
     * Unwrap optional IEEE 1609.2 UnsecuredData (0x03 0x80 ...) or SignedData header to locate inner J2735 MessageFrame.
     */
    private fun unwrapIeee16092(data: ByteArray, details: MutableMap<String, String>): ByteArray {
        if (data.size >= 4 && (data[0].toInt() and 0xFF) == 0x03) {
            val contentChoice = data[1].toInt() and 0xFF
            if (contentChoice == 0x80) {
                details["1609.2 Security"] = "UnsecuredData"
                var off = 2
                val lenB = data[off++].toInt() and 0xFF
                if ((lenB and 0x80) != 0 && off < data.size) off++
                return data.copyOfRange(off, data.size)
            } else if (contentChoice == 0x81) {
                details["1609.2 Security"] = "SignedData (Plaintext Payload)"
                // Scan for inner SAE J2735 MessageFrame IDs (18,19,20,22,27,28,29,30,31,32) within first 40 bytes
                val validIds = setOf(18, 19, 20, 22, 27, 28, 29, 30, 31, 32)
                for (i in 2 until minOf(40, data.size - 3)) {
                    if (data[i].toInt() == 0x00 && (data[i + 1].toInt() and 0xFF) in validIds) {
                        return data.copyOfRange(i, data.size)
                    }
                }
            }
        }
        return data
    }

    private fun tryParseJ2735Uper(data: ByteArray, details: MutableMap<String, String>): Triple<String, String, SpatMessage?>? {
        if (data.size < 4) return null
        val msgId = ((data[0].toInt() and 0xFF) shl 8) or (data[1].toInt() and 0xFF)
        val validIds = setOf(18, 19, 20, 22, 27, 28, 29, 30, 31, 32)
        if (msgId !in validIds) return null
        details["SAE J2735 MsgID"] = "$msgId"

        // Open-type length determinant after 16-bit messageId
        var off = 2
        val lenB = data[off++].toInt() and 0xFF
        if ((lenB and 0x80) != 0 && off < data.size) off++

        val br = UperBitReader(data, off)
        return when (msgId) {
            20 -> parseUperBsm(br, details)
            19 -> parseUperSpat(br, details)
            18 -> parseUperMap(br, details)
            31 -> { // TIM (Traveler Information Message)
                val msgCnt = if (br.remainingBits >= 8) { br.readBit(); br.readInt(7) } else 0
                details["Message"] = "Traveler Information Message (Work Zone / WWD / Advisory)"
                details["Msg Count"] = "$msgCnt"
                Triple("TIM", "V2I TIM Advisory (cnt=$msgCnt, ${data.size}B)", null)
            }
            29 -> { // SRM (Signal Request Message)
                details["Message"] = "Transit / Emergency Signal Priority Request (SRM)"
                Triple("SRM", "V2I SRM Signal Priority Request (${data.size}B)", null)
            }
            30 -> { // SSM (Signal Status Message)
                details["Message"] = "Intersection Signal Priority Status (SSM)"
                Triple("SSM", "V2I SSM Signal Priority Status (${data.size}B)", null)
            }
            28 -> { // RTCM Corrections
                details["Message"] = "RTCM Differential GPS Corrections (RSU)"
                Triple("RTCM", "V2I RTCM GPS Correction (${data.size}B)", null)
            }
            32 -> { // PSM (Personal Safety Message)
                details["Message"] = "Pedestrian / Cyclist Personal Safety Message (PSM)"
                Triple("PSM", "V2P PSM Pedestrian/Cyclist Alert (${data.size}B)", null)
            }
            22 -> { // EVA (Emergency Vehicle Alert)
                details["Message"] = "Emergency Vehicle Alert (Police/Fire/EMS)"
                Triple("EVA", "V2V EVA Emergency Vehicle Alert (${data.size}B)", null)
            }
            27 -> { // RSA (Road Side Alert)
                details["Message"] = "Road Side Hazard Alert (RSA)"
                Triple("RSA", "V2I RSA Roadside Hazard Alert (${data.size}B)", null)
            }
            else -> null
        }
    }

    /**
     * Real SAE J2735 ASN.1 UPER BSM (BasicSafetyMessage, MsgID 20) decoder.
     */
    private fun parseUperBsm(br: UperBitReader, details: MutableMap<String, String>): Triple<String, String, SpatMessage?> {
        if (br.remainingBits < 240) {
            return Triple("BSM", "Vehicle BSM (truncated)", null)
        }
        br.readBit() // extension marker
        val hasPartII = br.readBit() == 1
        val hasRegional = br.readBit() == 1

        // BSMcoreData (fixed SAE J2735 UPER bit layout)
        val msgCnt = br.readInt(7)
        val tempId = br.readBits(32)
        val secMark = br.readInt(16)
        val lat = br.readSignedInt(31, -900_000_000L) / 1e7
        val lon = br.readSignedInt(32, -1_799_999_999L) / 1e7
        val elev = br.readSignedInt(16, -4096L) * 0.1
        br.skip(32) // PositionalAccuracy (8 + 8 + 16)
        val trans = br.readInt(3)
        val speedRaw = br.readInt(13) // 0.02 m/s
        val speedMs = speedRaw * 0.02
        val speedMph = speedMs * 2.23694
        val heading = br.readInt(15) * 0.0125
        val steerAngle = br.readSignedInt(8, -126L) * 1.5
        br.skip(48) // AccelerationSet4Way
        val wheelBrakes = br.readInt(5)
        br.skip(10) // traction(2)+abs(2)+scs(2)+brakeBoost(2)+auxBrakes(2)
        val vehWidth = br.readInt(10) // cm
        val vehLength = br.readInt(12) // cm

        val transStr = when (trans) {
            0 -> "Neutral"; 1 -> "Park"; 2 -> "Forward"; 3 -> "Reverse"; else -> "Unavailable"
        }
        val braking = if ((wheelBrakes and 0x0F) != 0) "APPLIED (0x%02X)".format(wheelBrakes) else "Off"

        details["BSM Temp ID"] = "0x%08X".format(tempId)
        details["Msg Count / SecMark"] = "$msgCnt / ${secMark}ms"
        details["Vehicle GPS"] = "%.6f, %.6f (elev %.1fm)".format(lat, lon, elev)
        details["Speed"] = "%.1f mph (%.1f km/h)".format(speedMph, speedMs * 3.6)
        details["Heading / Steer"] = "%.1f° / %.1f°".format(heading, steerAngle)
        details["Transmission"] = transStr
        details["Brakes"] = braking
        details["Vehicle Size"] = "${vehWidth}cm W × ${vehLength}cm L"
        if (hasPartII) details["Part II Ext"] = "Present"
        if (hasRegional) details["Regional Ext"] = "Present"

        val summary = "V2V BSM 0x%08X · %.1f mph · Hdg %.0f° · Brakes %s".format(tempId, speedMph, heading, if ((wheelBrakes and 0x0F) != 0) "ON" else "off")
        return Triple("BSM", summary, null)
    }

    /**
     * Real SAE J2735 ASN.1 UPER SPaT (MsgID 19) decoder.
     */
    private fun parseUperSpat(br: UperBitReader, details: MutableMap<String, String>): Triple<String, String, SpatMessage?> {
        if (br.remainingBits < 32) {
            return Triple("SPaT", "SPaT (short)", null)
        }
        br.readBit() // SPAT extension marker
        val hasTimeStamp = br.readBit() == 1
        val hasName = br.readBit() == 1
        val hasRegional = br.readBit() == 1
        if (hasTimeStamp) br.skip(20) // MinuteOfTheYear (0..527040 -> 20 bits)
        var spatName = ""
        if (hasName) {
            val nLen = br.readInt(6) + 1 // DescriptiveName 1..63 IA5String (7 bits/char)
            val sb = StringBuilder()
            repeat(nLen) { sb.append(br.readInt(7).toChar()) }
            spatName = sb.toString().trim()
        }

        // IntersectionStateList (SIZE 1..32 -> 5 bits)
        val interCount = br.readInt(5) + 1
        var firstSpat: SpatMessage? = null
        val defaultLabels = listOf("NB Through", "NB Left", "EB Through", "EB Left", "SB Through", "WB Through")

        for (i in 0 until interCount.coerceAtMost(4)) {
            if (br.remainingBits < 32) break
            br.readBit() // IntersectionState extension
            val iHasName = br.readBit() == 1
            val iHasMoy = br.readBit() == 1
            val iHasTs = br.readBit() == 1
            val iHasEnabledLanes = br.readBit() == 1
            val iHasManeuver = br.readBit() == 1
            val iHasRegional = br.readBit() == 1

            var iName = spatName
            if (iHasName) {
                val nLen = br.readInt(6) + 1
                val sb = StringBuilder()
                repeat(nLen) { sb.append(br.readInt(7).toChar()) }
                iName = sb.toString().trim()
            }
            // IntersectionReferenceID (optional region 16b + id 16b)
            val hasRegion = br.readBit() == 1
            if (hasRegion) br.skip(16)
            val interId = br.readInt(16)
            val rev = br.readInt(7) // MsgCount
            br.skip(16) // IntersectionStatusObject (16 bits)
            if (iHasMoy) br.skip(20)
            if (iHasTs) br.skip(16)
            if (iHasEnabledLanes) {
                val lc = br.readInt(4) + 1
                br.skip(lc * 8)
            }

            // MovementList (SIZE 1..255 -> 8 bits)
            val moveCount = (br.readInt(8) + 1).coerceAtMost(12)
            val phases = mutableListOf<SignalPhaseState>()
            for (m in 0 until moveCount) {
                if (br.remainingBits < 16) break
                br.readBit() // MovementState extension
                val mHasName = br.readBit() == 1
                val mHasManeuver = br.readBit() == 1
                var mLabel = defaultLabels.getOrElse(m) { "Phase ${m + 1}" }
                if (mHasName) {
                    val ml = br.readInt(6) + 1
                    val sb = StringBuilder()
                    repeat(ml) { sb.append(br.readInt(7).toChar()) }
                    mLabel = sb.toString().trim()
                }
                val sg = br.readInt(8) // SignalGroupID (0..255)
                // MovementEventList (SIZE 1..16 -> 4 bits)
                val evCount = (br.readInt(4) + 1).coerceAtMost(4)
                var firstEvState = 0
                var firstCountdown: Float? = null
                for (e in 0 until evCount) {
                    if (br.remainingBits < 8) break
                    br.readBit() // MovementEvent extension
                    val eHasTiming = br.readBit() == 1
                    val eHasSpeeds = br.readBit() == 1
                    val evState = br.readInt(4) // MovementPhaseState (0..9)
                    var cdSec: Float? = null
                    if (eHasTiming && br.remainingBits >= 20) {
                        val tHasStart = br.readBit() == 1
                        val tHasMaxEnd = br.readBit() == 1
                        val tHasLikely = br.readBit() == 1
                        val tHasConf = br.readBit() == 1
                        val tHasNext = br.readBit() == 1
                        if (tHasStart) br.skip(16)
                        val minEndTenths = br.readInt(16) // TimeMark (0..36001 in tenths of sec)
                        if (minEndTenths in 0..36000) cdSec = minEndTenths / 10.0f
                        if (tHasMaxEnd) br.skip(16)
                        if (tHasLikely) br.skip(16)
                        if (tHasConf) br.skip(4)
                        if (tHasNext) br.skip(16)
                    }
                    if (e == 0) {
                        firstEvState = evState
                        firstCountdown = cdSec
                    }
                    if (eHasSpeeds) break
                }
                val (color, stateStr) = mapEventState(firstEvState)
                phases.add(SignalPhaseState(sg, mLabel, color, stateStr, firstCountdown))
                if (mHasManeuver) break
            }

            val finalName = iName.ifEmpty { "Intersection #$interId" }
            val spatObj = SpatMessage(interId, finalName, phases)
            if (firstSpat == null) firstSpat = spatObj
            details["Intersection ID"] = "$interId (rev $rev)"
            details["Intersection"] = finalName
            phases.forEach { ph ->
                val cd = ph.countdownSec?.let { " (${"%.1f".format(it)}s)" } ?: ""
                details["SG ${ph.signalGroup} (${ph.label})"] = "${ph.rawEventState}$cd"
            }
            if (iHasManeuver || iHasRegional || hasRegional) break
        }

        val primary = firstSpat?.phases?.firstOrNull()
        val summary = if (firstSpat != null && primary != null) {
            "V2I SPaT #${firstSpat.intersectionId} (${firstSpat.intersectionName}): SG${primary.signalGroup}=${primary.color.name} (${primary.countdownSec?.let { "%.1fs".format(it) } ?: "--"})"
        } else {
            "V2I SPaT Message"
        }
        return Triple("SPaT", summary, firstSpat)
    }

    /**
     * Real SAE J2735 ASN.1 UPER MAP (MsgID 18) decoder.
     */
    private fun parseUperMap(br: UperBitReader, details: MutableMap<String, String>): Triple<String, String, SpatMessage?> {
        if (br.remainingBits < 32) return Triple("MAP", "V2I MAP (short)", null)
        br.readBit() // ext
        val hasTs = br.readBit() == 1
        val hasLayerType = br.readBit() == 1
        val hasLayerId = br.readBit() == 1
        val hasIntersections = br.readBit() == 1
        br.skip(4) // remaining optional flags
        if (hasTs) br.skip(20)
        val rev = br.readInt(7)
        if (hasLayerType) br.skip(4)
        if (hasLayerId) br.skip(7)
        var interId = 0
        var lat = 0.0
        var lon = 0.0
        if (hasIntersections && br.remainingBits >= 64) {
            br.readInt(5) // count - 1
            br.readBit() // ext
            val iHasName = br.readBit() == 1
            val iHasLaneWidth = br.readBit() == 1
            val iHasSpeedLimits = br.readBit() == 1
            val iHasPreempt = br.readBit() == 1
            val iHasRegional = br.readBit() == 1
            if (iHasName) {
                val nl = br.readInt(6) + 1
                val sb = StringBuilder()
                repeat(nl) { sb.append(br.readInt(7).toChar()) }
                details["Intersection"] = sb.toString().trim()
            }
            if (br.readBit() == 1) br.skip(16) // optional region
            interId = br.readInt(16)
            br.skip(7) // revision
            // Position3D refPoint
            br.readBit() // ext
            val hasElev = br.readBit() == 1
            val hasReg = br.readBit() == 1
            lat = br.readSignedInt(31, -900_000_000L) / 1e7
            lon = br.readSignedInt(32, -1_799_999_999L) / 1e7
            if (hasElev) br.skip(16)
            if (hasReg || iHasLaneWidth || iHasSpeedLimits || iHasPreempt || iHasRegional) { /* ok */ }
        }
        details["Intersection ID"] = "$interId (rev $rev)"
        if (lat != 0.0 || lon != 0.0) details["Ref GPS"] = "%.6f, %.6f".format(lat, lon)
        return Triple("MAP", "V2I MAP Intersection #$interId (%.5f, %.5f)".format(lat, lon), null)
    }

    // =========================================================================
    // EU ITS-G5: GeoNetworking (EtherType 0x8947) -> BTP -> ETSI CAM / DENM / SPATEM / MAPEM
    // =========================================================================
    private fun decodeGeoNetworking(
        gn: ByteArray,
        ts: String,
        freqMhz: Double,
        rssiDbfs: Float,
        details: MutableMap<String, String>,
        hex: String,
        gpsLat: Double?,
        gpsLon: Double?
    ): DecodedPacket {
        if (gn.size < 16) {
            return DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "GN", "Short GeoNetworking (${gn.size}B)", details, hex, null, gpsLat, gpsLon)
        }
        // Basic Header (4 bytes): version(4) + NH(4) + reserved(8) + LT(8) + RHL(8)
        val gnVer = (gn[0].toInt() ushr 4) and 0x0F
        val basicNh = gn[0].toInt() and 0x0F
        details["GeoNet Version"] = "$gnVer"

        // Common Header starts at byte 4 (8 bytes)
        var off = 4
        if (basicNh == 2) {
            details["GeoNet Security"] = "Secured (ETSI 103 097)"
            return DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "GN", "Secured GeoNetworking Packet (${gn.size}B)", details, hex, null, gpsLat, gpsLon)
        }
        if (gn.size < off + 8) {
            return DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "GN", "GeoNetworking Header Only", details, hex, null, gpsLat, gpsLon)
        }
        val commonNh = (gn[off].toInt() ushr 4) and 0x0F // 1=BTP-A, 2=BTP-B
        val ht = (gn[off + 1].toInt() ushr 4) and 0x0F   // 5=TSB(SHB), 4=GBC
        val hst = gn[off + 1].toInt() and 0x0F
        val payloadLen = ((gn[off + 4].toInt() and 0xFF) shl 8) or (gn[off + 5].toInt() and 0xFF)
        details["GeoNet Type"] = when (ht) {
            1 -> "Beacon"; 4 -> "GeoBroadcast (GBC)"; 5 -> "Single-Hop Broadcast (SHB)"; else -> "HT=$ht/$hst"
        }

        // Extended header length: SHB = 28 bytes (24B LongPositionVector + 4B reserved), GBC = 44 bytes
        val extLen = when (ht) {
            5 -> 28 // SHB (CAM)
            4 -> 44 // GBC (DENM)
            1 -> 24 // Beacon
            else -> 28
        }
        val btpOff = off + 8 + extLen
        if ((commonNh == 1 || commonNh == 2) && gn.size >= btpOff + 10) {
            val destPort = ((gn[btpOff].toInt() and 0xFF) shl 8) or (gn[btpOff + 1].toInt() and 0xFF)
            details["BTP Dest Port"] = "$destPort"
            val itsOff = btpOff + 4 // 4-byte BTP header -> ETSI ITS PDU Header
            if (gn.size >= itsOff + 6) {
                val pduVer = gn[itsOff].toInt() and 0xFF
                val msgId = gn[itsOff + 1].toInt() and 0xFF
                val stationId = ((gn[itsOff + 2].toLong() and 0xFF) shl 24) or
                    ((gn[itsOff + 3].toLong() and 0xFF) shl 16) or
                    ((gn[itsOff + 4].toLong() and 0xFF) shl 8) or
                    (gn[itsOff + 5].toLong() and 0xFF)
                details["ETSI ITS PDU"] = "v$pduVer, MsgID=$msgId"
                details["Station ID"] = "$stationId"

                val br = UperBitReader(gn, itsOff + 6)
                return when (msgId) {
                    2 -> { // ETSI CAM (Cooperative Awareness Message)
                        val genDelta = if (br.remainingBits >= 16) br.readInt(16) else 0
                        val stationType = if (br.remainingBits >= 8) br.readInt(8) else 0
                        val lat = if (br.remainingBits >= 31) br.readSignedInt(31, -900_000_000L) / 1e7 else 0.0
                        val lon = if (br.remainingBits >= 32) br.readSignedInt(32, -1_800_000_000L) / 1e7 else 0.0
                        val speedKmh = if (br.remainingBits >= 14) (br.readInt(14) * 0.01 * 3.6) else 0.0
                        val heading = if (br.remainingBits >= 12) (br.readInt(12) * 0.1) else 0.0
                        val stName = when (stationType) {
                            5 -> "Passenger Car"; 6 -> "Bus"; 10 -> "Special/Emergency"; 15 -> "RSU"; else -> "Type $stationType"
                        }
                        details["Station Type"] = stName
                        details["Gen DeltaTime"] = "${genDelta}ms"
                        details["Position"] = "%.6f, %.6f".format(lat, lon)
                        details["Speed / Heading"] = "%.1f km/h · %.1f°".format(speedKmh, heading)
                        DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "CAM", "ETSI CAM #$stationId ($stName) · %.1f km/h · %.0f°".format(speedKmh, heading), details, hex, null, gpsLat, gpsLon)
                    }
                    1 -> { // ETSI DENM (Decentralized Environmental Notification Message)
                        val causeCode = if (br.remainingBits >= 8) br.readInt(8) else 0
                        val subCause = if (br.remainingBits >= 8) br.readInt(8) else 0
                        val lat = if (br.remainingBits >= 31) br.readSignedInt(31, -900_000_000L) / 1e7 else 0.0
                        val lon = if (br.remainingBits >= 32) br.readSignedInt(32, -1_800_000_000L) / 1e7 else 0.0
                        val causeStr = when (causeCode) {
                            1 -> "Traffic Condition / Jam"
                            2 -> "Accident"
                            3 -> "Roadworks"
                            6 -> "Adverse Weather"
                            91 -> "Vehicle Breakdown"
                            94 -> "Stationary Vehicle"
                            95 -> "Emergency Vehicle Approaching"
                            99 -> "Dangerous Situation"
                            else -> "Cause #$causeCode"
                        }
                        details["Hazard Cause"] = "$causeStr (sub=$subCause)"
                        details["Event GPS"] = "%.6f, %.6f".format(lat, lon)
                        DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "DENM", "ETSI DENM #$stationId · $causeStr", details, hex, null, gpsLat, gpsLon)
                    }
                    4 -> DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "SPATEM", "ETSI SPATEM RSU #$stationId ($payloadLen B)", details, hex, null, gpsLat, gpsLon)
                    5 -> DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "MAPEM", "ETSI MAPEM RSU #$stationId ($payloadLen B)", details, hex, null, gpsLat, gpsLon)
                    else -> DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "GN", "ETSI ITS MsgID=$msgId Station #$stationId", details, hex, null, gpsLat, gpsLon)
                }
            }
        }
        return DecodedPacket(nextId++, ts, freqMhz, rssiDbfs, "GN", "GeoNetworking ${details["GeoNet Type"]} (${gn.size}B)", details, hex, null, gpsLat, gpsLon)
    }

    private fun mapEventState(code: Int): Pair<LightColor, String> = when (code) {
        1 -> LightColor.DARK to "dark"
        2 -> LightColor.FLASHING_RED to "stop-Then-Proceed (Flashing Red)"
        3 -> LightColor.RED to "stop-And-Remain (Red)"
        4 -> LightColor.RED to "pre-Movement"
        5 -> LightColor.GREEN to "permissive-Movement-Allowed (Green)"
        6 -> LightColor.PROTECTED_GREEN to "protected-Movement-Allowed (Green Arrow)"
        7 -> LightColor.YELLOW to "permissive-clearance (Yellow)"
        8 -> LightColor.YELLOW to "protected-clearance (Yellow Arrow)"
        9 -> LightColor.YELLOW to "caution-Conflicting-Traffic"
        else -> LightColor.DARK to "unavailable ($code)"
    }

    // =========================================================================
    // Genuine Binary Frame Builders for Demo / Self-Test Mode (SPaT, BSM, CAM, DENM)
    // =========================================================================
    private fun wrap80211Snap(etherType: Int, srcLastByte: Int, payload: ByteArray): ByteArray {
        val out = ArrayList<Byte>(32 + payload.size)
        out.add(0x08); out.add(0x00); out.add(0x00); out.add(0x00)
        repeat(6) { out.add(0xFF.toByte()) }
        listOf(0x02, 0x56, 0x32, 0x58, 0x10, srcLastByte).forEach { out.add(it.toByte()) }
        repeat(6) { out.add(0xFF.toByte()) }
        out.add(0x00); out.add(0x00)
        listOf(0xAA, 0xAA, 0x03, 0x00, 0x00, 0x00, (etherType ushr 8) and 0xFF, etherType and 0xFF).forEach { out.add(it.toByte()) }
        payload.forEach { out.add(it) }
        return out.toByteArray()
    }

    private fun wrapWsmpJ2735(psidHi: Int, psidLo: Int, msgId: Int, uperBody: ByteArray): ByteArray {
        val out = ArrayList<Byte>(16 + uperBody.size)
        out.add(0x03); out.add(0x00)
        if (psidHi != 0) { out.add(psidHi.toByte()); out.add(psidLo.toByte()) } else { out.add(psidLo.toByte()) }
        val mfLen = 3 + uperBody.size
        out.add(mfLen.toByte())
        out.add(((msgId ushr 8) and 0xFF).toByte())
        out.add((msgId and 0xFF).toByte())
        out.add(uperBody.size.toByte())
        uperBody.forEach { out.add(it) }
        return wrap80211Snap(0x88DC, 0x01, out.toByteArray())
    }

    fun buildSimulatedSpatFrame(
        intersectionId: Int,
        phases: List<Triple<Int, Int, Int>> // (signalGroup, eventState, tenthsRemaining)
    ): ByteArray {
        val bw = UperBitWriter()
        bw.writeBit(0); bw.writeBit(0); bw.writeBit(0); bw.writeBit(0) // SPAT flags
        bw.writeBits(0, 5) // 1 intersection
        // IntersectionState flags: ext=0, name=0, moy=0, ts=0, enabledLanes=0, maneuver=0, regional=0
        repeat(7) { bw.writeBit(0) }
        bw.writeBit(0) // no region
        bw.writeBits(intersectionId.toLong(), 16)
        bw.writeBits(1, 7) // rev
        bw.writeBits(0, 16) // status
        bw.writeBits((phases.size - 1).toLong(), 8)
        for ((sg, state, tenths) in phases) {
            bw.writeBit(0); bw.writeBit(0); bw.writeBit(0) // ext, name, maneuver
            bw.writeBits(sg.toLong(), 8)
            bw.writeBits(0, 4) // 1 event
            bw.writeBit(0); bw.writeBit(1); bw.writeBit(0) // ext=0, hasTiming=1, hasSpeeds=0
            bw.writeBits(state.toLong(), 4)
            bw.writeBit(0); bw.writeBit(0); bw.writeBit(0); bw.writeBit(0); bw.writeBit(0) // only minEndTime
            bw.writeBits(tenths.toLong(), 16)
        }
        return wrapWsmpJ2735(0x80, 0x02, 19, bw.toByteArray())
    }

    fun buildSimulatedBsmFrame(tempId: Long, speedMph: Double, headingDeg: Double, braking: Boolean): ByteArray {
        val bw = UperBitWriter()
        bw.writeBit(0); bw.writeBit(0); bw.writeBit(0) // ext, partII, regional
        bw.writeBits(42, 7) // msgCnt
        bw.writeBits(tempId, 32)
        bw.writeBits(12500, 16) // secMark
        bw.writeBits((38.8977 * 1e7).toLong() + 900_000_000L, 31)
        bw.writeBits((-77.0365 * 1e7).toLong() + 1_799_999_999L, 32)
        bw.writeBits(250 + 4096, 16) // 25.0m elev
        bw.writeBits(0, 32) // accuracy
        bw.writeBits(2, 3) // Forward gear
        val speedRaw = ((speedMph / 2.23694) / 0.02).toLong().coerceIn(0, 8191)
        bw.writeBits(speedRaw, 13)
        bw.writeBits((headingDeg / 0.0125).toLong().coerceIn(0, 28800), 15)
        bw.writeBits(126, 8) // 0 deg steer
        bw.writeBits(0, 48) // accel
        bw.writeBits(if (braking) 0x0F else 0x00, 5) // wheelBrakes
        bw.writeBits(0, 10)
        bw.writeBits(195, 10) // 195 cm width
        bw.writeBits(465, 12) // 465 cm length
        return wrapWsmpJ2735(0x00, 0x20, 20, bw.toByteArray())
    }

    fun buildSimulatedEtsiCamFrame(stationId: Long, speedKmh: Double, headingDeg: Double): ByteArray {
        val bw = UperBitWriter()
        bw.writeBits(1500, 16) // genDeltaTime
        bw.writeBits(5, 8) // Passenger Car
        bw.writeBits((38.8981 * 1e7).toLong() + 900_000_000L, 31)
        bw.writeBits((-77.0359 * 1e7).toLong() + 1_800_000_000L, 32)
        bw.writeBits(((speedKmh / 3.6) / 0.01).toLong().coerceIn(0, 16383), 14)
        bw.writeBits((headingDeg / 0.1).toLong().coerceIn(0, 3600), 12)
        val body = bw.toByteArray()
        val gn = ByteArray(4 + 8 + 28 + 4 + 6 + body.size)
        gn[0] = 0x11 // Ver 1, NH=CommonHeader
        gn[4] = 0x20 // NH=BTP-B
        gn[5] = 0x50 // HT=SHB
        val btp = 4 + 8 + 28
        gn[btp] = 0x07; gn[btp + 1] = 0xD1.toByte() // Port 2001 = CAM
        val its = btp + 4
        gn[its] = 2; gn[its + 1] = 2 // v2, MsgID 2 = CAM
        gn[its + 2] = ((stationId ushr 24) and 0xFF).toByte()
        gn[its + 3] = ((stationId ushr 16) and 0xFF).toByte()
        gn[its + 4] = ((stationId ushr 8) and 0xFF).toByte()
        gn[its + 5] = (stationId and 0xFF).toByte()
        System.arraycopy(body, 0, gn, its + 6, body.size)
        return wrap80211Snap(0x8947, 0x22, gn)
    }

    fun buildSimulatedEtsiDenmFrame(stationId: Long, causeCode: Int): ByteArray {
        val bw = UperBitWriter()
        bw.writeBits(causeCode.toLong(), 8)
        bw.writeBits(1, 8) // subCause
        bw.writeBits((38.8975 * 1e7).toLong() + 900_000_000L, 31)
        bw.writeBits((-77.0361 * 1e7).toLong() + 1_800_000_000L, 32)
        val body = bw.toByteArray()
        val gn = ByteArray(4 + 8 + 44 + 4 + 6 + body.size)
        gn[0] = 0x11
        gn[4] = 0x20 // BTP-B
        gn[5] = 0x40 // HT=GBC
        val btp = 4 + 8 + 44
        gn[btp] = 0x07; gn[btp + 1] = 0xD2.toByte() // Port 2002 = DENM
        val its = btp + 4
        gn[its] = 2; gn[its + 1] = 1 // v2, MsgID 1 = DENM
        gn[its + 2] = ((stationId ushr 24) and 0xFF).toByte()
        gn[its + 3] = ((stationId ushr 16) and 0xFF).toByte()
        gn[its + 4] = ((stationId ushr 8) and 0xFF).toByte()
        gn[its + 5] = (stationId and 0xFF).toByte()
        System.arraycopy(body, 0, gn, its + 6, body.size)
        return wrap80211Snap(0x8947, 0x33, gn)
    }

    private fun formatMac(b: ByteArray, off: Int): String =
        (0..5).joinToString(":") { "%02X".format(b[off + it]) }

    private fun extractBeaconSsid(frame: ByteArray): String? {
        var idx = 36
        while (idx + 2 <= frame.size) {
            val elemId = frame[idx].toInt() and 0xFF
            val len = frame[idx + 1].toInt() and 0xFF
            if (idx + 2 + len > frame.size) break
            if (elemId == 0) {
                return if (len == 0) "<hidden>" else String(frame, idx + 2, len, Charsets.UTF_8)
            }
            idx += 2 + len
        }
        return null
    }
}
