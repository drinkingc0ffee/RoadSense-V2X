#!/Users/beisenmann/miniconda3/envs/dsrc/bin/python
"""
Offline IEEE 802.11p (DSRC / ITS-G5) & 802.11a Decoder for HackRF One .iq Files
Uses GNU Radio 3.10 + gr-ieee802-11 + gr-foo to demodulate raw HackRF int8 I/Q
recordings into a .pcap file, then runs tshark to display decoded packets
(including WSMP / ITS / DSRC / SAE J2735 traffic messages).
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import numpy as np

from gnuradio import blocks, fft, filter as gr_filter, gr
from gnuradio.fft import window
import foo
import ieee802_11


class Offline80211Decoder(gr.top_block):
    def __init__(self, fc32_path, pcap_path, samp_rate=10e6, center_freq=5900e6, freq_offset=0.0, decimation=1, chan_est=0):
        super().__init__("Offline80211Decoder")

        self.samp_rate = float(samp_rate)
        self.out_rate = self.samp_rate / decimation
        window_size = 48
        sync_length = 320

        # File source (complex64)
        self.src = blocks.file_source(gr.sizeof_gr_complex, fc32_path, False, 0, 0)

        # Optional frequency shift + decimation (e.g. 20 MS/s -> 10 MS/s centered on target channel)
        if decimation > 1 or abs(freq_offset) > 1.0:
            taps = gr_filter.firdes.low_pass(
                1.0,
                self.samp_rate,
                (self.out_rate * 0.45),
                (self.out_rate * 0.10),
                window.WIN_HAMMING,
            )
            self.xlating = gr_filter.freq_xlating_fir_filter_ccc(decimation, taps, freq_offset, self.samp_rate)
            front_end = self.xlating
            self.connect(self.src, self.xlating)
        else:
            front_end = self.src

        # Autocorrelation for short preamble detection
        self.delay16 = blocks.delay(gr.sizeof_gr_complex, 16)
        self.conj = blocks.conjugate_cc()
        self.mult = blocks.multiply_vcc(1)
        self.ma_corr = blocks.moving_average_cc(window_size, 1.0, 4000, 1)
        self.mag = blocks.complex_to_mag(1)

        self.mag_sq = blocks.complex_to_mag_squared(1)
        self.ma_pow = blocks.moving_average_ff(window_size + 16, 1.0, 4000, 1)
        self.div = blocks.divide_ff(1)

        # IEEE 802.11 PHY blocks
        self.sync_short = ieee802_11.sync_short(0.56, 2, False, False)
        self.delay_long = blocks.delay(gr.sizeof_gr_complex, sync_length)
        self.sync_long = ieee802_11.sync_long(sync_length, False, False)
        self.s2v = blocks.stream_to_vector(gr.sizeof_gr_complex, 64)
        self.fft_blk = fft.fft_vcc(64, True, window.rectangular(64), True, 1)
        self.equalizer = ieee802_11.frame_equalizer(
            ieee802_11.Equalizer(chan_est),
            float(center_freq),
            self.out_rate,
            False,
            False,
        )
        self.decode_mac = ieee802_11.decode_mac(False, False)

        # PCAP output via gr-foo wireshark_connector
        self.pcap_conn = foo.wireshark_connector(foo.WIFI, False)
        self.file_sink = blocks.file_sink(gr.sizeof_char, pcap_path, False)
        self.file_sink.set_unbuffered(True)

        # Wire up flowgraph
        self.connect(front_end, self.mag_sq, self.ma_pow, (self.div, 1))
        self.connect(front_end, self.delay16, self.conj, (self.mult, 1))
        self.connect(front_end, (self.mult, 0))
        self.connect(self.mult, self.ma_corr, self.mag, (self.div, 0))

        self.connect(self.delay16, (self.sync_short, 0))
        self.connect(self.ma_corr, (self.sync_short, 1))
        self.connect(self.div, (self.sync_short, 2))

        self.connect(self.sync_short, (self.sync_long, 0))
        self.connect(self.sync_short, self.delay_long, (self.sync_long, 1))
        self.connect(self.sync_long, self.s2v, self.fft_blk, self.equalizer, self.decode_mac)

        self.msg_connect((self.decode_mac, "out"), (self.pcap_conn, "in"))
        self.connect(self.pcap_conn, self.file_sink)


def run_tshark(pcap_path, verbose=False):
    tshark = "/usr/local/bin/tshark"
    if not os.path.exists(tshark):
        print(f"PCAP written to {pcap_path} (tshark not found at {tshark})")
        return 0

    if not os.path.exists(pcap_path) or os.path.getsize(pcap_path) <= 24:
        print("  0 IEEE 802.11 frames decoded from this channel/file.")
        return 0

    cmd = [tshark, "-r", pcap_path]
    if verbose:
        cmd.append("-V")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    if lines:
        print(f"\n================ DECODED PACKETS ({len(lines)} lines) ================")
        for ln in lines[:80]:
            print(" ", ln)
        if len(lines) > 80:
            print(f"  ... ({len(lines) - 80} more lines; open {pcap_path} in Wireshark to inspect all)")
    else:
        print("  0 packets displayed by tshark.")
    return len(lines)


def main():
    parser = argparse.ArgumentParser(description="Offline DSRC (802.11p) / 802.11a Decoder for HackRF .iq Files")
    parser.add_argument("iq_file", help="Path to HackRF raw int8 .iq file")
    parser.add_argument("--center-mhz", type=float, default=None, help="Capture center frequency in MHz (auto-read from .json sidecar if present)")
    parser.add_argument("--target-mhz", type=float, default=None, help="Target channel center frequency in MHz to shift to (defaults to capture center)")
    parser.add_argument("--samp-rate", type=float, default=None, help="Capture sample rate in Hz (auto-read from .json sidecar, default 20e6)")
    parser.add_argument("--bw", type=float, choices=[10.0, 20.0], default=10.0, help="Channel bandwidth in MHz: 10 = DSRC 802.11p (default), 20 = standard 802.11a Wi-Fi")
    parser.add_argument("--out-pcap", type=str, default=None, help="Output .pcap path (defaults to same path as .iq with .pcap extension)")
    parser.add_argument("--verbose", action="store_true", help="Show full verbose tshark dissection (-V)")
    args = parser.parse_args()

    iq_path = os.path.abspath(args.iq_file)
    if not os.path.exists(iq_path):
        print(f"Error: file not found: {iq_path}", file=sys.stderr)
        sys.exit(1)

    # Load .json sidecar if present
    meta = {}
    meta_path = os.path.splitext(iq_path)[0] + ".json"
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)

    samp_rate = args.samp_rate or float(meta.get("sample_rate_hz", 20_000_000))
    center_mhz = args.center_mhz or (float(meta["center_freq_hz"]) / 1e6 if "center_freq_hz" in meta else 5900.0)
    target_mhz = args.target_mhz if args.target_mhz is not None else center_mhz
    freq_offset_hz = (target_mhz - center_mhz) * 1e6

    target_rate = args.bw * 1e6
    decimation = max(1, int(round(samp_rate / target_rate)))

    pcap_path = args.out_pcap or (os.path.splitext(iq_path)[0] + f"_{int(round(target_mhz))}MHz.pcap")

    print("=== Offline IEEE 802.11p / DSRC Decoder ===")
    print(f"  Input IQ    : {iq_path}")
    print(f"  Capture     : center={center_mhz:.2f} MHz, SR={samp_rate/1e6:.1f} MS/s")
    print(f"  Decode Mode : {args.bw:.0f} MHz ({'DSRC 802.11p' if args.bw == 10.0 else 'Wi-Fi 802.11a'}) @ {target_mhz:.2f} MHz (offset {freq_offset_hz/1e6:+.2f} MHz, decim={decimation})")
    print(f"  Output PCAP : {pcap_path}")

    # Convert HackRF int8 interleaved I/Q -> complex64 temp file
    raw = np.fromfile(iq_path, dtype=np.int8)
    if raw.size % 2 != 0:
        raw = raw[:-1]
    iq = (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)) / 128.0
    iq -= np.mean(iq)  # remove HackRF DC offset

    with tempfile.NamedTemporaryFile(suffix=".fc32", delete=False) as tf:
        fc32_path = tf.name
        iq.astype(np.complex64).tofile(tf)

    try:
        tb = Offline80211Decoder(
            fc32_path=fc32_path,
            pcap_path=pcap_path,
            samp_rate=samp_rate,
            center_freq=target_mhz * 1e6,
            freq_offset=freq_offset_hz,
            decimation=decimation,
            chan_est=0,
        )
        tb.run()
    finally:
        if os.path.exists(fc32_path):
            os.remove(fc32_path)

    run_tshark(pcap_path, verbose=args.verbose)


if __name__ == "__main__":
    main()
