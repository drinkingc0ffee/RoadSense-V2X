package com.trafficlight.v2x.dsp

data class SpectrumResult(
    val avgDb: FloatArray,
    val peakDb: FloatArray,
    val noiseFloorDb: Float,
    val maxPeakDb: Float,
    val maxPeakBin: Int,
    val burstJumpDb: Float
)

object DsrcDsp {
    init {
        System.loadLibrary("dsrc_dsp")
    }

    external fun computeSpectrum(iqBytes: ByteArray, trimSamples: Int): FloatArray
    external fun decode80211pFrames(iqBytes: ByteArray, trimSamples: Int): Array<ByteArray>

    fun analyzeSpectrum(iqBytes: ByteArray, trimSamples: Int = 50_000): SpectrumResult? {
        val raw = computeSpectrum(iqBytes, trimSamples)
        if (raw.size < 516) return null
        return SpectrumResult(
            avgDb = raw.copyOfRange(0, 256),
            peakDb = raw.copyOfRange(256, 512),
            noiseFloorDb = raw[512],
            maxPeakDb = raw[513],
            maxPeakBin = raw[514].toInt(),
            burstJumpDb = raw[515]
        )
    }
}
