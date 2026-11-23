#include <cmath>
#include <complex>
#include <cstdint>
#include <cstring>
#include <vector>
#include <algorithm>

using cf = std::complex<float>;
static constexpr float PI_F = 3.14159265358979323846f;

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

static const int8_t LTS_FREQ[64] = {
    0,0,0,0,0,0,
    1,1,-1,-1,1,1,-1,1,-1,1,1,1,1,1,1,-1,-1,1,1,-1,1,-1,1,1,1,1,
    0,
    1,-1,-1,1,1,-1,1,-1,1,-1,-1,-1,-1,-1,1,1,-1,-1,1,-1,1,-1,1,1,1,1,
    0,0,0,0,0
};

static const int PILOT_IDX[4] = {11, 25, 39, 53};
static const int8_t PILOT_BASE[4] = {1, 1, 1, -1};
static const int8_t PILOT_POLARITY[127] = {
    1,1,1,1,-1,-1,-1,1,-1,-1,-1,-1,1,1,-1,1,-1,-1,1,1,-1,1,1,-1,1,1,1,1,1,1,-1,1,
    1,1,-1,1,1,-1,-1,1,1,1,-1,1,-1,-1,-1,1,-1,1,-1,-1,1,-1,-1,1,1,1,1,1,-1,-1,1,1,
    -1,-1,1,-1,1,-1,1,1,-1,-1,-1,1,1,-1,-1,-1,-1,1,-1,-1,1,-1,1,1,1,1,-1,1,-1,1,-1,1,
    -1,-1,-1,-1,-1,1,-1,1,1,-1,1,-1,1,1,1,-1,-1,1,-1,-1,-1,1,1,1,-1,-1,-1,-1,-1,-1,-1
};

static void get_data_indices(int out[48]) {
    int k = 0;
    for (int i = 6; i <= 58; i++) {
        if (i == 32 || i == 11 || i == 25 || i == 39 || i == 53) continue;
        out[k++] = i;
    }
}

static inline uint8_t parity7(uint8_t x) {
    x ^= x >> 4; x ^= x >> 2; x ^= x >> 1;
    return x & 1;
}

static std::vector<uint8_t> viterbi_decode(const std::vector<float>& soft_bits, int rate_num, int rate_den) {
    std::vector<float> depunc;
    depunc.reserve(soft_bits.size() * 2);
    if (rate_num == 1 && rate_den == 2) {
        depunc = soft_bits;
    } else if (rate_num == 3 && rate_den == 4) {
        for (size_t i = 0; i + 3 < soft_bits.size(); i += 4) {
            depunc.push_back(soft_bits[i]);
            depunc.push_back(soft_bits[i + 1]);
            depunc.push_back(soft_bits[i + 2]);
            depunc.push_back(0.0f);
            depunc.push_back(0.0f);
            depunc.push_back(soft_bits[i + 3]);
        }
    } else if (rate_num == 2 && rate_den == 3) {
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
    if (pm[0] > NEG_INF * 0.5f) state = 0;

    std::vector<uint8_t> decoded(n_bits, 0);
    for (int t = static_cast<int>(n_bits) - 1; t >= 0; t--) {
        uint8_t tr = trace[t * 64 + state];
        decoded[t] = tr & 1;
        state = tr >> 1;
    }
    return decoded;
}

static std::vector<float> deinterleave(const std::vector<float>& in, int n_cbps, int n_bpsc) {
    std::vector<float> out(n_cbps, 0.0f);
    int s = std::max(1, n_bpsc / 2);
    for (int j = 0; j < n_cbps; j++) {
        int i = s * (j / s) + (j + (16 * j) / n_cbps) % s;
        int k = 16 * i - (n_cbps - 1) * ((16 * i) / n_cbps);
        if (k >= 0 && k < n_cbps && j < (int)in.size()) {
            out[k] = in[j];
        }
    }
    return out;
}

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

extern "C" {

// out must have length >= 518:
// [0..255]=avgDb, [256..511]=peakDb, [512]=floorDb, [513]=maxPeakDb,
// [514]=maxPeakBin, [515]=burstJumpDb, [516]=avgBinsAbove, [517]=peakBinsAbove
int compute_spectrum(const int8_t* raw, int byte_len, int trim_samples, float* out) {
    const int totalSamples = byte_len / 2;
    const int nfft = 256;
    if (totalSamples <= trim_samples + nfft * 2) return 0;

    const int start = trim_samples;
    const int count = totalSamples - start;

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

        for (int i = 0; i < nfft; i++) {
            int s = (i + nfft / 2) & (nfft - 1);
            float p = std::norm(buf[i]) * scale;
            avgPow[s] += p;
            if (p > peakPow[s]) peakPow[s] = p;
        }
    }

    for (int i = 0; i < nfft; i++) avgPow[i] /= static_cast<float>(nFrames);

    int dc = nfft / 2;
    avgPow[dc] = 0.5f * (avgPow[dc - 2] + avgPow[dc + 2]);
    avgPow[dc - 1] = avgPow[dc];
    avgPow[dc + 1] = avgPow[dc];
    peakPow[dc] = 0.5f * (peakPow[dc - 2] + peakPow[dc + 2]);
    peakPow[dc - 1] = peakPow[dc];
    peakPow[dc + 1] = peakPow[dc];

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

    int avgAbove = 0, peakAbove = 0;
    for (int i = 24; i <= nfft - 24; i++) {
        if (out[i] - floorDb >= 6.0f) avgAbove++;
        if (out[256 + i] - floorDb >= 13.0f) peakAbove++;
    }

    std::vector<float> fSorted = frameDb;
    std::nth_element(fSorted.begin(), fSorted.begin() + fSorted.size() / 2, fSorted.end());
    float medFrame = fSorted[fSorted.size() / 2];
    float maxFrame = *std::max_element(frameDb.begin(), frameDb.end());

    out[512] = floorDb;
    out[513] = maxPeakDb;
    out[514] = static_cast<float>(maxPeakBin);
    out[515] = maxFrame - medFrame;
    out[516] = static_cast<float>(avgAbove);
    out[517] = static_cast<float>(peakAbove);
    return 1;
}

// Packs decoded frames into out_buf as [uint16_t len_le][frame_bytes...]
// Returns number of decoded frames.
int decode_80211p_frames(const int8_t* raw, int byte_len, int trim_samples,
                         uint8_t* out_buf, int out_buf_size) {
    const int totalSamples = byte_len / 2;
    if (totalSamples <= trim_samples + 400) return 0;

    const int N = totalSamples - trim_samples;
    std::vector<cf> x(N);
    float meanI = 0.0f, meanQ = 0.0f;
    for (int i = 0; i < N; i++) {
        float re = static_cast<float>(raw[2 * (trim_samples + i)]) / 128.0f;
        float im = static_cast<float>(raw[2 * (trim_samples + i) + 1]) / 128.0f;
        meanI += re; meanQ += im;
        x[i] = cf(re, im);
    }
    cf dcOff(meanI / N, meanQ / N);
    for (auto& s : x) s -= dcOff;

    int dataIdx[48];
    get_data_indices(dataIdx);

    static bool ltsRefInit = false;
    static cf ltsRef[64];
    if (!ltsRefInit) {
        std::vector<cf> tmp(64);
        for (int i = 0; i < 64; i++) {
            int s = (i + 32) & 63;
            tmp[i] = cf(static_cast<float>(LTS_FREQ[s]), 0.0f);
        }
        fft_inplace(tmp, true);
        float normSq = 0.0f;
        for (int i = 0; i < 64; i++) normSq += std::norm(tmp[i]);
        float invNorm = 1.0f / std::sqrt(normSq + 1e-12f);
        for (int i = 0; i < 64; i++) ltsRef[i] = tmp[i] * invNorm;
        ltsRefInit = true;
    }

    int outPos = 0;
    int frameCount = 0;

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
            float cfo = std::arg(corr) / 16.0f;
            int searchStart = n + 16;
            int searchEnd = std::min(n + 160, N - 208);
            if (searchStart >= searchEnd) break;

            int bestOff = searchStart;
            float bestLtsScore = 0.0f;
            for (int off = searchStart; off < searchEnd; off++) {
                cf c1(0.0f, 0.0f), c2(0.0f, 0.0f);
                float p1 = 0.0f, p2 = 0.0f;
                for (int k = 0; k < 64; k++) {
                    cf s1 = x[off + k] * std::polar(1.0f, -cfo * k);
                    cf s2 = x[off + 64 + k] * std::polar(1.0f, -cfo * (k + 64));
                    c1 += s1 * std::conj(ltsRef[k]);
                    c2 += s2 * std::conj(ltsRef[k]);
                    p1 += std::norm(s1);
                    p2 += std::norm(s2);
                }
                float sc1 = (p1 > 1e-6f) ? (std::abs(c1) / std::sqrt(p1)) : 0.0f;
                float sc2 = (p2 > 1e-6f) ? (std::abs(c2) / std::sqrt(p2)) : 0.0f;
                float sc = 0.5f * (sc1 + sc2);
                if (sc > bestLtsScore) {
                    bestLtsScore = sc;
                    bestOff = off;
                }
            }

            if (bestLtsScore < 0.48f) {
                plateau = 0;
                continue;
            }

            cf ltsCorr(0.0f, 0.0f);
            for (int k = 0; k < 64; k++) {
                ltsCorr += x[bestOff + 64 + k] * std::conj(x[bestOff + k]);
            }
            float fineCfo = std::arg(ltsCorr) / 64.0f;
            float totalCfo = cfo + std::remainder(fineCfo - cfo, 2.0f * PI_F / 64.0f);

            std::vector<cf> lts1(64), lts2(64);
            for (int k = 0; k < 64; k++) {
                lts1[k] = x[bestOff + k] * std::polar(1.0f, -totalCfo * k);
                lts2[k] = x[bestOff + 64 + k] * std::polar(1.0f, -totalCfo * (k + 64));
            }
            fft_inplace(lts1, false);
            fft_inplace(lts2, false);

            cf H[64];
            for (int i = 0; i < 64; i++) {
                int s = (i + 32) & 63;
                cf avg = 0.5f * (lts1[i] + lts2[i]);
                H[s] = (LTS_FREQ[s] != 0) ? (avg * static_cast<float>(LTS_FREQ[s])) : cf(1.0f, 0.0f);
            }

            auto decodeSymbol = [&](int symIdx, cf eqOut[64]) -> bool {
                int pos = bestOff + 128 + symIdx * 80 + 16;
                if (pos + 64 > N) return false;
                std::vector<cf> sym(64);
                for (int k = 0; k < 64; k++) {
                    float t = static_cast<float>(128 + symIdx * 80 + 16 + k);
                    sym[k] = x[pos + k] * std::polar(1.0f, -totalCfo * t);
                }
                fft_inplace(sym, false);

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

            cf sigEq[64];
            if (!decodeSymbol(0, sigEq)) break;
            std::vector<float> sigSoft(48);
            for (int k = 0; k < 48; k++) sigSoft[k] = sigEq[dataIdx[k]].real();
            auto sigDeint = deinterleave(sigSoft, 48, 1);
            auto sigBits = viterbi_decode(sigDeint, 1, 2);
            if (sigBits.size() < 24) { plateau = 0; continue; }

            int par = 0;
            for (int b = 0; b <= 17; b++) par ^= sigBits[b];
            int rateField = (sigBits[0] << 3) | (sigBits[1] << 2) | (sigBits[2] << 1) | sigBits[3];
            int psduLen = 0;
            for (int b = 0; b < 12; b++) psduLen |= (sigBits[5 + b] << b);

            int n_bpsc = 0, rNum = 1, rDen = 2;
            switch (rateField) {
                case 0xD: n_bpsc = 1; rNum = 1; rDen = 2; break;
                case 0xF: n_bpsc = 1; rNum = 3; rDen = 4; break;
                case 0x5: n_bpsc = 2; rNum = 1; rDen = 2; break;
                case 0x7: n_bpsc = 2; rNum = 3; rDen = 4; break;
                case 0x9: n_bpsc = 4; rNum = 1; rDen = 2; break;
                case 0xB: n_bpsc = 4; rNum = 3; rDen = 4; break;
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

            uint8_t reg[7];
            for (int b = 0; b < 7; b++) reg[b] = bits[6 - b];
            auto nextScram = [&]() -> uint8_t {
                uint8_t outb = reg[6] ^ reg[3];
                for (int k = 6; k > 0; k--) reg[k] = reg[k - 1];
                reg[0] = outb;
                return outb;
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

            uint32_t calcCrc = crc32_80211(psdu.data(), psduLen - 4);
            uint32_t pktCrc = static_cast<uint32_t>(psdu[psduLen - 4]) |
                              (static_cast<uint32_t>(psdu[psduLen - 3]) << 8) |
                              (static_cast<uint32_t>(psdu[psduLen - 2]) << 16) |
                              (static_cast<uint32_t>(psdu[psduLen - 1]) << 24);

            if (calcCrc == pktCrc) {
                int payloadLen = psduLen - 4;
                if (outPos + 2 + payloadLen <= out_buf_size) {
                    out_buf[outPos] = static_cast<uint8_t>(payloadLen & 0xFF);
                    out_buf[outPos + 1] = static_cast<uint8_t>((payloadLen >> 8) & 0xFF);
                    std::memcpy(out_buf + outPos + 2, psdu.data(), payloadLen);
                    outPos += 2 + payloadLen;
                    frameCount++;
                }
            }

            n = bestOff + 128 + (nSym + 1) * 80;
            corr = cf(0.0f, 0.0f);
            powSum = 0.0f;
            plateau = 0;
        }
    }
    return frameCount;
}

// Extract a 10 MHz sub-channel from a 20 MS/s int8 I/Q buffer via exact ±Fs/4
// frequency shift + 11-tap half-band FIR + decimate-by-2.
//   dir = -1 -> lower channel (Fc - 5 MHz, e.g. 5900 MHz from 5905 MHz)
//   dir = +1 -> upper channel (Fc + 5 MHz, e.g. 5910 MHz from 5905 MHz)
// out10m must have capacity >= (byte_len / 2). Returns output byte length.
int extract_subchannel_10m(const int8_t* raw20m, int byte_len, int dir, int8_t* out10m) {
    const int n_in = byte_len / 2;
    if (n_in < 16) return 0;

    // Shift by -dir * Fs/4 so target sub-channel moves to DC (0 Hz)
    // dir = -1 (lower, -Fs/4) -> multiply by exp(+j*pi/2*n): (1, +j, -1, -j)
    // dir = +1 (upper, +Fs/4) -> multiply by exp(-j*pi/2*n): (1, -j, -1, +j)
    std::vector<float> si(n_in), sq(n_in);
    const bool shift_pos = (dir < 0);
    for (int n = 0; n < n_in; n++) {
        float re = static_cast<float>(raw20m[2 * n]);
        float im = static_cast<float>(raw20m[2 * n + 1]);
        switch (n & 3) {
            case 0: si[n] =  re; sq[n] =  im; break;
            case 1:
                if (shift_pos) { si[n] = -im; sq[n] =  re; }
                else           { si[n] =  im; sq[n] = -re; }
                break;
            case 2: si[n] = -re; sq[n] = -im; break;
            case 3:
                if (shift_pos) { si[n] =  im; sq[n] = -re; }
                else           { si[n] = -im; sq[n] =  re; }
                break;
        }
    }

    // 11-tap half-band FIR (only 7 non-zero taps) + decimate by 2
    constexpr float h0 =  0.50000f;
    constexpr float h1 =  0.31120f;
    constexpr float h3 = -0.08020f;
    constexpr float h5 =  0.01900f;

    const int n_out = (n_in - 10) / 2;
    if (n_out <= 0) return 0;

    for (int m = 0; m < n_out; m++) {
        int c = 2 * m + 5;
        float oi = h0 * si[c]
                 + h1 * (si[c - 1] + si[c + 1])
                 + h3 * (si[c - 3] + si[c + 3])
                 + h5 * (si[c - 5] + si[c + 5]);
        float oq = h0 * sq[c]
                 + h1 * (sq[c - 1] + sq[c + 1])
                 + h3 * (sq[c - 3] + sq[c + 3])
                 + h5 * (sq[c - 5] + sq[c + 5]);
        int ii = static_cast<int>(std::lround(oi));
        int iq = static_cast<int>(std::lround(oq));
        if (ii > 127) ii = 127; else if (ii < -128) ii = -128;
        if (iq > 127) iq = 127; else if (iq < -128) iq = -128;
        out10m[2 * m]     = static_cast<int8_t>(ii);
        out10m[2 * m + 1] = static_cast<int8_t>(iq);
    }
    return n_out * 2;
}

} // extern "C"
