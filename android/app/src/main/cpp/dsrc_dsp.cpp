#include <jni.h>
#include <android/log.h>
#include <cmath>
#include <complex>
#include <cstdint>
#include <cstring>
#include <vector>
#include <algorithm>

#define LOG_TAG "DSRC_DSP"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)

using cf = std::complex<float>;
static constexpr float PI_F = 3.14159265358979323846f;

// ---------------------------------------------------------------------------
// Fast iterative in-place Radix-2 FFT (N must be power of 2)
// ---------------------------------------------------------------------------
static void fft_inplace(std::vector<cf>& a, bool inverse = false) {
    const size_t n = a.size();
    for (size_t i = 1, j = 0; i < n; i++) {
        size_t bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) std::swap(a[i], a[j]);
    }
    for (size_t len = 2; len <= n; len <<= 1) {
        float ang = 2.0f * PI_F / static_cast<float>(len) * (inverse ? 1.0f : -1.0f);
        cf wlen(std::cos(ang), std::sin(ang));
        for (size_t i = 0; i < n; i += len) {
            cf w(1.0f, 0.0f);
            for (size_t j = 0; j < len / 2; j++) {
                cf u = a[i + j];
                cf v = a[i + j + len / 2] * w;
                a[i + j] = u + v;
                a[i + j + len / 2] = u - v;
                w *= wlen;
            }
        }
    }
    if (inverse) {
        float invN = 1.0f / static_cast<float>(n);
        for (auto& x : a) x *= invN;
    }
}

// ---------------------------------------------------------------------------
// IEEE 802.11a/p Constants (64-pt OFDM, 48 data + 4 pilot subcarriers)
// ---------------------------------------------------------------------------
// Standard LTS in fftshift order (-32..+31)
static const int8_t LTS_FREQ[64] = {
    0,0,0,0,0,0,
    1,1,-1,-1,1,1,-1,1,-1,1,1,1,1,1,1,-1,-1,1,1,-1,1,-1,1,1,1,1,
    0,
    1,-1,-1,1,1,-1,1,-1,1,-1,-1,-1,-1,-1,1,1,-1,-1,1,-1,1,-1,1,1,1,1,
    0,0,0,0,0
};

// Pilot indices in shifted (-32..+31 -> 0..63) order: -21, -7, +7, +21 -> 11, 25, 39, 53
static const int PILOT_IDX[4] = {11, 25, 39, 53};
static const int8_t PILOT_BASE[4] = {1, 1, 1, -1};

// 127-element pilot polarity sequence (IEEE 802.11 Table 17-7)
static const int8_t PILOT_POLARITY[127] = {
    1,1,1,1,-1,-1,-1,1,-1,-1,-1,-1,1,1,-1,1,-1,-1,1,1,-1,1,1,-1,1,1,1,1,1,1,-1,1,
    1,1,-1,1,1,-1,-1,1,1,1,-1,1,-1,-1,-1,1,-1,1,-1,-1,1,-1,-1,1,1,1,1,1,-1,-1,1,1,
    -1,-1,1,-1,1,-1,1,1,-1,-1,-1,1,1,-1,-1,-1,-1,1,-1,-1,1,-1,1,1,1,1,-1,1,-1,1,-1,1,
    -1,-1,-1,-1,-1,1,-1,1,1,-1,1,-1,1,1,1,-1,-1,1,-1,-1,-1,1,1,1,-1,-1,-1,-1,-1,-1,-1
};

// 48 data subcarrier indices in shifted 0..63 order
static void get_data_indices(int out[48]) {
    int k = 0;
    for (int i = 6; i <= 58; i++) {
        if (i == 32 || i == 11 || i == 25 || i == 39 || i == 53) continue;
        out[k++] = i;
    }
}

// ---------------------------------------------------------------------------
// 64-State Soft-Decision Viterbi Decoder (G0 = 0133, G1 = 0171)
// ---------------------------------------------------------------------------
static inline uint8_t parity7(uint8_t x) {
    x ^= x >> 4; x ^= x >> 2; x ^= x >> 1;
    return x & 1;
}

static std::vector<uint8_t> viterbi_decode(const std::vector<float>& soft_bits, int rate_num, int rate_den) {
    // Depuncture to rate-1/2 pairs (0.0f = erased bit)
    std::vector<float> depunc;
    depunc.reserve(soft_bits.size() * 2);
    if (rate_num == 1 && rate_den == 2) {
        depunc = soft_bits;
    } else if (rate_num == 3 && rate_den == 4) {
        // Pattern: 1,1, 1,0, 0,1 (4 coded bits -> 6 depunctured)
        for (size_t i = 0; i + 3 < soft_bits.size(); i += 4) {
            depunc.push_back(soft_bits[i]);
            depunc.push_back(soft_bits[i + 1]);
            depunc.push_back(soft_bits[i + 2]);
            depunc.push_back(0.0f);
            depunc.push_back(0.0f);
            depunc.push_back(soft_bits[i + 3]);
        }
    } else if (rate_num == 2 && rate_den == 3) {
        // Pattern: 1,1, 1,0 (3 coded bits -> 4 depunctured)
        for (size_t i = 0; i + 2 < soft_bits.size(); i += 3) {
            depunc.push_back(soft_bits[i]);
            depunc.push_back(soft_bits[i + 1]);
            depunc.push_back(soft_bits[i + 2]);
            depunc.push_back(0.0f);
        }
    } else {
        depunc = soft_bits;
    }

    const size_t n_bits = depunc.size() / 2;
    if (n_bits == 0) return {};

    static bool table_init = false;
    static float out0[64][2], out1[64][2];
    if (!table_init) {
        for (int s = 0; s < 64; s++) {
            for (int b = 0; b < 2; b++) {
                uint8_t reg = static_cast<uint8_t>((b << 6) | s);
                // G0 = 133_8 = 0b1011011, G1 = 171_8 = 0b1111001
                uint8_t c0 = parity7(reg & 0x5B);
                uint8_t c1 = parity7(reg & 0x79);
                out0[s][b] = c0 ? 1.0f : -1.0f;
                out1[s][b] = c1 ? 1.0f : -1.0f;
            }
        }
        table_init = true;
    }

    const float NEG_INF = -1e30f;
    float pm[64], next_pm[64];
    for (int s = 0; s < 64; s++) pm[s] = (s == 0) ? 0.0f : NEG_INF;

    std::vector<uint8_t> trace(n_bits * 64, 0);
    for (size_t t = 0; t < n_bits; t++) {
        float r0 = depunc[2 * t];
        float r1 = depunc[2 * t + 1];
        for (int s = 0; s < 64; s++) next_pm[s] = NEG_INF;

        for (int prev = 0; prev < 64; prev++) {
            if (pm[prev] <= NEG_INF * 0.5f) continue;
            for (int b = 0; b < 2; b++) {
                int nxt = (prev >> 1) | (b << 5);
                float metric = pm[prev] + r0 * out0[prev][b] + r1 * out1[prev][b];
                if (metric > next_pm[nxt]) {
                    next_pm[nxt] = metric;
                    trace[t * 64 + nxt] = static_cast<uint8_t>((prev << 1) | b);
                }
            }
        }
        std::memcpy(pm, next_pm, sizeof(pm));
    }

    int state = 0;
    float best = pm[0];
    for (int s = 1; s < 64; s++) {
        if (pm[s] > best) { best = pm[s]; state = s; }
    }
    // Tail bits drive state to 0
    if (pm[0] > NEG_INF * 0.5f) state = 0;

    std::vector<uint8_t> decoded(n_bits, 0);
    for (int t = static_cast<int>(n_bits) - 1; t >= 0; t--) {
        uint8_t tr = trace[t * 64 + state];
        decoded[t] = tr & 1;
        state = tr >> 1;
    }
    return decoded;
}

// IEEE 802.11a/p Deinterleaver
static std::vector<float> deinterleave(const std::vector<float>& in, int n_cbps, int n_bpsc) {
    std::vector<float> out(n_cbps, 0.0f);
    int s = std::max(1, n_bpsc / 2);
    for (int j = 0; j < n_cbps; j++) {
        int i = s * (j / s) + (j + n_cbps - (16 * j) / n_cbps) % s;
        int k = 16 * i - (n_cbps - 1) * ((16 * i) / n_cbps);
        if (k >= 0 && k < n_cbps && j < (int)in.size()) {
            out[k] = in[j];
        }
    }
    return out;
}

// IEEE 802.11 CRC-32
static uint32_t crc32_80211(const uint8_t* data, size_t len) {
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++) {
            crc = (crc & 1) ? ((crc >> 1) ^ 0xEDB88320u) : (crc >> 1);
        }
    }
    return ~crc;
}

// ---------------------------------------------------------------------------
// JNI: Compute 256-bin Averaged & Peak-Hold Spectrum + Burst Jump
// Returns FloatArray[516]:
//   [0..255]   = avgDb
//   [256..511] = peakDb
//   [512]      = noiseFloorAvgDb
//   [513]      = maxPeakDb
//   [514]      = maxPeakBin
//   [515]      = burstJumpDb
// ---------------------------------------------------------------------------
extern "C" JNIEXPORT jfloatArray JNICALL
Java_com_trafficlight_v2x_dsp_DsrcDsp_computeSpectrum(
    JNIEnv* env, jobject /*thiz*/, jbyteArray iqBytes, jint trimSamples) {

    const jsize byteLen = env->GetArrayLength(iqBytes);
    const int totalSamples = byteLen / 2;
    const int nfft = 256;
    if (totalSamples <= trimSamples + nfft * 2) {
        return env->NewFloatArray(0);
    }

    jbyte* raw = env->GetByteArrayElements(iqBytes, nullptr);
    const int start = trimSamples;
    const int count = totalSamples - start;

    // Compute DC offset
    float meanI = 0.0f, meanQ = 0.0f;
    for (int i = 0; i < count; i++) {
        meanI += static_cast<float>(raw[2 * (start + i)]);
        meanQ += static_cast<float>(raw[2 * (start + i) + 1]);
    }
    meanI /= (count * 128.0f);
    meanQ /= (count * 128.0f);

    static float hann[256];
    static float winPow = 0.0f;
    if (winPow == 0.0f) {
        for (int i = 0; i < nfft; i++) {
            hann[i] = 0.5f * (1.0f - std::cos(2.0f * PI_F * i / (nfft - 1)));
            winPow += hann[i] * hann[i];
        }
    }

    const int nFrames = std::min(count / nfft, 1024);
    std::vector<float> avgPow(nfft, 0.0f);
    std::vector<float> peakPow(nfft, 0.0f);
    std::vector<float> frameDb;
    frameDb.reserve(nFrames);
    std::vector<cf> buf(nfft);

    const float scale = 1.0f / (winPow * nfft);
    for (int f = 0; f < nFrames; f++) {
        float fPow = 0.0f;
        const int base = start + f * nfft;
        for (int i = 0; i < nfft; i++) {
            float re = static_cast<float>(raw[2 * (base + i)]) / 128.0f - meanI;
            float im = static_cast<float>(raw[2 * (base + i) + 1]) / 128.0f - meanQ;
            fPow += re * re + im * im;
            buf[i] = cf(re * hann[i], im * hann[i]);
        }
        frameDb.push_back(10.0f * std::log10(fPow / nfft + 1e-12f));
        fft_inplace(buf, false);

        // fftshift into avgPow / peakPow
        for (int i = 0; i < nfft; i++) {
            int s = (i + nfft / 2) & (nfft - 1);
            float p = std::norm(buf[i]) * scale;
            avgPow[s] += p;
            if (p > peakPow[s]) peakPow[s] = p;
        }
    }
    env->ReleaseByteArrayElements(iqBytes, raw, JNI_ABORT);

    for (int i = 0; i < nfft; i++) avgPow[i] /= static_cast<float>(nFrames);

    // Suppress central 3 DC bins
    int dc = nfft / 2;
    avgPow[dc] = 0.5f * (avgPow[dc - 2] + avgPow[dc + 2]);
    avgPow[dc - 1] = avgPow[dc];
    avgPow[dc + 1] = avgPow[dc];
    peakPow[dc] = 0.5f * (peakPow[dc - 2] + peakPow[dc + 2]);
    peakPow[dc - 1] = peakPow[dc];
    peakPow[dc + 1] = peakPow[dc];

    float out[516];
    std::vector<float> avgSorted(nfft);
    float maxPeakDb = -200.0f;
    int maxPeakBin = 0;
    for (int i = 0; i < nfft; i++) {
        float aDb = 10.0f * std::log10(avgPow[i] + 1e-12f);
        float pDb = 10.0f * std::log10(peakPow[i] + 1e-12f);
        out[i] = aDb;
        out[256 + i] = pDb;
        avgSorted[i] = aDb;
        if (i >= 24 && i <= nfft - 24 && pDb > maxPeakDb) {
            maxPeakDb = pDb;
            maxPeakBin = i;
        }
    }
    std::nth_element(avgSorted.begin(), avgSorted.begin() + nfft / 2, avgSorted.end());
    float floorDb = avgSorted[nfft / 2];

    std::vector<float> fSorted = frameDb;
    std::nth_element(fSorted.begin(), fSorted.begin() + fSorted.size() / 2, fSorted.end());
    float medFrame = fSorted[fSorted.size() / 2];
    float maxFrame = *std::max_element(frameDb.begin(), frameDb.end());

    out[512] = floorDb;
    out[513] = maxPeakDb;
    out[514] = static_cast<float>(maxPeakBin);
    out[515] = maxFrame - medFrame;

    jfloatArray result = env->NewFloatArray(516);
    env->SetFloatArrayRegion(result, 0, 516, out);
    return result;
}

// ---------------------------------------------------------------------------
// JNI: Offline / Real-Time IEEE 802.11p (10 MHz) OFDM Burst Decoder
// Input: raw HackRF int8 interleaved I/Q sampled at 10 MS/s (or 20 MS/s for 11a)
// Output: Array of CRC-32 verified 802.11 MAC frame byte arrays
// ---------------------------------------------------------------------------
extern "C" JNIEXPORT jobjectArray JNICALL
Java_com_trafficlight_v2x_dsp_DsrcDsp_decode80211pFrames(
    JNIEnv* env, jobject /*thiz*/, jbyteArray iqBytes, jint trimSamples) {

    jclass byteArrCls = env->FindClass("[B");
    const jsize byteLen = env->GetArrayLength(iqBytes);
    const int totalSamples = byteLen / 2;
    if (totalSamples <= trimSamples + 400) {
        return env->NewObjectArray(0, byteArrCls, nullptr);
    }

    jbyte* raw = env->GetByteArrayElements(iqBytes, nullptr);
    const int N = totalSamples - trimSamples;
    std::vector<cf> x(N);
    float meanI = 0.0f, meanQ = 0.0f;
    for (int i = 0; i < N; i++) {
        float re = static_cast<float>(raw[2 * (trimSamples + i)]) / 128.0f;
        float im = static_cast<float>(raw[2 * (trimSamples + i) + 1]) / 128.0f;
        meanI += re; meanQ += im;
        x[i] = cf(re, im);
    }
    env->ReleaseByteArrayElements(iqBytes, raw, JNI_ABORT);
    cf dcOff(meanI / N, meanQ / N);
    for (auto& s : x) s -= dcOff;

    int dataIdx[48];
    get_data_indices(dataIdx);

    std::vector<std::vector<uint8_t>> decodedFrames;

    // Sliding Schmidl-Cox short preamble detector (delay=16, window=48)
    cf corr(0.0f, 0.0f);
    float powSum = 0.0f;
    const int W = 48;
    int plateau = 0;

    for (int n = 16; n < N - 320; n++) {
        corr += x[n] * std::conj(x[n - 16]);
        powSum += std::norm(x[n]);
        if (n >= 16 + W) {
            corr -= x[n - W] * std::conj(x[n - W - 16]);
            powSum -= std::norm(x[n - W]);
        }
        if (n < 16 + W) continue;

        float metric = (powSum > 1e-6f) ? (std::abs(corr) / powSum) : 0.0f;
        if (metric > 0.62f) {
            plateau++;
        } else {
            plateau = 0;
        }

        if (plateau == 32) {
            // Packet detected! Estimate coarse CFO from short preamble
            float cfo = std::arg(corr) / 16.0f;
            int ltsStart = n + 32; // approximate start of 160-sample LTS (32 CP + 64 + 64)
            if (ltsStart + 256 >= N) break;

            // Fine-search LTS symbol boundary by maximizing correlation between the two 64-sample LTS halves
            int bestOff = ltsStart + 32;
            float bestLtsScore = 0.0f;
            for (int off = ltsStart; off < ltsStart + 64 && off + 128 < N; off++) {
                cf c(0.0f, 0.0f);
                float p = 0.0f;
                for (int k = 0; k < 64; k++) {
                    cf s1 = x[off + k] * std::polar(1.0f, -cfo * k);
                    cf s2 = x[off + 64 + k] * std::polar(1.0f, -cfo * (k + 64));
                    c += s2 * std::conj(s1);
                    p += std::norm(s1) + std::norm(s2);
                }
                float sc = (p > 1e-6f) ? (std::abs(c) / (0.5f * p)) : 0.0f;
                if (sc > bestLtsScore) {
                    bestLtsScore = sc;
                    bestOff = off;
                }
            }

            if (bestLtsScore < 0.55f) {
                plateau = 0;
                continue;
            }

            // Refine CFO from the two LTS symbols
            cf ltsCorr(0.0f, 0.0f);
            for (int k = 0; k < 64; k++) {
                ltsCorr += x[bestOff + 64 + k] * std::conj(x[bestOff + k]);
            }
            float fineCfo = std::arg(ltsCorr) / 64.0f;
            // Combine coarse + fine CFO
            float totalCfo = cfo + std::remainder(fineCfo - cfo, 2.0f * PI_F / 64.0f);

            // FFT both LTS symbols and compute LS channel estimate H[64]
            std::vector<cf> lts1(64), lts2(64);
            for (int k = 0; k < 64; k++) {
                lts1[k] = x[bestOff + k] * std::polar(1.0f, -totalCfo * k);
                lts2[k] = x[bestOff + 64 + k] * std::polar(1.0f, -totalCfo * (k + 64));
            }
            fft_inplace(lts1, false);
            fft_inplace(lts2, false);

            cf H[64];
            for (int i = 0; i < 64; i++) {
                int s = (i + 32) & 63; // fftshift
                cf avg = 0.5f * (lts1[i] + lts2[i]);
                H[s] = (LTS_FREQ[s] != 0) ? (avg * static_cast<float>(LTS_FREQ[s])) : cf(1.0f, 0.0f);
            }

            // Helper to extract & equalize one OFDM symbol (80 samples: 16 CP + 64 FFT)
            auto decodeSymbol = [&](int symIdx, cf eqOut[64]) -> bool {
                int pos = bestOff + 128 + symIdx * 80 + 16;
                if (pos + 64 > N) return false;
                std::vector<cf> sym(64);
                for (int k = 0; k < 64; k++) {
                    float t = static_cast<float>(128 + symIdx * 80 + 16 + k);
                    sym[k] = x[pos + k] * std::polar(1.0f, -totalCfo * t);
                }
                fft_inplace(sym, false);

                // Pilot residual phase correction
                float pPol = static_cast<float>(PILOT_POLARITY[symIdx % 127]);
                cf pilotErr(0.0f, 0.0f);
                for (int p = 0; p < 4; p++) {
                    int idx = PILOT_IDX[p];
                    int rawIdx = (idx + 32) & 63;
                    cf eq = sym[rawIdx] / (H[idx] + cf(1e-9f, 0.0f));
                    pilotErr += eq * (pPol * static_cast<float>(PILOT_BASE[p]));
                }
                cf phaseCorr = std::polar(1.0f, -std::arg(pilotErr));
                for (int i = 0; i < 64; i++) {
                    int s = (i + 32) & 63;
                    eqOut[s] = (sym[i] / (H[s] + cf(1e-9f, 0.0f))) * phaseCorr;
                }
                return true;
            };

            // Symbol 0: SIGNAL field (BPSK, rate 1/2, 24 bits)
            cf sigEq[64];
            if (!decodeSymbol(0, sigEq)) break;
            std::vector<float> sigSoft(48);
            for (int k = 0; k < 48; k++) sigSoft[k] = sigEq[dataIdx[k]].real();
            auto sigDeint = deinterleave(sigSoft, 48, 1);
            auto sigBits = viterbi_decode(sigDeint, 1, 2);
            if (sigBits.size() < 24) { plateau = 0; continue; }

            // Check even parity (bit 17) & reserved bit (bit 4 == 0) & tail (bits 18..23 == 0)
            int par = 0;
            for (int b = 0; b <= 17; b++) par ^= sigBits[b];
            int rateField = (sigBits[0] << 3) | (sigBits[1] << 2) | (sigBits[2] << 1) | sigBits[3];
            int psduLen = 0;
            for (int b = 0; b < 12; b++) psduLen |= (sigBits[5 + b] << b);

            int n_bpsc = 0, rNum = 1, rDen = 2;
            switch (rateField) {
                case 0xD: n_bpsc = 1; rNum = 1; rDen = 2; break; // 6 Mbps (3 Mbps in 11p)
                case 0xF: n_bpsc = 1; rNum = 3; rDen = 4; break; // 9 Mbps (4.5 Mbps)
                case 0x5: n_bpsc = 2; rNum = 1; rDen = 2; break; // 12 Mbps (6 Mbps)
                case 0x7: n_bpsc = 2; rNum = 3; rDen = 4; break; // 18 Mbps (9 Mbps)
                case 0x9: n_bpsc = 4; rNum = 1; rDen = 2; break; // 24 Mbps (12 Mbps)
                case 0xB: n_bpsc = 4; rNum = 3; rDen = 4; break; // 36 Mbps (18 Mbps)
                default: n_bpsc = 0; break;
            }

            if (par != 0 || sigBits[4] != 0 || n_bpsc == 0 || psduLen < 14 || psduLen > 1500) {
                plateau = 0;
                continue;
            }

            int n_cbps = 48 * n_bpsc;
            int n_dbps = (n_cbps * rNum) / rDen;
            int totalDataBits = 16 + 8 * psduLen + 6;
            int nSym = (totalDataBits + n_dbps - 1) / n_dbps;

            std::vector<float> allCoded;
            allCoded.reserve(nSym * n_cbps);
            bool ok = true;
            for (int s = 1; s <= nSym; s++) {
                cf dEq[64];
                if (!decodeSymbol(s, dEq)) { ok = false; break; }
                std::vector<float> symSoft;
                symSoft.reserve(n_cbps);
                for (int k = 0; k < 48; k++) {
                    cf z = dEq[dataIdx[k]];
                    if (n_bpsc == 1) {
                        symSoft.push_back(z.real());
                    } else if (n_bpsc == 2) {
                        symSoft.push_back(z.real());
                        symSoft.push_back(z.imag());
                    } else if (n_bpsc == 4) {
                        // 16-QAM soft LLR approximation (norm = 1/sqrt(10) ~ 0.3162)
                        const float th = 2.0f / std::sqrt(10.0f);
                        symSoft.push_back(z.real());
                        symSoft.push_back(th - std::abs(z.real()));
                        symSoft.push_back(z.imag());
                        symSoft.push_back(th - std::abs(z.imag()));
                    }
                }
                auto deint = deinterleave(symSoft, n_cbps, n_bpsc);
                allCoded.insert(allCoded.end(), deint.begin(), deint.end());
            }

            if (!ok) { plateau = 0; continue; }

            auto bits = viterbi_decode(allCoded, rNum, rDen);
            if ((int)bits.size() < 16 + 8 * psduLen) { plateau = 0; continue; }

            // Descramble (first 7 bits of SERVICE field recover initial LFSR state)
            uint8_t lfsr = 0;
            for (int b = 0; b < 7; b++) {
                uint8_t fb = ((lfsr >> 6) ^ (lfsr >> 3)) & 1;
                // Wait: initial state is directly recovered from bits[0..6] (since SERVICE bits 0..6 are 0)
                (void)fb;
            }
            // Recover state from first 7 scrambled zeros: x[n] = s[n] ^ s[n-4] ^ s[n-7]
            uint8_t reg[7];
            for (int b = 0; b < 7; b++) reg[b] = bits[6 - b];
            // Step forward 9 more SERVICE bits (bits 7..15)
            auto nextScram = [&]() -> uint8_t {
                uint8_t out = reg[6] ^ reg[3];
                for (int k = 6; k > 0; k--) reg[k] = reg[k - 1];
                reg[0] = out;
                return out;
            };
            for (int b = 7; b < 16; b++) nextScram();

            std::vector<uint8_t> psdu(psduLen, 0);
            for (int byteIdx = 0; byteIdx < psduLen; byteIdx++) {
                uint8_t val = 0;
                for (int bitIdx = 0; bitIdx < 8; bitIdx++) {
                    uint8_t decBit = bits[16 + byteIdx * 8 + bitIdx] ^ nextScram();
                    val |= (decBit << bitIdx);
                }
                psdu[byteIdx] = val;
            }

            // Verify IEEE 802.11 CRC-32 (last 4 bytes)
            uint32_t calcCrc = crc32_80211(psdu.data(), psduLen - 4);
            uint32_t pktCrc = static_cast<uint32_t>(psdu[psduLen - 4]) |
                              (static_cast<uint32_t>(psdu[psduLen - 3]) << 8) |
                              (static_cast<uint32_t>(psdu[psduLen - 2]) << 16) |
                              (static_cast<uint32_t>(psdu[psduLen - 1]) << 24);

            if (calcCrc == pktCrc) {
                psdu.resize(psduLen - 4); // strip FCS
                decodedFrames.push_back(std::move(psdu));
            }

            // Advance past this packet
            n = bestOff + 128 + (nSym + 1) * 80;
            corr = cf(0.0f, 0.0f);
            powSum = 0.0f;
            plateau = 0;
        }
    }

    jobjectArray result = env->NewObjectArray(static_cast<jsize>(decodedFrames.size()), byteArrCls, nullptr);
    for (size_t i = 0; i < decodedFrames.size(); i++) {
        jbyteArray arr = env->NewByteArray(static_cast<jsize>(decodedFrames[i].size()));
        env->SetByteArrayRegion(arr, 0, static_cast<jsize>(decodedFrames[i].size()),
                                reinterpret_cast<const jbyte*>(decodedFrames[i].data()));
        env->SetObjectArrayElement(result, static_cast<jsize>(i), arr);
        env->DeleteLocalRef(arr);
    }
    return result;
}
