package com.trafficlight.v2x.storage

import android.content.Context
import java.io.BufferedReader
import java.io.File
import java.io.InputStreamReader
import kotlin.math.*

data class RsuSite(
    val name: String,
    val callsign: String,
    val lat: Double,
    val lon: Double,
    val freqMhz: Double,
    val protocol: String,
    val rangeM: Double,
    val source: String
)

data class RsuProximity(
    val site: RsuSite,
    val distanceM: Double,
    val inRange: Boolean
)

data class RsuCorridor(
    val name: String,
    val points: List<Pair<Double, Double>>
)

object RsuDatabase {

    /**
     * Load bundled assets/rsus.csv plus any user-pushed v2x_captures/rsus.csv (deduplicated by name+coords).
     */
    fun load(context: Context): List<RsuSite> {
        val out = LinkedHashMap<String, RsuSite>()

        // 1. Bundled NYC + NJ FCC ULS / USDOT RSU database
        try {
            context.assets.open("rsus.csv").use { stream ->
                parseCsv(BufferedReader(InputStreamReader(stream)), out)
            }
        } catch (_: Exception) {}

        // 2. Optional external override/additions from v2x_captures/rsus.csv
        try {
            val ext = File(File(context.getExternalFilesDir(null), "v2x_captures"), "rsus.csv")
            if (ext.exists() && ext.isFile) {
                ext.bufferedReader().use { parseCsv(it, out) }
            }
        } catch (_: Exception) {}

        return out.values.toList()
    }

    private fun parseCsv(reader: BufferedReader, out: MutableMap<String, RsuSite>) {
        val headerLine = reader.readLine() ?: return
        val cols = headerLine.split(",").map { it.trim().lowercase() }
        val idxName = cols.indexOf("name")
        val idxCall = cols.indexOf("callsign")
        val idxLat = cols.indexOf("lat")
        val idxLon = cols.indexOf("lon")
        val idxFreq = cols.indexOf("freq_mhz")
        val idxProto = cols.indexOf("protocol")
        val idxRange = cols.indexOf("range_m")
        val idxSrc = cols.indexOf("source")
        if (idxLat < 0 || idxLon < 0) return

        reader.forEachLine { line ->
            if (line.isBlank()) return@forEachLine
            val parts = line.split(",")
            val lat = parts.getOrNull(idxLat)?.trim()?.toDoubleOrNull() ?: return@forEachLine
            val lon = parts.getOrNull(idxLon)?.trim()?.toDoubleOrNull() ?: return@forEachLine
            val name = parts.getOrNull(idxName)?.trim()?.ifEmpty { "RSU" } ?: "RSU"
            val call = parts.getOrNull(idxCall)?.trim() ?: ""
            val freq = parts.getOrNull(idxFreq)?.trim()?.toDoubleOrNull() ?: 5905.0
            val proto = parts.getOrNull(idxProto)?.trim()?.ifEmpty { "ITS-5.9G" } ?: "ITS-5.9G"
            val range = parts.getOrNull(idxRange)?.trim()?.toDoubleOrNull() ?: 600.0
            val src = parts.getOrNull(idxSrc)?.trim() ?: "CSV"
            val key = "${name}_${"%.4f".format(lat)}_${"%.4f".format(lon)}"
            out[key] = RsuSite(name, call, lat, lon, freq, proto, range, src)
        }
    }

    fun haversineMeters(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val r = 6_371_000.0
        val p1 = Math.toRadians(lat1)
        val p2 = Math.toRadians(lat2)
        val dp = Math.toRadians(lat2 - lat1)
        val dl = Math.toRadians(lon2 - lon1)
        val a = sin(dp / 2).pow(2) + cos(p1) * cos(p2) * sin(dl / 2).pow(2)
        return 2.0 * r * atan2(sqrt(a), sqrt(1.0 - a))
    }

    fun nearest(sites: List<RsuSite>, myLat: Double?, myLon: Double?): RsuProximity? {
        if (sites.isEmpty() || myLat == null || myLon == null) return null
        var bestSite: RsuSite? = null
        var bestDist = Double.MAX_VALUE
        for (s in sites) {
            val d = haversineMeters(myLat, myLon, s.lat, s.lon)
            if (d < bestDist) {
                bestDist = d
                bestSite = s
            }
        }
        return bestSite?.let { RsuProximity(it, bestDist, bestDist <= it.rangeM) }
    }

    /**
     * Highlighted 5.9 GHz V2X registered routes/corridors for the Map tab.
     */
    val REGISTERED_CORRIDORS = listOf(
        RsuCorridor("NYC 1st Ave (CVPD)", listOf(40.7313 to -73.9818, 40.7440 to -73.9726, 40.7596 to -73.9614)),
        RsuCorridor("NYC 2nd Ave (CVPD)", listOf(40.7324 to -73.9845, 40.7450 to -73.9754, 40.7606 to -73.9642)),
        RsuCorridor("NYC 5th Ave (CVPD)", listOf(40.7359 to -73.9935, 40.7484 to -73.9845, 40.7536 to -73.9809, 40.7644 to -73.9730)),
        RsuCorridor("NYC 6th Ave (CVPD)", listOf(40.7372 to -73.9966, 40.7498 to -73.9878, 40.7549 to -73.9840, 40.7644 to -73.9772)),
        RsuCorridor("NYC FDR Drive (CVPD)", listOf(40.7582 to -73.9585, 40.7705 to -73.9476)),
        RsuCorridor("Brooklyn Flatbush Ave (CVPD)", listOf(40.6960 to -73.9847, 40.6845 to -73.9776, 40.6742 to -73.9702)),
        RsuCorridor("Newark NJIT Warren St (ICUC)", listOf(40.7432 to -74.1798, 40.7421 to -74.1783)),
        RsuCorridor("Newark Raymond Blvd (ICUC)", listOf(40.7388 to -74.1785, 40.7376 to -74.1712, 40.7359 to -74.1656)),
        RsuCorridor("NJDOT I-280 Newark (WRFR820)", listOf(40.752139 to -74.190278, 40.748056 to -74.171889)),
        RsuCorridor("NJDOT Rt 46 Fairfield (WRFR820)", listOf(40.867972 to -74.311556, 40.870222 to -74.307639, 40.881639 to -74.292083, 40.891194 to -74.269389)),
        RsuCorridor("NJDOT US-1 Princeton", listOf(40.2923 to -74.6828, 40.3266 to -74.6407))
    )
}
