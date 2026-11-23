#!/usr/bin/env python3
"""
RoadSense V2X — macOS Console Scanner & Protocol Decoder for HackRF One
Passive 5.9 GHz DSRC (802.11p / ITS-G5), C-V2X burst detector, and 5 GHz Wi-Fi test decoder.
Uses native C++17 OFDM PHY DSP (libdsrc_dsp.dylib) + hackrf_transfer (Bias-T forced OFF).
"""

import argparse
import csv
import ctypes
import datetime
import json
import math
import os
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DSP_SRC = os.path.join(SCRIPT_DIR, "dsrc_dsp_mac.cpp")
DSP_LIB = os.path.join(SCRIPT_DIR, "libdsrc_dsp.dylib")

PRESETS = {
    "scan-safety": (5905.0, 20_000_000, "dual-safety", "Smart 2-Hop Safety Scan (5900/5910/5920 + C-V2X 5915, Auto-Lock)"),
    "scan-all":    (5905.0, 20_000_000, "dual-all",    "Safety + Legacy Scan (5900/5910/5920 + 5860/5890, Auto-Lock)"),
    "ch183":       (5915.0, 20_000_000, "single",      "Ch 183 · 5915 MHz (20M C-V2X Continuous)"),
    "ch181":       (5905.0, 20_000_000, "single",      "Ch 181 · 5905 MHz (20M Continuous)"),
    "ch180":       (5900.0, 10_000_000, "single",      "Ch 180 · 5900 MHz (10M Safety Continuous)"),
    "ch182":       (5910.0, 10_000_000, "single",      "Ch 182 · 5910 MHz (10M Safety Continuous)"),
    "ch184":       (5920.0, 10_000_000, "single",      "Ch 184 · 5920 MHz (10M Safety Continuous)"),
    "ch172":       (5860.0, 10_000_000, "single",      "Ch 172 · 5860 MHz (10M Legacy DSRC)"),
    "ch178":       (5890.0, 10_000_000, "single",      "Ch 178 · 5890 MHz (10M Legacy Control)"),
    "wifi-test":   (5745.0, 20_000_000, "single",      "Wi-Fi Test · 5745 MHz (Ch 149 20M Continuous)"),
}

# Dual-channel 20 MS/s hops: (center_mhz, rate_hz, lower_mhz_or_None, upper_mhz_or_None, check_cv2x)
SAFETY_SCAN_HOPS = [
    (5905.0, 20_000_000, 5900.0, 5910.0, False),  # Ch 180 + Ch 182 simultaneously
    (5915.0, 20_000_000, 5910.0, 5920.0, True),   # Ch 182 + Ch 184 + 20M C-V2X Ch 183
]
LEGACY_SCAN_HOPS = [
    (5865.0, 20_000_000, 5860.0, 5870.0, False),  # Ch 172 + Ch 174 simultaneously
    (5885.0, 20_000_000, 5880.0, 5890.0, False),  # Ch 176 + Ch 178 simultaneously
]

# ANSI colors
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
PROTO_COLORS = {
    "SPaT": "\033[92m", "SPATEM": "\033[92m",
    "MAP": "\033[94m", "MAPEM": "\033[94m",
    "BSM": "\033[93m",
    "TIM": "\033[33m", "RSA": "\033[33m",
    "EVA": "\033[91m", "DENM": "\033[91m",
    "CAM": "\033[96m",
    "SRM": "\033[35m", "SSM": "\033[35m",
    "RTCM": "\033[32m", "PSM": "\033[95m",
    "WSMP": "\033[35m", "GN": "\033[35m",
    "C-V2X": "\033[96m", "RF_BURST": "\033[90m",
    "802.11": "\033[37m",
}


# ---------------------------------------------------------------------------
# Native C++ DSP loader
# ---------------------------------------------------------------------------
def load_dsp():
    need_build = (not os.path.exists(DSP_LIB)) or (
        os.path.exists(DSP_SRC) and os.path.getmtime(DSP_SRC) > os.path.getmtime(DSP_LIB)
    )
    if need_build:
        print(f"Building native DSP library: {DSP_LIB} ...", file=sys.stderr)
        cmd = ["clang++", "-O3", "-std=c++17", "-shared", "-fPIC", DSP_SRC, "-o", DSP_LIB]
        subprocess.run(cmd, check=True)

    lib = ctypes.CDLL(DSP_LIB)
    lib.compute_spectrum.argtypes = [
        ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_float)
    ]
    lib.compute_spectrum.restype = ctypes.c_int

    lib.decode_80211p_frames.argtypes = [
        ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int
    ]
    lib.decode_80211p_frames.restype = ctypes.c_int

    lib.extract_subchannel_10m.argtypes = [
        ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_char_p
    ]
    lib.extract_subchannel_10m.restype = ctypes.c_int
    return lib


def extract_subchannel_10m(lib, iq_20m: bytes, direction: int) -> bytes:
    """Extract lower (dir=-1, Fc-5MHz) or upper (dir=+1, Fc+5MHz) 10 MS/s sub-channel from 20 MS/s I/Q."""
    out_cap = (len(iq_20m) // 2) + 16
    out_buf = ctypes.create_string_buffer(out_cap)
    n_bytes = lib.extract_subchannel_10m(iq_20m, len(iq_20m), int(direction), out_buf)
    return out_buf.raw[:n_bytes]


def compute_spectrum_only(lib, iq_bytes: bytes, trim_samples: int):
    spec_out = (ctypes.c_float * 518)()
    ok = lib.compute_spectrum(iq_bytes, len(iq_bytes), trim_samples, spec_out)
    if not ok:
        return None
    return {
        "avg_db": [float(spec_out[i]) for i in range(256)],
        "peak_db": [float(spec_out[256 + i]) for i in range(256)],
        "floor_db": float(spec_out[512]),
        "max_peak_db": float(spec_out[513]),
        "max_peak_bin": int(spec_out[514]),
        "burst_jump_db": float(spec_out[515]),
        "avg_bins_above": int(spec_out[516]),
        "peak_bins_above": int(spec_out[517]),
    }


def decode_frames_only(lib, iq_bytes: bytes, trim_samples: int):
    out_buf = ctypes.create_string_buffer(262144)
    n_frames = lib.decode_80211p_frames(iq_bytes, len(iq_bytes), trim_samples, out_buf, len(out_buf))
    frames = []
    pos = 0
    raw_out = out_buf.raw
    for _ in range(n_frames):
        if pos + 2 > len(raw_out):
            break
        flen = struct.unpack_from("<H", raw_out, pos)[0]
        pos += 2
        if pos + flen > len(raw_out):
            break
        frames.append(raw_out[pos : pos + flen])
        pos += flen
    return frames


def analyze_iq(lib, iq_bytes: bytes, trim_samples: int, force_decode: bool = False):
    spec = compute_spectrum_only(lib, iq_bytes, trim_samples)
    # Fast energy gate: skip expensive OFDM preamble/Viterbi search on pure noise
    has_energy = force_decode or (spec is None) or (
        spec["burst_jump_db"] >= 2.5 or spec["avg_bins_above"] >= 10 or spec["peak_bins_above"] >= 10
    )
    frames = decode_frames_only(lib, iq_bytes, trim_samples) if has_energy else []
    return spec, frames


# ---------------------------------------------------------------------------
# UPER Bit Reader & V2X Protocol Decoder (SAE J2735 + ETSI ITS-G5 + 802.11)
# ---------------------------------------------------------------------------
class UperBitReader:
    def __init__(self, data: bytes, start_byte: int = 0):
        self.data = data
        self.bit_pos = start_byte * 8
        self.total_bits = len(data) * 8

    @property
    def remaining(self) -> int:
        return max(0, self.total_bits - self.bit_pos)

    def read_bit(self) -> int:
        if self.bit_pos >= self.total_bits:
            return 0
        b = self.data[self.bit_pos >> 3]
        shift = 7 - (self.bit_pos & 7)
        self.bit_pos += 1
        return (b >> shift) & 1

    def read_bits(self, count: int) -> int:
        v = 0
        for _ in range(count):
            v = (v << 1) | self.read_bit()
        return v

    def read_signed(self, count: int, offset: int) -> int:
        return self.read_bits(count) + offset

    def skip(self, bits: int):
        self.bit_pos = min(self.total_bits, self.bit_pos + bits)


def fmt_mac(frame: bytes, off: int) -> str:
    if off + 6 > len(frame):
        return "00:00:00:00:00:00"
    return ":".join(f"{b:02X}" for b in frame[off : off + 6])


def extract_beacon_ssid(frame: bytes):
    idx = 36
    while idx + 2 <= len(frame):
        tag_id = frame[idx]
        tag_len = frame[idx + 1]
        idx += 2
        if idx + tag_len > len(frame):
            break
        if tag_id == 0:
            if tag_len == 0:
                return "<hidden>"
            raw = frame[idx : idx + tag_len]
            return "".join(chr(c) if 32 <= c <= 126 else "?" for c in raw)
        idx += tag_len
    return None


def map_event_state(code: int) -> str:
    return {
        1: "DARK",
        2: "FLASHING_RED",
        3: "RED (stop-And-Remain)",
        4: "PRE_MOVEMENT",
        5: "GREEN (permissive)",
        6: "GREEN_ARROW (protected)",
        7: "YELLOW (permissive)",
        8: "YELLOW_ARROW (protected)",
        9: "CAUTION",
    }.get(code, f"state={code}")


def unwrap_1609_2(data: bytes, details: dict) -> bytes:
    if len(data) >= 4 and data[0] == 0x03:
        choice = data[1]
        if choice == 0x80:
            details["1609.2"] = "UnsecuredData"
            off = 2
            lb = data[off]
            off += 1
            if (lb & 0x80) and off < len(data):
                off += 1
            return data[off:]
        elif choice == 0x81:
            details["1609.2"] = "SignedData"
            valid_ids = {18, 19, 20, 22, 27, 28, 29, 30, 31, 32}
            for i in range(2, min(40, len(data) - 3)):
                if data[i] == 0x00 and data[i + 1] in valid_ids:
                    return data[i:]
    return data


def parse_j2735(data: bytes, details: dict):
    if len(data) < 4:
        return None
    msg_id = (data[0] << 8) | data[1]
    valid_ids = {18, 19, 20, 22, 27, 28, 29, 30, 31, 32}
    if msg_id not in valid_ids:
        return None
    details["J2735 MsgID"] = str(msg_id)
    off = 2
    lb = data[off]
    off += 1
    if (lb & 0x80) and off < len(data):
        off += 1
    br = UperBitReader(data, off)

    if msg_id == 20:  # BSM
        if br.remaining < 240:
            return "BSM", "V2V BSM (truncated)"
        br.read_bit()
        has_p2 = br.read_bit() == 1
        has_reg = br.read_bit() == 1
        msg_cnt = br.read_bits(7)
        temp_id = br.read_bits(32)
        sec_mark = br.read_bits(16)
        lat = br.read_signed(31, -900_000_000) / 1e7
        lon = br.read_signed(32, -1_799_999_999) / 1e7
        elev = br.read_signed(16, -4096) * 0.1
        br.skip(32)
        trans = br.read_bits(3)
        speed_ms = br.read_bits(13) * 0.02
        speed_mph = speed_ms * 2.23694
        heading = br.read_bits(15) * 0.0125
        steer = br.read_signed(8, -126) * 1.5
        br.skip(48)
        brakes = br.read_bits(5)
        br.skip(10)
        w_cm = br.read_bits(10)
        l_cm = br.read_bits(12)
        details["Temp ID"] = f"0x{temp_id:08X}"
        details["GPS"] = f"{lat:.6f}, {lon:.6f} (elev {elev:.1f}m)"
        details["Speed"] = f"{speed_mph:.1f} mph ({speed_ms*3.6:.1f} km/h)"
        details["Heading / Steer"] = f"{heading:.1f}° / {steer:.1f}°"
        details["Brakes"] = f"ON (0x{brakes:02X})" if (brakes & 0x0F) else "Off"
        details["Size"] = f"{w_cm}cm W x {l_cm}cm L"
        details["Gear / Cnt"] = f"{['N','P','D','R'][trans] if trans < 4 else '?'} / cnt={msg_cnt} sec={sec_mark}ms"
        if has_p2:
            details["Part II"] = "Present"
        if has_reg:
            details["Regional"] = "Present"
        return "BSM", f"V2V BSM 0x{temp_id:08X} · {speed_mph:.1f} mph · Hdg {heading:.0f}° · Brakes {'ON' if (brakes & 0x0F) else 'off'}"

    if msg_id == 19:  # SPaT
        if br.remaining < 32:
            return "SPaT", "V2I SPaT (short)"
        br.read_bit()
        has_ts = br.read_bit() == 1
        has_name = br.read_bit() == 1
        has_reg = br.read_bit() == 1
        if has_ts:
            br.skip(20)
        spat_name = ""
        if has_name:
            nl = br.read_bits(6) + 1
            spat_name = "".join(chr(br.read_bits(7)) for _ in range(nl)).strip()
        inter_cnt = min(br.read_bits(5) + 1, 4)
        summary = "V2I SPaT Message"
        for i in range(inter_cnt):
            if br.remaining < 32:
                break
            br.read_bit()
            i_name_f, i_moy, i_ts, i_lanes, i_man, i_reg = [br.read_bit() == 1 for _ in range(6)]
            iname = spat_name
            if i_name_f:
                nl = br.read_bits(6) + 1
                iname = "".join(chr(br.read_bits(7)) for _ in range(nl)).strip()
            if br.read_bit() == 1:
                br.skip(16)
            inter_id = br.read_bits(16)
            rev = br.read_bits(7)
            br.skip(16)
            if i_moy:
                br.skip(20)
            if i_ts:
                br.skip(16)
            if i_lanes:
                lc = br.read_bits(4) + 1
                br.skip(lc * 8)
            move_cnt = min(br.read_bits(8) + 1, 12)
            phases_str = []
            for _ in range(move_cnt):
                if br.remaining < 16:
                    break
                br.read_bit()
                m_name, m_man = br.read_bit() == 1, br.read_bit() == 1
                if m_name:
                    ml = br.read_bits(6) + 1
                    br.skip(ml * 7)
                sg = br.read_bits(8)
                ev_cnt = min(br.read_bits(4) + 1, 4)
                first_st, first_cd = 0, None
                for e in range(ev_cnt):
                    if br.remaining < 8:
                        break
                    br.read_bit()
                    e_timing, e_speeds = br.read_bit() == 1, br.read_bit() == 1
                    ev_st = br.read_bits(4)
                    cd = None
                    if e_timing and br.remaining >= 20:
                        t_st, t_max, t_lik, t_conf, t_nxt = [br.read_bit() == 1 for _ in range(5)]
                        if t_st:
                            br.skip(16)
                        min_end = br.read_bits(16)
                        if 0 <= min_end <= 36000:
                            cd = min_end / 10.0
                        if t_max:
                            br.skip(16)
                        if t_lik:
                            br.skip(16)
                        if t_conf:
                            br.skip(4)
                        if t_nxt:
                            br.skip(16)
                    if e == 0:
                        first_st, first_cd = ev_st, cd
                    if e_speeds:
                        break
                st_str = map_event_state(first_st)
                cd_str = f" {first_cd:.1f}s" if first_cd is not None else ""
                details[f"SG {sg}"] = f"{st_str}{cd_str}"
                phases_str.append(f"SG{sg}={st_str.split()[0]}{cd_str}")
                if m_man:
                    break
            final_name = iname or f"Intersection #{inter_id}"
            details["Intersection"] = f"{final_name} (ID {inter_id}, rev {rev})"
            if i == 0:
                summary = f"V2I SPaT #{inter_id} ({final_name}): " + ", ".join(phases_str[:4])
            if i_man or i_reg or has_reg:
                break
        return "SPaT", summary

    if msg_id == 18:  # MAP
        if br.remaining < 32:
            return "MAP", "V2I MAP (short)"
        br.read_bit()
        has_ts, has_lt, has_lid, has_inter = [br.read_bit() == 1 for _ in range(4)]
        br.skip(4)
        if has_ts:
            br.skip(20)
        rev = br.read_bits(7)
        if has_lt:
            br.skip(4)
        if has_lid:
            br.skip(7)
        inter_id, lat, lon = 0, 0.0, 0.0
        if has_inter and br.remaining >= 64:
            br.read_bits(5)
            br.read_bit()
            i_name = br.read_bit() == 1
            br.skip(4)
            if i_name:
                nl = br.read_bits(6) + 1
                details["Intersection"] = "".join(chr(br.read_bits(7)) for _ in range(nl)).strip()
            if br.read_bit() == 1:
                br.skip(16)
            inter_id = br.read_bits(16)
            br.skip(7)
            br.read_bit()
            has_elev, _ = br.read_bit() == 1, br.read_bit() == 1
            lat = br.read_signed(31, -900_000_000) / 1e7
            lon = br.read_signed(32, -1_799_999_999) / 1e7
            if has_elev:
                br.skip(16)
        details["Intersection ID"] = f"{inter_id} (rev {rev})"
        if lat or lon:
            details["Ref GPS"] = f"{lat:.6f}, {lon:.6f}"
        return "MAP", f"V2I MAP Intersection #{inter_id} ({lat:.5f}, {lon:.5f})"

    simple = {
        31: ("TIM", f"V2I TIM Roadway Advisory ({len(data)}B)"),
        29: ("SRM", f"V2I SRM Signal Priority Request ({len(data)}B)"),
        30: ("SSM", f"V2I SSM Signal Priority Status ({len(data)}B)"),
        28: ("RTCM", f"V2I RTCM GPS Correction ({len(data)}B)"),
        32: ("PSM", f"V2P PSM Pedestrian/Cyclist Alert ({len(data)}B)"),
        22: ("EVA", f"V2V EVA Emergency Vehicle Alert ({len(data)}B)"),
        27: ("RSA", f"V2I RSA Roadside Hazard Alert ({len(data)}B)"),
    }
    return simple.get(msg_id)


def decode_wsmp(wsmp: bytes, details: dict):
    if len(wsmp) < 4:
        return "WSMP", f"Short WSMP ({len(wsmp)}B)"
    idx = 0
    sub_ver = wsmp[idx]
    idx += 1
    details["WSMP Ver"] = str(sub_ver & 0x07)
    if (sub_ver & 0x08) and idx < len(wsmp):
        ext_cnt = wsmp[idx]
        idx += 1
        for _ in range(ext_cnt):
            if idx + 1 < len(wsmp):
                idx += 1
                ext_len = wsmp[idx]
                idx += 1 + ext_len
    if idx >= len(wsmp):
        return "WSMP", "WSMP (header only)"
    idx += 1  # TPID
    psid = 0
    if idx < len(wsmp):
        b0 = wsmp[idx]
        if (b0 & 0x80) == 0:
            psid = b0; idx += 1
        elif (b0 & 0xC0) == 0x80 and idx + 1 < len(wsmp):
            psid = ((b0 & 0x3F) << 8) | wsmp[idx + 1]; idx += 2
        elif (b0 & 0xE0) == 0xC0 and idx + 2 < len(wsmp):
            psid = ((b0 & 0x1F) << 16) | (wsmp[idx + 1] << 8) | wsmp[idx + 2]; idx += 3
        elif idx + 3 < len(wsmp):
            psid = ((b0 & 0x0F) << 24) | (wsmp[idx + 1] << 16) | (wsmp[idx + 2] << 8) | wsmp[idx + 3]; idx += 4
    details["PSID"] = f"0x{psid:X}"
    if idx < len(wsmp):
        lb = wsmp[idx]; idx += 1
        if (lb & 0x80) and idx < len(wsmp):
            idx += 1
    if idx < len(wsmp):
        j2735 = unwrap_1609_2(wsmp[idx:], details)
        parsed = parse_j2735(j2735, details)
        if parsed:
            return parsed
    return "WSMP", f"WSMP PSID=0x{psid:X} ({len(wsmp)}B)"


def decode_geonet(gn: bytes, details: dict):
    if len(gn) < 16:
        return "GN", f"Short GeoNetworking ({len(gn)}B)"
    basic_nh = gn[0] & 0x0F
    if basic_nh == 2:
        return "GN", f"Secured GeoNetworking ({len(gn)}B)"
    common_nh = (gn[4] >> 4) & 0x0F
    ht = (gn[5] >> 4) & 0x0F
    ext_len = {5: 28, 4: 44, 1: 24}.get(ht, 28)
    btp_off = 12 + ext_len
    if common_nh in (1, 2) and len(gn) >= btp_off + 10:
        dest_port = (gn[btp_off] << 8) | gn[btp_off + 1]
        details["BTP Port"] = str(dest_port)
        its_off = btp_off + 4
        if len(gn) >= its_off + 6:
            msg_id = gn[its_off + 1]
            station_id = struct.unpack_from(">I", gn, its_off + 2)[0]
            details["Station ID"] = str(station_id)
            br = UperBitReader(gn, its_off + 6)
            if msg_id == 2:
                br.skip(16)
                st_type = br.read_bits(8)
                lat = br.read_signed(31, -900_000_000) / 1e7
                lon = br.read_signed(32, -1_800_000_000) / 1e7
                kmh = br.read_bits(14) * 0.01 * 3.6
                hdg = br.read_bits(12) * 0.1
                details["GPS"] = f"{lat:.6f}, {lon:.6f}"
                return "CAM", f"ETSI CAM #{station_id} (type {st_type}) · {kmh:.1f} km/h · {hdg:.0f}°"
            elif msg_id == 1:
                cause = br.read_bits(8)
                sub = br.read_bits(8)
                causes = {1: "Traffic Jam", 2: "Accident", 3: "Roadworks", 6: "Weather", 91: "Breakdown", 94: "Stationary Veh", 95: "Emergency Veh", 99: "Hazard"}
                cstr = causes.get(cause, f"Cause #{cause}")
                details["Cause"] = f"{cstr} (sub={sub})"
                return "DENM", f"ETSI DENM #{station_id} · {cstr}"
            elif msg_id == 4:
                return "SPATEM", f"ETSI SPATEM RSU #{station_id}"
            elif msg_id == 5:
                return "MAPEM", f"ETSI MAPEM RSU #{station_id}"
    return "GN", f"GeoNetworking HT={ht} ({len(gn)}B)"


def decode_80211_frame(frame: bytes, freq_mhz: float, rssi_db: float):
    details = {"Len": f"{len(frame)}B", "Freq": f"{freq_mhz:.1f} MHz", "Peak": f"{rssi_db:.1f} dBFS"}
    if len(frame) < 24:
        return "802.11", f"Short 802.11 frame ({len(frame)}B)", details

    fc0 = frame[0]
    ftype = (fc0 >> 2) & 0x03
    subtype = (fc0 >> 4) & 0x0F
    dst = fmt_mac(frame, 4)
    src = fmt_mac(frame, 10)
    details["Src"] = src
    details["Dst"] = dst

    if ftype == 0 and subtype == 8 and len(frame) > 38:
        ssid = extract_beacon_ssid(frame)
        if ssid is not None:
            details["SSID"] = ssid
            return "802.11", f'Beacon SSID="{ssid}" ({src})', details

    if ftype == 2:
        hdr_len = 26 if (subtype & 0x08) else 24
        if len(frame) > hdr_len + 8 and frame[hdr_len] == 0xAA and frame[hdr_len + 1] == 0xAA:
            etype = (frame[hdr_len + 6] << 8) | frame[hdr_len + 7]
            payload = frame[hdr_len + 8 :]
            details["EtherType"] = f"0x{etype:04X}"
            if etype == 0x88DC:
                proto, summ = decode_wsmp(payload, details)
                return proto, summ, details
            elif etype == 0x8947:
                proto, summ = decode_geonet(payload, details)
                return proto, summ, details
            return "802.11", f"Data EtherType=0x{etype:04X} ({src} -> {dst})", details

    tname = {0: "Mgmt", 1: "Ctrl", 2: "Data"}.get(ftype, f"T{ftype}")
    return "802.11", f"{tname} sub={subtype} ({src} -> {dst})", details


# ---------------------------------------------------------------------------
# IP Geolocation (macOS fallback when --lat/--lon not supplied; Android uses GPS)
# ---------------------------------------------------------------------------
def get_ip_location(timeout_s: float = 4.0):
    """Return (lat, lon, city, region) from IP geolocation, or (None, None, '', '') on failure."""
    # Try ipinfo.io first
    try:
        req = urllib.request.Request("https://ipinfo.io/json", headers={"User-Agent": "RoadSenseV2X/1.9"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            d = json.loads(resp.read().decode("utf-8", errors="ignore"))
        loc = d.get("loc") or ""
        if "," in loc:
            lat_s, lon_s = loc.split(",", 1)
            return float(lat_s), float(lon_s), (d.get("city") or ""), (d.get("region") or "")
    except Exception:
        pass
    # Fallback to ip-api.com
    try:
        req = urllib.request.Request("http://ip-api.com/json/", headers={"User-Agent": "RoadSenseV2X/1.9"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            d = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if d.get("status") == "success":
            return float(d["lat"]), float(d["lon"]), (d.get("city") or ""), (d.get("regionName") or "")
    except Exception:
        pass
    return None, None, "", ""


# ---------------------------------------------------------------------------
# Shared RSU Proximity Database (reads rsus.csv generated by find_rsus.py)
# ---------------------------------------------------------------------------
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


DEFAULT_RF_DB = os.path.join(os.path.dirname(SCRIPT_DIR), "data", "roadsense_rf.db")


def load_rsu_db(db_or_csv_path: str):
    """Load RSU sites from either unified SQLite roadsense_rf.db or a CSV file."""
    rsus = []
    if not db_or_csv_path or not os.path.exists(db_or_csv_path):
        return rsus
    if db_or_csv_path.lower().endswith(".db") or db_or_csv_path.lower().endswith(".sqlite"):
        try:
            conn = sqlite3.connect(f"file:{db_or_csv_path}?mode=ro", uri=True)
            cur = conn.execute(
                "SELECT name, callsign, lat, lon, freq_mhz, protocol, range_m FROM rsu_sites WHERE lat IS NOT NULL AND lon IS NOT NULL"
            )
            for name, callsign, lat, lon, freq, proto, rng in cur.fetchall():
                rsus.append({
                    "name": (name or callsign or "RSU").strip(),
                    "callsign": (callsign or "").strip(),
                    "lat": float(lat),
                    "lon": float(lon),
                    "freq_mhz": float(freq or 5905.0),
                    "protocol": (proto or "ITS-5.9G").strip(),
                    "range_m": float(rng or 600.0),
                })
            conn.close()
        except Exception:
            pass
        return rsus

    with open(db_or_csv_path, newline="", encoding="utf-8", errors="ignore") as f:
        for row in csv.DictReader(f):
            try:
                lat = float(row.get("lat") or "")
                lon = float(row.get("lon") or "")
            except ValueError:
                continue
            try:
                freq = float(row.get("freq_mhz") or 5905.0)
            except ValueError:
                freq = 5905.0
            try:
                rng = float(row.get("range_m") or 600.0)
            except ValueError:
                rng = 600.0
            rsus.append({
                "name": (row.get("name") or row.get("callsign") or "RSU").strip(),
                "callsign": (row.get("callsign") or "").strip(),
                "lat": lat,
                "lon": lon,
                "freq_mhz": freq,
                "protocol": (row.get("protocol") or "ITS-5.9G").strip(),
                "range_m": rng,
            })
    return rsus


def check_rsu_proximity(rsus, my_lat, my_lon):
    """Return (in_range_list, nearest_tuple_or_None) where tuple is (dist_m, rsu_dict)."""
    if not rsus or my_lat is None or my_lon is None:
        return [], None
    scored = []
    for r in rsus:
        d = haversine_m(my_lat, my_lon, r["lat"], r["lon"])
        scored.append((d, r))
    scored.sort(key=lambda x: x[0])
    in_range = [(d, r) for d, r in scored if d <= r["range_m"]]
    return in_range, (scored[0] if scored else None)


# ---------------------------------------------------------------------------
# PCAP Writer (LINKTYPE_IEEE802_11 = 105)
# ---------------------------------------------------------------------------
def append_pcap(path: str, frame: bytes):
    write_hdr = (not os.path.exists(path)) or os.path.getsize(path) == 0
    with open(path, "ab") as f:
        if write_hdr:
            f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 105))
        now = time.time()
        sec = int(now)
        usec = int((now - sec) * 1_000_000)
        f.write(struct.pack("<IIII", sec, usec, len(frame), len(frame)))
        f.write(frame)


# ---------------------------------------------------------------------------
# HackRF Continuous Streamer (hackrf_transfer -r -) & One-Shot Capture
# ---------------------------------------------------------------------------
class HackRfStream:
    """Persistent continuous HackRF I/Q pipe (0% blind time while locked on a frequency)."""

    def __init__(self):
        self.proc = None
        self.cfg = None  # (freq_hz, sample_rate, lna, vga, amp)

    def ensure_running(self, freq_hz: int, sample_rate: int, lna: int, vga: int, amp: bool):
        want = (int(freq_hz), int(sample_rate), int(lna), int(vga), bool(amp))
        if self.proc is not None and self.proc.poll() is None and self.cfg == want:
            return
        self.close()
        bb_bw = 15_000_000 if sample_rate >= 20_000_000 else 7_000_000
        cmd = [
            "hackrf_transfer",
            "-r", "-",
            "-f", str(want[0]),
            "-s", str(want[1]),
            "-b", str(bb_bw),
            "-l", str(want[2]),
            "-g", str(want[3]),
            "-a", "1" if want[4] else "0",
            "-p", "0",  # Bias-T ALWAYS OFF
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # Never block on unread stderr pipe
            bufsize=0,
        )
        self.cfg = want
        # Discard first ~15 ms PLL/ADC startup transient once on stream start
        startup_bytes = int(sample_rate * 0.015) * 2
        self._read_exact(startup_bytes)

    def _read_exact(self, n_bytes: int, timeout_s: float = 3.0) -> bytes:
        import select
        if self.proc is None or self.proc.stdout is None:
            raise RuntimeError("HackRF stream not open")
        fd = self.proc.stdout.fileno()
        buf = bytearray()
        deadline = time.monotonic() + timeout_s
        while len(buf) < n_bytes:
            rem = deadline - time.monotonic()
            if rem <= 0:
                self.close()
                raise RuntimeError("HackRF stream read timeout")
            rlist, _, _ = select.select([fd], [], [], min(0.5, rem))
            if not rlist:
                continue
            chunk = os.read(fd, min(65536, n_bytes - len(buf)))
            if not chunk:
                self.close()
                raise RuntimeError("HackRF stream ended unexpectedly")
            buf.extend(chunk)
        return bytes(buf)

    def read_window(self, freq_hz: int, sample_rate: int, duration_s: float, lna: int, vga: int, amp: bool) -> bytes:
        self.ensure_running(freq_hz, sample_rate, lna, vga, amp)
        n_bytes = max(65536, int(sample_rate * duration_s) * 2)
        return self._read_exact(n_bytes)

    def close(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1.0)
            except Exception:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=0.5)
                except Exception:
                    pass
            try:
                if self.proc.stdout:
                    self.proc.stdout.close()
                if self.proc.stderr:
                    self.proc.stderr.close()
            except Exception:
                pass
        self.proc = None
        self.cfg = None


def capture_hackrf(freq_hz: int, sample_rate: int, duration_s: float, lna: int, vga: int, amp: bool, tmp_path: str):
    num_samples = int(sample_rate * duration_s)
    bb_bw = 15_000_000 if sample_rate >= 20_000_000 else 7_000_000
    cmd = [
        "hackrf_transfer",
        "-r", tmp_path,
        "-f", str(int(freq_hz)),
        "-s", str(int(sample_rate)),
        "-b", str(int(bb_bw)),
        "-n", str(int(num_samples)),
        "-l", str(int(lna)),
        "-g", str(int(vga)),
        "-a", "1" if amp else "0",
        "-p", "0",  # Bias-T ALWAYS OFF
    ]
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(4.0, duration_s + 3.0),
        )
    except subprocess.TimeoutExpired:
        time.sleep(0.25)
        raise RuntimeError(f"hackrf_transfer timed out @ {freq_hz/1e6:.1f} MHz")
    if res.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        err = res.stderr.strip().splitlines()[-1] if res.stderr.strip() else f"rc={res.returncode}"
        time.sleep(0.2)
        raise RuntimeError(err)
    with open(tmp_path, "rb") as f:
        data = f.read()
    time.sleep(0.03)  # brief macOS libusb settle pause between hops
    return data


def is_wideband_burst(spec) -> bool:
    if not spec:
        return False
    return (spec["avg_bins_above"] >= 28) or (
        spec["burst_jump_db"] >= 6.0 and spec["peak_bins_above"] >= 24
    )


def process_dual_or_single_iq(dsp, iq: bytes, center_mhz: float, rate_hz: int,
                              lower_mhz, upper_mhz, check_cv2x: bool, trim_samples: int):
    """
    Analyze either a single channel or a 20 MS/s dual-channel capture (lower=Fc-5M, upper=Fc+5M).
    Returns: (display_spec, events_list, lock_candidate_tuple_or_None)
      events_list items: (freq_mhz, proto, summary, details, raw_frame_or_None, rssi_db)
      lock_candidate: (target_freq_mhz, is_cv2x_20m)
    """
    events = []
    lock_candidate = None

    if lower_mhz is not None and upper_mhz is not None and rate_hz >= 20_000_000:
        # Fast spectrum-only on full 20 MHz band (no wasted 20M OFDM decode!)
        full_spec = compute_spectrum_only(dsp, iq, trim_samples)
        # Only run ±5 MHz sub-channel extraction & 10M OFDM decode if there is RF energy in the 20M band
        band_has_energy = (full_spec is None) or (
            full_spec["burst_jump_db"] >= 2.5 or full_spec["avg_bins_above"] >= 10 or full_spec["peak_bins_above"] >= 10
        )
        if band_has_energy:
            sub_trim = max(0, trim_samples // 2)
            for direction, sub_freq in ((-1, lower_mhz), (+1, upper_mhz)):
                iq10 = extract_subchannel_10m(dsp, iq, direction)
                if len(iq10) < 65536:
                    continue
                sub_spec, sub_frames = analyze_iq(dsp, iq10, sub_trim)
                if sub_frames:
                    for fr in sub_frames:
                        proto, summ, det = decode_80211_frame(fr, sub_freq, sub_spec["max_peak_db"] if sub_spec else -60.0)
                        events.append((sub_freq, proto, summ, det, fr, sub_spec["max_peak_db"] if sub_spec else -60.0))
                    if lock_candidate is None:
                        lock_candidate = (sub_freq, False)
                elif is_wideband_burst(sub_spec):
                    events.append((
                        sub_freq, "RF_BURST",
                        f"10M RF burst +{sub_spec['burst_jump_db']:.1f} dB (peak {sub_spec['max_peak_db']:.1f} dBFS)",
                        {"Floor": f"{sub_spec['floor_db']:.1f} dBFS", "Peak": f"{sub_spec['max_peak_db']:.1f} dBFS"},
                        None, sub_spec["max_peak_db"]
                    ))
                    if lock_candidate is None:
                        lock_candidate = (sub_freq, False)

            # Also check full 20 MHz band for C-V2X PC5 on Ch 183 (5915 MHz)
            if check_cv2x and not events and is_wideband_burst(full_spec):
                events.append((
                    center_mhz, "C-V2X",
                    f"20M C-V2X burst +{full_spec['burst_jump_db']:.1f} dB (peak {full_spec['max_peak_db']:.1f} dBFS, {full_spec['peak_bins_above']} bins)",
                    {"Floor": f"{full_spec['floor_db']:.1f} dBFS", "Peak": f"{full_spec['max_peak_db']:.1f} dBFS"},
                    None, full_spec["max_peak_db"]
                ))
                lock_candidate = (center_mhz, True)
        return full_spec, events, lock_candidate

    # Single-channel path
    full_spec, direct_frames = analyze_iq(dsp, iq, trim_samples)
    if direct_frames:
        for fr in direct_frames:
            proto, summ, det = decode_80211_frame(fr, center_mhz, full_spec["max_peak_db"] if full_spec else -60.0)
            events.append((center_mhz, proto, summ, det, fr, full_spec["max_peak_db"] if full_spec else -60.0))
        lock_candidate = (center_mhz, False)
    elif is_wideband_burst(full_spec):
        is_c2x = (rate_hz >= 20_000_000 and center_mhz >= 5895.0)
        tag = "C-V2X" if is_c2x else "RF_BURST"
        events.append((
            center_mhz, tag,
            f"Wideband RF burst +{full_spec['burst_jump_db']:.1f} dB (peak {full_spec['max_peak_db']:.1f} dBFS, {full_spec['peak_bins_above']} bins)",
            {"Floor": f"{full_spec['floor_db']:.1f} dBFS", "Peak": f"{full_spec['max_peak_db']:.1f} dBFS"},
            None, full_spec["max_peak_db"]
        ))
        lock_candidate = (center_mhz, is_c2x)
    return full_spec, events, lock_candidate


def main():
    ap = argparse.ArgumentParser(
        description="RoadSense V2X — macOS Console Scanner & Protocol Decoder for HackRF One"
    )
    ap.add_argument("--preset", choices=list(PRESETS.keys()), default="scan-all",
                    help="Channel preset (default: scan-all covering 5860-5920 MHz; use wifi-test for desk test on 5745 MHz)")
    ap.add_argument("--freq", type=float, default=None, help="Custom center frequency in MHz (overrides preset)")
    ap.add_argument("--rate", type=int, default=None, help="Custom sample rate in Hz (10000000 or 20000000)")
    ap.add_argument("--lock-rate", type=int, choices=[10_000_000, 20_000_000], default=10_000_000,
                    help="Continuous streaming sample rate while auto-locked on a DSRC channel (default: 10000000)")
    ap.add_argument("--lock-sec", type=float, default=8.0,
                    help="Seconds to stay continuously locked on an active channel after last hit (default: 8.0)")
    ap.add_argument("--lna", type=int, default=24, help="HackRF LNA gain 0-40 dB in 8 dB steps (default: 24)")
    ap.add_argument("--vga", type=int, default=24, help="HackRF VGA gain 0-62 dB in 2 dB steps (default: 24)")
    ap.add_argument("--amp", action="store_true", help="Enable HackRF +14 dB RF amplifier")
    ap.add_argument("--duration", type=float, default=0.15, help="Capture window in seconds per chunk (default: 0.15)")
    ap.add_argument("--iq-file", type=str, default=None, help="Decode an offline raw int8 .iq file instead of live HackRF")
    ap.add_argument("--pcap", type=str, default=None, help="Save decoded 802.11 frames to a Wireshark .pcap file")
    ap.add_argument("--rsu-db", type=str, default=DEFAULT_RF_DB if os.path.exists(DEFAULT_RF_DB) else None,
                    help="Path to roadsense_rf.db or rsus.csv for RSU proximity alerts (auto-loads data/roadsense_rf.db)")
    ap.add_argument("--lat", type=float, default=None, help="Current latitude (decimal degrees) for RSU range check")
    ap.add_argument("--lon", type=float, default=None, help="Current longitude (decimal degrees) for RSU range check")
    ap.add_argument("-v", "--verbose", action="store_true", help="Print unpacked protocol fields and hex preview")
    ap.add_argument("--no-color", action="store_true", help="Disable ANSI color output")
    args = ap.parse_args()

    use_color = (not args.no_color) and sys.stdout.isatty()

    def c(code, text):
        return f"{code}{text}{RESET}" if use_color else text

    dsp = load_dsp()

    p_freq, p_rate, mode, p_desc = PRESETS[args.preset]
    if args.freq is not None:
        p_freq = args.freq
        mode = "single"
    if args.rate is not None:
        p_rate = args.rate

    def print_pkt(proto, summary, details, freq_mhz, frame_bytes=None):
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        col = PROTO_COLORS.get(proto, "\033[37m")
        badge = c(BOLD + col, f"[{proto:^6}]")
        if sys.stdout.isatty():
            sys.stdout.write("\r\033[K")
        print(f"{c(DIM, ts)} {badge} {freq_mhz:6.1f} MHz  {summary}")
        if args.verbose:
            kv = "  ".join(f"{k}={v}" for k, v in details.items())
            print(f"           {c(DIM, kv)}")
            if frame_bytes:
                hx = " ".join(f"{b:02X}" for b in frame_bytes[:48])
                print(f"           {c(DIM, 'HEX: ' + hx)}")
        sys.stdout.flush()

    # Offline .iq mode
    if args.iq_file:
        with open(args.iq_file, "rb") as f:
            iq = f.read()
        trim = int(p_rate * 0.015)
        spec, frames = analyze_iq(dsp, iq, trim)
        print(f"Offline decode: {args.iq_file} ({len(iq):,} bytes @ {p_freq:.1f} MHz, {p_rate/1e6:.0f} MS/s)")
        if spec:
            print(f"Spectrum: floor={spec['floor_db']:.1f} dBFS, peak={spec['max_peak_db']:.1f} dBFS, jump=+{spec['burst_jump_db']:.1f} dB")
        for fr in frames:
            if args.pcap:
                append_pcap(args.pcap, fr)
            proto, summ, det = decode_80211_frame(fr, p_freq, spec["max_peak_db"] if spec else -60.0)
            print_pkt(proto, summ, det, p_freq, fr)
        print(f"Total decoded frames: {len(frames)}")
        return

    if not shutil.which("hackrf_transfer"):
        print("Error: 'hackrf_transfer' not found in PATH. Install with: brew install hackrf", file=sys.stderr)
        sys.exit(1)

    running = True
    def on_sigint(_sig, _frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, on_sigint)

    print(c(BOLD, "RoadSense V2X — macOS Console Decoder (Continuous Stream + Auto-Lock, Bias-T OFF)"))
    print(f"Mode: {p_desc if args.freq is None else f'{p_freq:.1f} MHz @ {p_rate/1e6:.0f} MS/s'} | LockRate={args.lock_rate//1_000_000} MS/s ({args.lock_sec:.0f}s) | LNA={args.lna}dB VGA={args.vga}dB Amp={'ON' if args.amp else 'OFF'}")
    if args.pcap:
        print(f"PCAP logging -> {args.pcap}")
    if args.rsu_db:
        rsus = load_rsu_db(args.rsu_db)
        my_lat, my_lon = args.lat, args.lon
        loc_src = "manual"
        if my_lat is None or my_lon is None:
            my_lat, my_lon, city, region = get_ip_location()
            loc_src = f"IP ({city}, {region})".strip(", ()") if my_lat is not None else "unavailable"
        in_rng, nearest = check_rsu_proximity(rsus, my_lat, my_lon)
        if in_rng:
            d0, r0 = in_rng[0]
            print(c(BOLD + "\033[92m", f"RSU IN RANGE ({len(in_rng)}): {r0['name']} [{r0['callsign']}] — {d0:.0f}m away ({r0['freq_mhz']:.1f} MHz, {r0['protocol']}) [{loc_src}]"))
        elif nearest:
            d0, r0 = nearest
            print(c("\033[93m", f"Nearest RSU: {r0['name']} [{r0['callsign']}] — {d0/1000.0:.2f} km away (loaded {len(rsus)} RSUs, loc: {loc_src})"))
        else:
            print(f"Loaded {len(rsus)} geolocated RSU(s) from {args.rsu_db} (location {loc_src})")
    print("Press Ctrl-C to stop.\n")

    sweeps = 0
    hits = 0
    pkts = 0
    scan_idx = 0
    locked_freq = None
    locked_rate = args.lock_rate
    lock_until = 0.0
    stream = HackRfStream()
    with tempfile.NamedTemporaryFile(prefix="roadsense_hop_", suffix=".iq", delete=False) as tf:
        tmp_path = tf.name

    hops = SAFETY_SCAN_HOPS if mode == "dual-safety" else (SAFETY_SCAN_HOPS + LEGACY_SCAN_HOPS if mode == "dual-all" else None)

    try:
        while running:
            now = time.monotonic()
            if locked_freq is not None and now > lock_until:
                if sys.stdout.isatty():
                    sys.stdout.write("\r\033[K")
                print(c(DIM, f"  [Auto-Lock expired on {locked_freq:.1f} MHz — resuming scan]"))
                locked_freq = None

            if locked_freq is not None:
                center_mhz, rate_hz, lower_mhz, upper_mhz, check_cv2x = locked_freq, locked_rate, None, None, False
                trim = 0  # already trimmed on stream start!
            elif hops is not None:
                center_mhz, rate_hz, lower_mhz, upper_mhz, check_cv2x = hops[scan_idx % len(hops)]
                scan_idx += 1
                trim = 0
            else:
                center_mhz, rate_hz, lower_mhz, upper_mhz, check_cv2x = p_freq, p_rate, None, None, False
                trim = 0

            try:
                if locked_freq is not None or hops is None:
                    # Continuous zero-gap stream when locked or on single channel
                    iq = stream.read_window(int(center_mhz * 1e6), rate_hz, args.duration, args.lna, args.vga, args.amp)
                else:
                    # Clean finite capture when hopping frequencies (never hangs on SIGTERM)
                    stream.close()
                    iq = capture_hackrf(int(center_mhz * 1e6), rate_hz, args.duration, args.lna, args.vga, args.amp, tmp_path)
                    trim = int(rate_hz * 0.015)
            except RuntimeError as e:
                if sys.stdout.isatty():
                    sys.stdout.write("\r\033[K")
                print(c("\033[91m", f"[HackRF error @ {center_mhz:.1f} MHz] {e} — retrying..."))
                time.sleep(0.4)
                continue

            sweeps += 1
            spec, events, lock_cand = process_dual_or_single_iq(
                dsp, iq, center_mhz, rate_hz, lower_mhz, upper_mhz, check_cv2x, trim
            )

            if events:
                hits += 1
                for ev_freq, proto, summ, det, fr, _ in events:
                    if fr is not None:
                        pkts += 1
                        if args.pcap:
                            append_pcap(args.pcap, fr)
                    print_pkt(proto, summ, det, ev_freq, fr)

                if hops is not None and lock_cand is not None:
                    cand_freq, cand_is_cv2x = lock_cand
                    new_rate = 20_000_000 if cand_is_cv2x else args.lock_rate
                    if locked_freq != cand_freq:
                        if sys.stdout.isatty():
                            sys.stdout.write("\r\033[K")
                        print(c("\033[92m", f"  [AUTO-LOCK -> {cand_freq:.1f} MHz @ {new_rate//1_000_000} MS/s continuous for {args.lock_sec:.0f}s]"))
                    locked_freq = cand_freq
                    locked_rate = new_rate
                    lock_until = time.monotonic() + args.lock_sec

            if sys.stdout.isatty():
                fl = f"{spec['floor_db']:.1f}" if spec else "--"
                tag = f"LOCK {locked_freq:.1f}M ({max(0.0, lock_until - time.monotonic()):.1f}s)" if locked_freq else f"{center_mhz:.1f}M"
                sys.stdout.write(
                    f"\r\033[K{c(DIM, f'Sweeps: {sweeps} | Hits: {hits} | Pkts: {pkts} | {tag} ({rate_hz//1_000_000} MS/s) | Floor {fl} dBFS')}"
                )
                sys.stdout.flush()
    finally:
        stream.close()
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        if sys.stdout.isatty():
            print()
        print(f"Stopped. Sweeps={sweeps}, Hits={hits}, Decoded Packets={pkts}")


if __name__ == "__main__":
    main()
