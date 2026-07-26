#!/usr/bin/env python3
"""Deterministic single-carrier QPSK PHY performance test for CN0566 + Pluto.

The tool transmits a cyclic known frame in hardware mode or generates a
repeatable software channel in simulation mode. It writes a timestamped report,
CSV metrics, raw capture (optional), waveform, PSD, correlation, constellation,
and a derived QPSK eye diagram.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import math
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import signal

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from cn0566_qpsk_audio import (
    AUDIO_FRAME_SAMPLES,
    CODED_PACKET_BITS,
    CORRELATION_THRESHOLD_DEFAULT,
    DATA_QPSK_SYMBOLS,
    DIGITAL_OFFSET_HZ,
    FILLER_SYMBOLS,
    FRAME_PERIOD_SAMPLES,
    LEADING_GUARD_SAMPLES,
    NOMINAL_XBAND_RF_HZ,
    PACKET_BYTES,
    PLUTO_IF_HZ,
    PREAMBLE_SYMBOL_COUNT,
    PREAMBLE_SYMBOLS,
    RF_BANDWIDTH_HZ,
    RRC_GROUP_DELAY,
    RRC_TAPS,
    RX_BUFFER_SAMPLES,
    SAMPLE_RATE,
    SAMPLES_PER_SYMBOL,
    SYMBOL_RATE,
    TOTAL_FRAME_SYMBOLS,
    StreamingQpskReceiver,
    _DETECTOR_TEMPLATE,
    _DETECTOR_TEMPLATE_ENERGY,
    _catmull_rom,
    _fractional_delay,
    _normalize_rx_return,
    _sliding_energy,
    build_packet,
    configure_hardware,
    convolutional_encode,
    generate_test_pcm,
    packet_to_radio_frame,
    pulse_shape,
    qpsk_map,
)

LOG = logging.getLogger("cn0566-qpsk-performance")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CN0566 deterministic QPSK PHY performance test")
    p.add_argument("--mode", choices=("simulate", "hardware", "analyze"), default="simulate")
    p.add_argument("--enable-tx", action="store_true")
    p.add_argument("--capture", type=Path, help="NPZ input for analyze mode")
    p.add_argument("--output-root", type=Path, default=Path("qpsk_performance_runs"))
    p.add_argument("--label", default="")
    p.add_argument("--run-seconds", type=float, default=6.0)
    p.add_argument("--frames", type=int, default=20)
    p.add_argument("--simulation-snr-db", type=float, default=24.0)
    p.add_argument("--simulation-cfo-hz", type=float, default=7.0)
    p.add_argument("--phaser-uri", default="ip:localhost")
    p.add_argument("--sdr-uri", default="ip:192.168.2.1")
    p.add_argument("--tx-output", type=int, choices=(1, 2), default=2)
    p.add_argument("--tx-gain", type=float, default=-60.0)
    p.add_argument("--rx-gain", type=float, default=20.0)
    p.add_argument("--array-gain", type=int, default=127)
    p.add_argument("--beam-phase-diff", type=float, default=0.0)
    p.add_argument("--cal-dir", type=Path)
    p.add_argument("--decode-channel", choices=("rx2", "mrc"), default="rx2")
    p.add_argument("--correlation-threshold", type=float, default=CORRELATION_THRESHOLD_DEFAULT)
    p.add_argument("--audio-device", default="default")
    p.add_argument("--audio-buffer-ms", type=int, default=1500)
    p.add_argument("--no-save-iq", action="store_true")
    p.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return p


def timestamp_dir(root: Path, mode: str, label: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "_" + "".join(c if c.isalnum() or c in "-_" else "_" for c in label.strip()) if label else ""
    path = root / f"{stamp}_{mode}{suffix}"
    path.mkdir(parents=True, exist_ok=False)
    (path / "plots").mkdir()
    (path / "data").mkdir()
    return path


def known_packet_and_frame() -> Tuple[object, np.ndarray, np.ndarray]:
    pcm = generate_test_pcm(0.020)[:AUDIO_FRAME_SAMPLES]
    packet = build_packet(0, pcm, AUDIO_FRAME_SAMPLES, 0)
    packet_bits = np.unpackbits(np.frombuffer(packet.raw, dtype=np.uint8), bitorder="big")
    coded = convolutional_encode(packet_bits)
    expected_data = qpsk_map(coded)
    return packet, packet_to_radio_frame(packet), expected_data


def simulate_capture(frame: np.ndarray, frames: int, snr_db: float, cfo_hz: float) -> Tuple[np.ndarray, np.ndarray]:
    tx = np.tile(frame, max(2, frames))
    delayed = _fractional_delay(tx, 0.43)
    delayed = np.concatenate((np.zeros(7321, dtype=np.complex64), delayed))
    n = np.arange(delayed.size, dtype=np.float64)
    delayed *= np.exp(1j * 2 * np.pi * cfo_hz * n / SAMPLE_RATE).astype(np.complex64)
    signal_rms = math.sqrt(float(np.mean(np.abs(delayed) ** 2)))
    noise_rms = signal_rms / (10.0 ** (snr_db / 20.0))
    sigma = noise_rms / math.sqrt(2.0)
    rng = np.random.default_rng(0xC0566)
    noise0 = (rng.normal(0, sigma, delayed.size) + 1j * rng.normal(0, sigma, delayed.size)).astype(np.complex64)
    noise1 = (rng.normal(0, sigma, delayed.size) + 1j * rng.normal(0, sigma, delayed.size)).astype(np.complex64)
    return (0.075 + 0.025j) * delayed + noise0, (-0.020 + 0.090j) * delayed + noise1


def hardware_capture(args: argparse.Namespace, frame: np.ndarray) -> Tuple[np.ndarray, np.ndarray, Dict[str, object]]:
    if not args.enable_tx:
        raise RuntimeError("hardware mode requires --enable-tx")
    phaser = None
    sdr = None
    blocks0: List[np.ndarray] = []
    blocks1: List[np.ndarray] = []
    metadata: Dict[str, object] = {}
    try:
        phaser, sdr, lock = configure_hardware(args)
        metadata["pll_lock"] = lock
        try:
            sdr.tx_destroy_buffer()
        except Exception:
            pass
        sdr.tx_cyclic_buffer = True
        sdr.tx(frame.astype(np.complex64))
        time.sleep(0.5)
        block_count = max(1, int(math.ceil(args.run_seconds * SAMPLE_RATE / RX_BUFFER_SAMPLES)))
        for _ in range(block_count):
            rx0, rx1 = _normalize_rx_return(sdr.rx(), args.decode_channel)
            blocks0.append(rx0.copy())
            blocks1.append(rx1.copy())
        return np.concatenate(blocks0), np.concatenate(blocks1), metadata
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


def downmix(values: np.ndarray) -> np.ndarray:
    x = np.asarray(values, dtype=np.complex64).reshape(-1)
    x = x - np.mean(x)
    n = np.arange(x.size, dtype=np.float64)
    return (x * np.exp(-1j * 2 * np.pi * DIGITAL_OFFSET_HZ * n / SAMPLE_RATE)).astype(np.complex64)


def detector_metric(source: np.ndarray) -> np.ndarray:
    corr = signal.fftconvolve(source, np.conj(_DETECTOR_TEMPLATE[::-1]), mode="valid")
    energy = _sliding_energy(source, _DETECTOR_TEMPLATE.size)
    metric = np.zeros(corr.size, dtype=np.float64)
    valid = energy > max(float(np.max(energy)) * 1e-12, 1e-20)
    metric[valid] = np.abs(corr[valid]) ** 2 / (_DETECTOR_TEMPLATE_ENERGY * energy[valid])
    return np.clip(metric, 0.0, 1.0)


def symbol_stream(channel: np.ndarray, active_start: int, timing: float) -> np.ndarray:
    active_len = FRAME_PERIOD_SAMPLES - LEADING_GUARD_SAMPLES
    segment = channel[active_start:active_start + active_len]
    matched = np.convolve(segment, RRC_TAPS, mode="full")
    positions = 2 * RRC_GROUP_DELAY + timing + np.arange(TOTAL_FRAME_SYMBOLS) * SAMPLES_PER_SYMBOL
    if positions[0] < 1 or positions[-1] >= matched.size - 2:
        raise ValueError("symbol sampling positions outside frame")
    return _catmull_rom(matched, positions).astype(np.complex64)


def extract_frame(
    rx0: np.ndarray,
    rx1: np.ndarray,
    active_start: int,
    decode_channel: str,
) -> Tuple[np.ndarray, float, float, float]:
    best = None
    for timing in np.arange(-2.0, 2.0001, 0.125):
        try:
            s1 = symbol_stream(rx1, active_start, float(timing))
            r1, h1, c1, mse1 = StreamingQpskReceiver._estimate_cfo_and_channel(s1)
            if decode_channel == "mrc":
                s0 = symbol_stream(rx0, active_start, float(timing))
                r0, h0, c0, mse0 = StreamingQpskReceiver._estimate_cfo_and_channel(s0)
                rad = 0.5 * (r0 + r1)
                idx = np.arange(s0.size)
                c0 = s0 * np.exp(-1j * rad * idx)
                c1 = s1 * np.exp(-1j * rad * idx)
                h0 = np.vdot(PREAMBLE_SYMBOLS, c0[:PREAMBLE_SYMBOL_COUNT]) / PREAMBLE_SYMBOL_COUNT
                h1 = np.vdot(PREAMBLE_SYMBOLS, c1[:PREAMBLE_SYMBOL_COUNT]) / PREAMBLE_SYMBOL_COUNT
                denom = abs(h0) ** 2 + abs(h1) ** 2
                equalized = (np.conj(h0) * c0 + np.conj(h1) * c1) / max(denom, 1e-12)
                quality = mse0 + mse1
            else:
                rad = r1
                equalized = c1 / (h1 if abs(h1) > 1e-12 else 1.0)
                quality = mse1
            if best is None or quality < best[0]:
                best = (quality, float(timing), float(rad), equalized.astype(np.complex64))
        except Exception:
            continue
    if best is None:
        raise RuntimeError("frame extraction failed")
    quality, timing, rad, equalized = best
    cfo_hz = rad * SYMBOL_RATE / (2 * np.pi)
    return equalized, timing, cfo_hz, quality


def hard_bits(symbols: np.ndarray) -> np.ndarray:
    out = np.empty(symbols.size * 2, dtype=np.uint8)
    out[0::2] = np.asarray(symbols.real < 0, dtype=np.uint8)
    out[1::2] = np.asarray(symbols.imag < 0, dtype=np.uint8)
    return out


def analyze(rx0_raw: np.ndarray, rx1_raw: np.ndarray, expected_data: np.ndarray, threshold: float, decode_channel: str) -> Dict[str, object]:
    rx0 = downmix(rx0_raw)
    rx1 = downmix(rx1_raw)
    source = rx1 if decode_channel == "rx2" else (rx0 if np.mean(np.abs(rx0)**2) > np.mean(np.abs(rx1)**2) else rx1)
    metric = detector_metric(source)
    peaks, properties = signal.find_peaks(metric, height=threshold, distance=FRAME_PERIOD_SAMPLES // 2)
    peaks = sorted(peaks.tolist(), key=lambda p: p)
    rows = []
    corrected_frames = []
    for start in peaks:
        if start + FRAME_PERIOD_SAMPLES - LEADING_GUARD_SAMPLES + 8 > source.size:
            continue
        try:
            symbols, timing, cfo_hz, quality = extract_frame(rx0, rx1, int(start), decode_channel)
        except Exception:
            continue
        data = symbols[PREAMBLE_SYMBOL_COUNT:PREAMBLE_SYMBOL_COUNT + DATA_QPSK_SYMBOLS]
        error = data - expected_data
        evm = math.sqrt(float(np.mean(np.abs(error) ** 2)))
        evm_db = 20 * math.log10(max(evm, 1e-12))
        bits_rx = hard_bits(data)
        bits_tx = hard_bits(expected_data)
        ber = float(np.mean(bits_rx != bits_tx))
        ser = float(np.mean(np.any(bits_rx.reshape(-1, 2) != bits_tx.reshape(-1, 2), axis=1)))
        rows.append({
            "sample_start": int(start),
            "correlation": float(metric[start]),
            "timing_samples": timing,
            "cfo_hz": cfo_hz,
            "evm_db": evm_db,
            "ber": ber,
            "ser": ser,
            "quality": quality,
        })
        corrected_frames.append(data)
    if not rows:
        raise RuntimeError("no complete QPSK frames were recovered")
    return {
        "rx0_raw": rx0_raw,
        "rx1_raw": rx1_raw,
        "rx0": rx0,
        "rx1": rx1,
        "metric": metric,
        "rows": rows,
        "symbols": np.concatenate(corrected_frames),
    }


def save_outputs(
    out: Path,
    mode: str,
    args: argparse.Namespace,
    frame: np.ndarray,
    expected_data: np.ndarray,
    result: Dict[str, object],
    metadata: Dict[str, object],
) -> None:
    rows: List[Dict[str, float]] = result["rows"]  # type: ignore[assignment]
    symbols = np.asarray(result["symbols"])
    rx1_raw = np.asarray(result["rx1_raw"])
    metric = np.asarray(result["metric"])

    with (out / "data" / "frame_metrics.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "mode": mode,
        "frames_analyzed": len(rows),
        "mean_correlation": float(np.mean([r["correlation"] for r in rows])),
        "mean_cfo_hz": float(np.mean([r["cfo_hz"] for r in rows])),
        "mean_evm_db": float(np.mean([r["evm_db"] for r in rows])),
        "mean_ber": float(np.mean([r["ber"] for r in rows])),
        "mean_ser": float(np.mean([r["ser"] for r in rows])),
        "sample_rate": SAMPLE_RATE,
        "symbol_rate": SYMBOL_RATE,
        "digital_offset_hz": DIGITAL_OFFSET_HZ,
        "nominal_xband_rf_hz": NOMINAL_XBAND_RF_HZ,
        "packet_bytes": PACKET_BYTES,
        "metadata": metadata,
    }
    (out / "data" / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    if not args.no_save_iq:
        np.savez_compressed(out / "data" / "qpsk_capture.npz", rx0=result["rx0_raw"], rx1=result["rx1_raw"], frame=frame)

    # Waveform comparison, normalized for visual comparison.
    length = min(8000, frame.size, rx1_raw.size)
    tx_view = frame[:length]
    rx_view = rx1_raw[:length]
    tx_norm = tx_view / max(np.sqrt(np.mean(np.abs(tx_view)**2)), 1e-12)
    rx_norm = rx_view / max(np.sqrt(np.mean(np.abs(rx_view)**2)), 1e-12)
    time_ms = 1e3 * np.arange(length) / SAMPLE_RATE
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(time_ms, tx_norm.real, label="Original TX I", linewidth=0.8)
    ax.plot(time_ms, rx_norm.real, label="Captured RX I", linewidth=0.8, alpha=0.75)
    ax.set_xlabel("Time (ms)"); ax.set_ylabel("Normalized amplitude"); ax.grid(True); ax.legend()
    fig.tight_layout(); fig.savefig(out / "plots" / "qpsk_waveform_original_vs_received.png", dpi=180); plt.close(fig)

    # PSD
    ftx, ptx = signal.welch(frame, fs=SAMPLE_RATE, nperseg=4096, return_onesided=False)
    frx, prx = signal.welch(rx1_raw - np.mean(rx1_raw), fs=SAMPLE_RATE, nperseg=4096, return_onesided=False)
    ftx, ptx = np.fft.fftshift(ftx), np.fft.fftshift(ptx)
    frx, prx = np.fft.fftshift(frx), np.fft.fftshift(prx)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(ftx/1e3, 10*np.log10(np.maximum(ptx, 1e-30)/np.max(ptx)), label="TX")
    ax.plot(frx/1e3, 10*np.log10(np.maximum(prx, 1e-30)/np.max(prx)), label="RX", alpha=0.8)
    ax.axvline(DIGITAL_OFFSET_HZ/1e3, linestyle="--", linewidth=1, label="QPSK center")
    ax.set_xlabel("Frequency relative to Pluto LO (kHz)"); ax.set_ylabel("Normalized PSD (dB/Hz)")
    ax.set_ylim(-80, 5); ax.grid(True); ax.legend(); fig.tight_layout()
    fig.savefig(out / "plots" / "qpsk_power_spectral_density.png", dpi=180); plt.close(fig)

    # Correlation
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(np.arange(metric.size)/SAMPLE_RATE, metric, linewidth=0.8)
    ax.axhline(args.correlation_threshold, linestyle="--", linewidth=1)
    ax.set_xlabel("Capture time (s)"); ax.set_ylabel("Normalized metric"); ax.set_ylim(0, 1.05); ax.grid(True)
    fig.tight_layout(); fig.savefig(out / "plots" / "qpsk_preamble_correlation.png", dpi=180); plt.close(fig)

    # Constellation
    limit = min(symbols.size, 30000)
    fig, ax = plt.subplots(figsize=(6, 6))
    ax.scatter(symbols[:limit].real, symbols[:limit].imag, s=3, alpha=0.25, label="RX")
    ax.scatter(expected_data.real, expected_data.imag, marker="x", s=80, linewidths=2, label="Ideal")
    ax.set_xlabel("I"); ax.set_ylabel("Q"); ax.axis("equal"); ax.grid(True); ax.legend(); fig.tight_layout()
    fig.savefig(out / "plots" / "qpsk_constellation_corrected.png", dpi=180); plt.close(fig)

    # Derived eye from equalized symbols.
    use = symbols[: min(symbols.size, 5000)]
    shaped = pulse_shape(use)
    traces_i = []
    traces_q = []
    span = 2 * SAMPLES_PER_SYMBOL
    for start in range(0, shaped.size - span, SAMPLES_PER_SYMBOL):
        traces_i.append(shaped[start:start+span].real)
        traces_q.append(shaped[start:start+span].imag)
    fig, axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
    t = np.arange(span) / SAMPLES_PER_SYMBOL
    for trace in traces_i[:1500]: axes[0].plot(t, trace, linewidth=0.35, alpha=0.15)
    for trace in traces_q[:1500]: axes[1].plot(t, trace, linewidth=0.35, alpha=0.15)
    axes[0].set_ylabel("I"); axes[1].set_ylabel("Q"); axes[1].set_xlabel("Symbol intervals")
    axes[0].grid(True); axes[1].grid(True); fig.suptitle("Derived QPSK decision eye")
    fig.tight_layout(); fig.savefig(out / "plots" / "qpsk_derived_eye.png", dpi=180); plt.close(fig)

    report = [
        "CN0566 / Pluto Single-Carrier QPSK Performance Report",
        "=" * 61,
        f"Mode: {mode}",
        f"Frames analyzed: {summary['frames_analyzed']}",
        f"Mean correlation: {summary['mean_correlation']:.4f}",
        f"Mean residual CFO: {summary['mean_cfo_hz']:+.3f} Hz",
        f"Mean EVM: {summary['mean_evm_db']:.3f} dB",
        f"Mean pre-FEC BER: {summary['mean_ber']:.6e}",
        f"Mean pre-FEC SER: {summary['mean_ser']:.6e}",
        "",
        "Profile",
        "-------",
        f"Sample rate: {SAMPLE_RATE} sample/s",
        f"Symbol rate: {SYMBOL_RATE} symbol/s",
        f"Samples/symbol: {SAMPLES_PER_SYMBOL}",
        f"Digital offset: {DIGITAL_OFFSET_HZ} Hz",
        f"RF bandwidth: {RF_BANDWIDTH_HZ} Hz",
        f"Pluto IF LO: {PLUTO_IF_HZ} Hz",
        f"Nominal X-band center: {NOMINAL_XBAND_RF_HZ} Hz",
        "",
        "Notes",
        "-----",
        "The eye diagram is reconstructed from equalized QPSK decisions for visualization.",
        "Hardware transmission must be conducted only in an authorized or shielded environment.",
    ]
    (out / "REPORT.txt").write_text("\n".join(report) + "\n")


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s: %(message)s")
    packet, frame, expected_data = known_packet_and_frame()
    metadata: Dict[str, object] = {}
    if args.mode == "simulate":
        rx0, rx1 = simulate_capture(frame, args.frames, args.simulation_snr_db, args.simulation_cfo_hz)
    elif args.mode == "hardware":
        rx0, rx1, metadata = hardware_capture(args, frame)
    else:
        if args.capture is None:
            raise SystemExit("analyze mode requires --capture")
        loaded = np.load(args.capture)
        rx0 = loaded["rx0"]
        rx1 = loaded["rx1"]
    out = timestamp_dir(args.output_root, args.mode, args.label)
    result = analyze(rx0, rx1, expected_data, args.correlation_threshold, args.decode_channel)
    save_outputs(out, args.mode, args, frame, expected_data, result, metadata)
    LOG.info("Performance results written to %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
