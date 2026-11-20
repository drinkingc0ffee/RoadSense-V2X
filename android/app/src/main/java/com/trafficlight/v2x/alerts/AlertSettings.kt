package com.trafficlight.v2x.alerts

import android.content.Context

enum class AlertCategory(
    val prefKey: String,
    val title: String,
    val subtitle: String,
    val defaultEnabled: Boolean
) {
    TRAFFIC_LIGHTS("cat_lights", "Traffic Lights", "SPaT, MAP, SPATEM, MAPEM", true),
    CV2X_BURSTS("cat_cv2x", "C-V2X / RF Bursts", "C-V2X_BURST, RF_BURST (20M PC5)", true),
    EMERGENCY_PRIORITY("cat_emerg", "Emergency & Priority", "EVA, SRM, SSM, PSM", true),
    HAZARDS("cat_hazards", "Hazards & Advisories", "TIM, RSA, DENM", true),
    VEHICLES("cat_vehicles", "Vehicles (V2V)", "BSM, CAM", true),
    RSU_UTILITIES("cat_rsu", "RSU Utilities", "RTCM GPS Corrections", false),
    WIFI_80211("cat_wifi", "General 802.11 / Wi-Fi", "802.11, WSMP, GN (unparsed)", false);

    companion object {
        fun forProtocol(protocol: String): AlertCategory = when (protocol) {
            "SPaT", "MAP", "SPATEM", "MAPEM" -> TRAFFIC_LIGHTS
            "C-V2X_BURST", "RF_BURST" -> CV2X_BURSTS
            "EVA", "SRM", "SSM", "PSM" -> EMERGENCY_PRIORITY
            "TIM", "RSA", "DENM" -> HAZARDS
            "BSM", "CAM" -> VEHICLES
            "RTCM" -> RSU_UTILITIES
            else -> WIFI_80211
        }
    }
}

data class AlertConfig(
    val enabledCategories: Set<AlertCategory> = AlertCategory.entries.filter { it.defaultEnabled }.toSet(),
    val soundEnabled: Boolean = true,
    val vibrateEnabled: Boolean = true,
    val bannerEnabled: Boolean = true,
    val cooldownSec: Int = 5,
    val newIdOnly: Boolean = false
) {
    val isSilent: Boolean get() = !soundEnabled && !vibrateEnabled && !bannerEnabled

    fun isProtocolEnabled(protocol: String): Boolean =
        AlertCategory.forProtocol(protocol) in enabledCategories

    companion object {
        private const val PREFS = "trafficlight_v2x_alerts"

        fun fieldDrive() = AlertConfig(
            enabledCategories = setOf(
                AlertCategory.TRAFFIC_LIGHTS,
                AlertCategory.CV2X_BURSTS,
                AlertCategory.EMERGENCY_PRIORITY,
                AlertCategory.HAZARDS,
                AlertCategory.VEHICLES
            ),
            soundEnabled = true,
            vibrateEnabled = true,
            bannerEnabled = true,
            cooldownSec = 5,
            newIdOnly = false
        )

        fun trafficLightsOnly() = AlertConfig(
            enabledCategories = setOf(AlertCategory.TRAFFIC_LIGHTS, AlertCategory.CV2X_BURSTS),
            soundEnabled = true,
            vibrateEnabled = true,
            bannerEnabled = true,
            cooldownSec = 5,
            newIdOnly = true
        )

        fun emergenciesOnly() = AlertConfig(
            enabledCategories = setOf(AlertCategory.EMERGENCY_PRIORITY, AlertCategory.HAZARDS),
            soundEnabled = true,
            vibrateEnabled = true,
            bannerEnabled = true,
            cooldownSec = 2,
            newIdOnly = false
        )

        fun silent() = AlertConfig(
            enabledCategories = emptySet(),
            soundEnabled = false,
            vibrateEnabled = false,
            bannerEnabled = false,
            cooldownSec = 10,
            newIdOnly = false
        )

        fun load(context: Context): AlertConfig {
            val sp = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val cats = AlertCategory.entries.filter { cat ->
                sp.getBoolean(cat.prefKey, cat.defaultEnabled)
            }.toSet()
            return AlertConfig(
                enabledCategories = cats,
                soundEnabled = sp.getBoolean("sound", true),
                vibrateEnabled = sp.getBoolean("vibrate", true),
                bannerEnabled = sp.getBoolean("banner", true),
                cooldownSec = sp.getInt("cooldown_sec", 5),
                newIdOnly = sp.getBoolean("new_id_only", false)
            )
        }

        fun save(context: Context, cfg: AlertConfig) {
            val ed = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            AlertCategory.entries.forEach { cat ->
                ed.putBoolean(cat.prefKey, cat in cfg.enabledCategories)
            }
            ed.putBoolean("sound", cfg.soundEnabled)
            ed.putBoolean("vibrate", cfg.vibrateEnabled)
            ed.putBoolean("banner", cfg.bannerEnabled)
            ed.putInt("cooldown_sec", cfg.cooldownSec)
            ed.putBoolean("new_id_only", cfg.newIdOnly)
            ed.apply()
        }
    }
}
