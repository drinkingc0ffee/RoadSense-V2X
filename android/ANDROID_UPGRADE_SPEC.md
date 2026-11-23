# GEMINI_SPEC: Android RoadSense V2X — Dual-Channel Safety Scan + Auto-Lock + Continuous Stream

TARGET_FILES:
1. `android/app/src/main/cpp/dsrc_dsp.cpp`
2. `android/app/src/main/java/com/trafficlight/v2x/dsp/DsrcDsp.kt`
3. `android/app/src/main/java/com/trafficlight/v2x/usb/HackRfUsbDriver.kt`
4. `android/app/src/main/java/com/trafficlight/v2x/MainActivity.kt`
5. `android/app/build.gradle.kts` (bump `versionCode` + `versionName`)

REFERENCE_IMPL (already working & verified on macOS):
- `macos/dsrc_dsp_mac.cpp` (`extract_subchannel_10m`)
- `macos/roadsense_cli.py` (`SAFETY_SCAN_HOPS`, `LEGACY_SCAN_HOPS`, `process_dual_or_single_iq`, Auto-Lock state machine)

---

## 1. JNI C++ (`android/app/src/main/cpp/dsrc_dsp.cpp`)
Add JNI export:
```cpp
extern "C" JNIEXPORT jbyteArray JNICALL
Java_com_trafficlight_v2x_dsp_DsrcDsp_extractSubchannel10M(
    JNIEnv* env, jobject, jbyteArray iq20mBytes, jint dir)
```
Algorithm (exact port of `macos/dsrc_dsp_mac.cpp::extract_subchannel_10m`):
- `dir == -1` -> lower 10 MHz sub-channel (`Fc - 5 MHz`, multiply by `exp(+j*pi/2*n)`: `n&3`: `0:(re,im)`, `1:(-im,re)`, `2:(-re,-im)`, `3:(im,-re)`).
- `dir == +1` -> upper 10 MHz sub-channel (`Fc + 5 MHz`, multiply by `exp(-j*pi/2*n)`: `n&3`: `0:(re,im)`, `1:(im,-re)`, `2:(-re,-im)`, `3:(-im,re)`).
- 11-tap half-band FIR (7 non-zero taps) + decimate ÷2 at `c = 2*m + 5`:
  - `h0=0.50000f, h1=0.31120f, h3=-0.08020f, h5=0.01900f`
  - `out[2*m] = clamp(lround(oi), -128, 127)`, `out[2*m+1] = clamp(lround(oq), -128, 127)`
- Return `jbyteArray` of size `n_out * 2`.

## 2. Kotlin JNI Wrapper (`DsrcDsp.kt`)
Add:
```kotlin
external fun extractSubchannel10M(iq20mBytes: ByteArray, dir: Int): ByteArray
```

## 3. Continuous Streaming Mode (`HackRfUsbDriver.kt`)
Current `captureIqChunk` calls `setTransceiverMode(MODE_RECEIVE)` -> read -> `setTransceiverMode(MODE_OFF)` on EVERY chunk.
Add persistent RX stream API for locked / single-channel zero-gap capture:
- State fields: `private var streamingActive = false`, `private val activeRequests = ArrayList<UsbRequest>()`, `private val activeInFlight = HashSet<UsbRequest>()`
- `fun startContinuousRx(freqHz: Long, sampleRateHz: Int, lnaGain: Int, vgaGain: Int, ampEnable: Boolean): Boolean`
  - If `streamingActive` and all cached RF params match -> return `true` (no-op, keep URBs flowing!).
  - Otherwise `stopContinuousRx()`, apply changed RF params (same cache logic, `setAntennaEnable(false)`), `setTransceiverMode(MODE_RECEIVE)`, allocate & queue `QUEUE_DEPTH=8` `UsbRequest`s with `ByteBuffer.allocateDirect(16384)`, discard initial `15 ms` startup bytes once, set `streamingActive = true`.
- `fun readContinuousChunk(totalBytes: Int): ByteArray?`
  - Reap `conn.requestWait(1500)` into `out = ByteArray(totalBytes)`, immediately re-queue each completed `UsbRequest` so URB pipeline never empties (0% blind time!).
  - On timeout/null -> `stopContinuousRx()`, `clearEndpointHalt(ep)`, return `null`.
- `fun stopContinuousRx()`
  - `setTransceiverMode(MODE_OFF)`, cancel & drain `activeInFlight` (`requestWait(50)`), close `activeRequests`, `streamingActive = false`.
- Call `stopContinuousRx()` inside `close()`.

## 4. Scan->Lock State Machine & UI (`MainActivity.kt`)
### Presets (`CHANNEL_PRESETS`)
Replace `Scan All DSRC` (7-hop) with:
- `enum class ScanMode { SINGLE, DUAL_SAFETY, DUAL_ALL }`
- `ChannelPreset("Smart Safety Scan", 5905.0, 20_000_000, ScanMode.DUAL_SAFETY)` — **DEFAULT (`selectedPresetIdx = 0`)**:
  - Hop 0: `5905.0 MHz @ 20 MS/s` -> `extractSubchannel10M(-1)` = `5900` (Ch 180) + `extractSubchannel10M(+1)` = `5910` (Ch 182)
  - Hop 1: `5915.0 MHz @ 20 MS/s` -> `extractSubchannel10M(-1)` = `5910` (Ch 182) + `extractSubchannel10M(+1)` = `5920` (Ch 184) + check full 20M spectrum for C-V2X Ch 183
- `ChannelPreset("Safety + Legacy Scan", 5905.0, 20_000_000, ScanMode.DUAL_ALL)`:
  - Adds Hop 2 (`5860.0 @ 10M`) and Hop 3 (`5890.0 @ 10M`)
- Keep individual presets (`Ch 183`, `Ch 181`, `Ch 180`, `Ch 182`, `Ch 184`, `Ch 172`, `Ch 178`, `Wi-Fi Test · 5745`).

### Configurable Lock Rate & Auto-Lock State
Add Compose state:
- `private val lockRateHz = mutableStateOf(10_000_000)` (toggleable `10 MS/s` / `20 MS/s` in `RfScopeTab` via `FilterChip` pair)
- `private val lockedFreqMhz = mutableStateOf<Double?>(null)`
- `private val lockUntilMs = mutableStateOf(0L)`
- Constant `LOCK_DURATION_MS = 8_000L`

### Worker Loop (`startLiveHackRf`)
1. If `lockedFreqMhz != null && System.currentTimeMillis() > lockUntilMs`: clear `lockedFreqMhz = null`.
2. Determine capture target:
   - If `lockedFreqMhz != null`: use `startContinuousRx(lockedFreq, lockedRate, ...)` + `readContinuousChunk(bytes)` (`trimSamples = 0`, 0% blind time!).
   - Else if `preset.mode == SINGLE`: use `startContinuousRx(preset.freq, preset.rate, ...)` + `readContinuousChunk(bytes)` (`trimSamples = 0`).
   - Else (`DUAL_SAFETY` / `DUAL_ALL`): `stopContinuousRx()`, run `captureIqChunk(hop.center, hop.rate, ...)` (`duration = 0.15s`, `trimSamples = (sr * 0.015).toInt()`).
3. Decode:
   - For dual 20M hop: call `DsrcDsp.extractSubchannel10M(iq, -1)` & `(+1)`, run `analyzeSpectrum` + `decode80211pFrames` on each 10M buffer (`subTrim = trimSamples / 2`), plus check full 20M `analyzeSpectrum` for `C-V2X` if `hop.checkCv2x`.
   - On ANY decoded frame OR wideband burst (`avgBinsAbove >= 28 || (burstJumpDb >= 6.0f && peakBinsAbove >= 24)`):
     - If in scan mode, set `lockedFreqMhz = hitFreq`, `lockedRate = if (isCv2x) 20_000_000 else lockRateHz.value`, `lockUntilMs = now + LOCK_DURATION_MS`.
4. UI additions in `RfScopeTab`:
   - `Lock Stream Rate`: `[10 MS/s]` `[20 MS/s]` `FilterChip`s
   - When `lockedFreqMhz != null`: show green `LOCKED @ 5900.0 MHz` badge + `Unlock` `OutlinedButton` (sets `lockedFreqMhz = null`).

## 5. Shared RSU Proximity Database (`RsuDatabase.kt` + `MainActivity.kt`)
Reads the exact CSV produced by `macos/find_rsus.py` pushed to:
`/sdcard/Android/data/com.trafficlight.v2x/files/v2x_captures/rsus.csv`
CSV header: `name,callsign,lat,lon,freq_mhz,protocol,range_m,source,uls_url`

### New file: `android/app/src/main/java/com/trafficlight/v2x/storage/RsuDatabase.kt`
```kotlin
data class RsuSite(val name: String, val callsign: String, val lat: Double, val lon: Double, val freqMhz: Double, val protocol: String, val rangeM: Double)
data class RsuProximity(val site: RsuSite, val distanceM: Double, val inRange: Boolean)
```
- `RsuDatabase.load(context)`: parses `File(context.getExternalFilesDir(null), "v2x_captures/rsus.csv")` skipping rows without valid `lat`/`lon`.
- `RsuDatabase.nearest(sites, myLat, myLon): RsuProximity?`: Haversine distance in meters (`R = 6371000.0`), returns closest site with `inRange = distanceM <= site.rangeM`.

### `MainActivity.kt` integration (Device GPS ONLY — do NOT use IP geolocation on Android)
- Load `rsuSites = RsuDatabase.load(this)` in `onCreate()`.
- Add `private val nearestRsu = mutableStateOf<RsuProximity?>(null)`.
- Inside `gpsListener` (and `startGpsUpdates` initial fix): update `nearestRsu.value = RsuDatabase.nearest(rsuSites, loc.latitude, loc.longitude)`.
- UI:
  - In `MainAppScreen` top bar (or `RfScopeTab` GPS card): if `nearestRsu.value?.inRange == true`, display green badge `"★ IN RANGE: ${p.site.name} (${p.distanceM.toInt()}m · ${p.site.freqMhz.toInt()} MHz)"`; else if `nearestRsu.value != null`, display `"Nearest RSU: ${p.site.callsign} (${"%.1f".format(p.distanceM / 1000.0)} km)"`.
  - Optional: trigger one-time `triggerHitAlert("RSU_NEAR", ...)` when transitioning from `!inRange` -> `inRange` for a new `callsign`.

## 6. Verification
- `./gradlew assembleDebug` -> copy `app-debug.apk` to `RoadSenseV2X-v2.0.apk`.
- Sanity test `Wi-Fi Test · 5745` (verifies continuous `UsbRequest` streaming with 0 glitches) + `Smart Safety Scan` (verifies 2-hop dual-channel cycle and auto-lock on hit) + `adb push rsus.csv /sdcard/Android/data/com.trafficlight.v2x/files/v2x_captures/rsus.csv` (verifies IN RANGE badge).
