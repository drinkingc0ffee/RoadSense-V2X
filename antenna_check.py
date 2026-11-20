#!/usr/bin/env python3
"""
HackRF One Antenna Reception Check (5700 - 5950 MHz)
Sweeps 5700-5950 MHz using hackrf_transfer + Welch FFT, prints an easy-to-read
text report, and saves a power-vs-frequency graph (PNG).
"""

import argparse
import os
import subprocess
import sys
import tempfile
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def capture_window(center_hz, sample_rate, num_samples, lna_gain, vga_gain, amp_enable, ant_power, tmp_path):
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
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        raise RuntimeError(f"hackrf_transfer failed at {center_hz/1e6:.2f} MHz:\n{res.stderr.strip()}")

    raw = np.fromfile(tmp_path, dtype=np.int8)
    if raw.size % 2 != 0:
        raw = raw[:-1]
    iq = (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)) / 128.0
    return iq


def compute_psd_welch(iq, sample_rate, nfft=1024):
    """Compute average and peak-hold PSD (in dBFS) using overlapping Hann-windowed FFTs."""
    # Remove DC offset
    iq = iq - np.mean(iq)

    step = nfft // 2
    num_frames = (len(iq) - nfft) // step + 1
    if num_frames < 1:
        raise ValueError("Not enough samples for FFT")

    window = np.hanning(nfft).astype(np.float32)
    win_power = np.sum(window ** 2)

    # Build strided frames
    frames = np.lib.stride_tricks.as_strided(
        iq,
        shape=(num_frames, nfft),
        strides=(iq.strides[0] * step, iq.strides[0]),
    )
    windowed = frames * window
    spec = np.fft.fftshift(np.fft.fft(windowed, axis=1), axes=1)
    power = (np.abs(spec) ** 2) / (win_power * nfft)

    avg_power = np.mean(power, axis=0)
    peak_power = np.max(power, axis=0)

    # Interpolate over the central 3 DC bins to suppress HackRF residual DC spike
    dc_idx = nfft // 2
    for arr in (avg_power, peak_power):
        left = arr[dc_idx - 2]
        right = arr[dc_idx + 2]
        arr[dc_idx - 1 : dc_idx + 2] = np.linspace(left, right, 3)

    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / sample_rate))
    avg_db = 10.0 * np.log10(avg_power + 1e-12)
    peak_db = 10.0 * np.log10(peak_power + 1e-12)
    return freqs, avg_db, peak_db


def ascii_bar(val, vmin, vmax, width=28):
    if vmax <= vmin:
        return ""
    frac = max(0.0, min(1.0, (val - vmin) / (vmax - vmin)))
    filled = int(round(frac * width))
    return "#" * filled + "-" * (width - filled)


def main():
    parser = argparse.ArgumentParser(description="HackRF One 5700-5950 MHz Antenna Reception Check")
    parser.add_argument("--start", type=float, default=5700.0, help="Start frequency in MHz (default: 5700)")
    parser.add_argument("--end", type=float, default=5950.0, help="End frequency in MHz (default: 5950)")
    parser.add_argument("--sample-rate", type=int, default=10_000_000, help="Sample rate in Hz (default: 10000000)")
    parser.add_argument("--samples", type=int, default=500_000, help="Samples per step (default: 500000)")
    parser.add_argument("--lna", type=int, default=24, help="RX LNA gain 0-40 dB, 8 dB steps (default: 24)")
    parser.add_argument("--vga", type=int, default=20, help="RX VGA gain 0-62 dB, 2 dB steps (default: 20)")
    parser.add_argument("--amp", type=int, default=1, help="RF amp 1=on, 0=off (default: 1)")
    parser.add_argument("--ant-power", type=int, default=0, help="Antenna bias-tee power 0=off (passive), 1=on (default: 0)")
    parser.add_argument("--out-dir", type=str, default=os.path.expanduser("~/.gemini/antigravity/scratch/antenna_check"), help="Output directory")
    parser.add_argument("--no-open", action="store_true", help="Do not auto-open the PNG plot")
    args = parser.parse_args()

    # Keep central 50% of each capture to avoid baseband filter roll-off
    usable_bw_mhz = (args.sample_rate / 1e6) * 0.5
    centers_mhz = np.arange(args.start + usable_bw_mhz / 2.0, args.end, usable_bw_mhz)

    print(f"=== HackRF One Antenna Reception Check ===")
    print(f"Range       : {args.start:.0f} - {args.end:.0f} MHz ({len(centers_mhz)} steps x {usable_bw_mhz:.1f} MHz)")
    print(f"Settings    : SR={args.sample_rate/1e6:.0f} MS/s, LNA={args.lna} dB, VGA={args.vga} dB, Amp={args.amp}, BiasT={args.ant_power}")
    print("Sweeping...", flush=True)

    all_freqs_mhz = []
    all_avg_db = []
    all_peak_db = []

    with tempfile.NamedTemporaryFile(suffix=".iq", delete=False) as tf:
        tmp_path = tf.name

    try:
        for i, c_mhz in enumerate(centers_mhz, 1):
            iq = capture_window(
                center_hz=c_mhz * 1e6,
                sample_rate=args.sample_rate,
                num_samples=args.samples,
                lna_gain=args.lna,
                vga_gain=args.vga,
                amp_enable=args.amp,
                ant_power=args.ant_power,
                tmp_path=tmp_path,
            )
            freqs_hz, avg_db, peak_db = compute_psd_welch(iq, args.sample_rate, nfft=1024)
            freqs_mhz = c_mhz + freqs_hz / 1e6

            # Keep only the clean central usable_bw_mhz window
            half = usable_bw_mhz / 2.0
            mask = (freqs_mhz >= c_mhz - half) & (freqs_mhz < c_mhz + half)
            all_freqs_mhz.append(freqs_mhz[mask])
            all_avg_db.append(avg_db[mask])
            all_peak_db.append(peak_db[mask])

            if i % 10 == 0 or i == len(centers_mhz):
                print(f"  Progress: {i}/{len(centers_mhz)} ({c_mhz:.1f} MHz)", flush=True)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    freqs = np.concatenate(all_freqs_mhz)
    avg_db = np.concatenate(all_avg_db)
    peak_db = np.concatenate(all_peak_db)

    # Save CSV
    os.makedirs(args.out_dir, exist_ok=True)
    csv_path = os.path.join(args.out_dir, "antenna_check_data.csv")
    plot_path = os.path.join(args.out_dir, "antenna_check_plot.png")
    np.savetxt(
        csv_path,
        np.column_stack([freqs, avg_db, peak_db]),
        delimiter=",",
        header="freq_mhz,avg_dbfs,peak_dbfs",
        comments="",
        fmt="%.4f,%.2f,%.2f",
    )

    # Summary statistics
    noise_floor = float(np.median(avg_db))
    peak_noise_floor = float(np.median(peak_db))
    max_avg_idx = int(np.argmax(avg_db))
    max_peak_idx = int(np.argmax(peak_db))

    print("\n================ RECEPTION SUMMARY ================")
    print(f"Median Noise Floor (Avg)  : {noise_floor:6.1f} dBFS")
    print(f"Strongest Sustained Signal: {avg_db[max_avg_idx]:6.1f} dBFS at {freqs[max_avg_idx]:.2f} MHz  (+{avg_db[max_avg_idx]-noise_floor:.1f} dB over floor)")
    print(f"Strongest Peak/Burst      : {peak_db[max_peak_idx]:6.1f} dBFS at {freqs[max_peak_idx]:.2f} MHz  (+{peak_db[max_peak_idx]-peak_noise_floor:.1f} dB over peak floor)")

    # Sub-band breakdown (every 25 MHz)
    print("\n------------ 25 MHz Sub-Band Breakdown ------------")
    print(f"{'Band (MHz)':<14} {'Avg (dBFS)':>10} {'Peak (dBFS)':>12}  {'Activity (Peak)':<28}")
    sub_edges = np.arange(args.start, args.end + 0.1, 25.0)
    vmin = peak_noise_floor - 2.0
    vmax = max(float(np.max(peak_db)), peak_noise_floor + 15.0)
    for lo, hi in zip(sub_edges[:-1], sub_edges[1:]):
        m = (freqs >= lo) & (freqs < hi)
        if not np.any(m):
            continue
        sub_avg = float(np.mean(avg_db[m]))
        sub_peak = float(np.max(peak_db[m]))
        bar = ascii_bar(sub_peak, vmin, vmax, width=26)
        tag = ""
        if 5850 <= lo < 5925:
            tag = " [ITS/DSRC]"
        elif 5725 <= lo < 5850:
            tag = " [5.8G ISM/Wi-Fi]"
        print(f"{lo:4.0f}-{hi:4.0f} MHz  {sub_avg:10.1f} {sub_peak:12.1f}  |{bar}|{tag}")

    # Top distinct peaks (separated by at least 5 MHz)
    print("\n------------ Top Detected Signals (Peak Hold) -----")
    order = np.argsort(peak_db)[::-1]
    top_peaks = []
    for idx in order:
        f_val = float(freqs[idx])
        p_val = float(peak_db[idx])
        if p_val < peak_noise_floor + 5.0:
            break
        if all(abs(f_val - pf) >= 5.0 for pf, _ in top_peaks):
            top_peaks.append((f_val, p_val))
        if len(top_peaks) >= 8:
            break

    if top_peaks:
        for rank, (f_val, p_val) in enumerate(top_peaks, 1):
            snr = p_val - peak_noise_floor
            print(f"  {rank}. {f_val:7.2f} MHz  |  Peak: {p_val:6.1f} dBFS  (+{snr:4.1f} dB above floor)")
    else:
        print("  No signals > 5 dB above the noise floor were detected during this sweep.")

    # Generate plot
    plt.figure(figsize=(12, 6), dpi=130)
    plt.axvspan(5725, 5875, color="#e8f4f8", alpha=0.6, label="5.8 GHz ISM / Wi-Fi / FPV (5725–5875 MHz)")
    plt.axvspan(5850, 5925, color="#fff3cd", alpha=0.5, label="ITS / DSRC Traffic Band (5850–5925 MHz)")

    plt.plot(freqs, peak_db, color="#d9534f", linewidth=0.9, alpha=0.85, label="Peak Hold (dBFS)")
    plt.plot(freqs, avg_db, color="#1f77b4", linewidth=1.1, label="Average Power (dBFS)")
    plt.axhline(noise_floor, color="#555555", linestyle="--", linewidth=0.9, label=f"Median Floor ({noise_floor:.1f} dBFS)")

    # Annotate top 3 well-separated peaks (at least 20 MHz apart so labels don't overlap)
    annotated = []
    for f_val, p_val in top_peaks:
        if all(abs(f_val - af) >= 20.0 for af, _ in annotated):
            annotated.append((f_val, p_val))
        if len(annotated) >= 3:
            break

    for f_val, p_val in annotated:
        plt.annotate(
            f"{f_val:.1f} MHz\n({p_val:.1f} dBFS)",
            xy=(f_val, p_val),
            xytext=(0, 10),
            textcoords="offset points",
            ha="center",
            fontsize=8,
            arrowprops=dict(arrowstyle="->", color="#333333", lw=0.8),
        )

    ymin, ymax = plt.ylim()
    plt.ylim(ymin, ymax + 4.5)

    plt.title(f"HackRF One Antenna Reception Check ({args.start:.0f}–{args.end:.0f} MHz)")
    plt.xlabel("Frequency (MHz)")
    plt.ylabel("Power (dBFS)")
    plt.xlim(args.start, args.end)
    plt.grid(True, linestyle=":", alpha=0.6)
    plt.legend(loc="lower right", fontsize=8.5)
    plt.tight_layout()
    plt.savefig(plot_path)
    plt.close()

    print("\n===================================================")
    print(f"CSV saved to  : {csv_path}")
    print(f"Graph saved to: {plot_path}")

    if not args.no_open and sys.platform == "darwin":
        subprocess.run(["open", plot_path], check=False)


if __name__ == "__main__":
    main()
