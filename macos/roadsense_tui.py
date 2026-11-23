#!/usr/bin/env python3
"""
RoadSense V2X — macOS ncurses TUI Scanner & Protocol Decoder for HackRF One
3-Tab terminal dashboard mirroring the Android app:
  [Tab 1] Protocol    — Scrollable V2X/802.11 packet feed + unpacked field & hex inspector
  [Tab 2] RF Scope    — Full-size live 256-bin FFT spectrum + peak hold + HackRF gain controls
  [Tab 3] Signal View — Live ASCII/Unicode Traffic Signal Head with SPaT countdown & SG switcher
"""

import argparse
import curses
import datetime
import os
import re
import shutil
import sys
import tempfile
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import roadsense_cli as cli

PRESET_KEYS = list(cli.PRESETS.keys())
TABS = ["Protocol", "RF Scope", "Signal View"]


def extract_spat_from_details(details: dict):
    """Build a structured SPaT dict for Signal View from decoded J2735 SPaT details."""
    inter = details.get("Intersection", "Intersection")
    phases = []
    for k, v in details.items():
        if k.startswith("SG "):
            m = re.match(r"SG\s+(\d+)", k)
            sg = int(m.group(1)) if m else len(phases) + 1
            up = v.upper()
            if "GREEN_ARROW" in up or "PROTECTED" in up and "GREEN" in up:
                color = "PROTECTED_GREEN"
            elif "GREEN" in up:
                color = "GREEN"
            elif "YELLOW" in up or "CAUTION" in up:
                color = "YELLOW"
            elif "FLASHING_RED" in up:
                color = "FLASHING_RED"
            elif "RED" in up or "PRE_MOVEMENT" in up:
                color = "RED"
            else:
                color = "DARK"
            cm = re.search(r"([0-9]+\.[0-9]+)s", v)
            cd = float(cm.group(1)) if cm else None
            phases.append({"sg": sg, "label": k, "color": color, "raw": v, "countdown": cd})
    if not phases:
        return None
    return {"name": inter, "phases": phases}


class AppState:
    def __init__(self, args):
        self.lock = threading.Lock()
        self.running = True
        self.capturing = not args.paused
        self.demo_mode = args.demo
        self.active_tab = 0  # 0=Protocol, 1=RF Scope, 2=Signal View
        self.preset_idx = PRESET_KEYS.index(args.preset) if args.preset in PRESET_KEYS else 0
        self.custom_freq = args.freq
        self.custom_rate = args.rate
        self.lna = args.lna
        self.vga = args.vga
        self.amp = args.amp
        self.duration = args.duration
        self.pcap_path = args.pcap
        self.lock_rate_hz = args.lock_rate
        self.lock_sec = args.lock_sec
        self.locked_freq = None
        self.locked_rate = args.lock_rate
        self.lock_until = 0.0
        self.rsus = cli.load_rsu_db(args.rsu_db) if args.rsu_db else []
        self.my_lat = args.lat
        self.my_lon = args.lon
        if self.rsus and (self.my_lat is None or self.my_lon is None):
            ip_lat, ip_lon, _, _ = cli.get_ip_location()
            self.my_lat, self.my_lon = ip_lat, ip_lon
        self.rsu_in_range, self.rsu_nearest = cli.check_rsu_proximity(self.rsus, self.my_lat, self.my_lon)

        self.current_freq_mhz = cli.PRESETS[PRESET_KEYS[self.preset_idx]][0]
        self.current_rate_hz = cli.PRESETS[PRESET_KEYS[self.preset_idx]][1]
        self.status_msg = "Initializing..."
        self.status_err = False

        self.sweeps = 0
        self.hits = 0
        self.pkts_total = 0
        self.latest_spec = None
        self.latest_spat = None
        self.selected_sg_idx = 0
        self.packets = []  # newest first
        self.selected_idx = 0
        self.scroll_offset = 0
        self.scan_idx = 0

    def active_preset_tuple(self):
        key = PRESET_KEYS[self.preset_idx]
        p_freq, p_rate, mode, p_desc = cli.PRESETS[key]
        if self.custom_freq is not None:
            p_freq = self.custom_freq
            mode = "single"
        if self.custom_rate is not None:
            p_rate = self.custom_rate
        return key, p_freq, p_rate, mode, p_desc

    def clear_all(self):
        with self.lock:
            self.packets.clear()
            self.sweeps = 0
            self.hits = 0
            self.pkts_total = 0
            self.latest_spec = None
            self.latest_spat = None
            self.selected_idx = 0
            self.scroll_offset = 0
            self.selected_sg_idx = 0
            self.locked_freq = None
            self.lock_until = 0.0
            self.status_msg = "Cleared all captured data"
            self.status_err = False


def capture_worker(state: AppState, dsp):
    stream = cli.HackRfStream()
    with tempfile.NamedTemporaryFile(prefix="roadsense_tui_hop_", suffix=".iq", delete=False) as tf:
        tmp_path = tf.name
    demo_tick = 0
    try:
        while True:
            with state.lock:
                if not state.running:
                    break
                capturing = state.capturing
                demo = state.demo_mode
                key, p_freq, p_rate, mode, _ = state.active_preset_tuple()
                lna, vga, amp, dur = state.lna, state.vga, state.amp, state.duration
                pcap_path = state.pcap_path
                scan_idx = state.scan_idx
                lock_rate_cfg = state.lock_rate_hz
                lock_sec = state.lock_sec
                locked_freq = state.locked_freq
                locked_rate = state.locked_rate
                lock_until = state.lock_until

            if not capturing:
                stream.close()
                with state.lock:
                    if not state.status_msg.startswith("Cleared"):
                        state.status_msg = "Paused — press [Space] to start"
                        state.status_err = False
                time.sleep(0.1)
                continue

            if demo:
                stream.close()
                demo_tick += 1
                t = (demo_tick * 2) % 300
                if t < 140:
                    sg2_col, sg2_rem, sg2_raw = "GREEN", (140 - t) / 10.0, f"GREEN (permissive) {(140-t)/10.0:.1f}s"
                elif t < 180:
                    sg2_col, sg2_rem, sg2_raw = "YELLOW", (180 - t) / 10.0, f"YELLOW (clearance) {(180-t)/10.0:.1f}s"
                else:
                    sg2_col, sg2_rem, sg2_raw = "RED", (300 - t) / 10.0, f"RED (stop-And-Remain) {(300-t)/10.0:.1f}s"

                if t < 60:
                    sg5_col, sg5_rem, sg5_raw = "PROTECTED_GREEN", (60 - t) / 10.0, f"GREEN_ARROW (protected) {(60-t)/10.0:.1f}s"
                elif t < 90:
                    sg5_col, sg5_rem, sg5_raw = "YELLOW", (90 - t) / 10.0, f"YELLOW_ARROW {(90-t)/10.0:.1f}s"
                else:
                    sg5_col, sg5_rem, sg5_raw = "RED", (300 - t) / 10.0, f"RED (stop-And-Remain) {(300-t)/10.0:.1f}s"

                if t < 180:
                    sg6_col, sg6_rem, sg6_raw = "RED", (180 - t) / 10.0, f"RED (stop-And-Remain) {(180-t)/10.0:.1f}s"
                elif t < 260:
                    sg6_col, sg6_rem, sg6_raw = "GREEN", (260 - t) / 10.0, f"GREEN (permissive) {(260-t)/10.0:.1f}s"
                else:
                    sg6_col, sg6_rem, sg6_raw = "YELLOW", (300 - t) / 10.0, f"YELLOW (clearance) {(300-t)/10.0:.1f}s"

                spat_obj = {
                    "name": "Intersection #4102 (Demo: Main & 5th)",
                    "phases": [
                        {"sg": 2, "label": "SG 2 · NB Through", "color": sg2_col, "raw": sg2_raw, "countdown": sg2_rem},
                        {"sg": 5, "label": "SG 5 · NB Left Arrow", "color": sg5_col, "raw": sg5_raw, "countdown": sg5_rem},
                        {"sg": 6, "label": "SG 6 · EB Through", "color": sg6_col, "raw": sg6_raw, "countdown": sg6_rem},
                    ],
                }

                avg_db = [
                    -76.0 + (18.0 if abs((i - 128) / 64.0) < 0.78 else 0.0)
                    + ((demo_tick * 7 + i * 11) % 7) * 0.45
                    for i in range(256)
                ]
                peak_db = [v + 5.5 + ((demo_tick + i) % 5) * 0.6 for i, v in enumerate(avg_db)]
                spec = {
                    "avg_db": avg_db,
                    "peak_db": peak_db,
                    "floor_db": -75.2,
                    "max_peak_db": max(peak_db[24:232]),
                    "max_peak_bin": 128,
                    "burst_jump_db": 8.4,
                    "avg_bins_above": 100,
                    "peak_bins_above": 110,
                }
                ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                new_pkts = []
                if demo_tick % 5 == 0:
                    new_pkts.append({
                        "ts": ts, "proto": "SPaT", "freq": 5900.0, "rssi": -48.2,
                        "summary": f"V2I SPaT #4102: SG2={sg2_col} ({sg2_rem:.1f}s), SG5={sg5_col}",
                        "details": {"Intersection": "Main & 5th (ID 4102)", "SG 2": sg2_raw, "SG 5": sg5_raw, "SG 6": sg6_raw},
                        "hex": "88 00 00 00 FF FF FF FF FF FF 02 00 00 00 00 01 AA AA 03 00 00 00 88 DC",
                    })
                if demo_tick % 9 == 0:
                    spd = 34.0 + (demo_tick % 9)
                    new_pkts.append({
                        "ts": ts, "proto": "BSM", "freq": 5890.0, "rssi": -51.4,
                        "summary": f"V2V BSM 0xA4F1029C · {spd:.1f} mph · Hdg 88° · Brakes {'ON' if sg2_col == 'RED' else 'off'}",
                        "details": {"Temp ID": "0xA4F1029C", "Speed": f"{spd:.1f} mph", "GPS": "37.774900, -122.419400"},
                        "hex": "88 00 00 00 FF FF FF FF FF FF 02 00 00 00 00 02 AA AA 03 00 00 00 88 DC",
                    })
                if demo_tick % 14 == 0:
                    new_pkts.append({
                        "ts": ts, "proto": "DENM", "freq": 5900.0, "rssi": -49.8,
                        "summary": "ETSI DENM #77001 · Roadworks ahead",
                        "details": {"Station ID": "77001", "Cause": "Roadworks (sub=0)", "BTP Port": "2002"},
                        "hex": "88 00 00 00 FF FF FF FF FF FF 02 00 00 00 00 03 AA AA 03 00 00 00 89 47",
                    })
                with state.lock:
                    state.current_freq_mhz = 5900.0
                    state.current_rate_hz = 10_000_000
                    state.sweeps += 1
                    state.latest_spec = spec
                    state.latest_spat = spat_obj
                    state.status_msg = "DEMO MODE — simulated 5.9 GHz V2X traffic"
                    state.status_err = False
                    for p in new_pkts:
                        state.hits += 1
                        state.pkts_total += 1
                        state.packets.insert(0, p)
                    if len(state.packets) > 300:
                        del state.packets[300:]
                time.sleep(0.20)
                continue

            # Live HackRF continuous streaming + 2-hop dual-channel scan + Auto-Lock
            hops = (
                cli.SAFETY_SCAN_HOPS if mode == "dual-safety"
                else (cli.SAFETY_SCAN_HOPS + cli.LEGACY_SCAN_HOPS if mode == "dual-all" else None)
            )
            now = time.monotonic()
            if locked_freq is not None and (now > lock_until or hops is None):
                locked_freq = None
                with state.lock:
                    state.locked_freq = None
                    state.lock_until = 0.0

            if locked_freq is not None:
                center_mhz, rate_hz, lower_mhz, upper_mhz, check_cv2x = locked_freq, locked_rate, None, None, False
                rem = max(0.0, lock_until - now)
                status_line = f"LOCKED @ {center_mhz:.1f} MHz ({rate_hz // 1_000_000} MS/s stream, {rem:.1f}s left)"
            elif hops is not None:
                center_mhz, rate_hz, lower_mhz, upper_mhz, check_cv2x = hops[scan_idx % len(hops)]
                with state.lock:
                    state.scan_idx = scan_idx + 1
                sub_str = f"{lower_mhz:.0f}+{upper_mhz:.0f}" if lower_mhz else f"{center_mhz:.0f}"
                status_line = f"Scanning {sub_str} MHz ({rate_hz // 1_000_000} MS/s)"
            else:
                center_mhz, rate_hz, lower_mhz, upper_mhz, check_cv2x = p_freq, p_rate, None, None, False
                status_line = f"Live Stream @ {center_mhz:.1f} MHz ({rate_hz // 1_000_000} MS/s)"

            with state.lock:
                state.current_freq_mhz = center_mhz
                state.current_rate_hz = rate_hz
                state.status_msg = status_line
                state.status_err = False

            try:
                if locked_freq is not None or hops is None:
                    # Persistent continuous stream (0% blind time) when locked or single-channel
                    iq = stream.read_window(int(center_mhz * 1e6), rate_hz, dur, lna, vga, amp)
                    trim = 0
                else:
                    # Clean finite capture when hopping between scan frequencies
                    stream.close()
                    iq = cli.capture_hackrf(int(center_mhz * 1e6), rate_hz, dur, lna, vga, amp, tmp_path)
                    trim = int(rate_hz * 0.015)

                spec, events, lock_cand = cli.process_dual_or_single_iq(
                    dsp, iq, center_mhz, rate_hz, lower_mhz, upper_mhz, check_cv2x, trim
                )
            except Exception as e:
                with state.lock:
                    state.status_msg = f"RX error: {e}"
                    state.status_err = True
                time.sleep(0.4)
                continue

            ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            decoded_batch = []
            new_spat = None
            for ev_freq, proto, summ, det, fr, rssi in events:
                if fr is not None and pcap_path:
                    cli.append_pcap(pcap_path, fr)
                hx = " ".join(f"{b:02X}" for b in fr[:48]) if fr else ""
                decoded_batch.append({
                    "ts": ts, "proto": proto, "freq": ev_freq,
                    "rssi": rssi, "summary": summ, "details": det, "hex": hx,
                })
                if proto == "SPaT":
                    sp = extract_spat_from_details(det)
                    if sp:
                        new_spat = sp

            with state.lock:
                state.sweeps += 1
                if spec:
                    state.latest_spec = spec
                if new_spat:
                    state.latest_spat = new_spat
                if decoded_batch:
                    state.hits += 1
                    for p in decoded_batch:
                        if p["proto"] not in ("C-V2X", "RF_BURST"):
                            state.pkts_total += 1
                        state.packets.insert(0, p)
                    if len(state.packets) > 300:
                        del state.packets[300:]
                    if hops is not None and lock_cand is not None:
                        cand_freq, cand_is_cv2x = lock_cand
                        state.locked_freq = cand_freq
                        state.locked_rate = 20_000_000 if cand_is_cv2x else lock_rate_cfg
                        state.lock_until = time.monotonic() + lock_sec
    finally:
        stream.close()
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def safe_addstr(win, y, x, text, attr=0):
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y or x >= max_x:
        return
    avail = max_x - x - 1
    if avail <= 0:
        return
    try:
        win.addstr(y, x, text[:avail], attr)
    except curses.error:
        pass


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)                  # cyan accent / spectrum avg
    curses.init_pair(2, curses.COLOR_GREEN, -1)                 # green / running / SPaT
    curses.init_pair(3, curses.COLOR_YELLOW, -1)                # yellow / BSM / paused
    curses.init_pair(4, curses.COLOR_RED, -1)                   # red / error / EVA / peak
    curses.init_pair(5, curses.COLOR_BLUE, -1)                  # blue / MAP / borders
    curses.init_pair(6, curses.COLOR_MAGENTA, -1)               # magenta / WSMP / GN
    curses.init_pair(7, curses.COLOR_WHITE, -1)                 # white / normal
    curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_CYAN)  # top/bottom bar
    curses.init_pair(9, curses.COLOR_BLACK, curses.COLOR_WHITE) # selected row / active tab
    curses.init_pair(10, curses.COLOR_BLACK, curses.COLOR_RED)     # lit RED lens
    curses.init_pair(11, curses.COLOR_BLACK, curses.COLOR_YELLOW)  # lit YELLOW lens
    curses.init_pair(12, curses.COLOR_BLACK, curses.COLOR_GREEN)   # lit GREEN lens


def proto_color(proto: str):
    return {
        "SPaT": curses.color_pair(2) | curses.A_BOLD,
        "SPATEM": curses.color_pair(2) | curses.A_BOLD,
        "MAP": curses.color_pair(5) | curses.A_BOLD,
        "MAPEM": curses.color_pair(5) | curses.A_BOLD,
        "BSM": curses.color_pair(3) | curses.A_BOLD,
        "TIM": curses.color_pair(3),
        "RSA": curses.color_pair(3),
        "EVA": curses.color_pair(4) | curses.A_BOLD,
        "DENM": curses.color_pair(4) | curses.A_BOLD,
        "CAM": curses.color_pair(1) | curses.A_BOLD,
        "C-V2X": curses.color_pair(1) | curses.A_BOLD,
        "WSMP": curses.color_pair(6),
        "GN": curses.color_pair(6),
        "SRM": curses.color_pair(6),
        "SSM": curses.color_pair(6),
        "PSM": curses.color_pair(6),
        "RTCM": curses.color_pair(2),
    }.get(proto, curses.color_pair(7))


# ---------------------------------------------------------------------------
# Tab 1: Protocol Feed + Packet Inspector
# ---------------------------------------------------------------------------
def draw_protocol_tab(stdscr, top_y, height, width, packets, sel, scroll_offset, freq_mhz, capturing):
    insp_h = 6 if height >= 14 else 4
    feed_h = max(3, height - insp_h)
    insp_top = top_y + feed_h

    safe_addstr(stdscr, top_y, 0, "─" * (width - 1), curses.color_pair(5))
    safe_addstr(stdscr, top_y, 2, f" Decoded Protocol Feed ({len(packets)}) — ↑/↓ to inspect ", curses.color_pair(1) | curses.A_BOLD)

    list_rows = max(1, feed_h - 1)
    if not packets:
        msg = (
            f"Listening @ {freq_mhz:.1f} MHz — waiting for decoded 802.11p / V2X / Wi-Fi packets..."
            if capturing else
            "Paused. Press [Space] to start listening, [9] for Wi-Fi Test (5745 MHz), or [d] for Demo."
        )
        safe_addstr(stdscr, top_y + 2, 2, msg, curses.A_DIM)
    else:
        for r in range(list_rows):
            idx = scroll_offset + r
            if idx >= len(packets):
                break
            p = packets[idx]
            y = top_y + 1 + r
            line = f" {p['ts']}  [{p['proto']:^6}]  {p['freq']:6.1f} MHz  {p['summary']}"
            if idx == sel:
                safe_addstr(stdscr, y, 0, line.ljust(width - 1), curses.color_pair(9) | curses.A_BOLD)
            else:
                safe_addstr(stdscr, y, 1, p["ts"], curses.A_DIM)
                safe_addstr(stdscr, y, 15, f"[{p['proto']:^6}]", proto_color(p["proto"]))
                safe_addstr(stdscr, y, 25, f"{p['freq']:6.1f} MHz  {p['summary']}", curses.color_pair(7))

    # Inspector Drawer
    safe_addstr(stdscr, insp_top, 0, "─" * (width - 1), curses.color_pair(5))
    safe_addstr(stdscr, insp_top, 2, " Packet Inspector ", curses.color_pair(1) | curses.A_BOLD)
    if packets and sel < len(packets):
        p = packets[sel]
        kv_items = [f"{k}: {v}" for k, v in p["details"].items()]
        safe_addstr(stdscr, insp_top + 1, 2, "  |  ".join(kv_items[:4]), curses.color_pair(7))
        if insp_h >= 5 and len(kv_items) > 4:
            safe_addstr(stdscr, insp_top + 2, 2, "  |  ".join(kv_items[4:8]), curses.color_pair(7))
        if p["hex"] and insp_h >= 4:
            hx_y = insp_top + (3 if insp_h >= 5 else 2)
            safe_addstr(stdscr, hx_y, 2, f"HEX: {p['hex']}", curses.color_pair(2))
    else:
        safe_addstr(stdscr, insp_top + 1, 2, "Select a packet to view unpacked fields & hex dump.", curses.A_DIM)


# ---------------------------------------------------------------------------
# Tab 2: Live RF Scope + HackRF Controls
# ---------------------------------------------------------------------------
def draw_rf_scope_tab(stdscr, top_y, height, width, spec, freq_mhz, rate_hz, preset_idx, lna, vga, amp):
    ctrl_h = 7 if height >= 16 else 4
    spec_h = max(6, height - ctrl_h)
    ctrl_top = top_y + spec_h

    safe_addstr(stdscr, top_y, 0, "─" * (width - 1), curses.color_pair(5))
    title = f" Live RF Spectrum @ {freq_mhz:.1f} MHz ({rate_hz // 1_000_000} MS/s) "
    if spec:
        title += f"[Floor {spec['floor_db']:.1f} | Peak {spec['max_peak_db']:.1f} dBFS | Jump +{spec['burst_jump_db']:.1f} dB] "
    safe_addstr(stdscr, top_y, 2, title, curses.color_pair(1) | curses.A_BOLD)

    plot_h = spec_h - 2
    plot_w = max(16, width - 10)
    x0 = 8
    min_db, max_db = -90.0, -30.0

    for row in range(plot_h):
        db_val = max_db - (max_db - min_db) * (row / max(1, plot_h - 1))
        if row in (0, plot_h // 2, plot_h - 1):
            safe_addstr(stdscr, top_y + 1 + row, 0, f"{db_val:5.0f} │", curses.color_pair(7) | curses.A_DIM)
        else:
            safe_addstr(stdscr, top_y + 1 + row, 6, "│", curses.color_pair(7) | curses.A_DIM)

    if spec and "avg_db" in spec:
        avg_arr = spec["avg_db"]
        peak_arr = spec["peak_db"]
        for col in range(plot_w):
            b0 = int(col * 256 / plot_w)
            b1 = max(b0 + 1, int((col + 1) * 256 / plot_w))
            avg_v = sum(avg_arr[b0:b1]) / (b1 - b0)
            pk_v = max(peak_arr[b0:b1])

            avg_norm = max(0.0, min(1.0, (avg_v - min_db) / (max_db - min_db)))
            pk_norm = max(0.0, min(1.0, (pk_v - min_db) / (max_db - min_db)))
            avg_rows = int(round(avg_norm * plot_h))
            pk_row = plot_h - 1 - int(round(pk_norm * (plot_h - 1)))

            for r in range(plot_h):
                from_bottom = plot_h - r
                y = top_y + 1 + r
                if from_bottom <= avg_rows:
                    safe_addstr(stdscr, y, x0 + col, "█", curses.color_pair(1))
                elif r == pk_row and pk_norm > 0.05:
                    safe_addstr(stdscr, y, x0 + col, "·", curses.color_pair(4) | curses.A_BOLD)
    else:
        safe_addstr(stdscr, top_y + 1 + plot_h // 2, x0 + 4, "Waiting for RF samples... (press [Space] to start or [d] for Demo)", curses.A_DIM)

    bw_mhz = rate_hz / 1e6
    axis_y = top_y + spec_h - 1
    safe_addstr(stdscr, axis_y, x0, f"{freq_mhz - bw_mhz / 2:.1f} MHz", curses.A_DIM)
    mid_f = f"{freq_mhz:.1f} MHz"
    safe_addstr(stdscr, axis_y, x0 + max(0, plot_w // 2 - len(mid_f) // 2), mid_f, curses.color_pair(1))
    right_f = f"{freq_mhz + bw_mhz / 2:.1f} MHz"
    safe_addstr(stdscr, axis_y, x0 + max(0, plot_w - len(right_f)), right_f, curses.A_DIM)

    # HackRF Controls Panel
    safe_addstr(stdscr, ctrl_top, 0, "─" * (width - 1), curses.color_pair(5))
    safe_addstr(stdscr, ctrl_top, 2, " HackRF One RF Settings (Bias-T always OFF) ", curses.color_pair(1) | curses.A_BOLD)

    def bar(val, max_v, w=20):
        filled = int(round((val / max_v) * w))
        return "█" * filled + "░" * (w - filled)

    safe_addstr(stdscr, ctrl_top + 1, 2, f"LNA Gain [l/L]: {lna:2d} dB  [{bar(lna, 40)}]   RF Amp [a]: {'ON (+14 dB)' if amp else 'OFF'}", curses.color_pair(7))
    safe_addstr(stdscr, ctrl_top + 2, 2, f"VGA Gain [v/V]: {vga:2d} dB  [{bar(vga, 62)}]   Legend: █ Avg  · Peak Hold", curses.color_pair(7))
    if ctrl_h >= 6:
        preset_line = "Presets [1-9]: " + "  ".join(
            f"[{i+1}]{k}" if i != preset_idx else f"*{i+1}:{k}*"
            for i, k in enumerate(PRESET_KEYS[:5])
        )
        preset_line2 = "               " + "  ".join(
            f"[{i+1}]{k}" if i != preset_idx else f"*{i+1}:{k}*"
            for i, k in enumerate(PRESET_KEYS[5:], start=5)
        )
        safe_addstr(stdscr, ctrl_top + 4, 2, preset_line, curses.color_pair(1))
        safe_addstr(stdscr, ctrl_top + 5, 2, preset_line2, curses.color_pair(1))


# ---------------------------------------------------------------------------
# Tab 3: Live Traffic Signal Head (SPaT View)
# ---------------------------------------------------------------------------
def draw_signal_view_tab(stdscr, top_y, height, width, spat, sg_idx, freq_mhz):
    safe_addstr(stdscr, top_y, 0, "─" * (width - 1), curses.color_pair(5))
    safe_addstr(stdscr, top_y, 2, " Bonus View: Live SPaT Traffic Signal Head (←/→ switch Signal Group) ", curses.color_pair(1) | curses.A_BOLD)

    phases = spat["phases"] if spat else []
    active = phases[sg_idx % len(phases)] if phases else None
    inter_name = spat["name"] if spat else f"Waiting for SPaT Signal @ {freq_mhz:.1f} MHz..."
    color = active["color"] if active else "DARK"
    cd_str = f"{active['countdown']:.1f}s" if (active and active["countdown"] is not None) else "--.-s"

    # Intersection Header
    safe_addstr(stdscr, top_y + 2, 4, inter_name, curses.color_pair(7) | curses.A_BOLD)
    cd_attr = {
        "RED": curses.color_pair(4) | curses.A_BOLD,
        "FLASHING_RED": curses.color_pair(4) | curses.A_BOLD,
        "YELLOW": curses.color_pair(3) | curses.A_BOLD,
        "GREEN": curses.color_pair(2) | curses.A_BOLD,
        "PROTECTED_GREEN": curses.color_pair(2) | curses.A_BOLD,
    }.get(color, curses.A_DIM)
    safe_addstr(stdscr, top_y + 2, max(4, width - 16), f"TIME: {cd_str}", cd_attr)
    if active:
        safe_addstr(stdscr, top_y + 3, 4, f"Active: {active['label']} — {active['raw']}", curses.color_pair(1))
    else:
        safe_addstr(stdscr, top_y + 3, 4, "Press [d] for Demo Mode to preview live SPaT countdown.", curses.A_DIM)

    # Traffic Signal Housing (centered)
    cx = max(12, width // 2 - 8)
    sy = top_y + 5

    red_lit = color in ("RED", "FLASHING_RED")
    yel_lit = color == "YELLOW"
    grn_lit = color in ("GREEN", "PROTECTED_GREEN")
    is_arrow = color == "PROTECTED_GREEN"

    if sy + 12 < top_y + height:
        safe_addstr(stdscr, sy,     cx, "╔══════════════╗", curses.color_pair(7))
        # Red lens
        r_attr = curses.color_pair(10) | curses.A_BOLD if red_lit else curses.A_DIM
        r_txt = "   ██████   " if red_lit else "   ░░░░░░   "
        safe_addstr(stdscr, sy + 1, cx, "║ ", curses.color_pair(7))
        safe_addstr(stdscr, sy + 1, cx + 2, r_txt, r_attr)
        safe_addstr(stdscr, sy + 1, cx + 14, " ║", curses.color_pair(7))
        safe_addstr(stdscr, sy + 2, cx, "║ ", curses.color_pair(7))
        safe_addstr(stdscr, sy + 2, cx + 2, "    STOP    " if red_lit else "   ░░░░░░   ", r_attr)
        safe_addstr(stdscr, sy + 2, cx + 14, " ║", curses.color_pair(7))
        safe_addstr(stdscr, sy + 3, cx, "╟──────────────╢", curses.color_pair(7) | curses.A_DIM)

        # Yellow lens
        y_attr = curses.color_pair(11) | curses.A_BOLD if yel_lit else curses.A_DIM
        y_txt = "   ██████   " if yel_lit else "   ░░░░░░   "
        safe_addstr(stdscr, sy + 4, cx, "║ ", curses.color_pair(7))
        safe_addstr(stdscr, sy + 4, cx + 2, y_txt, y_attr)
        safe_addstr(stdscr, sy + 4, cx + 14, " ║", curses.color_pair(7))
        safe_addstr(stdscr, sy + 5, cx, "║ ", curses.color_pair(7))
        safe_addstr(stdscr, sy + 5, cx + 2, "   YELLOW   " if yel_lit else "   ░░░░░░   ", y_attr)
        safe_addstr(stdscr, sy + 5, cx + 14, " ║", curses.color_pair(7))
        safe_addstr(stdscr, sy + 6, cx, "╟──────────────╢", curses.color_pair(7) | curses.A_DIM)

        # Green lens
        g_attr = curses.color_pair(12) | curses.A_BOLD if grn_lit else curses.A_DIM
        g_top = "  ◄◄ LEFT   " if is_arrow else ("   ██████   " if grn_lit else "   ░░░░░░   ")
        g_bot = "   ARROW    " if is_arrow else ("     GO     " if grn_lit else "   ░░░░░░   ")
        safe_addstr(stdscr, sy + 7, cx, "║ ", curses.color_pair(7))
        safe_addstr(stdscr, sy + 7, cx + 2, g_top, g_attr)
        safe_addstr(stdscr, sy + 7, cx + 14, " ║", curses.color_pair(7))
        safe_addstr(stdscr, sy + 8, cx, "║ ", curses.color_pair(7))
        safe_addstr(stdscr, sy + 8, cx + 2, g_bot, g_attr)
        safe_addstr(stdscr, sy + 8, cx + 14, " ║", curses.color_pair(7))
        safe_addstr(stdscr, sy + 9, cx, "╚══════════════╝", curses.color_pair(7))

    # Signal Group Chips at bottom of Signal View
    if phases:
        chip_y = min(top_y + height - 2, sy + 11)
        x = 4
        for idx, ph in enumerate(phases):
            selected = (idx == (sg_idx % len(phases)))
            chip = f" {ph['label']} ({ph['color']}) "
            attr = curses.color_pair(9) | curses.A_BOLD if selected else curses.color_pair(7)
            safe_addstr(stdscr, chip_y, x, chip, attr)
            x += len(chip) + 2


def tui_main(stdscr, state: AppState, dsp):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(80)
    init_colors()

    worker = threading.Thread(target=capture_worker, args=(state, dsp), daemon=True)
    worker.start()

    try:
        while True:
            ch = stdscr.getch()
            if ch != -1:
                if ch in (ord("q"), ord("Q")):
                    with state.lock:
                        state.running = False
                    break
                elif ch in (9, ord("t"), ord("T")):  # Tab or 't' cycles tabs
                    with state.lock:
                        state.active_tab = (state.active_tab + 1) % len(TABS)
                elif ch == curses.KEY_BTAB:
                    with state.lock:
                        state.active_tab = (state.active_tab - 1) % len(TABS)
                elif ch == curses.KEY_F1:
                    with state.lock:
                        state.active_tab = 0
                elif ch == curses.KEY_F2:
                    with state.lock:
                        state.active_tab = 1
                elif ch == curses.KEY_F3:
                    with state.lock:
                        state.active_tab = 2
                elif ch == ord(" "):
                    with state.lock:
                        state.capturing = not state.capturing
                elif ch in (ord("c"), ord("C")):
                    state.clear_all()
                elif ch in (ord("a"), ord("A")):
                    with state.lock:
                        state.amp = not state.amp
                elif ch in (ord("r"), ord("R")):
                    with state.lock:
                        state.lock_rate_hz = 20_000_000 if state.lock_rate_hz == 10_000_000 else 10_000_000
                        if state.locked_freq is not None:
                            state.locked_rate = state.lock_rate_hz
                elif ch in (ord("u"), ord("U")):
                    with state.lock:
                        state.locked_freq = None
                        state.lock_until = 0.0
                elif ch in (ord("d"), ord("D")):
                    with state.lock:
                        state.demo_mode = not state.demo_mode
                elif ch == ord("l"):
                    with state.lock:
                        state.lna = max(0, state.lna - 8)
                elif ch == ord("L"):
                    with state.lock:
                        state.lna = min(40, state.lna + 8)
                elif ch == ord("v"):
                    with state.lock:
                        state.vga = max(0, state.vga - 2)
                elif ch == ord("V"):
                    with state.lock:
                        state.vga = min(62, state.vga + 2)
                elif ch in (ord("p"), ord("P")):
                    with state.lock:
                        state.custom_freq = None
                        state.custom_rate = None
                        state.preset_idx = (state.preset_idx + 1) % len(PRESET_KEYS)
                elif ord("1") <= ch <= ord("9"):
                    idx = ch - ord("1")
                    if idx < len(PRESET_KEYS):
                        with state.lock:
                            state.custom_freq = None
                            state.custom_rate = None
                            state.preset_idx = idx
                elif ch in (curses.KEY_DOWN, ord("j")):
                    with state.lock:
                        if state.packets:
                            state.selected_idx = min(len(state.packets) - 1, state.selected_idx + 1)
                elif ch in (curses.KEY_UP, ord("k")):
                    with state.lock:
                        state.selected_idx = max(0, state.selected_idx - 1)
                elif ch == curses.KEY_RIGHT:
                    with state.lock:
                        if state.active_tab == 2:
                            state.selected_sg_idx += 1
                        else:
                            state.active_tab = (state.active_tab + 1) % len(TABS)
                elif ch == curses.KEY_LEFT:
                    with state.lock:
                        if state.active_tab == 2:
                            state.selected_sg_idx = max(0, state.selected_sg_idx - 1)
                        else:
                            state.active_tab = (state.active_tab - 1) % len(TABS)
                elif ch == curses.KEY_HOME:
                    with state.lock:
                        state.selected_idx = 0

            # Snapshot state
            with state.lock:
                tab = state.active_tab
                capturing = state.capturing
                demo = state.demo_mode
                preset_idx = state.preset_idx
                key, _, _, _, _ = state.active_preset_tuple()
                freq_mhz = state.current_freq_mhz
                rate_hz = state.current_rate_hz
                lna, vga, amp = state.lna, state.vga, state.amp
                lock_rate_hz = state.lock_rate_hz
                locked_freq = state.locked_freq
                sweeps, hits, pkts_total = state.sweeps, state.hits, state.pkts_total
                status_msg, status_err = state.status_msg, state.status_err
                spec = state.latest_spec
                spat = state.latest_spat
                sg_idx = state.selected_sg_idx
                packets = list(state.packets)
                sel = min(state.selected_idx, max(0, len(packets) - 1))
                state.selected_idx = sel

            stdscr.erase()
            max_y, max_x = stdscr.getmaxyx()
            if max_y < 14 or max_x < 56:
                safe_addstr(stdscr, 0, 0, "Terminal too small (need >= 56x14). Resize or press 'q'.", curses.color_pair(4))
                stdscr.refresh()
                continue

            # Row 0: Top Banner
            banner = f" RoadSense V2X [macOS] | {key} | LockRate:{lock_rate_hz//1_000_000}M | LNA:{lna} VGA:{vga} AMP:{'ON' if amp else 'OFF'} | Bias-T:OFF"
            safe_addstr(stdscr, 0, 0, banner.ljust(max_x - 1), curses.color_pair(8) | curses.A_BOLD)

            # Row 1: Status & Counters
            run_tag = "[DEMO]" if demo else ("[LOCK]" if locked_freq else ("[LIVE]" if capturing else "[PAUSED]"))
            run_attr = curses.color_pair(3) | curses.A_BOLD if (demo or not capturing) else (
                curses.color_pair(4) | curses.A_BOLD if status_err else curses.color_pair(2) | curses.A_BOLD
            )
            safe_addstr(stdscr, 1, 1, run_tag, run_attr)
            safe_addstr(stdscr, 1, len(run_tag) + 2, status_msg, curses.color_pair(4) if status_err else curses.color_pair(7))
            counters = f"Sweeps:{sweeps}  Hits:{hits}  Pkts:{pkts_total}"
            safe_addstr(stdscr, 1, max(len(run_tag) + 4, max_x - len(counters) - 2), counters, curses.color_pair(1) | curses.A_BOLD)

            # Row 2: Tab Bar (matching Android app: Protocol | RF Scope | Signal View) + RSU Proximity Badge
            tx = 1
            for i, tname in enumerate(TABS):
                lbl = f" [F{i+1}] {tname} ({len(packets)}) " if i == 0 else f" [F{i+1}] {tname} "
                attr = curses.color_pair(9) | curses.A_BOLD if i == tab else curses.color_pair(7) | curses.A_DIM
                safe_addstr(stdscr, 2, tx, lbl, attr)
                tx += len(lbl) + 2
            if state.rsu_in_range:
                d0, r0 = state.rsu_in_range[0]
                prox = f" ★ IN RANGE: {r0['name'][:22]} ({d0:.0f}m · {r0['freq_mhz']:.0f}M) "
                safe_addstr(stdscr, 2, max(tx + 1, max_x - len(prox) - 1), prox, curses.color_pair(12) | curses.A_BOLD)
            elif state.rsu_nearest:
                d0, r0 = state.rsu_nearest
                prox = f" Nearest RSU: {r0['callsign'] or r0['name'][:14]} ({d0/1000.0:.1f}km) "
                safe_addstr(stdscr, 2, max(tx + 1, max_x - len(prox) - 1), prox, curses.color_pair(3))

            # Main Content Area (rows 3 .. max_y - 2)
            body_top = 3
            body_h = max(6, max_y - 4)

            if tab == 0:
                insp_h = 6 if body_h >= 14 else 4
                list_rows = max(1, body_h - insp_h - 1)
                if sel < state.scroll_offset:
                    state.scroll_offset = sel
                elif sel >= state.scroll_offset + list_rows:
                    state.scroll_offset = sel - list_rows + 1
                draw_protocol_tab(stdscr, body_top, body_h, max_x, packets, sel, state.scroll_offset, freq_mhz, capturing)
            elif tab == 1:
                draw_rf_scope_tab(stdscr, body_top, body_h, max_x, spec, freq_mhz, rate_hz, preset_idx, lna, vga, amp)
            else:
                draw_signal_view_tab(stdscr, body_top, body_h, max_x, spat, sg_idx, freq_mhz)

            # Bottom Hotkey Footer
            footer = " [Tab/F1-F3]Tabs [Space]Run [p/1-9]Preset [r]Lock10/20M [u]Unlock [l/L]LNA [v/V]VGA [a]Amp [d]Demo [c]Clear [q]Quit"
            safe_addstr(stdscr, max_y - 1, 0, footer.ljust(max_x - 1), curses.color_pair(8))

            stdscr.refresh()
    finally:
        with state.lock:
            state.running = False
        worker.join(timeout=2.0)


def main():
    ap = argparse.ArgumentParser(description="RoadSense V2X — macOS 3-Tab ncurses TUI for HackRF One")
    ap.add_argument("--preset", choices=PRESET_KEYS, default="scan-all", help="Initial channel preset (default: scan-all)")
    ap.add_argument("--freq", type=float, default=None, help="Custom center frequency in MHz")
    ap.add_argument("--rate", type=int, default=None, help="Custom sample rate in Hz")
    ap.add_argument("--lock-rate", type=int, choices=[10_000_000, 20_000_000], default=10_000_000,
                    help="Continuous streaming rate when auto-locked on a DSRC channel (10000000 or 20000000, default: 10000000)")
    ap.add_argument("--lock-sec", type=float, default=8.0, help="Seconds to stay locked after last hit (default: 8.0)")
    ap.add_argument("--lna", type=int, default=24, help="Initial LNA gain 0-40 dB (default: 24)")
    ap.add_argument("--vga", type=int, default=24, help="Initial VGA gain 0-62 dB (default: 24)")
    ap.add_argument("--amp", action="store_true", help="Enable +14 dB RF amp on startup")
    ap.add_argument("--duration", type=float, default=0.12, help="Capture window in seconds per chunk (default: 0.12)")
    ap.add_argument("--pcap", type=str, default=None, help="Save decoded 802.11 frames to Wireshark .pcap")
    ap.add_argument("--rsu-db", type=str, default=cli.DEFAULT_RF_DB if os.path.exists(cli.DEFAULT_RF_DB) else None,
                    help="Path to roadsense_rf.db or rsus.csv for RSU proximity alerts (auto-loads data/roadsense_rf.db)")
    ap.add_argument("--lat", type=float, default=None, help="Current latitude (decimal degrees) for RSU range check")
    ap.add_argument("--lon", type=float, default=None, help="Current longitude (decimal degrees) for RSU range check")
    ap.add_argument("--demo", action="store_true", help="Start in Demo Mode (no HackRF required)")
    ap.add_argument("--paused", action="store_true", help="Start paused")
    args = ap.parse_args()

    if not args.demo and not shutil.which("hackrf_transfer"):
        print("Error: 'hackrf_transfer' not found. Install with 'brew install hackrf' or run with --demo.", file=sys.stderr)
        sys.exit(1)

    dsp = cli.load_dsp()
    state = AppState(args)
    try:
        curses.wrapper(tui_main, state, dsp)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
