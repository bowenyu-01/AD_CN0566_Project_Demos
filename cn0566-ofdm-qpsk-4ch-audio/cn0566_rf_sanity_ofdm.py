#!/usr/bin/env python3
"""Low-risk profile-aware RF-path sanity test for the OFDM release."""

from __future__ import annotations

import argparse
import math
import time
from pathlib import Path

import numpy as np

from cn0566_ofdm_core import (
    RX_BUFFER_SAMPLES,
    SAMPLE_RATE,
    PROFILE_NAME,
    RF_SANITY_TONE_HZ,
    TX_DIGITAL_PEAK_DEFAULT,
    TX_DIGITAL_RMS_DEFAULT,
    configure_hardware,
)

TONE_HZ = RF_SANITY_TONE_HZ


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CN0566 profile-aware cyclic-tone RF sanity test")
    p.add_argument("--phaser-uri", default="ip:localhost")
    p.add_argument("--sdr-uri", default="ip:192.168.2.1")
    p.add_argument("--enable-tx", action="store_true")
    p.add_argument("--tx-gain", type=float, default=-40.0)
    p.add_argument("--rx-gain", type=float, default=20.0)
    p.add_argument("--array-gain", type=int, default=127)
    p.add_argument("--beam-phase-diff", type=float, default=0.0)
    p.add_argument("--calibration-dir", type=Path)
    p.add_argument("--decode-mode", choices=("rx2", "mrc"), default="rx2")
    p.add_argument("--captures", type=int, default=5)
    p.add_argument("--tx-digital-rms", type=float, default=TX_DIGITAL_RMS_DEFAULT)
    p.add_argument("--tx-digital-peak", type=float, default=TX_DIGITAL_PEAK_DEFAULT)
    p.add_argument("--correlation-threshold", type=float, default=0.20)
    return p


def summarize(samples: np.ndarray, label: str) -> float:
    x = np.asarray(samples, dtype=np.complex64).reshape(-1)
    x = x - np.mean(x)
    n = np.arange(x.size, dtype=np.float64)
    positive = np.exp(1j * 2.0 * math.pi * TONE_HZ * n / SAMPLE_RATE)
    negative = np.conj(positive)
    a_pos = np.vdot(positive, x) / x.size
    a_neg = np.vdot(negative, x) / x.size
    if abs(a_pos) >= abs(a_neg):
        amplitude = a_pos
        orientation = +TONE_HZ
        reference = positive
    else:
        amplitude = a_neg
        orientation = -TONE_HZ
        reference = negative
    residual = x - amplitude * reference
    residual_rms = math.sqrt(float(np.mean(np.abs(residual) ** 2)))
    tone_amp = float(abs(amplitude))
    snr_db = 20.0 * math.log10(max(tone_amp, 1e-12) / max(residual_rms, 1e-12))

    spectrum = np.fft.fftshift(np.fft.fft(x))
    frequency = np.fft.fftshift(np.fft.fftfreq(x.size, 1.0 / SAMPLE_RATE))
    strongest = float(frequency[int(np.argmax(np.abs(spectrum)))])
    median = float(np.median(np.abs(spectrum)))
    target_index = int(np.argmin(np.abs(frequency - orientation)))
    target_ratio_db = 20.0 * math.log10(max(float(abs(spectrum[target_index])), 1e-12) / max(median, 1e-12))
    print(
        f"  {label}: max|ADC|={np.max(np.abs(x)):8.1f}, RMS={np.sqrt(np.mean(np.abs(x)**2)):8.2f}, "
        f"strongest={strongest/1e3:+9.2f} kHz, coherent={orientation/1e3:+8.1f} kHz, "
        f"tone/median={target_ratio_db:6.1f} dB, CW_SNR={snr_db:+6.1f} dB"
    )
    return snr_db


def main() -> int:
    args = parser().parse_args()
    if not args.enable_tx:
        raise SystemExit("RF transmission disabled; add --enable-tx after checking the antenna setup")
    phaser = None
    sdr = None
    try:
        phaser, sdr, lock = configure_hardware(args)
        if lock == 0:
            raise RuntimeError("CN0566 PLL is not locked")
        n = np.arange(RX_BUFFER_SAMPLES, dtype=np.float64)
        tone = args.tx_digital_peak * np.exp(1j * 2.0 * math.pi * TONE_HZ * n / SAMPLE_RATE)
        tone = tone.astype(np.complex64)
        sdr.tx_destroy_buffer()
        sdr.tx_cyclic_buffer = True
        sdr.tx(tone)
        print(f"Cyclic +{TONE_HZ/1e3:.1f} kHz tone started for {PROFILE_NAME} at {SAMPLE_RATE/1e6:.1f} MSPS")
        time.sleep(0.2)
        snr_values = []
        for capture in range(args.captures):
            data = sdr.rx()
            print(f"Capture {capture + 1}/{args.captures}")
            if args.decode_mode == "mrc":
                snr_values.append(summarize(data[0], "RX1A <- CN0566 RX1"))
                snr_values.append(summarize(data[1], "RX2A <- CN0566 RX2"))
            else:
                if isinstance(data, (list, tuple)):
                    data = data[0]
                snr_values.append(summarize(data, "RX2A <- CN0566 RX2"))

        if snr_values:
            cw_snr = float(np.mean(snr_values[-args.captures:]))
            crest_backoff_db = 20.0 * math.log10(args.tx_digital_peak / args.tx_digital_rms)
            estimated_ofdm_snr = cw_snr - crest_backoff_db
            print()
            print(
                f"OFDM level estimate: CW_SNR={cw_snr:+.1f} dB, "
                f"configured RMS/peak backoff={crest_backoff_db:.1f} dB, "
                f"estimated wideband OFDM SNR={estimated_ofdm_snr:+.1f} dB"
            )
            if estimated_ofdm_snr < 3.0:
                print("Result: too weak for reliable four-stream OFDM; raise TX level gradually after checking ADC headroom.")
            elif estimated_ofdm_snr < 7.0:
                print("Result: marginal; acquisition may work, but -35 dB TX attenuation may be needed if the ADC remains linear.")
            else:
                print("Result: useful QPSK-OFDM margin is expected.")
    finally:
        if sdr is not None:
            try:
                sdr.tx_destroy_buffer()
                sdr.rx_destroy_buffer()
                sdr.tx_cyclic_buffer = False
                sdr.tx_hardwaregain_chan0 = -88.0
                sdr.tx_hardwaregain_chan1 = -88.0
            except Exception:
                pass
        del sdr
        del phaser
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
