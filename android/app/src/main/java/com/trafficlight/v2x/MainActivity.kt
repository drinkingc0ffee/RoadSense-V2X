package com.trafficlight.v2x

import android.Manifest
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Canvas as AndroidCanvas
import android.graphics.Paint
import android.graphics.drawable.BitmapDrawable
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.media.AudioManager
import android.media.ToneGenerator
import android.os.Build
import android.os.Bundle
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.view.WindowManager
import androidx.compose.ui.viewinterop.AndroidView
import org.osmdroid.config.Configuration
import org.osmdroid.tileprovider.tilesource.TileSourceFactory
import org.osmdroid.util.GeoPoint
import org.osmdroid.views.MapView
import org.osmdroid.views.overlay.Marker
import org.osmdroid.views.overlay.Polygon
import org.osmdroid.views.overlay.Polyline
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.ListAlt
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.trafficlight.v2x.alerts.AlertCategory
import com.trafficlight.v2x.alerts.AlertConfig
import com.trafficlight.v2x.dsp.DsrcDsp
import com.trafficlight.v2x.dsp.SpectrumResult
import com.trafficlight.v2x.protocol.DecodedPacket
import com.trafficlight.v2x.protocol.LightColor
import com.trafficlight.v2x.protocol.SpatMessage
import com.trafficlight.v2x.protocol.V2xDecoder
import com.trafficlight.v2x.storage.CaptureLogger
import com.trafficlight.v2x.storage.RsuDatabase
import com.trafficlight.v2x.storage.RsuProximity
import com.trafficlight.v2x.storage.RsuSite
import com.trafficlight.v2x.usb.HackRfUsbDriver
import kotlinx.coroutines.*
import kotlin.math.max
import kotlin.math.min

data class ChannelPreset(val label: String, val freqMhz: Double, val sampleRateHz: Int, val scanAll: Boolean = false)

data class SignalMapHit(
    val id: Long,
    val timestamp: String,
    val lat: Double,
    val lon: Double,
    val freqMhz: Double,
    val rssiDbfs: Float,
    val protocol: String,
    val summary: String,
    val details: Map<String, String>,
    val spat: SpatMessage? = null
)

val CHANNEL_PRESETS = listOf(
    ChannelPreset("Scan All DSRC", 5900.0, 10_000_000, scanAll = true),
    ChannelPreset("Ch 183 · 5915 (20M C-V2X)", 5915.0, 20_000_000),
    ChannelPreset("Ch 181 · 5905 (20M)", 5905.0, 20_000_000),
    ChannelPreset("Ch 172 · 5860", 5860.0, 10_000_000),
    ChannelPreset("Ch 178 · 5890", 5890.0, 10_000_000),
    ChannelPreset("Ch 180 · 5900", 5900.0, 10_000_000),
    ChannelPreset("Ch 182 · 5910", 5910.0, 10_000_000),
    ChannelPreset("Ch 184 · 5920", 5920.0, 10_000_000),
    ChannelPreset("Wi-Fi Test · 5745", 5745.0, 20_000_000)
)

private val DSRC_SCAN_FREQS = listOf(5860.0, 5870.0, 5880.0, 5890.0, 5900.0, 5910.0, 5920.0)

class MainActivity : ComponentActivity() {

    companion object {
        private const val ACTION_USB_PERMISSION = "com.trafficlight.v2x.USB_PERMISSION"
    }

    private lateinit var hackRf: HackRfUsbDriver
    private lateinit var logger: CaptureLogger
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var workerJob: Job? = null

    // Observable UI state
    private val isRunning = mutableStateOf(false)
    private val simulatorMode = mutableStateOf(false)
    private val statusText = mutableStateOf("Ready — Connect HackRF One via USB-C OTG or try Demo Mode")
    private val usbConnected = mutableStateOf(false)
    private val selectedPresetIdx = mutableStateOf(1) // Ch 183 (5915 MHz 20M C-V2X)
    private val currentFreqMhz = mutableStateOf(5915.0)
    private val lnaGain = mutableStateOf(24)
    private val vgaGain = mutableStateOf(24)
    private val ampEnabled = mutableStateOf(true)
    private val recordIqOnHit = mutableStateOf(true)
    private val alertConfig = mutableStateOf(AlertConfig.fieldDrive())
    private val showAlertMenu = mutableStateOf(false)
    private val showClearDialog = mutableStateOf(false)
    private val activeBanner = mutableStateOf<Pair<String, String>?>(null) // (protocol, message)
    private val currentLat = mutableStateOf<Double?>(null)
    private val currentLon = mutableStateOf<Double?>(null)
    private val rsuSites = mutableStateListOf<RsuSite>()
    private val nearestRsu = mutableStateOf<RsuProximity?>(null)
    private val signalMapHits = mutableStateListOf<SignalMapHit>()
    private val selectedTab = mutableStateOf(0)
    private var nextMapHitId = 1L
    private val lastAlertByCategory = mutableMapOf<AlertCategory, Long>()
    private val seenEntityKeys = mutableSetOf<String>()
    private var bannerHideJob: Job? = null

    private var locationManager: LocationManager? = null
    private val gpsListener = LocationListener { loc: Location ->
        currentLat.value = loc.latitude
        currentLon.value = loc.longitude
        nearestRsu.value = RsuDatabase.nearest(rsuSites, loc.latitude, loc.longitude)
    }
    private val requestLocationPerm = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { perms ->
        if (perms[Manifest.permission.ACCESS_FINE_LOCATION] == true ||
            perms[Manifest.permission.ACCESS_COARSE_LOCATION] == true
        ) {
            startGpsUpdates()
        }
    }

    private val latestSpectrum = mutableStateOf<SpectrumResult?>(null)
    private val latestSpat = mutableStateOf<SpatMessage?>(null)
    private val selectedSignalGroup = mutableStateOf(2)
    private val sweepCount = mutableStateOf(0)
    private val burstCount = mutableStateOf(0)
    private val packetLog = mutableStateListOf<DecodedPacket>()

    private val usbReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action == ACTION_USB_PERMISSION) {
                val granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)
                val dev = if (Build.VERSION.SDK_INT >= 33) {
                    intent.getParcelableExtra(UsbManager.EXTRA_DEVICE, UsbDevice::class.java)
                } else {
                    @Suppress("DEPRECATION") intent.getParcelableExtra(UsbManager.EXTRA_DEVICE)
                }
                if (granted && dev != null) {
                    statusText.value = "HackRF USB permission granted — starting..."
                    startLiveHackRf(dev)
                } else {
                    statusText.value = "USB permission denied for HackRF One"
                }
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Configuration.getInstance().userAgentValue = packageName
        hackRf = HackRfUsbDriver(this)
        logger = CaptureLogger(this)
        alertConfig.value = AlertConfig.load(this)
        rsuSites.addAll(RsuDatabase.load(this))

        val filter = IntentFilter(ACTION_USB_PERMISSION)
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(usbReceiver, filter, RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(usbReceiver, filter)
        }
        usbConnected.value = hackRf.findHackRf() != null
        initGps()

        setContent {
            TrafficLightV2xTheme {
                MainAppScreen()
            }
        }
    }

    private fun initGps() {
        locationManager = getSystemService(Context.LOCATION_SERVICE) as LocationManager
        val fineOk = ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
        if (fineOk) {
            startGpsUpdates()
        } else {
            requestLocationPerm.launch(
                arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)
            )
        }
    }

    @Suppress("MissingPermission")
    private fun startGpsUpdates() {
        try {
            val lm = locationManager ?: return
            val last = lm.getLastKnownLocation(LocationManager.GPS_PROVIDER)
                ?: lm.getLastKnownLocation(LocationManager.NETWORK_PROVIDER)
            if (last != null) {
                currentLat.value = last.latitude
                currentLon.value = last.longitude
                nearestRsu.value = RsuDatabase.nearest(rsuSites, last.latitude, last.longitude)
            }
            if (lm.isProviderEnabled(LocationManager.GPS_PROVIDER)) {
                lm.requestLocationUpdates(LocationManager.GPS_PROVIDER, 2000L, 2f, gpsListener)
            }
            if (lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) {
                lm.requestLocationUpdates(LocationManager.NETWORK_PROVIDER, 5000L, 5f, gpsListener)
            }
        } catch (_: Exception) {}
    }

    private fun updateAlertConfig(newCfg: AlertConfig) {
        alertConfig.value = newCfg
        AlertConfig.save(this, newCfg)
    }

    private fun triggerHitAlert(protocol: String, summary: String, entityKey: String? = null, forceTest: Boolean = false) {
        val cfg = alertConfig.value
        if (!forceTest) {
            if (cfg.isSilent || !cfg.isProtocolEnabled(protocol)) return
            val cat = AlertCategory.forProtocol(protocol)
            if (cfg.newIdOnly && entityKey != null && entityKey in seenEntityKeys) return
            val now = System.currentTimeMillis()
            val last = lastAlertByCategory[cat] ?: 0L
            if (now - last < cfg.cooldownSec * 1000L) return
            lastAlertByCategory[cat] = now
            if (entityKey != null) {
                seenEntityKeys.add(entityKey)
                if (seenEntityKeys.size > 200) seenEntityKeys.clear()
            }
        }

        val cat = AlertCategory.forProtocol(protocol)

        // Banner
        if (cfg.bannerEnabled || forceTest) {
            activeBanner.value = protocol to summary
            bannerHideJob?.cancel()
            bannerHideJob = scope.launch {
                delay(4000)
                activeBanner.value = null
            }
        }

        // Sound (distinct tone per priority)
        if (cfg.soundEnabled || forceTest) {
            val tone = when (cat) {
                AlertCategory.EMERGENCY_PRIORITY, AlertCategory.HAZARDS -> ToneGenerator.TONE_CDMA_ALERT_CALL_GUARD
                AlertCategory.TRAFFIC_LIGHTS -> ToneGenerator.TONE_PROP_BEEP2
                else -> ToneGenerator.TONE_PROP_ACK
            }
            val dur = if (cat == AlertCategory.EMERGENCY_PRIORITY || cat == AlertCategory.HAZARDS) 260 else 160
            try {
                ToneGenerator(AudioManager.STREAM_NOTIFICATION, 88).startTone(tone, dur)
            } catch (_: Exception) {}
        }

        // Vibration (double-pulse for emergency/hazard, single pulse otherwise)
        if (cfg.vibrateEnabled || forceTest) {
            try {
                val effect = if (cat == AlertCategory.EMERGENCY_PRIORITY || cat == AlertCategory.HAZARDS) {
                    VibrationEffect.createWaveform(longArrayOf(0, 120, 80, 160), -1)
                } else {
                    VibrationEffect.createOneShot(120, VibrationEffect.DEFAULT_AMPLITUDE)
                }
                if (Build.VERSION.SDK_INT >= 31) {
                    val vm = getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as VibratorManager
                    vm.defaultVibrator.vibrate(effect)
                } else {
                    @Suppress("DEPRECATION")
                    val v = getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
                    v.vibrate(effect)
                }
            } catch (_: Exception) {}
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        stopWorker()
        try { locationManager?.removeUpdates(gpsListener) } catch (_: Exception) {}
        try { unregisterReceiver(usbReceiver) } catch (_: Exception) {}
        scope.cancel()
    }

    private fun toggleRun() {
        if (isRunning.value) {
            stopWorker()
            return
        }
        if (simulatorMode.value) {
            startSimulator()
        } else {
            val dev = hackRf.findHackRf()
            if (dev == null) {
                usbConnected.value = false
                statusText.value = "No HackRF One found on USB-C OTG (switch on Demo Mode to preview UI)"
                return
            }
            usbConnected.value = true
            if (hackRf.hasPermission(dev)) {
                startLiveHackRf(dev)
            } else {
                val usbManager = getSystemService(Context.USB_SERVICE) as UsbManager
                val permIntent = Intent(ACTION_USB_PERMISSION).setPackage(packageName)
                val piFlags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    PendingIntent.FLAG_MUTABLE
                } else {
                    0
                }
                val pi = PendingIntent.getBroadcast(this, 0, permIntent, piFlags)
                statusText.value = "Requesting USB permission for HackRF One..."
                usbManager.requestPermission(dev, pi)
            }
        }
    }

    private fun clearCapturedData(deleteFiles: Boolean) {
        packetLog.clear()
        signalMapHits.clear()
        sweepCount.value = 0
        burstCount.value = 0
        latestSpat.value = null
        latestSpectrum.value = null
        seenEntityKeys.clear()
        lastAlertByCategory.clear()
        activeBanner.value = null
        if (deleteFiles) {
            val n = logger.clearAllCaptures()
            statusText.value = "Cleared log, counters & $n saved capture file(s)"
        } else {
            statusText.value = "Cleared log & counters"
        }
    }

    private fun stopWorker() {
        val oldJob = workerJob
        workerJob = null
        oldJob?.cancel()
        hackRf.close()
        isRunning.value = false
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        statusText.value = "Stopped"
    }

    private fun startLiveHackRf(dev: UsbDevice) {
        stopWorker()
        if (!hackRf.open(dev)) {
            statusText.value = "Failed to open HackRF USB: ${hackRf.lastError}"
            return
        }
        isRunning.value = true
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        val fw = hackRf.firmwareVersion.ifEmpty { "connected" }
        statusText.value = "HackRF One ($fw) active"

        lateinit var currentJob: Job
        currentJob = scope.launch(Dispatchers.IO) {
            var scanIdx = 0
            var consecutiveGlitches = 0
            try {
                while (isActive) {
                    val preset = CHANNEL_PRESETS[selectedPresetIdx.value]
                    val freqMhz = if (preset.scanAll) {
                        val f = DSRC_SCAN_FREQS[scanIdx % DSRC_SCAN_FREQS.size]
                        scanIdx++
                        f
                    } else preset.freqMhz

                    val sr = preset.sampleRateHz
                    // 0.15 s capture (~3 MB @ 10 MS/s, ~6 MB @ 20 MS/s)
                    val captureBytes = ((sr * 0.15).toInt() * 2).coerceAtMost(8_000_000)

                    withContext(Dispatchers.Main) {
                        currentFreqMhz.value = freqMhz
                        statusText.value = "Live RX @ ${"%.1f".format(freqMhz)} MHz (${sr / 1_000_000} MS/s)"
                    }

                    val iq = hackRf.captureIqChunk(
                        freqHz = (freqMhz * 1_000_000.0).toLong(),
                        sampleRateHz = sr,
                        lnaGain = lnaGain.value,
                        vgaGain = vgaGain.value,
                        ampEnable = ampEnabled.value,
                        totalBytes = captureBytes
                    )

                    if (iq == null) {
                        consecutiveGlitches++
                        val err = hackRf.lastError.ifEmpty { "unknown USB error" }
                        withContext(Dispatchers.Main) {
                            statusText.value = "USB glitch ($consecutiveGlitches): $err"
                        }
                        if (consecutiveGlitches >= 2) {
                            // Re-open HackRF in case of USB stall or power brownout reset
                            val freshDev = hackRf.findHackRf()
                            if (freshDev != null && hackRf.hasPermission(freshDev)) {
                                hackRf.open(freshDev)
                            }
                            consecutiveGlitches = 0
                        }
                        delay(300)
                        continue
                    }
                    consecutiveGlitches = 0

                    // Discard first 15 ms startup transient (HackRF PLL/ADC settle)
                    val trimSamples = (sr * 0.015).toInt()
                    val spec = DsrcDsp.analyzeSpectrum(iq, trimSamples)
                    val frames = DsrcDsp.decode80211pFrames(iq, trimSamples)

                    // Wideband burst check: require multi-bin occupancy so 1-bin spurs & AWGN peak-hold don't false-trigger
                    val avgBinsAbove = spec?.let { s -> (24..232).count { i -> s.avgDb[i] - s.noiseFloorDb >= 6.0f } } ?: 0
                    val peakBinsAbove = spec?.let { s -> (24..232).count { i -> s.peakDb[i] - s.noiseFloorDb >= 13.0f } } ?: 0
                    val isBurst = spec != null && (
                        avgBinsAbove >= 28 ||
                        (spec.burstJumpDb >= 6.0f && peakBinsAbove >= 24)
                    )
                    val lat = currentLat.value
                    val lon = currentLon.value
                    var savedIqName = ""
                    if ((isBurst || frames.isNotEmpty()) && recordIqOnHit.value) {
                        savedIqName = logger.saveIqBurst(iq, freqMhz).name
                    }

                    val decodedList = frames.map { f ->
                        logger.appendPcapFrame(f)
                        V2xDecoder.decode80211Frame(f, freqMhz, spec?.maxPeakDb ?: -60f, lat, lon)
                    }

                    if (decodedList.isNotEmpty()) {
                        decodedList.forEach { p ->
                            logger.logGpsHit(lat, lon, freqMhz, p.protocol, p.rssiDbfs, p.summary, savedIqName)
                        }
                    } else if (isBurst) {
                        val protoTag = if (sr >= 20_000_000 && freqMhz >= 5895.0) "C-V2X_BURST" else "RF_BURST"
                        logger.logGpsHit(lat, lon, freqMhz, protoTag, spec?.maxPeakDb ?: -60f, "RF burst +${"%.1f".format(spec?.burstJumpDb ?: 0f)}dB", savedIqName)
                    }

                    withContext(Dispatchers.Main) {
                        sweepCount.value += 1
                        if (spec != null) latestSpectrum.value = spec
                        if (isBurst || decodedList.isNotEmpty()) {
                            burstCount.value += 1
                        }
                        if (decodedList.isNotEmpty()) {
                            for (pkt in decodedList) {
                                packetLog.add(0, pkt)
                                if (packetLog.size > 150) packetLog.removeAt(packetLog.lastIndex)
                                if (pkt.spat != null) {
                                    latestSpat.value = pkt.spat
                                }
                                if (lat != null && lon != null) {
                                    signalMapHits.add(
                                        0,
                                        SignalMapHit(
                                            id = nextMapHitId++,
                                            timestamp = pkt.timestamp,
                                            lat = lat,
                                            lon = lon,
                                            freqMhz = pkt.freqMhz,
                                            rssiDbfs = pkt.rssiDbfs,
                                            protocol = pkt.protocol,
                                            summary = pkt.summary,
                                            details = pkt.details,
                                            spat = pkt.spat
                                        )
                                    )
                                    if (signalMapHits.size > 200) signalMapHits.removeAt(signalMapHits.lastIndex)
                                }
                                val key = pkt.details["Intersection ID"] ?: pkt.details["BSM Temp ID"] ?: pkt.details["Station ID"] ?: "${pkt.protocol}@${pkt.freqMhz.toInt()}"
                                triggerHitAlert(pkt.protocol, pkt.summary, key)
                            }
                        } else if (isBurst) {
                            val protoTag = if (sr >= 20_000_000 && freqMhz >= 5895.0) "C-V2X_BURST" else "RF_BURST"
                            val burstSum = "$protoTag @ ${"%.1f".format(freqMhz)} MHz (+${"%.1f".format(spec?.burstJumpDb ?: 0f)} dB)"
                            if (lat != null && lon != null) {
                                val tsNow = java.text.SimpleDateFormat("HH:mm:ss.SSS", java.util.Locale.US).format(java.util.Date())
                                signalMapHits.add(
                                    0,
                                    SignalMapHit(
                                        id = nextMapHitId++,
                                        timestamp = tsNow,
                                        lat = lat,
                                        lon = lon,
                                        freqMhz = freqMhz,
                                        rssiDbfs = spec?.maxPeakDb ?: -60f,
                                        protocol = protoTag,
                                        summary = burstSum,
                                        details = mapOf(
                                            "Noise Floor" to "${"%.1f".format(spec?.noiseFloorDb ?: -80f)} dBFS",
                                            "Peak" to "${"%.1f".format(spec?.maxPeakDb ?: -60f)} dBFS",
                                            "Burst Jump" to "+${"%.1f".format(spec?.burstJumpDb ?: 0f)} dB"
                                        ),
                                        spat = null
                                    )
                                )
                                if (signalMapHits.size > 200) signalMapHits.removeAt(signalMapHits.lastIndex)
                            }
                            triggerHitAlert(protoTag, burstSum, "$protoTag@${freqMhz.toInt()}")
                        }
                    }
                    delay(20)
                }
            } finally {
                if (workerJob === currentJob) {
                    hackRf.close()
                }
            }
        }
        workerJob = currentJob
    }

    /**
     * Live SAE J2735 SPaT Simulator — builds real 802.11p + WSMP + J2735 SPaT binary frames
     * and runs them through V2xDecoder so you can preview the glowing Traffic Light UI & Protocol Log.
     */
    private fun startSimulator() {
        stopWorker()
        isRunning.value = true
        statusText.value = "Demo Mode — Decoding live simulated SAE J2735 SPaT @ 5900 MHz"

        workerJob = scope.launch(Dispatchers.Default) {
            // 30s cycle: 14s Green, 4s Yellow, 12s Red (NB Through = SG 2; EB Through = SG 6 opposite)
            var tick = 0
            while (isActive) {
                val t = tick % 300 // tenths of a second (30.0 s cycle)
                val (sg2State, sg2Rem) = when {
                    t < 140 -> 5 to (140 - t)      // Green
                    t < 180 -> 7 to (180 - t)      // Yellow
                    else -> 3 to (300 - t)         // Red
                }
                val (sg5State, sg5Rem) = when {
                    t < 60 -> 6 to (60 - t)        // Protected Green Arrow
                    t < 90 -> 8 to (90 - t)        // Yellow Arrow
                    else -> 3 to (300 - t)         // Red
                }
                val (sg6State, sg6Rem) = when {
                    t < 180 -> 3 to (180 - t)      // Red while NB is Green/Yellow
                    t < 260 -> 5 to (260 - t)      // Green
                    else -> 7 to (300 - t)         // Yellow
                }

                val frame = V2xDecoder.buildSimulatedSpatFrame(
                    intersectionId = 4102,
                    phases = listOf(
                        Triple(2, sg2State, sg2Rem),
                        Triple(5, sg5State, sg5Rem),
                        Triple(6, sg6State, sg6Rem)
                    )
                )
                val pkt = V2xDecoder.decode80211Frame(frame, 5900.0, -48.2f)

                // Synthetic spectrum with a crisp 10 MHz DSRC hump
                val avg = FloatArray(256) { i ->
                    val d = (i - 128).toFloat() / 64f
                    val sig = if (kotlin.math.abs(d) < 0.8f) 18f else 0f
                    -76f + sig + ((tick * 13 + i * 7) % 5) * 0.4f
                }
                val peak = FloatArray(256) { i -> avg[i] + 5.5f }
                val spec = SpectrumResult(avg, peak, -76.0f, -52.5f, 128, 6.2f)

                val extraPkt = when (tick % 30) {
                    10 -> V2xDecoder.decode80211Frame(
                        V2xDecoder.buildSimulatedBsmFrame(0xA4F1029CL, 34.5 + (tick % 7), 88.0, braking = (sg2State == 3)),
                        5890.0, -51.4f
                    )
                    20 -> V2xDecoder.decode80211Frame(
                        V2xDecoder.buildSimulatedEtsiCamFrame(90412L, 58.0, 179.5),
                        5900.0, -53.1f
                    )
                    0 -> if (tick > 0) V2xDecoder.decode80211Frame(
                        V2xDecoder.buildSimulatedEtsiDenmFrame(77001L, if ((tick / 30) % 2 == 0) 95 else 3),
                        5900.0, -49.8f
                    ) else null
                    else -> null
                }

                withContext(Dispatchers.Main) {
                    currentFreqMhz.value = 5900.0
                    sweepCount.value += 1
                    latestSpectrum.value = spec
                    if (pkt.spat != null) latestSpat.value = pkt.spat
                    if (tick % 10 == 0) { // log 1 SPaT packet/sec
                        burstCount.value += 1
                        packetLog.add(0, pkt)
                        val dLat = currentLat.value ?: 40.7421
                        val dLon = currentLon.value ?: -74.1783
                        if (signalMapHits.size < 12) {
                            signalMapHits.add(
                                0,
                                SignalMapHit(
                                    id = nextMapHitId++,
                                    timestamp = pkt.timestamp,
                                    lat = dLat + ((tick % 5) - 2) * 0.0004,
                                    lon = dLon + ((tick % 3) - 1) * 0.0004,
                                    freqMhz = pkt.freqMhz,
                                    rssiDbfs = pkt.rssiDbfs,
                                    protocol = pkt.protocol,
                                    summary = pkt.summary,
                                    details = pkt.details,
                                    spat = pkt.spat
                                )
                            )
                        }
                        triggerHitAlert(pkt.protocol, pkt.summary, "SPaT#${pkt.spat?.intersectionId}")
                    }
                    if (extraPkt != null) {
                        burstCount.value += 1
                        packetLog.add(0, extraPkt)
                        val eKey = extraPkt.details["BSM Temp ID"] ?: extraPkt.details["Station ID"] ?: extraPkt.protocol
                        triggerHitAlert(extraPkt.protocol, extraPkt.summary, "${extraPkt.protocol}#$eKey")
                    }
                    while (packetLog.size > 150) packetLog.removeAt(packetLog.lastIndex)
                }
                tick += 2 // 5 Hz update
                delay(200)
            }
        }
    }

    @Composable
    fun MainAppScreen() {
        val currentTab = selectedTab.value

        Scaffold(
            containerColor = Color(0xFF0D1117),
            topBar = {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(Color(0xFF161B22))
                        .statusBarsPadding()
                        .padding(horizontal = 16.dp, vertical = 12.dp)
                ) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Column(modifier = Modifier.weight(1f).padding(end = 10.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(
                                    "RoadSense V2X",
                                    color = Color.White,
                                    fontSize = 20.sp,
                                    fontWeight = FontWeight.Bold
                                )
                                Spacer(Modifier.width(8.dp))
                                Text(
                                    "v2.0",
                                    color = Color(0xFF58A6FF),
                                    fontSize = 12.sp,
                                    fontWeight = FontWeight.SemiBold,
                                    fontFamily = FontFamily.Monospace
                                )
                            }
                            Text(
                                statusText.value,
                                color = if (isRunning.value) Color(0xFF3FB950) else Color(0xFF8B949E),
                                fontSize = 12.sp,
                                maxLines = 2,
                                overflow = TextOverflow.Ellipsis
                            )
                        }
                        IconButton(onClick = { showClearDialog.value = true }) {
                            Icon(
                                Icons.Default.DeleteSweep,
                                contentDescription = "Clear Data",
                                tint = Color(0xFF8B949E)
                            )
                        }
                        IconButton(onClick = { showAlertMenu.value = true }) {
                            Icon(
                                if (alertConfig.value.isSilent) Icons.Default.NotificationsOff else Icons.Default.NotificationsActive,
                                contentDescription = "Alert Settings",
                                tint = if (alertConfig.value.isSilent) Color(0xFF8B949E) else Color(0xFFFFD60A)
                            )
                        }
                        Button(
                            onClick = { toggleRun() },
                            colors = ButtonDefaults.buttonColors(
                                containerColor = if (isRunning.value) Color(0xFFDA3633) else Color(0xFF238636)
                            ),
                            shape = RoundedCornerShape(10.dp)
                        ) {
                            Icon(
                                if (isRunning.value) Icons.Default.Stop else Icons.Default.PlayArrow,
                                contentDescription = null,
                                modifier = Modifier.size(18.dp)
                            )
                            Spacer(Modifier.width(4.dp))
                            Text(if (isRunning.value) "Stop" else "Start", fontWeight = FontWeight.Bold)
                        }
                    }

                    Spacer(Modifier.height(8.dp))
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("SPaT Demo", color = Color(0xFFC9D1D9), fontSize = 12.sp)
                            Spacer(Modifier.width(6.dp))
                            Switch(
                                checked = simulatorMode.value,
                                onCheckedChange = {
                                    if (isRunning.value) stopWorker()
                                    simulatorMode.value = it
                                }
                            )
                        }
                        Text(
                            "Sweeps: ${sweepCount.value}  |  Hits: ${burstCount.value}  |  Pkts: ${packetLog.size}",
                            color = Color(0xFF8B949E),
                            fontSize = 12.sp,
                            fontFamily = FontFamily.Monospace
                        )
                    }
                }
            },
            bottomBar = {
                NavigationBar(containerColor = Color(0xFF161B22)) {
                    NavigationBarItem(
                        selected = currentTab == 0,
                        onClick = { selectedTab.value = 0 },
                        icon = { Icon(Icons.AutoMirrored.Filled.ListAlt, null) },
                        label = { Text("Protocol") }
                    )
                    NavigationBarItem(
                        selected = currentTab == 1,
                        onClick = { selectedTab.value = 1 },
                        icon = { Icon(Icons.Default.GraphicEq, null) },
                        label = { Text("RF Scope") }
                    )
                    NavigationBarItem(
                        selected = currentTab == 2,
                        onClick = { selectedTab.value = 2 },
                        icon = { Icon(Icons.Default.Map, null) },
                        label = { Text("Map") }
                    )
                    NavigationBarItem(
                        selected = currentTab == 3,
                        onClick = { selectedTab.value = 3 },
                        icon = { Icon(Icons.Default.Traffic, null) },
                        label = { Text("Signal") }
                    )
                }
            }
        ) { padding ->
            Box(modifier = Modifier.padding(padding).fillMaxSize()) {
                when (currentTab) {
                    0 -> ProtocolLogTab()
                    1 -> RfScopeTab()
                    2 -> MapTab()
                    3 -> TrafficLightTab()
                }

                // Live On-Screen Alert Banner
                activeBanner.value?.let { (proto, msg) ->
                    Card(
                        colors = CardDefaults.cardColors(containerColor = Color(0xFF1F6FEB)),
                        shape = RoundedCornerShape(12.dp),
                        modifier = Modifier
                            .align(Alignment.TopCenter)
                            .padding(horizontal = 12.dp, vertical = 8.dp)
                            .fillMaxWidth()
                            .clickable { activeBanner.value = null }
                    ) {
                        Row(
                            modifier = Modifier.padding(horizontal = 12.dp, vertical = 10.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Icon(Icons.Default.NotificationsActive, null, tint = Color.White, modifier = Modifier.size(18.dp))
                            Spacer(Modifier.width(8.dp))
                            Text("[$proto] $msg", color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.SemiBold, maxLines = 2)
                        }
                    }
                }
            }
        }

        if (showAlertMenu.value) {
            AlertSettingsDialog(onDismiss = { showAlertMenu.value = false })
        }

        if (showClearDialog.value) {
            AlertDialog(
                onDismissRequest = { showClearDialog.value = false },
                containerColor = Color(0xFF161B22),
                titleContentColor = Color.White,
                title = { Text("Clear Captured Data", fontWeight = FontWeight.Bold, fontSize = 17.sp) },
                text = {
                    Text(
                        "Clear decoded packets, sweep/hit counters, and signal view. You can also delete saved .iq, .pcap, and GPS CSV test files from disk.",
                        color = Color(0xFFC9D1D9),
                        fontSize = 13.sp
                    )
                },
                confirmButton = {
                    Column(horizontalAlignment = Alignment.End, verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Button(
                            onClick = {
                                clearCapturedData(deleteFiles = false)
                                showClearDialog.value = false
                            }
                        ) {
                            Text("Clear Log Only")
                        }
                        Button(
                            onClick = {
                                clearCapturedData(deleteFiles = true)
                                showClearDialog.value = false
                            },
                            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFDA3633))
                        ) {
                            Text("Clear Log + Saved Files")
                        }
                    }
                },
                dismissButton = {
                    TextButton(onClick = { showClearDialog.value = false }) {
                        Text("Cancel", color = Color(0xFF8B949E))
                    }
                }
            )
        }
    }

    @Composable
    fun AlertSettingsDialog(onDismiss: () -> Unit) {
        val cfg = alertConfig.value
        AlertDialog(
            onDismissRequest = onDismiss,
            containerColor = Color(0xFF161B22),
            titleContentColor = Color.White,
            title = {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text("Signal Alerts", fontWeight = FontWeight.Bold, fontSize = 18.sp)
                    TextButton(onClick = {
                        triggerHitAlert("SPaT", "Test Alert — Traffic Light Signal Detected", forceTest = true)
                    }) {
                        Text("Test Chime", color = Color(0xFF58A6FF), fontSize = 12.sp)
                    }
                }
            },
            text = {
                Column(
                    modifier = Modifier.verticalScroll(rememberScrollState()),
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    // Quick Presets
                    Text("Quick Presets", color = Color(0xFF8B949E), fontSize = 11.sp, fontWeight = FontWeight.Bold)
                    Row(
                        modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                        horizontalArrangement = Arrangement.spacedBy(6.dp)
                    ) {
                        AssistChip(onClick = { updateAlertConfig(AlertConfig.fieldDrive()) }, label = { Text("Field Drive", fontSize = 11.sp) })
                        AssistChip(onClick = { updateAlertConfig(AlertConfig.trafficLightsOnly()) }, label = { Text("Lights Only", fontSize = 11.sp) })
                        AssistChip(onClick = { updateAlertConfig(AlertConfig.emergenciesOnly()) }, label = { Text("Emergencies", fontSize = 11.sp) })
                        AssistChip(onClick = { updateAlertConfig(AlertConfig.silent()) }, label = { Text("Silent", fontSize = 11.sp) })
                    }

                    HorizontalDivider(color = Color(0xFF30363D))

                    // Delivery & Anti-Spam
                    Text("Delivery & Anti-Spam", color = Color(0xFF8B949E), fontSize = 11.sp, fontWeight = FontWeight.Bold)
                    AlertSwitchRow("Sound Chime", "Distinct tones for Emergency vs. Traffic Light", cfg.soundEnabled) {
                        updateAlertConfig(cfg.copy(soundEnabled = it))
                    }
                    AlertSwitchRow("Vibration", "Double pulse for Hazards/Emergency", cfg.vibrateEnabled) {
                        updateAlertConfig(cfg.copy(vibrateEnabled = it))
                    }
                    AlertSwitchRow("On-Screen Banner", "Pop-up alert banner on any tab", cfg.bannerEnabled) {
                        updateAlertConfig(cfg.copy(bannerEnabled = it))
                    }
                    AlertSwitchRow("New Intersection / ID Only", "Alert once per new RSU or vehicle ID", cfg.newIdOnly) {
                        seenEntityKeys.clear()
                        updateAlertConfig(cfg.copy(newIdOnly = it))
                    }

                    Text("Alert Cooldown", color = Color(0xFFC9D1D9), fontSize = 12.sp)
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        listOf(2, 5, 10, 30).forEach { sec ->
                            FilterChip(
                                selected = cfg.cooldownSec == sec,
                                onClick = { updateAlertConfig(cfg.copy(cooldownSec = sec)) },
                                label = { Text("${sec}s", fontSize = 11.sp) }
                            )
                        }
                    }

                    HorizontalDivider(color = Color(0xFF30363D))

                    // Protocol Category Toggles
                    Text("Alert on Signal Types", color = Color(0xFF8B949E), fontSize = 11.sp, fontWeight = FontWeight.Bold)
                    AlertCategory.entries.forEach { cat ->
                        val enabled = cat in cfg.enabledCategories
                        AlertSwitchRow(cat.title, cat.subtitle, enabled) { checked ->
                            val next = if (checked) cfg.enabledCategories + cat else cfg.enabledCategories - cat
                            updateAlertConfig(cfg.copy(enabledCategories = next))
                        }
                    }
                }
            },
            confirmButton = {
                Button(onClick = onDismiss) {
                    Text("Done")
                }
            }
        )
    }

    @Composable
    fun AlertSwitchRow(title: String, subtitle: String, checked: Boolean, onCheckedChange: (Boolean) -> Unit) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(modifier = Modifier.weight(1f).padding(end = 8.dp)) {
                Text(title, color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.Medium)
                Text(subtitle, color = Color(0xFF8B949E), fontSize = 11.sp)
            }
            Switch(checked = checked, onCheckedChange = onCheckedChange)
        }
    }

    private fun makeDotDrawable(fillArgb: Int, strokeArgb: Int, radiusPx: Int = 18): BitmapDrawable {
        val size = radiusPx * 2 + 8
        val bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888)
        val c = AndroidCanvas(bmp)
        val cx = size / 2f
        val cy = size / 2f
        val fillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = fillArgb
            style = Paint.Style.FILL
        }
        val strokePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = strokeArgb
            style = Paint.Style.STROKE
            strokeWidth = 4f
        }
        c.drawCircle(cx, cy, radiusPx.toFloat(), fillPaint)
        c.drawCircle(cx, cy, radiusPx.toFloat(), strokePaint)
        return BitmapDrawable(resources, bmp)
    }

    @Composable
    fun MapTab() {
        var selectedHit by remember { mutableStateOf<SignalMapHit?>(null) }
        var selectedRsu by remember { mutableStateOf<RsuSite?>(null) }
        var mapViewRef by remember { mutableStateOf<MapView?>(null) }

        val myLat = currentLat.value
        val myLon = currentLon.value
        val prox = nearestRsu.value
        val hitsSnapshot = signalMapHits.toList()
        val rsusSnapshot = rsuSites.toList()

        val redDotIcon = remember { makeDotDrawable(0xFFE53935.toInt(), 0xFFFFFFFF.toInt(), 16) }
        val rsuPinIcon = remember { makeDotDrawable(0xFF00BCD4.toInt(), 0xFFFFFFFF.toInt(), 13) }
        val meDotIcon = remember { makeDotDrawable(0xFF2979FF.toInt(), 0xFFFFFFFF.toInt(), 17) }

        Box(modifier = Modifier.fillMaxSize()) {
            AndroidView(
                modifier = Modifier.fillMaxSize(),
                factory = { ctx ->
                    MapView(ctx).apply {
                        setTileSource(TileSourceFactory.MAPNIK)
                        setMultiTouchControls(true)
                        controller.setZoom(12.5)
                        val startPt = if (myLat != null && myLon != null) {
                            GeoPoint(myLat, myLon)
                        } else {
                            GeoPoint(40.7480, -74.1750) // Newark / NYC corridor default
                        }
                        controller.setCenter(startPt)
                        mapViewRef = this
                    }
                },
                update = { mv ->
                    mv.overlays.clear()

                    // 1. Highlighted registered V2X routes / corridors (cyan polylines)
                    for (corridor in RsuDatabase.REGISTERED_CORRIDORS) {
                        val poly = Polyline(mv).apply {
                            setPoints(corridor.points.map { (la, lo) -> GeoPoint(la, lo) })
                            outlinePaint.color = 0xCC00D4FF.toInt()
                            outlinePaint.strokeWidth = 10f
                        }
                        mv.overlays.add(poly)
                    }

                    // 2. Registered RSU sites + reception radius circles
                    for (site in rsusSnapshot) {
                        val pt = GeoPoint(site.lat, site.lon)
                        val circle = Polygon(mv).apply {
                            points = Polygon.pointsAsCircle(pt, site.rangeM)
                            fillPaint.color = 0x2200D4FF
                            outlinePaint.color = 0x8800D4FF.toInt()
                            outlinePaint.strokeWidth = 2f
                        }
                        mv.overlays.add(circle)

                        val marker = Marker(mv).apply {
                            position = pt
                            icon = rsuPinIcon
                            setAnchor(Marker.ANCHOR_CENTER, Marker.ANCHOR_CENTER)
                            title = site.name
                            setOnMarkerClickListener { _, _ ->
                                selectedHit = null
                                selectedRsu = site
                                true
                            }
                        }
                        mv.overlays.add(marker)
                    }

                    // 3. Phone GPS position (blue dot)
                    if (myLat != null && myLon != null) {
                        val meMarker = Marker(mv).apply {
                            position = GeoPoint(myLat, myLon)
                            icon = meDotIcon
                            setAnchor(Marker.ANCHOR_CENTER, Marker.ANCHOR_CENTER)
                            title = "Your Phone"
                        }
                        mv.overlays.add(meMarker)
                    }

                    // 4. Received signals mapped as clickable RED DOTS
                    for (hit in hitsSnapshot) {
                        val hitMarker = Marker(mv).apply {
                            position = GeoPoint(hit.lat, hit.lon)
                            icon = redDotIcon
                            setAnchor(Marker.ANCHOR_CENTER, Marker.ANCHOR_CENTER)
                            title = "${hit.protocol} @ ${"%.1f".format(hit.freqMhz)} MHz"
                            setOnMarkerClickListener { _, _ ->
                                selectedRsu = null
                                selectedHit = hit
                                true
                            }
                        }
                        mv.overlays.add(hitMarker)
                    }

                    mv.invalidate()
                }
            )

            // Top Proximity & Controls Overlay
            Card(
                colors = CardDefaults.cardColors(containerColor = Color(0xEE161B22)),
                shape = RoundedCornerShape(12.dp),
                modifier = Modifier
                    .align(Alignment.TopCenter)
                    .padding(10.dp)
                    .fillMaxWidth()
            ) {
                Column(Modifier.padding(10.dp)) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(Modifier.weight(1f)) {
                            val proxText = when {
                                prox != null && prox.inRange ->
                                    "★ IN RANGE: ${prox.site.name} (${prox.distanceM.toInt()}m)"
                                prox != null ->
                                    "Nearest RSU: ${prox.site.name} (${"%.2f".format(prox.distanceM / 1000.0)} km)"
                                myLat == null ->
                                    "Waiting for GPS lock (${rsusSnapshot.size} RSUs loaded)"
                                else ->
                                    "${rsusSnapshot.size} registered RSUs loaded"
                            }
                            Text(
                                proxText,
                                color = if (prox?.inRange == true) Color(0xFF3FB950) else Color.White,
                                fontSize = 12.sp,
                                fontWeight = FontWeight.Bold,
                                maxLines = 2
                            )
                            Text(
                                "Cyan = Registered V2X Routes/RSUs  •  Red = Received Signals (${hitsSnapshot.size})",
                                color = Color(0xFF8B949E),
                                fontSize = 10.sp
                            )
                        }
                        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                            OutlinedButton(
                                onClick = {
                                    if (myLat != null && myLon != null) {
                                        mapViewRef?.controller?.animateTo(GeoPoint(myLat, myLon))
                                        mapViewRef?.controller?.setZoom(14.5)
                                    }
                                },
                                contentPadding = PaddingValues(horizontal = 8.dp, vertical = 4.dp)
                            ) {
                                Text("Me", fontSize = 11.sp)
                            }
                            OutlinedButton(
                                onClick = {
                                    mapViewRef?.controller?.animateTo(GeoPoint(40.7480, -74.1750))
                                    mapViewRef?.controller?.setZoom(12.0)
                                },
                                contentPadding = PaddingValues(horizontal = 8.dp, vertical = 4.dp)
                            ) {
                                Text("RSUs", fontSize = 11.sp)
                            }
                        }
                    }
                }
            }

            // Bottom Detail Card when a RED SIGNAL DOT is clicked
            selectedHit?.let { hit ->
                Card(
                    colors = CardDefaults.cardColors(containerColor = Color(0xF5161B22)),
                    shape = RoundedCornerShape(14.dp),
                    modifier = Modifier
                        .align(Alignment.BottomCenter)
                        .padding(10.dp)
                        .fillMaxWidth()
                ) {
                    Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Box(
                                    modifier = Modifier
                                        .clip(RoundedCornerShape(6.dp))
                                        .background(Color(0xFFDA3633))
                                        .padding(horizontal = 8.dp, vertical = 2.dp)
                                ) {
                                    Text(hit.protocol, color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                                }
                                Spacer(Modifier.width(8.dp))
                                Text("${hit.timestamp}  •  ${"%.1f".format(hit.freqMhz)} MHz", color = Color(0xFF58A6FF), fontSize = 11.sp, fontFamily = FontFamily.Monospace)
                            }
                            IconButton(onClick = { selectedHit = null }, modifier = Modifier.size(24.dp)) {
                                Icon(Icons.Default.Close, contentDescription = "Close", tint = Color(0xFF8B949E))
                            }
                        }
                        Text(hit.summary, color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                        Text(
                            "GPS: ${"%.5f".format(hit.lat)}, ${"%.5f".format(hit.lon)}  |  RSSI: ${"%.1f".format(hit.rssiDbfs)} dBFS",
                            color = Color(0xFF8B949E),
                            fontSize = 11.sp,
                            fontFamily = FontFamily.Monospace
                        )
                        hit.details.entries.take(4).forEach { (k, v) ->
                            Text("$k: $v", color = Color(0xFFC9D1D9), fontSize = 11.sp)
                        }
                        Button(
                            onClick = {
                                if (hit.spat != null) latestSpat.value = hit.spat
                                selectedTab.value = 3 // Jump to SPaT Signal View
                            },
                            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF238636)),
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Icon(Icons.Default.Traffic, contentDescription = null, modifier = Modifier.size(18.dp))
                            Spacer(Modifier.width(6.dp))
                            Text("View SPaT Signal Status", fontWeight = FontWeight.Bold)
                        }
                    }
                }
            }

            // Bottom Detail Card when a CYAN REGISTERED RSU PIN is clicked
            selectedRsu?.let { site ->
                val distStr = if (myLat != null && myLon != null) {
                    val d = RsuDatabase.haversineMeters(myLat, myLon, site.lat, site.lon)
                    if (d <= site.rangeM) "IN RANGE (${d.toInt()} m)" else "${"%.2f".format(d / 1000.0)} km away"
                } else "GPS unavailable"
                Card(
                    colors = CardDefaults.cardColors(containerColor = Color(0xF5161B22)),
                    shape = RoundedCornerShape(14.dp),
                    modifier = Modifier
                        .align(Alignment.BottomCenter)
                        .padding(10.dp)
                        .fillMaxWidth()
                ) {
                    Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text(site.name, color = Color.White, fontSize = 14.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                            IconButton(onClick = { selectedRsu = null }, modifier = Modifier.size(24.dp)) {
                                Icon(Icons.Default.Close, contentDescription = "Close", tint = Color(0xFF8B949E))
                            }
                        }
                        Text(
                            "Callsign: ${site.callsign}  •  ${site.protocol}  •  ${"%.1f".format(site.freqMhz)} MHz",
                            color = Color(0xFF58A6FF),
                            fontSize = 11.sp,
                            fontFamily = FontFamily.Monospace
                        )
                        Text(
                            "Proximity: $distStr  |  Est. Range: ${site.rangeM.toInt()} m",
                            color = Color(0xFF3FB950),
                            fontSize = 12.sp,
                            fontWeight = FontWeight.SemiBold
                        )
                        Button(
                            onClick = { selectedTab.value = 3 },
                            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF238636)),
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Icon(Icons.Default.Traffic, contentDescription = null, modifier = Modifier.size(18.dp))
                            Spacer(Modifier.width(6.dp))
                            Text("View SPaT Signal Status", fontWeight = FontWeight.Bold)
                        }
                    }
                }
            }
        }
    }

    @Composable
    fun TrafficLightTab() {
        val spat = latestSpat.value
        val activePhase = spat?.phases?.firstOrNull { it.signalGroup == selectedSignalGroup.value }
            ?: spat?.phases?.firstOrNull()

        Column(
            modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            // Channel preset bar
            Row(
                modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                CHANNEL_PRESETS.forEachIndexed { idx, preset ->
                    FilterChip(
                        selected = selectedPresetIdx.value == idx,
                        onClick = { selectedPresetIdx.value = idx },
                        label = { Text(preset.label, fontSize = 12.sp) }
                    )
                }
            }

            Spacer(Modifier.height(12.dp))

            // Intersection Header Card
            Card(
                colors = CardDefaults.cardColors(containerColor = Color(0xFF161B22)),
                modifier = Modifier.fillMaxWidth()
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth().padding(12.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column {
                        Text(
                            spat?.intersectionName ?: "Waiting for SPaT Signal...",
                            color = Color.White,
                            fontWeight = FontWeight.Bold,
                            fontSize = 16.sp
                        )
                        Text(
                            activePhase?.rawEventState ?: "Tuned to ${"%.1f".format(currentFreqMhz.value)} MHz",
                            color = Color(0xFF8B949E),
                            fontSize = 12.sp
                        )
                    }
                    Text(
                        activePhase?.countdownSec?.let { "${"%.1f".format(it)}s" } ?: "--.-s",
                        color = when (activePhase?.color) {
                            LightColor.RED, LightColor.FLASHING_RED -> Color(0xFFFF5555)
                            LightColor.YELLOW -> Color(0xFFFFD600)
                            LightColor.GREEN, LightColor.PROTECTED_GREEN -> Color(0xFF00E676)
                            else -> Color(0xFF8B949E)
                        },
                        fontSize = 28.sp,
                        fontWeight = FontWeight.Bold,
                        fontFamily = FontFamily.Monospace
                    )
                }
            }

            Spacer(Modifier.height(18.dp))

            // Realistic Traffic Light Head
            TrafficSignalHead(
                color = activePhase?.color ?: LightColor.DARK
            )

            Spacer(Modifier.height(18.dp))

            // Signal Group / Approach selector
            if (spat != null && spat.phases.isNotEmpty()) {
                Text("Signal Groups at Intersection #${spat.intersectionId}", color = Color(0xFF8B949E), fontSize = 12.sp)
                Spacer(Modifier.height(8.dp))
                Row(
                    modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    spat.phases.forEach { ph ->
                        val dotColor = when (ph.color) {
                            LightColor.RED, LightColor.FLASHING_RED -> Color(0xFFFF3B30)
                            LightColor.YELLOW -> Color(0xFFFFCC00)
                            LightColor.GREEN, LightColor.PROTECTED_GREEN -> Color(0xFF34C759)
                            LightColor.DARK -> Color.Gray
                        }
                        FilterChip(
                            selected = activePhase?.signalGroup == ph.signalGroup,
                            onClick = { selectedSignalGroup.value = ph.signalGroup },
                            leadingIcon = {
                                Box(Modifier.size(10.dp).clip(CircleShape).background(dotColor))
                            },
                            label = {
                                Text("SG ${ph.signalGroup} · ${ph.label}", fontSize = 12.sp)
                            }
                        )
                    }
                }
            } else {
                Text(
                    "Connect HackRF One via USB-C OTG & tap Start,\nor toggle 'SPaT Demo' above to preview live decoding.",
                    color = Color(0xFF8B949E),
                    fontSize = 13.sp
                )
            }
        }
    }

    @Composable
    fun TrafficSignalHead(color: LightColor) {
        val redLit = color == LightColor.RED || color == LightColor.FLASHING_RED
        val yellowLit = color == LightColor.YELLOW
        val greenLit = color == LightColor.GREEN || color == LightColor.PROTECTED_GREEN
        val isArrow = color == LightColor.PROTECTED_GREEN

        Column(
            modifier = Modifier
                .width(142.dp)
                .clip(RoundedCornerShape(28.dp))
                .background(Color(0xFF1C2128))
                .border(3.dp, Color(0xFF30363D), RoundedCornerShape(28.dp))
                .padding(vertical = 18.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            SignalLens(lit = redLit, activeColor = Color(0xFFFF2D20), dimColor = Color(0xFF3B1212))
            SignalLens(lit = yellowLit, activeColor = Color(0xFFFFD60A), dimColor = Color(0xFF3B3210))
            SignalLens(
                lit = greenLit,
                activeColor = Color(0xFF00E676),
                dimColor = Color(0xFF0F3522),
                showLeftArrow = isArrow
            )
        }
    }

    @Composable
    fun SignalLens(lit: Boolean, activeColor: Color, dimColor: Color, showLeftArrow: Boolean = false) {
        Box(
            contentAlignment = Alignment.Center,
            modifier = Modifier
                .size(88.dp)
                .clip(CircleShape)
                .background(
                    if (lit) {
                        Brush.radialGradient(listOf(Color.White, activeColor, activeColor.copy(alpha = 0.7f)))
                    } else {
                        Brush.radialGradient(listOf(dimColor, Color(0xFF101418)))
                    }
                )
                .border(3.dp, if (lit) activeColor else Color(0xFF2B3138), CircleShape)
        ) {
            if (showLeftArrow && lit) {
                Icon(
                    Icons.AutoMirrored.Filled.ArrowBack,
                    contentDescription = "Protected Left",
                    tint = Color(0xFF003314),
                    modifier = Modifier.size(46.dp)
                )
            }
        }
    }

    @Composable
    fun RfScopeTab() {
        val spec = latestSpectrum.value
        Column(modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
            Card(
                colors = CardDefaults.cardColors(containerColor = Color(0xFF161B22)),
                modifier = Modifier.fillMaxWidth().height(230.dp)
            ) {
                Column(Modifier.fillMaxSize().padding(12.dp)) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Text(
                            "Live Spectrum @ ${"%.1f".format(currentFreqMhz.value)} MHz",
                            color = Color.White, fontWeight = FontWeight.Bold, fontSize = 14.sp
                        )
                        Text(
                            if (spec != null) "Floor ${"%.1f".format(spec.noiseFloorDb)} | Peak ${"%.1f".format(spec.maxPeakDb)} dBFS" else "No RF data",
                            color = Color(0xFF58A6FF), fontSize = 11.sp, fontFamily = FontFamily.Monospace
                        )
                    }
                    Spacer(Modifier.height(8.dp))
                    Canvas(modifier = Modifier.fillMaxSize()) {
                        val w = size.width
                        val h = size.height
                        // Grid lines
                        for (g in 1..3) {
                            val y = h * g / 4f
                            drawLine(Color(0xFF30363D), Offset(0f, y), Offset(w, y), 1f)
                        }
                        if (spec != null) {
                            val minDb = -90f
                            val maxDb = -30f
                            fun dbToY(db: Float): Float {
                                val norm = ((db - minDb) / (maxDb - minDb)).coerceIn(0f, 1f)
                                return h * (1f - norm)
                            }
                            val peakPath = Path()
                            val avgPath = Path()
                            for (i in 0 until 256) {
                                val x = w * i / 255f
                                val yp = dbToY(spec.peakDb[i])
                                val ya = dbToY(spec.avgDb[i])
                                if (i == 0) {
                                    peakPath.moveTo(x, yp); avgPath.moveTo(x, ya)
                                } else {
                                    peakPath.lineTo(x, yp); avgPath.lineTo(x, ya)
                                }
                            }
                            drawPath(peakPath, Color(0xFFF85149), style = Stroke(width = 2f))
                            drawPath(avgPath, Color(0xFF58A6FF), style = Stroke(width = 2.5f))
                        }
                    }
                }
            }

            Spacer(Modifier.height(14.dp))

            // Gain & Recording Controls
            Card(colors = CardDefaults.cardColors(containerColor = Color(0xFF161B22)), modifier = Modifier.fillMaxWidth()) {
                Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("HackRF One RF Settings (Bias-T always OFF)", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 14.sp)
                    Text("LNA Gain: ${lnaGain.value} dB", color = Color(0xFFC9D1D9), fontSize = 12.sp)
                    Slider(value = lnaGain.value.toFloat(), onValueChange = { lnaGain.value = (it.toInt() / 8) * 8 }, valueRange = 0f..40f, steps = 4)

                    Text("VGA Gain: ${vgaGain.value} dB", color = Color(0xFFC9D1D9), fontSize = 12.sp)
                    Slider(value = vgaGain.value.toFloat(), onValueChange = { vgaGain.value = (it.toInt() / 2) * 2 }, valueRange = 0f..62f)

                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                        Text("RF Amp (+14 dB)", color = Color(0xFFC9D1D9), fontSize = 13.sp)
                        Switch(checked = ampEnabled.value, onCheckedChange = { ampEnabled.value = it })
                    }
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                        Text("Save raw .iq, .pcap & GPS CSV on hit", color = Color(0xFFC9D1D9), fontSize = 13.sp)
                        Switch(checked = recordIqOnHit.value, onCheckedChange = { recordIqOnHit.value = it })
                    }
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                        Text("Signal Alerts (${alertConfig.value.enabledCategories.size}/7 active)", color = Color(0xFFC9D1D9), fontSize = 13.sp)
                        OutlinedButton(onClick = { showAlertMenu.value = true }) {
                            Text("Configure", fontSize = 12.sp)
                        }
                    }
                    val gpsStr = if (currentLat.value != null && currentLon.value != null) {
                        "GPS Lock: ${"%.5f".format(currentLat.value)}, ${"%.5f".format(currentLon.value)}"
                    } else "GPS: Waiting for lock..."
                    Text(gpsStr, color = Color(0xFF3FB950), fontSize = 11.sp, fontFamily = FontFamily.Monospace)
                    Text("Captures: ${logger.captureDirPath}", color = Color(0xFF8B949E), fontSize = 10.sp)
                }
            }
        }
    }

    @Composable
    fun ProtocolLogTab() {
        var expandedId by remember { mutableStateOf<Long?>(null) }

        if (packetLog.isEmpty()) {
            Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
                Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Text(
                        if (isRunning.value) {
                            "Listening @ ${"%.1f".format(currentFreqMhz.value)} MHz — waiting for decoded 802.11p / Wi-Fi packets..."
                        } else {
                            "No decoded packets yet."
                        },
                        color = Color(0xFF8B949E),
                        fontSize = 14.sp
                    )
                    if (!isRunning.value) {
                        Button(
                            onClick = { toggleRun() },
                            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF238636)),
                            shape = RoundedCornerShape(10.dp)
                        ) {
                            Icon(Icons.Default.PlayArrow, contentDescription = null, modifier = Modifier.size(18.dp))
                            Spacer(Modifier.width(6.dp))
                            Text("Start Listening", fontWeight = FontWeight.Bold)
                        }
                    }
                }
            }
            return
        }

        Column(modifier = Modifier.fillMaxSize()) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 12.dp, vertical = 6.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    "${packetLog.size} decoded packet(s)",
                    color = Color(0xFF8B949E),
                    fontSize = 12.sp,
                    fontFamily = FontFamily.Monospace
                )
                TextButton(
                    onClick = { showClearDialog.value = true },
                    contentPadding = PaddingValues(horizontal = 10.dp, vertical = 2.dp)
                ) {
                    Icon(Icons.Default.DeleteSweep, contentDescription = null, tint = Color(0xFFF85149), modifier = Modifier.size(16.dp))
                    Spacer(Modifier.width(4.dp))
                    Text("Clear Data", color = Color(0xFFF85149), fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                }
            }
            LazyColumn(
                modifier = Modifier.weight(1f).fillMaxWidth().padding(horizontal = 12.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
            items(packetLog, key = { it.id }) { pkt ->
                val expanded = expandedId == pkt.id
                val badgeColor = when (pkt.protocol) {
                    "SPaT", "SPATEM" -> Color(0xFF238636)
                    "MAP", "MAPEM" -> Color(0xFF1F6FEB)
                    "BSM" -> Color(0xFFD29922)
                    "TIM", "RSA" -> Color(0xFFE36209)
                    "SRM", "SSM" -> Color(0xFF9E6A03)
                    "RTCM" -> Color(0xFF2EA043)
                    "PSM" -> Color(0xFFDB61A2)
                    "EVA", "DENM" -> Color(0xFFDA3633)
                    "CAM" -> Color(0xFF0096C7)
                    "WSMP", "GN" -> Color(0xFF8957E5)
                    else -> Color(0xFF484F58)
                }
                Card(
                    colors = CardDefaults.cardColors(containerColor = Color(0xFF161B22)),
                    modifier = Modifier.fillMaxWidth().clickable {
                        expandedId = if (expanded) null else pkt.id
                    }
                ) {
                    Column(Modifier.padding(12.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Box(
                                modifier = Modifier
                                    .clip(RoundedCornerShape(6.dp))
                                    .background(badgeColor)
                                    .padding(horizontal = 8.dp, vertical = 2.dp)
                            ) {
                                Text(pkt.protocol, color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                            }
                            Spacer(Modifier.width(8.dp))
                            Text(pkt.timestamp, color = Color(0xFF8B949E), fontSize = 11.sp, fontFamily = FontFamily.Monospace)
                            Spacer(Modifier.weight(1f))
                            Text("${"%.1f".format(pkt.freqMhz)} MHz", color = Color(0xFF58A6FF), fontSize = 11.sp)
                        }
                        Spacer(Modifier.height(6.dp))
                        Text(pkt.summary, color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.Medium)

                        if (expanded) {
                            Spacer(Modifier.height(8.dp))
                            HorizontalDivider(color = Color(0xFF30363D))
                            Spacer(Modifier.height(6.dp))
                            pkt.details.forEach { (k, v) ->
                                Row(Modifier.fillMaxWidth().padding(vertical = 1.dp)) {
                                    Text("$k: ", color = Color(0xFF8B949E), fontSize = 11.sp, fontWeight = FontWeight.Bold)
                                    Text(v, color = Color(0xFFC9D1D9), fontSize = 11.sp, fontFamily = FontFamily.Monospace)
                                }
                            }
                            Spacer(Modifier.height(6.dp))
                            Text("HEX: ${pkt.hexPreview}", color = Color(0xFF7EE787), fontSize = 10.sp, fontFamily = FontFamily.Monospace)
                        }
                    }
                }
            }
            }
        }
    }
}

@Composable
fun TrafficLightV2xTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = darkColorScheme(
            primary = Color(0xFF58A6FF),
            secondary = Color(0xFF3FB950),
            background = Color(0xFF0D1117),
            surface = Color(0xFF161B22)
        ),
        content = content
    )
}
