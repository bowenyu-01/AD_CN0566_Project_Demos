#!/usr/bin/env python3
"""Low-power cyclic-tone RF path test for the CN0566 QPSK hardware profile."""
from __future__ import annotations

import argparse
import math
import time
from pathlib import Path

import numpy as np

from cn0566_qpsk_audio import (
    DIGITAL_OFFSET_HZ,
    RX_BUFFER_SAMPLES,
    SAMPLE_RATE,
    configure_hardware,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CN0566/Pluto cyclic-tone RF sanity test")
    parser.add_argument("--phaser-uri", default="ip:localhost")
    parser.add_argument("--sdr-uri", default="ip:192.168.2.1")
    parser.add_argument("--enable-tx", action="store_true")
    parser.add_argument("--tx-gain", type=float, default=-60.0)
    parser.add_argument("--rx-gain", type=float, default=20.0)
    parser.add_argument("--array-gain", type=int, default=127)
    parser.add_argument("--beam-phase-diff", type=float, default=0.0)
    parser.add_argument("--cal-dir", type=Path)
    parser.add_argument("--tx-output", type=int, choices=(1, 2), default=2)
    parser.add_argument("--decode-channel", choices=("rx2", "mrc"), default="rx2")
    parser.add_argument("--captures", type=int, default=5)
    parser.add_argument("--tone-amplitude", type=float, default=6000.0)
    parser.add_argument("--correlation-threshold", type=float, default=0.18)
    parser.add_argument("--audio-device", default="default")
    parser.add_argument("--audio-buffer-ms", type=int, default=1500)
    return parser


def summarize(samples: np.ndarray, label: str) -> float:
    values = np.asarray(samples, dtype=np.complex64).reshape(-1)
    values = values - np.mean(values)
    index = np.arange(values.size, dtype=np.float64)
    positive = np.exp(1j * 2.0 * math.pi * DIGITAL_OFFSET_HZ * index / SAMPLE_RATE)
    negative = np.conj(positive)
    coefficient_positive = np.vdot(positive, values) / values.size
    coefficient_negative = np.vdot(negative, values) / values.size
    if abs(coefficient_positive) >= abs(coefficient_negative):
        amplitude = coefficient_positive
        orientation = +DIGITAL_OFFSET_HZ
        reference = positive
    else:
        amplitude = coefficient_negative
        orientation = -DIGITAL_OFFSET_HZ
        reference = negative
    residual = values - amplitude * reference
    tone_amplitude = float(abs(amplitude))
    residual_rms = math.sqrt(float(np.mean(np.abs(residual) ** 2)))
    coherent_snr_db = 20.0 * math.log10(max(tone_amplitude, 1e-12) / max(residual_rms, 1e-12))

    spectrum = np.fft.fftshift(np.fft.fft(values))
    frequencies = np.fft.fftshift(np.fft.fftfreq(values.size, 1.0 / SAMPLE_RATE))
    strongest_hz = float(frequencies[int(np.argmax(np.abs(spectrum)))])
    median = float(np.median(np.abs(spectrum)))
    target_index = int(np.argmin(np.abs(frequencies - orientation)))
    ratio_db = 20.0 * math.log10(max(float(abs(spectrum[target_index])), 1e-12) / max(median, 1e-12))
    print(
        f"  {label}: max|ADC|={np.max(np.abs(values)):8.1f}, "
        f"RMS={np.sqrt(np.mean(np.abs(values)**2)):8.2f}, "
        f"strongest={strongest_hz/1e3:+9.2f} kHz, "
        f"coherent={orientation/1e3:+8.1f} kHz, "
        f"tone/median={ratio_db:6.1f} dB, CW_SNR={coherent_snr_db:+6.1f} dB"
    )
    return coherent_snr_db


def main() -> int:
    args = build_parser().parse_args()
    if not args.enable_tx:
        raise SystemExit("RF transmission disabled; add --enable-tx after checking the antenna setup")
    phaser = None
    sdr = None
    try:
        phaser, sdr, lock = configure_hardware(args)
        if lock == 0:
            raise RuntimeError("CN0566 PLL is not locked")
        index = np.arange(RX_BUFFER_SAMPLES, dtype=np.float64)
        tone = args.tone_amplitude * np.exp(
            1j * 2.0 * math.pi * DIGITAL_OFFSET_HZ * index / SAMPLE_RATE
        )
        tone = tone.astype(np.complex64)
        try:
            sdr.tx_destroy_buffer()
        except Exception:
            pass
        sdr.tx_cyclic_buffer = True
        sdr.tx(tone)
        print(f"Cyclic +{DIGITAL_OFFSET_HZ/1e3:.1f} kHz tone started at {SAMPLE_RATE/1e6:.1f} MSPS")
        time.sleep(0.3)
        results = []
        for capture in range(args.captures):
            data = sdr.rx()
            print(f"Capture {capture + 1}/{args.captures}")
            if args.decode_channel == "mrc":
                results.append(summarize(data[0], "RX1A <- CN0566 RX1"))
                results.append(summarize(data[1], "RX2A <- CN0566 RX2"))
            else:
                if isinstance(data, (list, tuple)):
                    data = data[-1]
                results.append(summarize(data, "RX2A <- CN0566 RX2"))
        if results:
            print(f"Mean coherent CW SNR: {float(np.mean(results)):+.1f} dB")
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
