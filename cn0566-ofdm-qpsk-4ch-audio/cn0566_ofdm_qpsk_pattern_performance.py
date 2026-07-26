#!/usr/bin/env python3
"""Four-channel deterministic QPSK-OFDM PHY performance test for CN0566 + Pluto.

This tool deliberately does NOT read sample_*.wav or use the application audio
packet format.  It transmits four known QPSK test sequences, one on each of the
four interleaved logical carrier groups, captures the real CN0566/Pluto receive
signal, and performs offline measurement and plotting.

Expected project context
------------------------
Copy this file into the active CN0566_OFDM_4CH_RPi4_Release directory.  It uses
that release's cn0566_ofdm_core.py for the selected OFDM profile, preamble,
pilots, subcarrier allocation, hardware configuration, and receiver acquisition
helpers.  The release should be version 2.3.0 or later.

Per-channel test patterns
-------------------------
Channel 1: PRBS7,  polynomial x^7  + x^6  + 1
Channel 2: PRBS9,  polynomial x^9  + x^5  + 1
Channel 3: PRBS15, polynomial x^15 + x^14 + 1
Channel 4: PRBS23, polynomial x^23 + x^18 + 1

Every test run creates a timestamp-named output directory containing plots,
CSV/JSON data, and REPORT.txt.  Plot generation occurs only after RF capture,
so Matplotlib cannot cause receive-buffer drops during acquisition.

The "eye" plot is a derived QPSK decision eye made from equalized OFDM data
symbols.  OFDM itself does not have the same raw matched-filter eye as a
single-carrier QPSK waveform; this distinction is stated in every report.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import math
import os
import signal as os_signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from scipy import signal

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    from cn0566_ofdm_core import (  # type: ignore
        ACTIVE_BINS,
        CHANNEL_ACTIVE_POSITIONS,
        CHANNEL_DATA_BINS,
        CHANNEL_SIGNED_CARRIERS,
        CN0566_LO_HZ,
        CP_LEN,
        DATA_BINS,
        DATA_OFDM_SYMBOLS,
        LEADING_GUARD_SAMPLES,
        NFFT,
        NOMINAL_XBAND_RF_HZ,
        OFDM_SYMBOL_SAMPLES,
        OCCUPIED_BANDWIDTH_HZ,
        PILOT_BINS,
        PILOT_VALUES,
        PLUTO_IF_HZ,
        PREAMBLE_SYMBOLS,
        PREAMBLE_TIME,
        PROFILE_NAME,
        RF_BANDWIDTH_HZ,
        RX_BUFFER_SAMPLES,
        SAMPLE_RATE,
        SUBCARRIER_SPACING_HZ,
        SUPERFRAME_SAMPLES,
        TOTAL_OFDM_SYMBOLS,
        TRAINING_FREQUENCY,
        TX_DIGITAL_PEAK_DEFAULT,
        TX_DIGITAL_RMS_DEFAULT,
        StreamingOfdmReceiver,
        _normalize_rx_return,
        _qpsk_map,
        configure_hardware,
        profile_summary,
    )
except ImportError as exc:
    raise SystemExit(
        "Run this file from the active CN0566_OFDM_4CH_RPi4_Release directory "
        "so cn0566_ofdm_core.py is importable."
    ) from exc

LOG = logging.getLogger("cn0566-qpsk-pattern-performance")

CHANNEL_COUNT = 4
CARRIERS_PER_CHANNEL = 48
SYMBOLS_PER_CHANNEL_FRAME = DATA_OFDM_SYMBOLS * CARRIERS_PER_CHANNEL
BITS_PER_CHANNEL_FRAME = 2 * SYMBOLS_PER_CHANNEL_FRAME
PATTERN_SET_NAME = "standard_prbs_qpsk_4ch_v1"

PATTERN_DESCRIPTIONS: Tuple[str, ...] = (
    "PRBS7: polynomial x^7 + x^6 + 1, all-ones initial state",
    "PRBS9: polynomial x^9 + x^5 + 1, all-ones initial state",
    "PRBS15: polynomial x^15 + x^14 + 1, all-ones initial state",
    "PRBS23: polynomial x^23 + x^18 + 1, all-ones initial state",
)


# ---------------------------------------------------------------------------
# Deterministic test data
# ---------------------------------------------------------------------------

def repeat_dibit_pattern(dibits: Sequence[Tuple[int, int]], length_bits: int) -> np.ndarray:
    flat = np.array([bit for dibit in dibits for bit in dibit], dtype=np.uint8)
    repeats = int(math.ceil(length_bits / flat.size))
    return np.tile(flat, repeats)[:length_bits].astype(np.uint8, copy=False)


def prbs_bits(order: int, tap: int, length: int) -> np.ndarray:
    """Generate a binary maximal-length-style recurrence for x^order+x^tap+1.

    The recurrence is b[n] = b[n-order] XOR b[n-tap], initialized with all
    ones.  It is deterministic and suitable for repeatable BER measurements.
    """
    if not 1 < tap < order:
        raise ValueError("PRBS requires 1 < tap < order")
    if length <= 0:
        return np.empty(0, dtype=np.uint8)
    out = np.ones(max(order, length), dtype=np.uint8)
    for index in range(order, length):
        out[index] = out[index - order] ^ out[index - tap]
    return out[:length]


def make_channel_bits() -> Tuple[np.ndarray, ...]:
    length = BITS_PER_CHANNEL_FRAME
    return (
        prbs_bits(order=7, tap=6, length=length),
        prbs_bits(order=9, tap=5, length=length),
        prbs_bits(order=15, tap=14, length=length),
        prbs_bits(order=23, tap=18, length=length),
    )


CHANNEL_BITS: Tuple[np.ndarray, ...] = make_channel_bits()
CHANNEL_SYMBOLS: Tuple[np.ndarray, ...] = tuple(_qpsk_map(bits) for bits in CHANNEL_BITS)


# ---------------------------------------------------------------------------
# TX waveform construction
# ---------------------------------------------------------------------------

@dataclass
class TxWaveformInfo:
    frame: np.ndarray
    unscaled_rms: float
    unscaled_peak: float
    output_rms: float
    output_peak: float
    unscaled_papr_db: float
    output_papr_db: float
    clip_fraction: float
    channel_waveforms: Tuple[np.ndarray, ...]


def _channel_time_waveform(channel_index: int, symbols: np.ndarray) -> np.ndarray:
    grid = np.zeros((DATA_OFDM_SYMBOLS, NFFT), dtype=np.complex64)
    grid[:, CHANNEL_DATA_BINS[channel_index]] = symbols.reshape(
        DATA_OFDM_SYMBOLS, CARRIERS_PER_CHANNEL
    )
    scale = NFFT / math.sqrt(float(ACTIVE_BINS.size))
    useful = (np.fft.ifft(grid, axis=1) * scale).astype(np.complex64)
    return np.concatenate((useful[:, -CP_LEN:], useful), axis=1).reshape(-1)


def build_test_superframe(rms_target: float, peak_target: float) -> TxWaveformInfo:
    grid = np.zeros((DATA_OFDM_SYMBOLS, NFFT), dtype=np.complex64)
    grid[:, PILOT_BINS] = PILOT_VALUES
    channel_waveforms: List[np.ndarray] = []
    for channel in range(CHANNEL_COUNT):
        symbols = CHANNEL_SYMBOLS[channel].reshape(DATA_OFDM_SYMBOLS, CARRIERS_PER_CHANNEL)
        grid[:, CHANNEL_DATA_BINS[channel]] = symbols
        channel_waveforms.append(_channel_time_waveform(channel, CHANNEL_SYMBOLS[channel]))

    scale = NFFT / math.sqrt(float(ACTIVE_BINS.size))
    useful = (np.fft.ifft(grid, axis=1) * scale).astype(np.complex64)
    data_time = np.concatenate((useful[:, -CP_LEN:], useful), axis=1).reshape(-1)
    active = np.concatenate((PREAMBLE_TIME, data_time)).astype(np.complex64)

    frame = np.zeros(SUPERFRAME_SAMPLES, dtype=np.complex64)
    frame[LEADING_GUARD_SAMPLES:LEADING_GUARD_SAMPLES + active.size] = active

    magnitude = np.abs(frame)
    unscaled_rms = float(np.sqrt(np.mean(magnitude ** 2)))
    unscaled_peak = float(np.max(magnitude))
    if not np.isfinite(unscaled_rms) or unscaled_rms <= 0:
        raise RuntimeError("invalid generated OFDM RMS")

    frame *= float(rms_target) / unscaled_rms
    magnitude = np.abs(frame)
    clipped = magnitude > float(peak_target)
    clip_fraction = float(np.mean(clipped))
    if np.any(clipped):
        frame[clipped] *= (float(peak_target) / magnitude[clipped]).astype(np.float32)

    output_rms = float(np.sqrt(np.mean(np.abs(frame) ** 2)))
    output_peak = float(np.max(np.abs(frame)))
    unscaled_papr_db = 20.0 * math.log10(max(unscaled_peak / unscaled_rms, 1.0))
    output_papr_db = 20.0 * math.log10(max(output_peak / max(output_rms, 1e-12), 1.0))

    # The per-channel reference waveforms are unit-level reconstructions.  Their
    # shape, not their absolute amplitude, is used for comparison plots.
    return TxWaveformInfo(
        frame=frame.astype(np.complex64, copy=False),
        unscaled_rms=unscaled_rms,
        unscaled_peak=unscaled_peak,
        output_rms=output_rms,
        output_peak=output_peak,
        unscaled_papr_db=unscaled_papr_db,
        output_papr_db=output_papr_db,
        clip_fraction=clip_fraction,
        channel_waveforms=tuple(channel_waveforms),
    )


# ---------------------------------------------------------------------------
# Receiver and metrics
# ---------------------------------------------------------------------------

@dataclass
class ChannelFrameMeasurement:
    channel: int
    symbols: np.ndarray
    expected: np.ndarray
    evm_rms: float
    evm_db: float
    ber: float
    ser: float
    phase_error_std_deg: float
    amplitude_mean: float
    amplitude_std: float
    waveform: np.ndarray


@dataclass
class FrameMeasurement:
    frame_start_global: int
    correlation: float
    cfo_hz: float
    timing_offset_samples: int
    channel_magnitude_mean: float
    valid: bool
    channels: Dict[int, ChannelFrameMeasurement]


@dataclass
class PatternCollector:
    max_frames: int
    max_symbols_per_channel: int
    frames: List[FrameMeasurement] = field(default_factory=list)
    symbols: List[List[np.ndarray]] = field(default_factory=lambda: [[], [], [], []])
    waveforms: List[Optional[np.ndarray]] = field(default_factory=lambda: [None, None, None, None])
    raw_frame: Optional[np.ndarray] = None

    def add(self, measurement: FrameMeasurement, raw_frame: np.ndarray) -> None:
        if len(self.frames) >= self.max_frames:
            return
        self.frames.append(measurement)
        if self.raw_frame is None:
            self.raw_frame = raw_frame.copy()
        for channel, item in measurement.channels.items():
            current = sum(block.size for block in self.symbols[channel])
            if current < self.max_symbols_per_channel:
                remaining = self.max_symbols_per_channel - current
                self.symbols[channel].append(item.symbols[:remaining].copy())
            if self.waveforms[channel] is None:
                self.waveforms[channel] = item.waveform.copy()

    def concatenated_symbols(self, channel: int) -> np.ndarray:
        if not self.symbols[channel]:
            return np.empty(0, dtype=np.complex64)
        return np.concatenate(self.symbols[channel]).astype(np.complex64, copy=False)


def bits_from_symbols(symbols: np.ndarray) -> np.ndarray:
    symbols = np.asarray(symbols).reshape(-1)
    bits = np.empty(symbols.size * 2, dtype=np.uint8)
    bits[0::2] = symbols.real < 0
    bits[1::2] = symbols.imag < 0
    return bits


def qpsk_hard_symbols(symbols: np.ndarray) -> np.ndarray:
    i = np.where(np.real(symbols) >= 0, 1.0, -1.0)
    q = np.where(np.imag(symbols) >= 0, 1.0, -1.0)
    return ((i + 1j * q) / math.sqrt(2.0)).astype(np.complex64)


def channel_measurement(channel: int, symbols: np.ndarray) -> ChannelFrameMeasurement:
    expected = CHANNEL_SYMBOLS[channel]
    if symbols.size != expected.size:
        raise ValueError("received symbol count does not match pattern")
    error = symbols - expected
    evm_rms = math.sqrt(
        float(np.mean(np.abs(error) ** 2))
        / max(float(np.mean(np.abs(expected) ** 2)), 1e-30)
    )
    evm_db = 20.0 * math.log10(max(evm_rms, 1e-12))
    hard_bits = bits_from_symbols(symbols)
    ber = float(np.mean(hard_bits != CHANNEL_BITS[channel]))
    hard_symbols = qpsk_hard_symbols(symbols)
    ser = float(np.mean(np.abs(hard_symbols - expected) > 1e-6))
    phase_error = np.angle(symbols * np.conj(expected))
    phase_error_std_deg = float(np.degrees(np.std(np.unwrap(phase_error))))
    amplitude = np.abs(symbols)
    return ChannelFrameMeasurement(
        channel=channel,
        symbols=symbols.astype(np.complex64, copy=False),
        expected=expected,
        evm_rms=evm_rms,
        evm_db=evm_db,
        ber=ber,
        ser=ser,
        phase_error_std_deg=phase_error_std_deg,
        amplitude_mean=float(np.mean(amplitude)),
        amplitude_std=float(np.std(amplitude)),
        waveform=_channel_time_waveform(channel, symbols),
    )


class PatternOfdmReceiver(StreamingOfdmReceiver):
    """Reuse the release acquisition/equalization path, but decode known patterns."""

    def __init__(
        self,
        collector: PatternCollector,
        correlation_threshold: float,
        decode_mode: str,
    ) -> None:
        super().__init__(
            fec=object(),  # not used by this subclass
            packet_callback=lambda *_: None,
            diagnostics_callback=None,
            correlation_threshold=correlation_threshold,
            decode_mode=decode_mode,
            channel_workers=1,
            metrics_interval=1,
            channel_selector=None,
        )
        self.collector = collector
        self.rejected_pattern_frames = 0

    def _decode_frame(self, preamble_global: int, correlation: float, timing_offset: int) -> bool:
        local = preamble_global - self.buffer_global_start
        count = TOTAL_OFDM_SYMBOLS * OFDM_SYMBOL_SAMPLES
        raw_rx2 = self.buffer_rx2[local:local + count].copy()
        if raw_rx2.size != count:
            return False

        cfo_rx2 = self._estimate_cfo(raw_rx2)
        frequency_rx2 = self._extract_frequency_symbols(raw_rx2, cfo_rx2)

        if self.decode_mode == "mrc":
            raw_rx1 = self.buffer_rx1[local:local + count].copy()
            if raw_rx1.size != count:
                return False
            cfo_rx1 = self._estimate_cfo(raw_rx1)
            cfo = 0.5 * (cfo_rx1 + cfo_rx2)
            frequency_rx1 = self._extract_frequency_symbols(raw_rx1, cfo)
            frequency_rx2 = self._extract_frequency_symbols(raw_rx2, cfo)
            equalized_active, _channel, channel_magnitude = self._equalize_mrc(
                frequency_rx1, frequency_rx2
            )
        else:
            cfo = cfo_rx2
            equalized_active, _channel, channel_magnitude = self._equalize_single(frequency_rx2)

        equalized_active = self._pilot_phase_correct(equalized_active)
        measurements: Dict[int, ChannelFrameMeasurement] = {}
        for channel in range(CHANNEL_COUNT):
            symbols = equalized_active[:, CHANNEL_ACTIVE_POSITIONS[channel]].reshape(-1)
            measurements[channel] = channel_measurement(channel, symbols)

        median_ber = float(np.median([item.ber for item in measurements.values()]))
        median_evm_db = float(np.median([item.evm_db for item in measurements.values()]))
        valid = bool(median_ber < 0.25 and median_evm_db < 0.0)
        if not valid:
            self.rejected_pattern_frames += 1
            return False

        frame = FrameMeasurement(
            frame_start_global=preamble_global - LEADING_GUARD_SAMPLES,
            correlation=float(correlation),
            cfo_hz=float(cfo),
            timing_offset_samples=int(timing_offset),
            channel_magnitude_mean=float(channel_magnitude),
            valid=True,
            channels=measurements,
        )
        self.collector.add(frame, raw_rx2)

        self.stats.frames_detected += 1
        self.stats.frames_decoded += 1
        self.stats.last_correlation = float(correlation)
        self.stats.last_cfo_hz = float(cfo)
        self.stats.last_timing_offset = int(timing_offset)
        self._decoded_frame_times.append(time.monotonic())
        return True


# ---------------------------------------------------------------------------
# Plot helpers
# ---------------------------------------------------------------------------

def downsample(x: np.ndarray, max_points: int) -> np.ndarray:
    x = np.asarray(x)
    if x.size <= max_points:
        return x
    idx = np.linspace(0, x.size - 1, max_points, dtype=np.int64)
    return x[idx]


def align_complex(reference: np.ndarray, measured: np.ndarray) -> np.ndarray:
    n = min(reference.size, measured.size)
    if n == 0:
        return measured
    ref = reference[:n]
    mea = measured[:n]
    denominator = np.vdot(mea, mea)
    gain = np.vdot(mea, ref) / denominator if abs(denominator) > 1e-30 else 1.0 + 0j
    out = measured.copy()
    out[:n] *= gain
    return out


def rrc_taps(beta: float = 0.35, sps: int = 8, span_symbols: int = 8) -> np.ndarray:
    count = sps * span_symbols
    t = np.arange(-count / 2, count / 2 + 1, dtype=np.float64) / sps
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
    return taps.astype(np.float32)


def plot_constellation(symbols: np.ndarray, expected: np.ndarray, path: Path, channel: int) -> None:
    if symbols.size == 0:
        return
    measured = downsample(symbols, 50_000)
    fig, ax = plt.subplots(figsize=(6.4, 6.2))
    ax.scatter(measured.real, measured.imag, s=3, alpha=0.20, label="measured/equalized")
    ideal = np.array([1+1j, 1-1j, -1-1j, -1+1j], dtype=np.complex64) / math.sqrt(2.0)
    ax.scatter(ideal.real, ideal.imag, marker="x", s=100, linewidths=2.2, label="ideal QPSK")
    ax.axhline(0, linewidth=0.8)
    ax.axvline(0, linewidth=0.8)
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, alpha=0.30)
    ax.set_xlabel("In-phase")
    ax.set_ylabel("Quadrature")
    ax.set_title(f"Channel {channel}: {PATTERN_DESCRIPTIONS[channel-1]}\nEqualized QPSK constellation")
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def plot_derived_eye(symbols: np.ndarray, path: Path, channel: int) -> None:
    if symbols.size < 100:
        return
    sps = 8
    source = symbols[:min(symbols.size, 3000)]
    up = np.zeros(source.size * sps, dtype=np.complex64)
    up[::sps] = source
    shaped = np.convolve(up, rrc_taps(sps=sps), mode="same")
    width = 2 * sps
    starts = np.arange(4*sps, shaped.size-width-4*sps, sps)[:500]
    x = np.arange(width) / sps
    fig, axes = plt.subplots(2, 1, figsize=(7.4, 6.4), sharex=True)
    for start in starts:
        segment = shaped[start:start+width]
        axes[0].plot(x, segment.real, linewidth=0.65, alpha=0.09)
        axes[1].plot(x, segment.imag, linewidth=0.65, alpha=0.09)
    axes[0].set_ylabel("I")
    axes[1].set_ylabel("Q")
    axes[1].set_xlabel("Equivalent symbol intervals")
    axes[0].set_title(
        f"Channel {channel}: derived QPSK decision eye\n"
        "Analysis view from equalized OFDM symbols; not a raw OFDM eye"
    )
    for ax in axes:
        ax.grid(True, alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def plot_waveform_comparison(
    original: np.ndarray,
    measured: np.ndarray,
    path: Path,
    title: str,
    milliseconds: float,
) -> None:
    if original.size == 0 or measured.size == 0:
        return
    measured = align_complex(original, measured)
    count = int(min(original.size, measured.size, SAMPLE_RATE * milliseconds / 1000.0))
    if count < 16:
        return
    original = original[:count] / max(float(np.max(np.abs(original[:count]))), 1e-12)
    measured = measured[:count] / max(float(np.max(np.abs(measured[:count]))), 1e-12)
    t_ms = np.arange(count) / SAMPLE_RATE * 1e3
    fig, axes = plt.subplots(2, 1, figsize=(10.2, 6.4), sharex=True)
    axes[0].plot(t_ms, original.real, label="original I", linewidth=0.9)
    axes[0].plot(t_ms, measured.real, label="received/equalized I", linewidth=0.9, alpha=0.72)
    axes[1].plot(t_ms, original.imag, label="original Q", linewidth=0.9)
    axes[1].plot(t_ms, measured.imag, label="received/equalized Q", linewidth=0.9, alpha=0.72)
    axes[0].set_title(title)
    axes[0].set_ylabel("Normalized I")
    axes[1].set_ylabel("Normalized Q")
    axes[1].set_xlabel("Time (ms)")
    for ax in axes:
        ax.grid(True, alpha=0.28)
        ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def compute_psd(waveform: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    waveform = np.asarray(waveform, dtype=np.complex64)
    nperseg = min(4096, waveform.size)
    frequency, psd = signal.welch(
        waveform,
        fs=SAMPLE_RATE,
        nperseg=nperseg,
        noverlap=nperseg // 2,
        return_onesided=False,
        scaling="density",
    )
    order = np.argsort(frequency)
    return frequency[order], psd[order]


def occupied_bandwidth_99(waveform: np.ndarray) -> float:
    frequency, psd = compute_psd(waveform)
    power = np.maximum(psd, 0.0)
    total = float(np.sum(power))
    if total <= 0:
        return float("nan")
    cumulative = np.cumsum(power) / total
    lo = int(np.searchsorted(cumulative, 0.005))
    hi = int(np.searchsorted(cumulative, 0.995))
    return float(frequency[min(hi, frequency.size-1)] - frequency[min(lo, frequency.size-1)])


def plot_psd(original: np.ndarray, received: np.ndarray, path: Path, title: str) -> None:
    if original.size < 1024 or received.size < 1024:
        return
    f_tx, p_tx = compute_psd(original)
    f_rx, p_rx = compute_psd(received)
    fig, ax = plt.subplots(figsize=(9.2, 5.0))
    ax.plot(f_tx/1e6, 10*np.log10(np.maximum(p_tx, 1e-24)), label="original TX")
    ax.plot(f_rx/1e6, 10*np.log10(np.maximum(p_rx, 1e-24)), label="captured/equalized RX", alpha=0.80)
    limit = 0.55 * SAMPLE_RATE / 1e6
    ax.set_xlim(-limit, limit)
    ax.set_xlabel("Frequency relative to Pluto LO (MHz)")
    ax.set_ylabel("PSD (dB/Hz, relative)")
    ax.set_title(title)
    ax.grid(True, alpha=0.30)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def plot_channel_response(frames: Sequence[FrameMeasurement], path: Path) -> None:
    # This test tool currently stores the mean channel magnitude only.  The
    # per-carrier response is already applied by the release equalizer, so this
    # figure summarizes EVM versus carrier group instead of inventing a response.
    if not frames:
        return
    fig, ax = plt.subplots(figsize=(9.0, 4.8))
    for channel in range(CHANNEL_COUNT):
        values = [frame.channels[channel].evm_db for frame in frames if channel in frame.channels]
        ax.plot(np.arange(len(values)), values, linewidth=0.9, label=f"channel {channel+1}")
    ax.set_xlabel("Decoded test frame")
    ax.set_ylabel("EVM (dB)")
    ax.set_title("Per-frame QPSK EVM stability")
    ax.grid(True, alpha=0.30)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Capture, simulation, reports
# ---------------------------------------------------------------------------

def make_output_dir(root: Path, mode: str, label: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label.strip())
    suffix = f"_{safe}" if safe else ""
    out = root / f"{stamp}_{mode}_{PROFILE_NAME}_{PATTERN_SET_NAME}{suffix}"
    out.mkdir(parents=True, exist_ok=False)
    (out / "plots").mkdir()
    (out / "data").mkdir()
    return out


def simulate_capture(tx_frame: np.ndarray, frames: int, snr_db: float, cfo_hz: float) -> List[np.ndarray]:
    rng = np.random.default_rng(0xC0566A55)
    prefix = np.zeros(9137, dtype=np.complex64)
    stream = np.concatenate((prefix, np.tile(tx_frame, frames), np.zeros(RX_BUFFER_SAMPLES, dtype=np.complex64)))
    n = np.arange(stream.size, dtype=np.float64)
    channel = 0.017 * np.exp(1j*0.63)
    delayed = np.convolve(stream, np.array([1.0+0j, 0.21*np.exp(1j*0.9), 0.08*np.exp(-1j*0.5)], dtype=np.complex64), mode="full")[:stream.size]
    received = channel * delayed * np.exp(1j*2*math.pi*cfo_hz*n/SAMPLE_RATE)
    signal_power = float(np.mean(np.abs(received[np.abs(received) > 0])**2))
    noise_power = signal_power / (10**(snr_db/10.0))
    sigma = math.sqrt(noise_power/2.0)
    noise = sigma*(rng.standard_normal(stream.size)+1j*rng.standard_normal(stream.size))
    received = (received + noise).astype(np.complex64)
    return [received[start:start+RX_BUFFER_SAMPLES] for start in range(0, received.size, RX_BUFFER_SAMPLES) if received[start:start+RX_BUFFER_SAMPLES].size]


def capture_hardware(args: argparse.Namespace, tx_frame: np.ndarray) -> Tuple[List[np.ndarray], List[Optional[np.ndarray]], Dict[str, object]]:
    phaser = None
    sdr = None
    rx2_blocks: List[np.ndarray] = []
    rx1_blocks: List[Optional[np.ndarray]] = []
    rx_rms: List[float] = []
    rx_peak: List[float] = []
    stop = False

    def request_stop(*_unused: object) -> None:
        nonlocal stop
        stop = True

    old_int = os_signal.signal(os_signal.SIGINT, request_stop)
    old_term = os_signal.signal(os_signal.SIGTERM, request_stop)
    try:
        phaser, sdr, lock = configure_hardware(args)
        if lock == 0:
            raise RuntimeError("CN0566 PLL is not locked")
        sdr.tx_cyclic_buffer = True
        try:
            sdr.tx_destroy_buffer()
        except Exception:
            pass
        sdr.tx(tx_frame)
        LOG.info("Cyclic deterministic QPSK-OFDM test frame started")

        for _ in range(4):
            _ = sdr.rx()

        started = time.monotonic()
        while not stop and time.monotonic() - started < args.capture_seconds:
            raw = sdr.rx()
            rx2, rx1 = _normalize_rx_return(raw, args.decode_mode)
            rx2 = np.asarray(rx2, dtype=np.complex64).copy()
            rx2_blocks.append(rx2)
            rx1_blocks.append(None if rx1 is None else np.asarray(rx1, dtype=np.complex64).copy())
            rx_rms.append(float(np.sqrt(np.mean(np.abs(rx2)**2))))
            rx_peak.append(float(np.max(np.abs(rx2))))
            if len(rx2_blocks) % 8 == 0:
                LOG.info("Captured %d blocks, %.2f s", len(rx2_blocks), time.monotonic()-started)
        elapsed = time.monotonic() - started
        return rx2_blocks, rx1_blocks, {
            "pll_lock": lock,
            "elapsed_seconds": elapsed,
            "rx_blocks": len(rx2_blocks),
            "rx_samples": int(sum(block.size for block in rx2_blocks)),
            "rx_rms_median": float(np.median(rx_rms)) if rx_rms else float("nan"),
            "rx_peak_max": float(np.max(rx_peak)) if rx_peak else float("nan"),
        }
    finally:
        os_signal.signal(os_signal.SIGINT, old_int)
        os_signal.signal(os_signal.SIGTERM, old_term)
        if sdr is not None:
            try:
                sdr.tx_destroy_buffer()
            except Exception:
                pass
            try:
                sdr.rx_destroy_buffer()
            except Exception:
                pass
            try:
                sdr.tx_hardwaregain_chan0 = -88.0
                sdr.tx_hardwaregain_chan1 = -88.0
            except Exception:
                pass
        del sdr
        del phaser


def analyze_blocks(
    rx2_blocks: Sequence[np.ndarray],
    rx1_blocks: Sequence[Optional[np.ndarray]],
    args: argparse.Namespace,
) -> Tuple[PatternOfdmReceiver, PatternCollector]:
    collector = PatternCollector(
        max_frames=args.max_frames,
        max_symbols_per_channel=args.max_symbols_per_channel,
    )
    receiver = PatternOfdmReceiver(
        collector=collector,
        correlation_threshold=args.correlation_threshold,
        decode_mode=args.decode_mode,
    )
    for index, rx2 in enumerate(rx2_blocks):
        rx1 = rx1_blocks[index] if index < len(rx1_blocks) else None
        receiver.feed(rx2, rx1)
        if len(collector.frames) >= args.max_frames:
            break
    return receiver, collector


def aggregate_channel_rows(collector: PatternCollector) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for channel in range(CHANNEL_COUNT):
        items = [frame.channels[channel] for frame in collector.frames if channel in frame.channels]
        symbols = collector.concatenated_symbols(channel)
        evm = [item.evm_db for item in items]
        ber = [item.ber for item in items]
        ser = [item.ser for item in items]
        phase = [item.phase_error_std_deg for item in items]
        rows.append({
            "channel": channel+1,
            "pattern": PATTERN_DESCRIPTIONS[channel],
            "carrier_count": int(CHANNEL_DATA_BINS[channel].size),
            "carrier_min_khz": float(np.min(CHANNEL_SIGNED_CARRIERS[channel])*SUBCARRIER_SPACING_HZ/1e3),
            "carrier_max_khz": float(np.max(CHANNEL_SIGNED_CARRIERS[channel])*SUBCARRIER_SPACING_HZ/1e3),
            "frames": len(items),
            "symbols": int(symbols.size),
            "evm_median_db": float(np.median(evm)) if evm else float("nan"),
            "evm_p10_db": float(np.percentile(evm, 10)) if evm else float("nan"),
            "evm_p90_db": float(np.percentile(evm, 90)) if evm else float("nan"),
            "ber_mean": float(np.mean(ber)) if ber else float("nan"),
            "ber_max": float(np.max(ber)) if ber else float("nan"),
            "ser_mean": float(np.mean(ser)) if ser else float("nan"),
            "phase_error_std_median_deg": float(np.median(phase)) if phase else float("nan"),
            "snr_from_evm_median_db": float(-np.median(evm)) if evm else float("nan"),
        })
    return rows


def write_data_files(out: Path, collector: PatternCollector, rows: List[Dict[str, object]], capture: Mapping[str, object]) -> None:
    data = out / "data"
    with (data / "channel_summary.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    fields = ["frame", "frame_start_global", "correlation", "cfo_hz", "timing_offset_samples", "channel_magnitude_mean"]
    for channel in range(1, 5):
        fields += [f"ch{channel}_evm_db", f"ch{channel}_ber", f"ch{channel}_ser", f"ch{channel}_phase_std_deg"]
    with (data / "frame_metrics.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, frame in enumerate(collector.frames):
            row: Dict[str, object] = {
                "frame": index,
                "frame_start_global": frame.frame_start_global,
                "correlation": frame.correlation,
                "cfo_hz": frame.cfo_hz,
                "timing_offset_samples": frame.timing_offset_samples,
                "channel_magnitude_mean": frame.channel_magnitude_mean,
            }
            for channel in range(4):
                item = frame.channels[channel]
                row[f"ch{channel+1}_evm_db"] = item.evm_db
                row[f"ch{channel+1}_ber"] = item.ber
                row[f"ch{channel+1}_ser"] = item.ser
                row[f"ch{channel+1}_phase_std_deg"] = item.phase_error_std_deg
            writer.writerow(row)

    with (data / "test_patterns.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["channel", "description", "first_64_dibits"])
        for channel in range(CHANNEL_COUNT):
            bits = CHANNEL_BITS[channel][:128].reshape(-1, 2)
            dibits = " ".join("".join(str(int(v)) for v in pair) for pair in bits)
            writer.writerow([channel+1, PATTERN_DESCRIPTIONS[channel], dibits])

    meta = dict(capture)
    meta.update({
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "profile": PROFILE_NAME,
        "profile_summary": profile_summary(),
        "pattern_set": PATTERN_SET_NAME,
        "sample_rate": SAMPLE_RATE,
        "fft_size": NFFT,
        "cyclic_prefix": CP_LEN,
        "data_ofdm_symbols": DATA_OFDM_SYMBOLS,
        "bits_per_channel_frame": BITS_PER_CHANNEL_FRAME,
    })
    (data / "capture_metadata.json").write_text(json.dumps(meta, indent=2, sort_keys=True)+"\n")

    for channel in range(CHANNEL_COUNT):
        symbols = collector.concatenated_symbols(channel)
        if symbols.size:
            np.savez_compressed(
                data / f"channel_{channel+1}_equalized_symbols.npz",
                measured=symbols,
                expected=CHANNEL_SYMBOLS[channel],
            )


def generate_plots(out: Path, tx_info: TxWaveformInfo, collector: PatternCollector) -> List[str]:
    plots = out / "plots"
    generated: List[str] = []
    if collector.raw_frame is not None:
        original_active = tx_info.frame[LEADING_GUARD_SAMPLES:LEADING_GUARD_SAMPLES + TOTAL_OFDM_SYMBOLS*OFDM_SYMBOL_SAMPLES]
        name = "ofdm_waveform_original_vs_captured.png"
        plot_waveform_comparison(
            original_active,
            collector.raw_frame,
            plots/name,
            "Full OFDM waveform: original TX vs aligned captured RX\n(normalized; raw RX is not channel-equalized)",
            milliseconds=4.0,
        )
        generated.append(name)
        name = "ofdm_power_spectral_density.png"
        plot_psd(original_active, collector.raw_frame, plots/name, "Full OFDM power spectral density")
        generated.append(name)

    for channel in range(CHANNEL_COUNT):
        symbols = collector.concatenated_symbols(channel)
        if symbols.size == 0:
            continue
        name = f"channel_{channel+1}_constellation.png"
        plot_constellation(symbols, CHANNEL_SYMBOLS[channel], plots/name, channel+1)
        generated.append(name)
        name = f"channel_{channel+1}_eye.png"
        plot_derived_eye(symbols, plots/name, channel+1)
        generated.append(name)
        measured_waveform = collector.waveforms[channel]
        if measured_waveform is not None:
            name = f"channel_{channel+1}_waveform_original_vs_received.png"
            plot_waveform_comparison(
                tx_info.channel_waveforms[channel],
                measured_waveform,
                plots/name,
                f"Channel {channel+1}: original QPSK carrier-group waveform vs equalized received reconstruction",
                milliseconds=5.0,
            )
            generated.append(name)
            name = f"channel_{channel+1}_power_spectral_density.png"
            plot_psd(
                tx_info.channel_waveforms[channel],
                measured_waveform,
                plots/name,
                f"Channel {channel+1}: interleaved carrier-group power spectral density",
            )
            generated.append(name)

    name = "evm_stability.png"
    plot_channel_response(collector.frames, plots/name)
    generated.append(name)
    return generated


def write_report(
    out: Path,
    args: argparse.Namespace,
    tx_info: TxWaveformInfo,
    receiver: PatternOfdmReceiver,
    collector: PatternCollector,
    rows: List[Dict[str, object]],
    capture: Mapping[str, object],
    generated: Sequence[str],
) -> None:
    correlations = [frame.correlation for frame in collector.frames]
    cfos = [frame.cfo_hz for frame in collector.frames]
    elapsed = float(capture.get("elapsed_seconds", float("nan")))
    if len(collector.frames) >= 2:
        frame_span_samples = (
            collector.frames[-1].frame_start_global - collector.frames[0].frame_start_global
        )
        frame_rate = (
            (len(collector.frames) - 1) * SAMPLE_RATE / frame_span_samples
            if frame_span_samples > 0 else float("nan")
        )
    else:
        frame_rate = float("nan")
    rx_raw = collector.raw_frame if collector.raw_frame is not None else np.empty(0, dtype=np.complex64)
    tx_active = tx_info.frame[LEADING_GUARD_SAMPLES:LEADING_GUARD_SAMPLES + TOTAL_OFDM_SYMBOLS*OFDM_SYMBOL_SAMPLES]

    lines = [
        "CN0566 / PLUTO / RASPBERRY PI QPSK-OFDM PHY PERFORMANCE REPORT",
        "="*72,
        f"Run directory: {out}",
        f"Mode: {args.mode}",
        f"Profile: {profile_summary()}",
        f"Pattern set: {PATTERN_SET_NAME}",
        "",
        "Purpose",
        "  This test uses deterministic PHY patterns, not sample_*.wav files.",
        "  It measures the four interleaved logical QPSK carrier groups directly.",
        "",
        "Four channel test sequences",
    ]
    for channel, description in enumerate(PATTERN_DESCRIPTIONS, start=1):
        lines.append(f"  Channel {channel}: {description}")
    lines += [
        "",
        "OFDM profile",
        f"  Sample rate: {SAMPLE_RATE} sample/s",
        f"  FFT / cyclic prefix: {NFFT} / {CP_LEN}",
        f"  OFDM symbol samples: {OFDM_SYMBOL_SAMPLES}",
        f"  Total/data OFDM symbols per 20 ms frame: {TOTAL_OFDM_SYMBOLS}/{DATA_OFDM_SYMBOLS}",
        f"  Data carriers / pilots: {DATA_BINS.size}/{PILOT_BINS.size}",
        f"  Data carriers per logical channel: {CARRIERS_PER_CHANNEL}",
        f"  Subcarrier spacing: {SUBCARRIER_SPACING_HZ:.3f} Hz",
        f"  Configured occupied bandwidth: {OCCUPIED_BANDWIDTH_HZ/1e6:.6f} MHz",
        f"  Pluto RF bandwidth: {RF_BANDWIDTH_HZ/1e6:.6f} MHz",
        f"  IF / nominal X-band center: {PLUTO_IF_HZ/1e9:.6f} / {NOMINAL_XBAND_RF_HZ/1e9:.6f} GHz",
        "",
        "TX waveform",
        f"  Target RMS / peak: {args.tx_digital_rms:.1f} / {args.tx_digital_peak:.1f}",
        f"  Actual RMS / peak: {tx_info.output_rms:.3f} / {tx_info.output_peak:.3f}",
        f"  Output PAPR: {tx_info.output_papr_db:.3f} dB",
        f"  Limited sample fraction: {tx_info.clip_fraction*100:.6f}%",
        f"  TX 99% occupied bandwidth estimate: {occupied_bandwidth_99(tx_active)/1e6:.6f} MHz",
        "",
        "Hardware/capture",
        f"  CN0566 URI / Pluto URI: {args.phaser_uri} / {args.sdr_uri}",
        f"  TX gain / RX gain / array gain: {args.tx_gain:.1f} dB / {args.rx_gain:.1f} dB / {args.array_gain}",
        f"  Beam phase difference: {args.beam_phase_diff:.3f} degrees",
        f"  Decode mode: {args.decode_mode}",
        f"  PLL lock: {capture.get('pll_lock', 'simulation')}",
        f"  Capture elapsed: {elapsed:.3f} s",
        f"  RX blocks / samples: {capture.get('rx_blocks', 'n/a')} / {capture.get('rx_samples', 'n/a')}",
        f"  RX RMS median / peak max: {float(capture.get('rx_rms_median', float('nan'))):.3f} / {float(capture.get('rx_peak_max', float('nan'))):.3f}",
        f"  RX 99% occupied bandwidth estimate: {occupied_bandwidth_99(rx_raw)/1e6:.6f} MHz" if rx_raw.size else "  RX 99% occupied bandwidth estimate: unavailable",
        "",
        "Frame acquisition",
        f"  Accepted test frames: {len(collector.frames)}",
        f"  Rejected pattern candidates: {receiver.rejected_pattern_frames}",
        f"  Accepted frame rate over capture: {frame_rate:.3f} frame/s",
        f"  Correlation median/min/max: {np.median(correlations):.4f}/{np.min(correlations):.4f}/{np.max(correlations):.4f}" if correlations else "  Correlation: unavailable",
        f"  CFO median/std: {np.median(cfos):+.3f}/{np.std(cfos):.3f} Hz" if cfos else "  CFO: unavailable",
        "",
        "Per-channel results",
    ]
    for row in rows:
        lines += [
            f"  Channel {row['channel']}: {row['pattern']}",
            f"    Frames / symbols: {row['frames']} / {row['symbols']}",
            f"    Carrier span: {row['carrier_min_khz']:+.3f} to {row['carrier_max_khz']:+.3f} kHz",
            f"    EVM median (p10/p90): {row['evm_median_db']:.3f} dB ({row['evm_p10_db']:.3f}/{row['evm_p90_db']:.3f})",
            f"    BER mean/max: {row['ber_mean']:.6e} / {row['ber_max']:.6e}",
            f"    SER mean: {row['ser_mean']:.6e}",
            f"    Median phase-error standard deviation: {row['phase_error_std_median_deg']:.3f} degrees",
            f"    SNR estimate from EVM: {row['snr_from_evm_median_db']:.3f} dB",
        ]
    lines += [
        "",
        "Generated plots",
    ]
    lines.extend(f"  plots/{name}" for name in generated)
    lines += [
        "",
        "Interpretation and limitations",
        "  - BER and SER are measured against deterministic known QPSK patterns.",
        "  - EVM is calculated after training-based equalization and pilot phase correction.",
        "  - SNR from EVM is an approximation and assumes residual error behaves like uncorrelated noise.",
        "  - PSD values are relative, not calibrated RF power measurements.",
        "  - Waveform comparisons are normalized and complex-gain aligned for shape comparison.",
        "  - The eye plots are derived QPSK decision eyes. OFDM does not have the same raw pulse-shaped eye as single-carrier QPSK.",
        "  - This PHY test does not exercise audio coding, packet FEC, jitter buffers, or ALSA.",
    ]
    (out/"REPORT.txt").write_text("\n".join(lines)+"\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic four-channel QPSK-OFDM PHY performance test for CN0566/Pluto"
    )
    parser.add_argument("--mode", choices=("hardware", "simulate"), default="hardware")
    parser.add_argument("--enable-tx", action="store_true", help="required for hardware RF transmission")
    parser.add_argument("--output-root", type=Path, default=Path("qpsk_pattern_performance_runs"))
    parser.add_argument("--label", default="")
    parser.add_argument("--capture-seconds", type=float, default=8.0)
    parser.add_argument("--simulation-frames", type=int, default=120)
    parser.add_argument("--simulation-snr-db", type=float, default=22.0)
    parser.add_argument("--simulation-cfo-hz", type=float, default=120.0)
    parser.add_argument("--max-frames", type=int, default=300)
    parser.add_argument("--max-symbols-per-channel", type=int, default=200_000)
    parser.add_argument("--phaser-uri", default="ip:localhost")
    parser.add_argument("--sdr-uri", default="ip:192.168.2.1")
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
    if args.mode == "hardware" and not args.enable_tx:
        raise ValueError("hardware mode requires --enable-tx")
    if args.capture_seconds <= 0:
        raise ValueError("--capture-seconds must be positive")
    if args.simulation_frames <= 0:
        raise ValueError("--simulation-frames must be positive")
    if args.max_frames <= 0 or args.max_symbols_per_channel <= 0:
        raise ValueError("analysis limits must be positive")
    if not -88.0 <= args.tx_gain <= 0.0:
        raise ValueError("--tx-gain must be in [-88, 0] dB")
    if not -3.0 <= args.rx_gain <= 71.0:
        raise ValueError("--rx-gain should be in [-3, 71] dB at 2.2 GHz")
    if not 0 <= args.array_gain <= 127:
        raise ValueError("--array-gain must be in [0, 127]")
    if not 0.05 <= args.correlation_threshold <= 0.95:
        raise ValueError("--correlation-threshold must be in [0.05, 0.95]")
    if not 0 < args.tx_digital_rms < args.tx_digital_peak <= 12000:
        raise ValueError("require 0 < RMS < peak <= 12000")


def main() -> int:
    args = build_parser().parse_args()
    validate_args(args)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
    )
    out = make_output_dir(args.output_root, args.mode, args.label)
    LOG.info("Output directory: %s", out)
    LOG.info("Profile: %s", profile_summary())
    for channel, description in enumerate(PATTERN_DESCRIPTIONS, start=1):
        LOG.info("Channel %d test pattern: %s", channel, description)

    tx_info = build_test_superframe(args.tx_digital_rms, args.tx_digital_peak)
    LOG.info(
        "Test frame: %d samples, RMS=%.1f, peak=%.1f, clip=%.5f%%, PAPR=%.2f dB",
        tx_info.frame.size,
        tx_info.output_rms,
        tx_info.output_peak,
        tx_info.clip_fraction*100.0,
        tx_info.output_papr_db,
    )

    if args.mode == "hardware":
        rx2_blocks, rx1_blocks, capture = capture_hardware(args, tx_info.frame)
    else:
        rx2_blocks = simulate_capture(
            tx_info.frame,
            frames=args.simulation_frames,
            snr_db=args.simulation_snr_db,
            cfo_hz=args.simulation_cfo_hz,
        )
        rx1_blocks = [None] * len(rx2_blocks)
        all_rx = np.concatenate(rx2_blocks) if rx2_blocks else np.empty(0, dtype=np.complex64)
        capture = {
            "pll_lock": "simulation",
            "elapsed_seconds": args.simulation_frames * SUPERFRAME_SAMPLES / SAMPLE_RATE,
            "rx_blocks": len(rx2_blocks),
            "rx_samples": int(all_rx.size),
            "rx_rms_median": float(np.sqrt(np.mean(np.abs(all_rx)**2))) if all_rx.size else float("nan"),
            "rx_peak_max": float(np.max(np.abs(all_rx))) if all_rx.size else float("nan"),
        }

    receiver, collector = analyze_blocks(rx2_blocks, rx1_blocks, args)
    if not collector.frames:
        write_data_files(out, collector, aggregate_channel_rows(collector), capture)
        (out/"REPORT.txt").write_text(
            "No valid QPSK test frames were decoded.\n"
            f"Last detector peak: {receiver.stats.last_detector_peak:.6f}\n"
            f"Threshold: {args.correlation_threshold:.6f}\n"
            "Check RF signal level, profile selection, and spectral orientation.\n"
        )
        raise RuntimeError(f"No valid test frames decoded; see {out/'REPORT.txt'}")

    rows = aggregate_channel_rows(collector)
    write_data_files(out, collector, rows, capture)
    generated = generate_plots(out, tx_info, collector)
    write_report(out, args, tx_info, receiver, collector, rows, capture, generated)

    LOG.info("Accepted %d deterministic QPSK test frames", len(collector.frames))
    for row in rows:
        LOG.info(
            "ch%d: EVM=%.2f dB BER=%.3e SER=%.3e symbols=%d",
            row["channel"], row["evm_median_db"], row["ber_mean"], row["ser_mean"], row["symbols"],
        )
    LOG.info("Report: %s", out/"REPORT.txt")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        LOG.info("Interrupted")
        raise SystemExit(130)
    except Exception as exc:
        LOG.exception("Fatal error: %s", exc)
        raise SystemExit(1)
