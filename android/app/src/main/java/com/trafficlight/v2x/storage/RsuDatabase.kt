package com.trafficlight.v2x.storage

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import java.io.File
import java.io.FileOutputStream
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

/**
 * Unified SQLite RF database reader for Android (roadsense_rf.db — 8,129+ nationwide FCC ULS + USDOT RSUs).
 * Supports indexed viewport bounding-box queries for smooth 60 fps Map rendering and instant GPS proximity checks.
 */
class RsuDatabase(context: Context) {

    private var db: SQLiteDatabase? = null
    var totalSites: Int = 0
        private set

    init {
        val dbFile = prepareDbFile(context)
        if (dbFile != null && dbFile.exists()) {
            try {
                db = SQLiteDatabase.openDatabase(dbFile.absolutePath, null, SQLiteDatabase.OPEN_READONLY)
                db?.rawQuery("SELECT COUNT(*) FROM rsu_sites", null)?.use { c ->
                    if (c.moveToFirst()) totalSites = c.getInt(0)
                }
            } catch (_: Exception) {}
        }
    }

    private fun prepareDbFile(context: Context): File? {
        // 1. Prefer user-pushed external v2x_captures/roadsense_rf.db if present
        val extFile = File(File(context.getExternalFilesDir(null), "v2x_captures"), "roadsense_rf.db")
        if (extFile.exists() && extFile.length() > 10_000L) {
            return extFile
        }
        // 2. Otherwise copy bundled assets/roadsense_rf.db to internal filesDir
        val internal = File(context.filesDir, "roadsense_rf.db")
        try {
            val assetSize = context.assets.openFd("roadsense_rf.db").use { it.length }
            if (internal.exists() && internal.length() == assetSize) {
                return internal
            }
        } catch (_: Exception) {
            if (internal.exists() && internal.length() > 10_000L) return internal
        }
        return try {
            context.assets.open("roadsense_rf.db").use { input ->
                FileOutputStream(internal).use { output ->
                    input.copyTo(output, bufferSize = 65536)
                }
            }
            internal
        } catch (_: Exception) {
            null
        }
    }

    /**
     * Fast indexed bounding-box query for Map viewport rendering (uses idx_rsu_lat_lon).
     */
    fun queryViewport(minLat: Double, maxLat: Double, minLon: Double, maxLon: Double, limit: Int = 160): List<RsuSite> {
        val d = db ?: return emptyList()
        val out = ArrayList<RsuSite>()
        try {
            d.rawQuery(
                """SELECT name, callsign, lat, lon, freq_mhz, protocol, range_m, source
                   FROM rsu_sites
                   WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
                   LIMIT ?""",
                arrayOf(minLat.toString(), maxLat.toString(), minLon.toString(), maxLon.toString(), limit.toString())
            ).use { c ->
                while (c.moveToNext()) {
                    out.add(
                        RsuSite(
                            name = c.getString(0) ?: "RSU",
                            callsign = c.getString(1) ?: "",
                            lat = c.getDouble(2),
                            lon = c.getDouble(3),
                            freqMhz = c.getDouble(4),
                            protocol = c.getString(5) ?: "ITS-5.9G",
                            rangeM = c.getDouble(6),
                            source = c.getString(7) ?: "DB"
                        )
                    )
                }
            }
        } catch (_: Exception) {}
        return out
    }

    /**
     * Fast nearest-RSU proximity query from live phone GPS.
     */
    fun nearest(myLat: Double?, myLon: Double?): RsuProximity? {
        if (myLat == null || myLon == null) return null
        // Check within ~55 km (±0.5 deg) first using index; expand if none nearby
        var candidates = queryViewport(myLat - 0.5, myLat + 0.5, myLon - 0.65, myLon + 0.65, limit = 500)
        if (candidates.isEmpty()) {
            candidates = queryViewport(myLat - 3.0, myLat + 3.0, myLon - 4.0, myLon + 4.0, limit = 500)
        }
        var bestSite: RsuSite? = null
        var bestDist = Double.MAX_VALUE
        for (s in candidates) {
            val dist = haversineMeters(myLat, myLon, s.lat, s.lon)
            if (dist < bestDist) {
                bestDist = dist
                bestSite = s
            }
        }
        return bestSite?.let { RsuProximity(it, bestDist, bestDist <= it.rangeM) }
    }

    fun close() {
        try { db?.close() } catch (_: Exception) {}
        db = null
    }

    companion object {
        fun haversineMeters(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
            val r = 6_371_000.0
            val p1 = Math.toRadians(lat1)
            val p2 = Math.toRadians(lat2)
            val dp = Math.toRadians(lat2 - lat1)
            val dl = Math.toRadians(lon2 - lon1)
            val a = sin(dp / 2).pow(2) + cos(p1) * cos(p2) * sin(dl / 2).pow(2)
            return 2.0 * r * atan2(sqrt(a), sqrt(1.0 - a))
        }

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
}
