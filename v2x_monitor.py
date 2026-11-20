#!/usr/bin/env python3
"""
Continuous DSRC & C-V2X Traffic Monitor + I/Q Recorder for HackRF One
Monitors US 5.9 GHz ITS channels (5850-5925 MHz), or locks onto a single
frequency (--lock 5900 / 5910 / 5915), alerts on detection, tracks inter-arrival
intervals, saves raw int8 I/Q files for offline decoding (e.g. gr-ieee802-11 +
Wireshark/tshark), and generates timestamped spectrum + waterfall spectrograms.
"""

import argparse
import csv
import datetime
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


ITS_CHANNELS = [
    (5860.0, 10.0, "Ch 172 (Legacy DSRC / UNII-4)"),
    (5870.0, 10.0, "Ch 174 (Legacy DSRC / UNII-4)"),
    (5880.0, 10.0, "Ch 176 (Legacy DSRC / UNII-4)"),
    (5890.0, 10.0, "Ch 178 (Legacy DSRC Control / UNII-4)"),
    (5900.0, 10.0, "Ch 180 (C-V2X / DSRC Safety)"),
    (5910.0, 10.0, "Ch 182 (C-V2X / DSRC Safety)"),
    (5915.0, 20.0, "Ch 183 (C-V2X Primary 20MHz Safety)"),
    (5920.0, 10.0, "Ch 184 (C-V2X / DSRC Safety)"),
]


def nearest_dsrc_channel(freq_mhz):
    """Return nearest standard 10 MHz US DSRC channel center (5860..5920 MHz)."""
    channels = [5860.0, 5870.0, 5880.0, 5890.0, 5900.0, 5910.0, 5920.0]
    return min(channels, key=lambda ch: abs(ch - freq_mhz))


def decode_iq_offline(iq_path, target_mhz):
    """Run decode_dsrc.py on a saved .iq file and return its summary output."""
    decoder = "/Users/beisenmann/Projects/TrafficLight/decode_dsrc.py"
    if not os.path.exists(decoder):
        return "Decoder script not found."
    res = subprocess.run(
        [decoder, iq_path, "--target-mhz", str(target_mhz), "--bw", "10"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out_lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    if res.returncode != 0:
        return f"Decode error: {res.stderr.strip()}"
    # Return the decoded packets section or last 2 lines
    for i, ln in enumerate(out_lines):
        if "DECODED PACKETS" in ln:
            return "\n".join(out_lines[i : i + 25])
    return out_lines[-1] if out_lines else "0 frames decoded."


def identify_channel(freq_mhz):
    best_name = "5.9 GHz ITS Band"
    best_dist = 999.0
    for center, bw, name in ITS_CHANNELS:
        half = bw / 2.0
        if center - half <= freq_mhz <= center + half:
            dist = abs(freq_mhz - center)
            if dist < best_dist:
                best_dist = dist
                best_name = name
    return best_name


def capture_window(center_hz, sample_rate, num_samples, lna_gain, vga_gain, amp_enable, ant_power, tmp_path, retries=5):
    cmd = [
        "hackrf_transfer",
        "-r", tmp_path,
        "-f", str(int(center_hz)),
        "-s", str(int(sample_rate)),
        "-n", str(int(num_samples)),
        "-l", str(int(lna_gain)),
        "-g", str(int(vga_gain)),
        "-a", str(int(amp_enable)),
        "-p", str(int(ant_power)),
    ]
    last_err = ""
    for attempt in range(1, retries + 1):
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            raw = np.fromfile(tmp_path, dtype=np.int8)
            if raw.size % 2 != 0:
                raw = raw[:-1]
            iq = (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)) / 128.0
            time.sleep(0.05)  # brief USB settle pause for macOS libusb
            return raw, iq
        last_err = res.stderr.strip()
        time.sleep(0.3 * attempt)

    raise RuntimeError(f"hackrf_transfer failed at {center_hz/1e6:.1f} MHz after {retries} tries: {last_err}")


def analyze_capture(iq, sample_rate, nfft=1024):
    # Trim first 5 ms to discard HackRF PLL/DC startup transient
    trim = int(sample_rate * 0.005)
    if len(iq) > trim + nfft * 4:
        iq = iq[trim:]
    iq = iq - np.mean(iq)
    num_frames = len(iq) // nfft
    if num_frames < 2:
        raise ValueError("Not enough samples")

    frames = iq[: num_frames * nfft].reshape(num_frames, nfft)
    window = np.hanning(nfft).astype(np.float32)
    win_power = float(np.sum(window ** 2))

    # Time-domain frame power (catches wideband 10/20 MHz OFDM bursts)
    frame_power = np.mean(np.abs(frames) ** 2, axis=1)
    frame_db = 10.0 * np.log10(frame_power + 1e-12)
    frame_median = float(np.median(frame_db))
    peak_frame_idx = int(np.argmax(frame_db))
    max_burst_jump = float(frame_db[peak_frame_idx] - frame_median)

    # Frequency-domain FFT
    spec = np.fft.fftshift(np.fft.fft(frames * window, axis=1), axes=1)
    power = (np.abs(spec) ** 2) / (win_power * nfft)

    avg_power = np.mean(power, axis=0)
    peak_power = np.max(power, axis=0)

    # Suppress HackRF DC spike (central 3 bins)
    dc = nfft // 2
    for arr in (avg_power, peak_power):
        arr[dc - 1 : dc + 2] = np.linspace(arr[dc - 2], arr[dc + 2], 3)

    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / sample_rate))
    avg_db = 10.0 * np.log10(avg_power + 1e-12)
    peak_db = 10.0 * np.log10(peak_power + 1e-12)
    return freqs, avg_db, peak_db, max_burst_jump, power, peak_frame_idx


def send_macos_notification(title, message, sound="Glass"):
    if sys.platform != "darwin":
        return
    t = title.replace('"', '\\"')
    m = message.replace('"', '\\"')
    script = f'display notification "{m}" with title "{t}" sound name "{sound}"'
    subprocess.run(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def save_detection_waterfall(center_mhz, sample_rate, freqs_mhz, avg_db, peak_db, power_matrix, peak_frame_idx, det_info, plot_path, timestamp_str):
    """Save a 2-panel plot: top = Spectrum (Avg + Peak), bottom = Zoomed Waterfall around the burst."""
    f_val, p_val, snr, bjump, ch = det_info
    n_frames, nfft = power_matrix.shape
    frame_ms = (nfft / sample_rate) * 1000.0  # ~0.0512 ms per frame at 20 MS/s

    # Zoom waterfall to +/- 150 frames (~15 ms window) around peak_frame_idx
    half_win = 150
    f0 = max(0, peak_frame_idx - half_win)
    f1 = min(n_frames, peak_frame_idx + half_win)
    wf_db = 10.0 * np.log10(power_matrix[f0:f1, :] + 1e-12)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7.5), dpi=120, gridspec_kw={"height_ratios": [1, 1.3]})

    # Top panel: Spectrum
    ax1.plot(freqs_mhz, peak_db, color="#d9534f", linewidth=0.9, alpha=0.9, label="Peak Hold (dBFS)")
    ax1.plot(freqs_mhz, avg_db, color="#1f77b4", linewidth=1.1, label="Average (dBFS)")
    ax1.annotate(
        f"{f_val:.2f} MHz (+{snr:.1f} dB)",
        xy=(f_val, p_val),
        xytext=(0, 10),
        textcoords="offset points",
        ha="center",
        fontsize=8.5,
        arrowprops=dict(arrowstyle="->", color="#222222", lw=0.8),
    )
    ymin, ymax = ax1.get_ylim()
    ax1.set_ylim(ymin, ymax + 4.0)
    ax1.set_xlim(freqs_mhz[0], freqs_mhz[-1])
    ax1.set_ylabel("Power (dBFS)")
    ax1.set_title(f"Detection @ {f_val:.2f} MHz ({ch}) — {timestamp_str} | Burst: +{bjump:.1f} dB")
    ax1.grid(True, linestyle=":", alpha=0.6)
    ax1.legend(loc="lower right", fontsize=8)

    # Bottom panel: Waterfall spectrogram around the burst
    t0_ms = f0 * frame_ms
    t1_ms = f1 * frame_ms
    vmin = float(np.percentile(wf_db, 10))
    vmax = float(np.max(wf_db))
    im = ax2.imshow(
        wf_db,
        aspect="auto",
        origin="lower",
        extent=[freqs_mhz[0], freqs_mhz[-1], t0_ms, t1_ms],
        cmap="viridis",
        vmin=vmin,
        vmax=vmax,
    )
    ax2.set_xlabel("Frequency (MHz)")
    ax2.set_ylabel("Time within capture (ms)")
    ax2.set_title("Zoomed Waterfall (~15 ms around burst — 10 MHz wide = DSRC 802.11p, 20 MHz = C-V2X)")
    fig.colorbar(im, ax=ax2, label="Power (dBFS)", pad=0.01)

    plt.tight_layout()
    plt.savefig(plot_path)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description="Continuous DSRC & C-V2X Monitor + Raw I/Q Recorder")
    parser.add_argument(
        "--mode",
        choices=["all", "cv2x"],
        default="all",
        help="'all' = 5850-5925 MHz (5 windows), 'cv2x' = 5895-5925 MHz (2 windows)",
    )
    parser.add_argument(
        "--lock",
        type=float,
        default=None,
        help="Lock continuously onto a single center frequency in MHz (e.g. --lock 5900 or --lock 5910) with zero hopping",
    )
    parser.add_argument("--dwell", type=float, default=0.25, help="Dwell time per capture in seconds (default: 0.25)")
    parser.add_argument("--threshold", type=float, default=8.0, help="Spectral SNR threshold in dB above peak floor (default: 8.0)")
    parser.add_argument("--burst-threshold", type=float, default=4.5, help="Wideband time-domain burst threshold in dB (default: 4.5)")
    parser.add_argument("--lna", type=int, default=24, help="RX LNA gain 0-40 dB (default: 24)")
    parser.add_argument("--vga", type=int, default=24, help="RX VGA gain 0-62 dB (default: 24)")
    parser.add_argument("--amp", type=int, default=1, help="RF amp 1=on, 0=off (default: 1)")
    parser.add_argument("--cooldown", type=float, default=10.0, help="Seconds between macOS notifications (default: 10)")
    parser.add_argument("--no-notify", action="store_true", help="Disable macOS desktop notifications/sound")
    parser.add_argument("--no-save-iq", action="store_true", help="Do not save raw .iq files on detection")
    parser.add_argument("--no-decode", action="store_true", help="Disable automatic offline DSRC decoding on detection")
    parser.add_argument("--cycles", type=int, default=0, help="Sweep cycles to run (0 = continuous until Ctrl-C)")
    parser.add_argument("--out-dir", type=str, default=os.path.expanduser("~/.gemini/antigravity/scratch/v2x_monitor"), help="Output directory")
    args = parser.parse_args()

    sample_rate = 20_000_000  # 20 MS/s
    usable_bw_mhz = 15.0
    num_samples = int(sample_rate * args.dwell)

    if args.lock is not None:
        centers_mhz = np.array([args.lock], dtype=float)
        band_desc = f"LOCKED @ {args.lock:.1f} MHz ({args.lock-10:.1f}–{args.lock+10:.1f} MHz @ 20 MS/s)"
        usable_bw_mhz = 18.0
    elif args.mode == "cv2x":
        centers_mhz = np.arange(5895.0 + usable_bw_mhz / 2.0, 5925.0, usable_bw_mhz)
        band_desc = f"5895–5925 MHz ({len(centers_mhz)} windows x {usable_bw_mhz:.0f} MHz)"
    else:
        centers_mhz = np.arange(5850.0 + usable_bw_mhz / 2.0, 5925.0, usable_bw_mhz)
        band_desc = f"5850–5925 MHz ({len(centers_mhz)} windows x {usable_bw_mhz:.0f} MHz)"

    captures_dir = os.path.join(args.out_dir, "captures")
    os.makedirs(captures_dir, exist_ok=True)
    log_path = os.path.join(args.out_dir, "detections.csv")

    if not os.path.exists(log_path):
        with open(log_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "freq_mhz", "peak_dbfs", "snr_db", "burst_jump_db", "channel", "iq_file"])

    stop_requested = False

    def handle_sigint(sig, frame):
        nonlocal stop_requested
        stop_requested = True
        print("\nStopping monitor...", flush=True)

    signal.signal(signal.SIGINT, handle_sigint)

    print("============================================================")
    print("  DSRC / C-V2X Continuous Monitor + I/Q Recorder")
    print("============================================================")
    print(f"  Band       : {band_desc}")
    print(f"  Dwell      : {args.dwell:.2f} s/capture")
    print(f"  Thresholds : +{args.threshold:.1f} dB spectral SNR OR +{args.burst_threshold:.1f} dB wideband burst")
    print(f"  Gain       : LNA={args.lna} dB, VGA={args.vga} dB, Amp={args.amp}, BiasT=0")
    print(f"  Save I/Q   : {'NO' if args.no_save_iq else 'YES -> ' + captures_dir}")
    print(f"  Auto-Decode: {'NO' if (args.no_decode or args.no_save_iq) else 'YES (10 MHz DSRC -> .pcap + tshark)'}")
    print(f"  Log file   : {log_path}")
    print("  Press Ctrl-C to stop.")
    print("============================================================\n", flush=True)

    last_notify_time = 0.0
    last_hit_time = None
    hit_intervals = []
    total_detections = 0
    cycle = 0

    with tempfile.NamedTemporaryFile(suffix=".iq", delete=False) as tf:
        tmp_path = tf.name

    try:
        while not stop_requested:
            cycle += 1
            now_dt = datetime.datetime.now()
            now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            file_ts = now_dt.strftime("%Y%m%d_%H%M%S")

            cycle_floors = []
            cycle_max_p = -999.0
            cycle_max_f = 0.0
            hit_in_cycle = False

            for c_mhz in centers_mhz:
                if stop_requested:
                    break
                try:
                    raw, iq = capture_window(
                        center_hz=c_mhz * 1e6,
                        sample_rate=sample_rate,
                        num_samples=num_samples,
                        lna_gain=args.lna,
                        vga_gain=args.vga,
                        amp_enable=args.amp,
                        ant_power=0,
                        tmp_path=tmp_path,
                    )
                except RuntimeError as e:
                    print(f"[{now_str}] ⚠️  USB glitch ({e.args[0].splitlines()[-1]}), retrying...", flush=True)
                    time.sleep(2.0)
                    continue

                freqs_hz, avg_db, peak_db, burst_jump, power_mat, peak_frame_idx = analyze_capture(iq, sample_rate, nfft=1024)
                freqs_mhz = c_mhz + freqs_hz / 1e6

                half = usable_bw_mhz / 2.0
                mask = (freqs_mhz >= c_mhz - half) & (freqs_mhz < c_mhz + half)
                f_slice = freqs_mhz[mask]
                a_slice = avg_db[mask]
                p_slice = peak_db[mask]

                cycle_floors.append(float(np.median(a_slice)))
                max_idx = int(np.argmax(p_slice))
                max_p = float(p_slice[max_idx])
                max_f = float(f_slice[max_idx])
                if max_p > cycle_max_p:
                    cycle_max_p = max_p
                    cycle_max_f = max_f

                peak_floor = float(np.median(p_slice))
                snr = max_p - peak_floor

                if snr >= args.threshold or burst_jump >= args.burst_threshold:
                    hit_in_cycle = True
                    total_detections += 1
                    ch_name = identify_channel(max_f)
                    now_ts = time.time()

                    dt_str = ""
                    if last_hit_time is not None:
                        delta_s = now_ts - last_hit_time
                        hit_intervals.append(delta_s)
                        avg_dt = float(np.mean(hit_intervals[-5:]))
                        dt_str = f" | Δt={delta_s:.1f}s (avg {avg_dt:.1f}s)"
                    last_hit_time = now_ts

                    iq_saved_path = ""
                    base_name = f"det_{file_ts}_{int(round(c_mhz))}MHz_sr20M"
                    if not args.no_save_iq:
                        iq_saved_path = os.path.join(captures_dir, base_name + ".iq")
                        raw.tofile(iq_saved_path)
                        meta_path = os.path.join(captures_dir, base_name + ".json")
                        with open(meta_path, "w") as mf:
                            json.dump({
                                "timestamp": now_str,
                                "center_freq_hz": int(c_mhz * 1e6),
                                "sample_rate_hz": int(sample_rate),
                                "dtype": "int8_interleaved_iq",
                                "lna_gain_db": args.lna,
                                "vga_gain_db": args.vga,
                                "amp_enable": args.amp,
                                "detected_freq_mhz": round(max_f, 4),
                                "peak_dbfs": round(max_p, 2),
                                "snr_db": round(snr, 2),
                                "burst_jump_db": round(burst_jump, 2),
                                "channel": ch_name,
                            }, mf, indent=2)

                    png_path = os.path.join(captures_dir, base_name + ".png")
                    save_detection_waterfall(
                        center_mhz=c_mhz,
                        sample_rate=sample_rate,
                        freqs_mhz=freqs_mhz,
                        avg_db=avg_db,
                        peak_db=peak_db,
                        power_matrix=power_mat,
                        peak_frame_idx=peak_frame_idx,
                        det_info=(max_f, max_p, snr, burst_jump, ch_name),
                        plot_path=png_path,
                        timestamp_str=now_str,
                    )

                    print(f"\n********************** DSRC / V2X SIGNAL DETECTED [{now_str}]{dt_str} **********************")
                    print(f"  >>> {max_f:7.2f} MHz | Peak: {max_p:6.1f} dBFS (+{snr:4.1f} dB) | Burst: +{burst_jump:.1f} dB | {ch_name}")
                    if iq_saved_path:
                        print(f"  >>> Saved I/Q  : {iq_saved_path}")
                    print(f"  >>> Waterfall  : {png_path}")
                    if iq_saved_path and not args.no_decode:
                        target_ch = nearest_dsrc_channel(max_f)
                        print(f"  >>> Decoding   : 10 MHz DSRC @ {target_ch:.0f} MHz...", flush=True)
                        dec_out = decode_iq_offline(iq_saved_path, target_ch)
                        for dline in dec_out.splitlines():
                            print(f"      {dline}")
                    print("************************************************************************************\a\n", flush=True)

                    with open(log_path, "a", newline="") as f:
                        writer = csv.writer(f)
                        writer.writerow([now_str, f"{max_f:.3f}", f"{max_p:.2f}", f"{snr:.2f}", f"{burst_jump:.2f}", ch_name, iq_saved_path])

                    if not args.no_notify and (now_ts - last_notify_time >= args.cooldown):
                        last_notify_time = now_ts
                        send_macos_notification(
                            "DSRC / V2X Signal Detected",
                            f"{max_f:.2f} MHz ({ch_name}) | +{snr:.1f} dB",
                        )

            if stop_requested:
                break

            if not hit_in_cycle and cycle_floors:
                print(
                    f"[{now_str}] Sweep #{cycle:<4d} | Quiet | Floor: {np.mean(cycle_floors):5.1f} dBFS | Max: {cycle_max_p:5.1f} dBFS @ {cycle_max_f:.1f} MHz",
                    flush=True,
                )

            if args.cycles > 0 and cycle >= args.cycles:
                break
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    print(f"\nMonitor finished after {cycle} sweeps. Total detections logged: {total_detections}")
    if total_detections > 0:
        print(f"  Detections CSV : {log_path}")
        print(f"  Captures Folder: {captures_dir}")


if __name__ == "__main__":
    main()
