#!/usr/bin/env python3
"""Simultaneous four-channel OFDM performance test and report generator.

Plots are generated after acquisition, not in the real-time receive loop. This avoids
Matplotlib causing Pluto RX queue drops. Each logical audio channel has 48 interleaved
OFDM data carriers spread across the occupied band.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import queue
import signal as os_signal
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
from scipy import signal

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from cn0566_ofdm_core import (
    ACTIVE_BINS,
    AUDIO_FRAME_SECONDS,
    CHANNEL_DATA_BINS,
    CHANNEL_SIGNED_CARRIERS,
    CP_LEN,
    DATA_OFDM_SYMBOLS,
    DiagnosticCollector,
    FecCodec,
    FourAudioSource,
    NFFT,
    OFDM_SYMBOL_SAMPLES,
    OfdmFrameBuilder,
    RX_BUFFER_SAMPLES,
    RX_QUEUE_BUFFERS,
    RuntimeStatistics,
    SAMPLE_RATE,
    StreamingOfdmReceiver,
    SUBCARRIER_SPACING_HZ,
    TX_DIGITAL_PEAK_DEFAULT,
    TX_DIGITAL_RMS_DEFAULT,
    TX_INITIAL_PREFILL_CHUNKS,
    TX_PREFETCH_CHUNKS,
    configure_hardware,
    format_status,
    load_wav_as_pcm16,
    profile_summary,
    run_software_simulation,
    rx_acquisition_worker,
    tx_producer_worker,
    tx_pusher_worker,
)

LOG = logging.getLogger("cn0566-ofdm-performance")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Four-channel OFDM performance and plot test")
    parser.add_argument("--mode", choices=("simulate", "hardware"), default="simulate")
    parser.add_argument("--wav1", type=Path, default=Path("sample_1.wav"))
    parser.add_argument("--wav2", type=Path, default=Path("sample_2.wav"))
    parser.add_argument("--wav3", type=Path, default=Path("sample_3.wav"))
    parser.add_argument("--wav4", type=Path, default=Path("sample_4.wav"))
    parser.add_argument("--output-dir", type=Path, default=Path("performance_output"))
    parser.add_argument("--frames", type=int, default=100, help="simulation frames")
    parser.add_argument("--run-seconds", type=float, default=30.0, help="hardware capture duration")
    parser.add_argument("--simulation-snr-db", type=float, default=24.0)
    parser.add_argument("--simulation-cfo-hz", type=float, default=180.0)
    parser.add_argument("--phaser-uri", default="ip:localhost")
    parser.add_argument("--sdr-uri", default="ip:192.168.2.1")
    parser.add_argument("--enable-tx", action="store_true")
    parser.add_argument("--tx-gain", type=float, default=-40.0)
    parser.add_argument("--rx-gain", type=float, default=20.0)
    parser.add_argument("--array-gain", type=int, default=127)
    parser.add_argument("--beam-phase-diff", type=float, default=0.0)
    parser.add_argument("--calibration-dir", type=Path)
    parser.add_argument("--decode-mode", choices=("rx2", "mrc"), default="rx2")
    parser.add_argument("--correlation-threshold", type=float, default=0.20)
    parser.add_argument("--tx-digital-rms", type=float, default=TX_DIGITAL_RMS_DEFAULT)
    parser.add_argument("--tx-digital-peak", type=float, default=TX_DIGITAL_PEAK_DEFAULT)
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.frames <= 0:
        raise ValueError("--frames must be positive")
    if args.run_seconds <= 0 and args.mode == "hardware":
        raise ValueError("--run-seconds must be positive in hardware mode")
    if not 0.05 <= args.correlation_threshold <= 0.95:
        raise ValueError("--correlation-threshold must be in [0.05, 0.95]")
    if not 250.0 <= args.tx_digital_rms < args.tx_digital_peak:
        raise ValueError("--tx-digital-rms must be at least 250 and below --tx-digital-peak")
    if not 500.0 <= args.tx_digital_peak <= 12_000.0:
        raise ValueError("--tx-digital-peak must be in [500, 12000]")


def rrc_taps(beta: float = 0.35, samples_per_symbol: int = 8, span_symbols: int = 8) -> np.ndarray:
    count = samples_per_symbol * span_symbols
    t = np.arange(-count / 2, count / 2 + 1, dtype=np.float64) / samples_per_symbol
    taps = np.zeros_like(t)
    for index, value in enumerate(t):
        if abs(value) < 1e-12:
            taps[index] = 1.0 + beta * (4.0 / math.pi - 1.0)
        elif abs(abs(4.0 * beta * value) - 1.0) < 1e-10:
            taps[index] = (beta / math.sqrt(2.0)) * (
                (1.0 + 2.0 / math.pi) * math.sin(math.pi / (4.0 * beta))
                + (1.0 - 2.0 / math.pi) * math.cos(math.pi / (4.0 * beta))
            )
        else:
            numerator = math.sin(math.pi * value * (1.0 - beta))
            numerator += 4.0 * beta * value * math.cos(math.pi * value * (1.0 + beta))
            denominator = math.pi * value * (1.0 - (4.0 * beta * value) ** 2)
            taps[index] = numerator / denominator
    taps /= math.sqrt(float(np.sum(taps * taps)))
    return taps


def plot_constellation(symbols: np.ndarray, path: Path, channel: int) -> None:
    if symbols.size == 0:
        return
    sample = symbols
    if sample.size > 20_000:
        indices = np.linspace(0, sample.size - 1, 20_000, dtype=int)
        sample = sample[indices]
    fig, ax = plt.subplots(figsize=(6.4, 6.0))
    ax.scatter(sample.real, sample.imag, s=3, alpha=0.25)
    ideal = np.array([1+1j, 1-1j, -1+1j, -1-1j]) / math.sqrt(2.0)
    ax.scatter(ideal.real, ideal.imag, marker="x", s=90, linewidths=2, label="ideal QPSK")
    ax.axhline(0, linewidth=0.8)
    ax.axvline(0, linewidth=0.8)
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, alpha=0.3)
    ax.set_xlabel("In-phase")
    ax.set_ylabel("Quadrature")
    ax.set_title(f"Logical audio channel {channel}: equalized QPSK constellation")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def plot_equivalent_eye(symbols: np.ndarray, path: Path, channel: int) -> None:
    """Plot an analysis-only decision eye derived from equalized OFDM QPSK symbols.

    OFDM does not have the same continuous pulse-shaped eye as a single-carrier link.
    This plot serializes the equalized QPSK decisions and applies a reference RRC solely
    to visualize decision separation and noise. It is explicitly labelled as derived.
    """
    if symbols.size < 100:
        return
    sps = 8
    source = symbols[: min(symbols.size, 1500)]
    up = np.zeros(source.size * sps, dtype=np.complex64)
    up[::sps] = source
    shaped = np.convolve(up, rrc_taps(samples_per_symbol=sps), mode="same")
    width = 2 * sps
    starts = np.arange(4 * sps, shaped.size - width - 4 * sps, sps)
    starts = starts[:300]
    x = np.arange(width) / sps
    fig, axes = plt.subplots(2, 1, figsize=(7.2, 6.2), sharex=True)
    for start in starts:
        segment = shaped[start:start + width]
        axes[0].plot(x, segment.real, alpha=0.10, linewidth=0.8)
        axes[1].plot(x, segment.imag, alpha=0.10, linewidth=0.8)
    axes[0].set_ylabel("I")
    axes[1].set_ylabel("Q")
    axes[1].set_xlabel("Equivalent symbol intervals")
    axes[0].set_title(
        f"Logical audio channel {channel}: derived QPSK decision eye\n"
        "(analysis view from equalized OFDM symbols; not a raw OFDM time-domain eye)"
    )
    for ax in axes:
        ax.grid(True, alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def plot_waveform(waveform: np.ndarray, path: Path, channel: int) -> None:
    if waveform is None or waveform.size == 0:
        return
    count = min(waveform.size, 6 * OFDM_SYMBOL_SAMPLES)
    t_ms = np.arange(count) / SAMPLE_RATE * 1e3
    fig, ax = plt.subplots(figsize=(9.0, 4.8))
    ax.plot(t_ms, waveform[:count].real, label="I", linewidth=0.9)
    ax.plot(t_ms, waveform[:count].imag, label="Q", linewidth=0.9, alpha=0.8)
    ax.set_xlabel("Time (ms)")
    ax.set_ylabel("Normalized amplitude")
    ax.set_title(
        f"Logical audio channel {channel}: reconstructed OFDM contribution waveform\n"
        "(other three data-carrier groups removed after equalization)"
    )
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def plot_spectrum(waveform: np.ndarray, path: Path, channel: int) -> None:
    if waveform is None or waveform.size < 1024:
        return
    frequency, psd = signal.welch(
        waveform,
        fs=SAMPLE_RATE,
        nperseg=min(4096, waveform.size),
        noverlap=min(2048, waveform.size // 2),
        return_onesided=False,
        scaling="density",
    )
    order = np.argsort(frequency)
    frequency = frequency[order] / 1e6
    psd_db = 10.0 * np.log10(np.maximum(psd[order], 1e-20))
    fig, ax = plt.subplots(figsize=(9.0, 4.8))
    ax.plot(frequency, psd_db, linewidth=1.0)
    ax.set_xlim(-1.0, 1.0)
    ax.set_xlabel("Frequency relative to Pluto LO (MHz)")
    ax.set_ylabel("PSD (dB/Hz, relative)")
    ax.set_title(
        f"Logical audio channel {channel}: interleaved OFDM carrier-group spectrum"
    )
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def write_reports(collector: DiagnosticCollector, output_dir: Path, receiver=None, runtime=None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    for channel in range(4):
        symbols = collector.concatenated_symbols(channel)
        waveform = collector.waveforms[channel]
        evm_values = [
            frame.channels[channel].evm_db
            for frame in collector.frame_diagnostics
            if channel in frame.channels and math.isfinite(frame.channels[channel].evm_db)
        ]
        ber_values = [
            frame.channels[channel].raw_prbs_ber
            for frame in collector.frame_diagnostics
            if channel in frame.channels and math.isfinite(frame.channels[channel].raw_prbs_ber)
        ]
        summary_rows.append(
            {
                "channel": channel + 1,
                "carrier_count": len(CHANNEL_DATA_BINS[channel]),
                "carrier_min_khz": float(np.min(CHANNEL_SIGNED_CARRIERS[channel]) * SUBCARRIER_SPACING_HZ / 1e3),
                "carrier_max_khz": float(np.max(CHANNEL_SIGNED_CARRIERS[channel]) * SUBCARRIER_SPACING_HZ / 1e3),
                "packets_valid": collector.packet_counts[channel],
                "symbols_collected": int(symbols.size),
                "median_evm_db": float(np.median(evm_values)) if evm_values else float("nan"),
                "median_raw_prbs_ber": float(np.median(ber_values)) if ber_values else float("nan"),
            }
        )
        plot_constellation(symbols, output_dir / f"channel_{channel+1}_constellation.png", channel + 1)
        plot_equivalent_eye(symbols, output_dir / f"channel_{channel+1}_eye.png", channel + 1)
        if waveform is not None:
            plot_waveform(waveform, output_dir / f"channel_{channel+1}_waveform.png", channel + 1)
            plot_spectrum(waveform, output_dir / f"channel_{channel+1}_spectrum.png", channel + 1)

    with (output_dir / "channel_summary.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)

    with (output_dir / "frame_metrics.csv").open("w", newline="") as handle:
        fields = ["frame", "correlation", "cfo_hz", "timing_offset_samples", "channel_magnitude_mean"]
        for channel in range(1, 5):
            fields += [f"ch{channel}_packet_valid", f"ch{channel}_evm_db", f"ch{channel}_raw_prbs_ber"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, frame in enumerate(collector.frame_diagnostics):
            row = {
                "frame": index,
                "correlation": frame.correlation,
                "cfo_hz": frame.cfo_hz,
                "timing_offset_samples": frame.timing_offset_samples,
                "channel_magnitude_mean": frame.channel_magnitude_mean,
            }
            for channel in range(4):
                item = frame.channels[channel]
                row[f"ch{channel+1}_packet_valid"] = int(item.packet_valid)
                row[f"ch{channel+1}_evm_db"] = item.evm_db
                row[f"ch{channel+1}_raw_prbs_ber"] = item.raw_prbs_ber
            writer.writerow(row)

    text = [
        "CN0566 four-channel QPSK-OFDM performance report",
        f"Profile: {profile_summary()}",
        f"Frames captured: {collector.frame_count}",
        "",
        "Plot interpretation:",
        "- Constellation: measured equalized QPSK data-carrier symbols for that logical channel.",
        "- Eye: analysis-only equivalent decision eye derived from the measured equalized symbols.",
        "- Waveform: channel contribution reconstructed by retaining only that channel's interleaved carriers.",
        "- Spectrum: PSD of that reconstructed channel contribution.",
        "",
    ]
    for row in summary_rows:
        text.append(
            f"Channel {row['channel']}: valid={row['packets_valid']}, "
            f"symbols={row['symbols_collected']}, median EVM={row['median_evm_db']:.2f} dB, "
            f"median raw PRBS BER={row['median_raw_prbs_ber']:.3e}"
        )
    if receiver is not None:
        text += ["", f"Receiver frames decoded: {receiver.stats.frames_decoded}"]
    if runtime is not None:
        text += [
            f"RX application drops: {runtime.rx_queue_drops}",
            f"TX starvation events: {runtime.tx_starves}",
        ]
    (output_dir / "REPORT.txt").write_text("\n".join(text) + "\n")
    LOG.info("Performance output written to %s", output_dir)


def run_simulation(args: argparse.Namespace) -> int:
    collector = DiagnosticCollector()
    result = run_software_simulation(
        [args.wav1, args.wav2, args.wav3, args.wav4],
        frames=args.frames,
        snr_db=args.simulation_snr_db,
        cfo_hz=args.simulation_cfo_hz,
        decode_mode=args.decode_mode,
        diagnostics_collector=collector,
    )
    write_reports(collector, args.output_dir, receiver=result["receiver"])
    if result["matching_packets"] != result["expected_packets"]:
        LOG.error("Simulation packet mismatch: %d/%d exact", result["matching_packets"], result["expected_packets"])
        return 2
    LOG.info("Performance simulation passed with %d exact packets", result["matching_packets"])
    return 0


def run_hardware(args: argparse.Namespace) -> int:
    if not args.enable_tx:
        raise RuntimeError("Hardware performance mode requires --enable-tx")
    paths = [args.wav1, args.wav2, args.wav3, args.wav4]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing WAV files: " + ", ".join(missing))

    stop_event = threading.Event()
    os_signal.signal(os_signal.SIGINT, lambda *_: stop_event.set())
    os_signal.signal(os_signal.SIGTERM, lambda *_: stop_event.set())
    runtime = RuntimeStatistics()
    collector = DiagnosticCollector()
    fec = FecCodec()
    pcm = [load_wav_as_pcm16(path) for path in paths]
    source = FourAudioSource(pcm, repeat=True)
    builder = OfdmFrameBuilder(fec)
    received_packets = [0, 0, 0, 0]

    def packet_callback(packet, _pcm):
        received_packets[packet.stream_id] += 1

    receiver = StreamingOfdmReceiver(
        fec=fec,
        packet_callback=packet_callback,
        diagnostics_callback=collector,
        correlation_threshold=args.correlation_threshold,
        decode_mode=args.decode_mode,
    )
    rx_queue = queue.Queue(maxsize=RX_QUEUE_BUFFERS)
    tx_queue = queue.Queue(maxsize=TX_PREFETCH_CHUNKS)
    threads = []
    phaser = None
    sdr = None
    try:
        phaser, sdr, lock = configure_hardware(args)
        if lock == 0:
            raise RuntimeError("CN0566 PLL is not locked")
        for _ in range(4):
            _ = sdr.rx()
        prefill = min(TX_INITIAL_PREFILL_CHUNKS, TX_PREFETCH_CHUNKS)
        for _index in range(prefill):
            chunk, _history, _rms = builder.build_scaled_chunk(
                source,
                peak_target=args.tx_digital_peak,
                rms_target=args.tx_digital_rms,
            )
            tx_queue.put(chunk)
            runtime.tx_chunks_built += 1
        rx_thread = threading.Thread(
            target=rx_acquisition_worker,
            args=(sdr, args.decode_mode, rx_queue, stop_event, runtime),
            name="perf-rx", daemon=True,
        )
        producer = threading.Thread(
            target=tx_producer_worker,
            args=(
                builder, source, tx_queue, stop_event, runtime,
                args.tx_digital_peak, args.tx_digital_rms,
            ),
            name="perf-tx-producer", daemon=True,
        )
        pusher = threading.Thread(
            target=tx_pusher_worker,
            args=(sdr, tx_queue, stop_event, runtime),
            name="perf-tx-pusher", daemon=True,
        )
        for thread in (rx_thread, producer, pusher):
            thread.start()
            threads.append(thread)

        started = time.monotonic()
        last_report = started
        while not stop_event.is_set() and time.monotonic() - started < args.run_seconds:
            try:
                rx2, rx1 = rx_queue.get(timeout=0.05)
                receiver.feed(rx2, rx1)
            except queue.Empty:
                pass
            now = time.monotonic()
            if now - last_report >= 1.0:
                LOG.info(format_status(receiver, runtime, now - started))
                last_report = now
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=2.0)
        if sdr is not None:
            try:
                sdr.tx_destroy_buffer()
                sdr.rx_destroy_buffer()
                sdr.tx_hardwaregain_chan0 = -88.0
                sdr.tx_hardwaregain_chan1 = -88.0
            except Exception:
                pass
        del sdr
        del phaser

    write_reports(collector, args.output_dir, receiver=receiver, runtime=runtime)
    return 0


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
    )
    try:
        validate_args(args)
        if args.mode == "simulate":
            return run_simulation(args)
        return run_hardware(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        LOG.exception("Performance test failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
