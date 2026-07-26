#!/usr/bin/env python3
"""Core modem/runtime support for four-stream QPSK-OFDM on CN0566 + Pluto.

This module implements selectable four-stream profiles.  The default is the
measured-stable 1 MSPS profile; 1.5 and 2 MSPS remain controlled benchmarks:

* default Pluto sample rate: 1 MSPS
* 256-point OFDM FFT
* 32-sample cyclic prefix
* 192 QPSK data subcarriers, interleaved into four 48-carrier logical channels
* 16 BPSK pilots
* two identical training symbols per 20 ms superframe
* rate-1/2, K=7 convolutional coding with a native C codec
* one 20 ms independent IMA-ADPCM packet per logical audio channel

Hardware profile:
    Pluto TX2A -> CN0566 TX_IN
    CN0566 TX_OUT_2 -> external Vivaldi antenna
    CN0566 RX1 -> Pluto RX1A
    CN0566 RX2 -> Pluto RX2A

The real-time default is RX2-only. Optional MRC is retained for controlled
experimentation after RX2-only operation is stable.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import ctypes
import dataclasses
import logging
import math
import os
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import numpy as np
from scipy import signal
from scipy import fft as scipy_fft
from scipy.io import wavfile

LOG = logging.getLogger("cn0566-ofdm")

# ---------------------------------------------------------------------------
# Selectable OFDM profiles
# ---------------------------------------------------------------------------

# The Pi-side benchmark is the acceptance test for a profile.  The measured
# 2 MSPS unlocked detector needed about 0.106--0.126 s for each 0.065536 s RX
# block on the user's Raspberry Pi 4, so 2 MSPS is not the default.  The stable
# 1 MSPS profile still carries all four protected ADPCM streams.
PROFILE_NAME = os.environ.get("CN0566_OFDM_PROFILE", "stable_1m").strip().lower()
_PROFILE_TABLE = {
    "stable_1m": {
        "sample_rate": 1_000_000,
        "leading_guard": 64,
        "trailing_guard": 64,
        "rf_bandwidth": 1_250_000,
        "rx_buffer_samples": 131_072,
        "acquisition_stride": 2,
        "description": "stable four-stream profile",
    },
    "balanced_1p5m": {
        "sample_rate": 1_500_000,
        "leading_guard": 24,
        "trailing_guard": 24,
        "rf_bandwidth": 1_800_000,
        "rx_buffer_samples": 131_072,
        "acquisition_stride": 2,
        "description": "intermediate benchmark profile",
    },
    "high_2m": {
        "sample_rate": 2_000_000,
        "leading_guard": 128,
        "trailing_guard": 128,
        "rf_bandwidth": 2_500_000,
        "rx_buffer_samples": 131_072,
        "acquisition_stride": 3,
        "description": "experimental high-rate profile",
    },
}
if PROFILE_NAME not in _PROFILE_TABLE:
    raise RuntimeError(
        f"Unknown CN0566_OFDM_PROFILE={PROFILE_NAME!r}; choose "
        + ", ".join(sorted(_PROFILE_TABLE))
    )
_PROFILE = _PROFILE_TABLE[PROFILE_NAME]

SAMPLE_RATE = int(_PROFILE["sample_rate"])
NFFT = 256
CP_LEN = 32
OFDM_SYMBOL_SAMPLES = NFFT + CP_LEN
SUPERFRAME_SECONDS = 0.020
SUPERFRAME_SAMPLES = int(round(SAMPLE_RATE * SUPERFRAME_SECONDS))
LEADING_GUARD_SAMPLES = int(_PROFILE["leading_guard"])
TRAILING_GUARD_SAMPLES = int(_PROFILE["trailing_guard"])
TOTAL_OFDM_SYMBOLS = (
    SUPERFRAME_SAMPLES - LEADING_GUARD_SAMPLES - TRAILING_GUARD_SAMPLES
) // OFDM_SYMBOL_SAMPLES
PREAMBLE_SYMBOLS = 2
DATA_OFDM_SYMBOLS = TOTAL_OFDM_SYMBOLS - PREAMBLE_SYMBOLS
if DATA_OFDM_SYMBOLS <= 0:
    raise RuntimeError("selected OFDM profile has no data symbols")
if (
    LEADING_GUARD_SAMPLES
    + TOTAL_OFDM_SYMBOLS * OFDM_SYMBOL_SAMPLES
    + TRAILING_GUARD_SAMPLES
    != SUPERFRAME_SAMPLES
):
    raise RuntimeError("OFDM profile does not exactly fill the 20 ms superframe")

SUBCARRIER_SPACING_HZ = SAMPLE_RATE / NFFT
SIGNED_ACTIVE_CARRIERS = np.concatenate(
    (np.arange(-108, -4, dtype=np.int16), np.arange(5, 109, dtype=np.int16))
)
PILOT_SIGNED_CARRIERS = np.array(
    [-100, -87, -74, -61, -48, -35, -22, -9, 9, 22, 35, 48, 61, 74, 87, 100],
    dtype=np.int16,
)
DATA_SIGNED_CARRIERS = np.array(
    [value for value in SIGNED_ACTIVE_CARRIERS.tolist() if value not in set(PILOT_SIGNED_CARRIERS.tolist())],
    dtype=np.int16,
)
assert SIGNED_ACTIVE_CARRIERS.size == 208
assert PILOT_SIGNED_CARRIERS.size == 16
assert DATA_SIGNED_CARRIERS.size == 192

ACTIVE_BINS = np.mod(SIGNED_ACTIVE_CARRIERS, NFFT).astype(np.int16)
PILOT_BINS = np.mod(PILOT_SIGNED_CARRIERS, NFFT).astype(np.int16)
DATA_BINS = np.mod(DATA_SIGNED_CARRIERS, NFFT).astype(np.int16)

# Four interleaved logical channels. Each channel's carriers span the occupied band.
CHANNEL_DATA_BINS: Tuple[np.ndarray, ...] = tuple(DATA_BINS[index::4] for index in range(4))
CHANNEL_SIGNED_CARRIERS: Tuple[np.ndarray, ...] = tuple(DATA_SIGNED_CARRIERS[index::4] for index in range(4))
assert all(carriers.size == 48 for carriers in CHANNEL_DATA_BINS)

# Positions within ACTIVE_BINS are fixed; precompute them once.
_ACTIVE_POSITION_BY_BIN = {int(bin_index): position for position, bin_index in enumerate(ACTIVE_BINS.tolist())}
PILOT_ACTIVE_POSITIONS = np.array(
    [_ACTIVE_POSITION_BY_BIN[int(bin_index)] for bin_index in PILOT_BINS], dtype=np.int16
)
CHANNEL_ACTIVE_POSITIONS: Tuple[np.ndarray, ...] = tuple(
    np.array([_ACTIVE_POSITION_BY_BIN[int(bin_index)] for bin_index in bins], dtype=np.int16)
    for bins in CHANNEL_DATA_BINS
)

OCCUPIED_BANDWIDTH_HZ = 2.0 * np.max(np.abs(SIGNED_ACTIVE_CARRIERS)) * SUBCARRIER_SPACING_HZ
RF_BANDWIDTH_HZ = int(_PROFILE["rf_bandwidth"])
PLUTO_IF_HZ = 2_200_000_000
CN0566_LO_HZ = 12_400_000_000
NOMINAL_XBAND_RF_HZ = CN0566_LO_HZ - PLUTO_IF_HZ
RX_BUFFER_SAMPLES = int(_PROFILE["rx_buffer_samples"])
RX_QUEUE_BUFFERS = 32
ACQUISITION_BLOCK_STRIDE = int(_PROFILE["acquisition_stride"])
TX_SUPERFRAMES_PER_BUFFER = 50
TX_PREFETCH_CHUNKS = 4
TX_INITIAL_PREFILL_CHUNKS = 2
TX_DIGITAL_RMS_DEFAULT = 2_000.0
TX_DIGITAL_PEAK_DEFAULT = 6_000.0
RF_SANITY_TONE_HZ = SAMPLE_RATE / 8.0

# Raspberry Pi realtime controls.  Four independent K=7 Viterbi decoders are
# the dominant locked-receiver cost.  The native decoder releases the GIL and
# uses thread-local workspaces, so decoding the four logical channels in
# parallel uses the Pi 4 cores rather than serializing all channels on one core.
CHANNEL_DECODE_WORKERS = max(
    1, min(4, int(os.environ.get("CN0566_OFDM_CHANNEL_WORKERS", "4")))
)
LIVE_METRICS_INTERVAL = max(
    1, int(os.environ.get("CN0566_OFDM_METRICS_INTERVAL", "5"))
)
AUDIO_WRITE_FRAMES = max(
    1, int(os.environ.get("CN0566_OFDM_AUDIO_WRITE_FRAMES", "10"))
)
DECODE_SCOPE_DEFAULT = os.environ.get(
    "CN0566_OFDM_DECODE_SCOPE", "selected"
).strip().lower()
if DECODE_SCOPE_DEFAULT not in {"selected", "all"}:
    raise RuntimeError(
        "CN0566_OFDM_DECODE_SCOPE must be selected or all"
    )
_CHANNEL_DECODE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=CHANNEL_DECODE_WORKERS,
    thread_name_prefix="ofdm-channel",
)

PLUTO_TX_COMPLEX_CHANNEL = 1  # physical TX2A
PLUTO_RX1_COMPLEX_CHANNEL = 0
PLUTO_RX2_COMPLEX_CHANNEL = 1
CN0566_TX_OUTPUT_NUMBER = 2
CN0566_TX_SWITCH_GPIO_VALUE = 0
PORT_PROFILE_DESCRIPTION = (
    "Pluto TX2A -> CN0566 TX_IN; CN0566 TX_OUT_2 -> Vivaldi; "
    "CN0566 RX1 -> Pluto RX1A; CN0566 RX2 -> Pluto RX2A"
)

# ---------------------------------------------------------------------------
# Audio, packet and FEC profile
# ---------------------------------------------------------------------------

AUDIO_SAMPLE_RATE = 16_000
AUDIO_FRAME_SAMPLES = 320
AUDIO_FRAME_SECONDS = AUDIO_FRAME_SAMPLES / AUDIO_SAMPLE_RATE
assert abs(AUDIO_FRAME_SECONDS - SUPERFRAME_SECONDS) < 1e-12

ADPCM_HEADER_BYTES = 4
ADPCM_CODE_BYTES = math.ceil((AUDIO_FRAME_SAMPLES - 1) / 2)
AUDIO_PAYLOAD_BYTES = ADPCM_HEADER_BYTES + ADPCM_CODE_BYTES  # 164

MAGIC = b"OF"
PROTOCOL_VERSION = 1
# magic, version, stream id, sequence, valid samples, flags, payload length
PACKET_HEADER = struct.Struct(">2sBBHHBH")
CRC_STRUCT = struct.Struct(">I")
PACKET_HEADER_BYTES = PACKET_HEADER.size  # 11
PACKET_BYTES = PACKET_HEADER_BYTES + AUDIO_PAYLOAD_BYTES + CRC_STRUCT.size  # 179
PACKET_BITS = PACKET_BYTES * 8  # 1432

FLAG_START_OF_FILE = 0x01
FLAG_END_OF_FILE = 0x02
FLAG_IDLE = 0x04

CONSTRAINT_LENGTH = 7
CONV_TAIL_BITS = CONSTRAINT_LENGTH - 1
CONV_G0 = 0o133
CONV_G1 = 0o171
CODED_PACKET_BITS = 2 * (PACKET_BITS + CONV_TAIL_BITS)  # 2876
CHANNEL_CODED_BITS_PER_FRAME = DATA_OFDM_SYMBOLS * 48 * 2  # 13056
CHANNEL_PRBS_BITS = CHANNEL_CODED_BITS_PER_FRAME - CODED_PACKET_BITS
assert CHANNEL_PRBS_BITS > 0

# Data-carrier capacity of the selected profile.
RAW_DATA_CARRIER_BPS = DATA_BINS.size * 2 * SAMPLE_RATE / OFDM_SYMBOL_SAMPLES
NET_RATE_HALF_FEC_BPS = DATA_OFDM_SYMBOLS * DATA_BINS.size * 2 * 0.5 / SUPERFRAME_SECONDS

# ---------------------------------------------------------------------------
# Deterministic training, pilots and PRBS
# ---------------------------------------------------------------------------

def _qpsk_map(bits: np.ndarray) -> np.ndarray:
    bits = np.asarray(bits, dtype=np.uint8).reshape(-1)
    if bits.size % 2:
        raise ValueError("QPSK mapping requires an even number of bits")
    i = 1.0 - 2.0 * bits[0::2].astype(np.float32)
    q = 1.0 - 2.0 * bits[1::2].astype(np.float32)
    return ((i + 1j * q) / math.sqrt(2.0)).astype(np.complex64)


def _bits_from_symbols(symbols: np.ndarray) -> np.ndarray:
    symbols = np.asarray(symbols)
    bits = np.empty(symbols.size * 2, dtype=np.uint8)
    bits[0::2] = np.real(symbols).reshape(-1) < 0
    bits[1::2] = np.imag(symbols).reshape(-1) < 0
    return bits


def _pilot_matrix() -> np.ndarray:
    rng = np.random.default_rng(0xC0566F01)
    bits = rng.integers(0, 2, size=(DATA_OFDM_SYMBOLS, PILOT_BINS.size), dtype=np.uint8)
    return (1.0 - 2.0 * bits.astype(np.float32)).astype(np.complex64)


def _training_frequency_symbol() -> np.ndarray:
    rng = np.random.default_rng(0xC0566F02)
    frequency = np.zeros(NFFT, dtype=np.complex64)
    bits = rng.integers(0, 2, size=ACTIVE_BINS.size, dtype=np.uint8)
    frequency[ACTIVE_BINS] = 1.0 - 2.0 * bits.astype(np.float32)
    return frequency


def _ofdm_time_symbol(frequency: np.ndarray) -> np.ndarray:
    # Unit-RMS normalization when all 208 active carriers have unit magnitude.
    scale = NFFT / math.sqrt(float(ACTIVE_BINS.size))
    useful = (np.fft.ifft(frequency, n=NFFT) * scale).astype(np.complex64)
    return np.concatenate((useful[-CP_LEN:], useful)).astype(np.complex64)


PILOT_VALUES = _pilot_matrix()
TRAINING_FREQUENCY = _training_frequency_symbol()
TRAINING_TIME = _ofdm_time_symbol(TRAINING_FREQUENCY)
PREAMBLE_TIME = np.concatenate((TRAINING_TIME, TRAINING_TIME)).astype(np.complex64)
PREAMBLE_ENERGY = float(np.vdot(PREAMBLE_TIME, PREAMBLE_TIME).real)

CHANNEL_PRBS: Tuple[np.ndarray, ...] = tuple(
    np.random.default_rng(0xC056600 + channel).integers(
        0, 2, size=CHANNEL_PRBS_BITS, dtype=np.uint8
    )
    for channel in range(4)
)
CHANNEL_PRBS_SYMBOLS: Tuple[np.ndarray, ...] = tuple(_qpsk_map(bits) for bits in CHANNEL_PRBS)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class AudioPacket:
    stream_id: int
    sequence: int
    valid_samples: int
    flags: int
    payload: bytes
    raw: bytes


@dataclasses.dataclass
class ChannelFrameDiagnostics:
    stream_id: int
    packet_valid: bool = False
    sequence: Optional[int] = None
    evm_db: float = float("nan")
    raw_prbs_ber: float = float("nan")
    equalized_symbols: Optional[np.ndarray] = None
    expected_symbols: Optional[np.ndarray] = None
    reconstructed_waveform: Optional[np.ndarray] = None


@dataclasses.dataclass
class ChannelDecodeResult:
    stream_id: int
    packet_valid: bool
    packet: Optional[AudioPacket]
    pcm: Optional[np.ndarray]
    evm_db: float
    raw_prbs_ber: float
    equalized_symbols: Optional[np.ndarray]
    reconstructed_waveform: Optional[np.ndarray]


@dataclasses.dataclass
class OfdmFrameDiagnostics:
    frame_start_global: int
    correlation: float
    cfo_hz: float
    timing_offset_samples: int
    channel_magnitude_mean: float
    noise_evm_db: float
    channels: Dict[int, ChannelFrameDiagnostics]


@dataclasses.dataclass
class ReceiverStatistics:
    frames_detected: int = 0
    frames_decoded: int = 0
    acquisition_searches: int = 0
    tracking_searches: int = 0
    tracking_misses: int = 0
    reacquisitions: int = 0
    queue_drops: int = 0
    last_correlation: float = 0.0
    last_detector_peak: float = 0.0
    last_cfo_hz: float = float("nan")
    last_timing_offset: int = 0
    last_rx_rms: float = 0.0
    last_rx_peak: float = 0.0
    packet_valid: List[int] = dataclasses.field(default_factory=lambda: [0, 0, 0, 0])
    packet_fail: List[int] = dataclasses.field(default_factory=lambda: [0, 0, 0, 0])
    sequence_gaps: List[int] = dataclasses.field(default_factory=lambda: [0, 0, 0, 0])
    last_sequence: List[Optional[int]] = dataclasses.field(default_factory=lambda: [None, None, None, None])
    last_evm_db: List[float] = dataclasses.field(default_factory=lambda: [float("nan")] * 4)
    last_prbs_ber: List[float] = dataclasses.field(default_factory=lambda: [float("nan")] * 4)


@dataclasses.dataclass
class RuntimeStatistics:
    rx_queue_drops: int = 0
    tx_starves: int = 0
    tx_chunks_pushed: int = 0
    tx_chunks_built: int = 0
    tx_queue_depth: int = 0
    audio_concealed: List[int] = dataclasses.field(default_factory=lambda: [0, 0, 0, 0])
    audio_drops: int = 0

# ---------------------------------------------------------------------------
# IMA ADPCM codec: independent 20 ms blocks
# ---------------------------------------------------------------------------

_IMA_INDEX_TABLE = np.array(
    [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8],
    dtype=np.int16,
)
_NATIVE_ADPCM_LIBRARY: Optional[ctypes.CDLL] = None

_IMA_STEP_TABLE = np.array(
    [
        7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
        34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
        130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371,
        408, 449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166,
        1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024,
        3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845,
        8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500,
        20350, 22385, 24623, 27086, 29794, 32767,
    ],
    dtype=np.int32,
)


def _ima_encode_nibble(sample: int, predictor: int, index: int) -> Tuple[int, int, int]:
    step = int(_IMA_STEP_TABLE[index])
    difference = int(sample) - predictor
    code = 0
    if difference < 0:
        code = 8
        difference = -difference

    delta = step >> 3
    if difference >= step:
        code |= 4
        difference -= step
        delta += step
    if difference >= (step >> 1):
        code |= 2
        difference -= step >> 1
        delta += step >> 1
    if difference >= (step >> 2):
        code |= 1
        delta += step >> 2

    predictor = predictor - delta if (code & 8) else predictor + delta
    predictor = max(-32768, min(32767, predictor))
    index = max(0, min(88, index + int(_IMA_INDEX_TABLE[code & 0x0F])))
    return code & 0x0F, predictor, index


def _ima_decode_nibble(code: int, predictor: int, index: int) -> Tuple[int, int]:
    step = int(_IMA_STEP_TABLE[index])
    delta = step >> 3
    if code & 4:
        delta += step
    if code & 2:
        delta += step >> 1
    if code & 1:
        delta += step >> 2
    predictor = predictor - delta if (code & 8) else predictor + delta
    predictor = max(-32768, min(32767, predictor))
    index = max(0, min(88, index + int(_IMA_INDEX_TABLE[code & 0x0F])))
    return predictor, index


def ima_adpcm_encode_block(samples: np.ndarray) -> bytes:
    pcm = np.ascontiguousarray(np.asarray(samples, dtype=np.int16).reshape(-1))
    if pcm.size != AUDIO_FRAME_SAMPLES:
        raise ValueError(f"IMA block requires {AUDIO_FRAME_SAMPLES} samples")
    if _NATIVE_ADPCM_LIBRARY is not None:
        output = np.empty(AUDIO_PAYLOAD_BYTES, dtype=np.uint8)
        result = _NATIVE_ADPCM_LIBRARY.cn0566_ima_adpcm_encode(
            pcm.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            int(pcm.size),
            output.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
            int(output.size),
        )
        if result != AUDIO_PAYLOAD_BYTES:
            raise RuntimeError(f"native IMA-ADPCM encoder returned {result}")
        return output.tobytes()

    predictor = int(pcm[0])
    index = 0
    nibbles: List[int] = []
    for sample in pcm[1:]:
        code, predictor, index = _ima_encode_nibble(int(sample), predictor, index)
        nibbles.append(code)

    packed = bytearray(ADPCM_CODE_BYTES)
    for i, code in enumerate(nibbles):
        if i & 1:
            packed[i // 2] |= (code & 0x0F) << 4
        else:
            packed[i // 2] = code & 0x0F
    header = struct.pack("<hBB", int(pcm[0]), 0, 0)
    return header + bytes(packed)


def ima_adpcm_decode_block(payload: bytes, valid_samples: int) -> np.ndarray:
    if len(payload) != AUDIO_PAYLOAD_BYTES:
        raise ValueError("invalid IMA ADPCM payload length")
    valid_samples = max(0, min(AUDIO_FRAME_SAMPLES, int(valid_samples)))
    if _NATIVE_ADPCM_LIBRARY is not None:
        encoded = np.ascontiguousarray(np.frombuffer(payload, dtype=np.uint8))
        output = np.zeros(AUDIO_FRAME_SAMPLES, dtype=np.int16)
        result = _NATIVE_ADPCM_LIBRARY.cn0566_ima_adpcm_decode(
            encoded.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
            int(encoded.size),
            int(valid_samples),
            output.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            int(output.size),
        )
        if result != AUDIO_FRAME_SAMPLES:
            raise RuntimeError(f"native IMA-ADPCM decoder returned {result}")
        return output

    predictor, index, _reserved = struct.unpack("<hBB", payload[:ADPCM_HEADER_BYTES])
    output = np.zeros(AUDIO_FRAME_SAMPLES, dtype=np.int16)
    if valid_samples <= 0:
        return output
    output[0] = predictor
    position = 1
    for byte in payload[ADPCM_HEADER_BYTES:]:
        for code in (byte & 0x0F, (byte >> 4) & 0x0F):
            if position >= valid_samples:
                return output
            predictor, index = _ima_decode_nibble(code, predictor, index)
            output[position] = predictor
            position += 1
    return output


# ---------------------------------------------------------------------------
# Native convolutional codec and reference fallback
# ---------------------------------------------------------------------------

_PARITY = tuple(bin(value).count("1") & 1 for value in range(128))


def reference_conv_encode(bits: np.ndarray) -> np.ndarray:
    source = np.asarray(bits, dtype=np.uint8).reshape(-1)
    output = np.empty(2 * (source.size + CONV_TAIL_BITS), dtype=np.uint8)
    state = 0
    for i in range(source.size + CONV_TAIL_BITS):
        bit = int(source[i]) if i < source.size else 0
        register = ((state << 1) | bit) & 0x7F
        output[2 * i] = _PARITY[register & CONV_G0]
        output[2 * i + 1] = _PARITY[register & CONV_G1]
        state = register & 0x3F
    return output


def reference_viterbi_decode(soft: np.ndarray) -> np.ndarray:
    evidence = np.asarray(soft, dtype=np.float32).reshape(-1)
    if evidence.size % 2:
        raise ValueError("coded soft vector must have even length")
    steps = evidence.size // 2
    metrics = np.full(64, 1e30, dtype=np.float64)
    metrics[0] = 0.0
    previous = np.zeros((steps, 64), dtype=np.uint8)
    decisions = np.zeros((steps, 64), dtype=np.uint8)
    for t in range(steps):
        next_metrics = np.full(64, 1e30, dtype=np.float64)
        y0, y1 = float(evidence[2 * t]), float(evidence[2 * t + 1])
        for prev_state in range(64):
            base = metrics[prev_state]
            if base > 1e29:
                continue
            for bit in (0, 1):
                register = ((prev_state << 1) | bit) & 0x7F
                next_state = register & 0x3F
                e0 = _PARITY[register & CONV_G0]
                e1 = _PARITY[register & CONV_G1]
                cost = base - ((1.0 if e0 == 0 else -1.0) * y0 + (1.0 if e1 == 0 else -1.0) * y1)
                if cost < next_metrics[next_state]:
                    next_metrics[next_state] = cost
                    previous[t, next_state] = prev_state
                    decisions[t, next_state] = bit
        metrics = next_metrics - np.min(next_metrics)
    state = 0
    decoded = np.empty(steps, dtype=np.uint8)
    for t in range(steps - 1, -1, -1):
        decoded[t] = decisions[t, state]
        state = previous[t, state]
    return decoded[:-CONV_TAIL_BITS]


class FecCodec:
    """Native convolutional codec with a functional Python fallback."""

    def __init__(self, library_path: Optional[Path] = None) -> None:
        global _NATIVE_ADPCM_LIBRARY
        if library_path is None:
            library_path = Path(__file__).resolve().with_name("libcn0566_fec.so")
        self.library_path = Path(library_path)
        self.library: Optional[ctypes.CDLL] = None
        self.backend = "Python reference"
        self.audio_backend = "Python reference"
        if self.library_path.is_file():
            try:
                library = ctypes.CDLL(str(self.library_path))
                library.cn0566_conv_encode.argtypes = [
                    ctypes.POINTER(ctypes.c_uint8), ctypes.c_int,
                    ctypes.POINTER(ctypes.c_uint8), ctypes.c_int,
                ]
                library.cn0566_conv_encode.restype = ctypes.c_int
                library.cn0566_viterbi_decode.argtypes = [
                    ctypes.POINTER(ctypes.c_float), ctypes.c_int,
                    ctypes.POINTER(ctypes.c_uint8), ctypes.c_int,
                ]
                library.cn0566_viterbi_decode.restype = ctypes.c_int
                if hasattr(library, "cn0566_ima_adpcm_encode") and hasattr(library, "cn0566_ima_adpcm_decode"):
                    library.cn0566_ima_adpcm_encode.argtypes = [
                        ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                        ctypes.POINTER(ctypes.c_uint8), ctypes.c_int,
                    ]
                    library.cn0566_ima_adpcm_encode.restype = ctypes.c_int
                    library.cn0566_ima_adpcm_decode.argtypes = [
                        ctypes.POINTER(ctypes.c_uint8), ctypes.c_int, ctypes.c_int,
                        ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                    ]
                    library.cn0566_ima_adpcm_decode.restype = ctypes.c_int
                    _NATIVE_ADPCM_LIBRARY = library
                    self.audio_backend = "native C"
                self.library = library
                self.backend = "native C"
            except Exception as exc:
                LOG.warning("Could not load native FEC library %s: %s", self.library_path, exc)

    def encode(self, bits: np.ndarray) -> np.ndarray:
        source = np.ascontiguousarray(np.asarray(bits, dtype=np.uint8).reshape(-1))
        if self.library is None:
            return reference_conv_encode(source)
        output = np.empty(2 * (source.size + CONV_TAIL_BITS), dtype=np.uint8)
        result = self.library.cn0566_conv_encode(
            source.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
            int(source.size),
            output.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
            int(output.size),
        )
        if result != output.size:
            raise RuntimeError(f"native convolutional encoder returned {result}")
        return output

    def decode(self, soft: np.ndarray) -> np.ndarray:
        evidence = np.ascontiguousarray(np.asarray(soft, dtype=np.float32).reshape(-1))
        if self.library is None:
            return reference_viterbi_decode(evidence)
        info_bits = evidence.size // 2 - CONV_TAIL_BITS
        output = np.empty(info_bits, dtype=np.uint8)
        result = self.library.cn0566_viterbi_decode(
            evidence.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            int(evidence.size),
            output.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
            int(output.size),
        )
        if result != output.size:
            raise RuntimeError(f"native Viterbi decoder returned {result}")
        return output


# ---------------------------------------------------------------------------
# Packet construction and parsing
# ---------------------------------------------------------------------------

def build_audio_packet(
    stream_id: int,
    sequence: int,
    pcm: np.ndarray,
    valid_samples: int,
    flags: int,
) -> AudioPacket:
    if stream_id not in range(4):
        raise ValueError("stream_id must be 0..3")
    payload = ima_adpcm_encode_block(pcm)
    header = PACKET_HEADER.pack(
        MAGIC,
        PROTOCOL_VERSION,
        stream_id,
        sequence & 0xFFFF,
        int(valid_samples) & 0xFFFF,
        flags & 0xFF,
        len(payload),
    )
    body = header + payload
    raw = body + CRC_STRUCT.pack(zlib.crc32(body) & 0xFFFFFFFF)
    if len(raw) != PACKET_BYTES:
        raise RuntimeError("internal packet-size error")
    return AudioPacket(stream_id, sequence & 0xFFFF, valid_samples, flags, payload, raw)


def parse_audio_packet(raw: bytes) -> AudioPacket:
    if len(raw) != PACKET_BYTES:
        raise ValueError("incorrect packet length")
    body, crc_bytes = raw[:-CRC_STRUCT.size], raw[-CRC_STRUCT.size:]
    expected_crc = CRC_STRUCT.unpack(crc_bytes)[0]
    actual_crc = zlib.crc32(body) & 0xFFFFFFFF
    if expected_crc != actual_crc:
        raise ValueError("CRC mismatch")
    magic, version, stream_id, sequence, valid_samples, flags, payload_length = PACKET_HEADER.unpack(
        body[:PACKET_HEADER_BYTES]
    )
    if magic != MAGIC or version != PROTOCOL_VERSION:
        raise ValueError("protocol header mismatch")
    if stream_id not in range(4):
        raise ValueError("invalid stream id")
    if payload_length != AUDIO_PAYLOAD_BYTES:
        raise ValueError("invalid payload length")
    payload = body[PACKET_HEADER_BYTES:]
    return AudioPacket(stream_id, sequence, valid_samples, flags, payload, raw)


def bytes_to_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8), bitorder="big")


def bits_to_bytes(bits: np.ndarray) -> bytes:
    source = np.asarray(bits, dtype=np.uint8).reshape(-1)
    if source.size % 8:
        raise ValueError("bit length must be divisible by eight")
    return np.packbits(source, bitorder="big").tobytes()

# ---------------------------------------------------------------------------
# WAV loading and four synchronized audio sources
# ---------------------------------------------------------------------------

def load_wav_as_pcm16(path: Path) -> np.ndarray:
    sample_rate, raw = wavfile.read(path)
    values = np.asarray(raw)
    if values.ndim == 2:
        values = values.astype(np.float64).mean(axis=1)
    elif values.ndim != 1:
        raise ValueError(f"unsupported WAV shape {values.shape} for {path}")

    if np.issubdtype(values.dtype, np.floating):
        audio = values.astype(np.float64)
    elif values.dtype == np.uint8:
        audio = (values.astype(np.float64) - 128.0) / 128.0
    elif np.issubdtype(values.dtype, np.signedinteger):
        info = np.iinfo(values.dtype)
        audio = values.astype(np.float64) / float(max(abs(info.min), info.max))
    else:
        raise ValueError(f"unsupported WAV dtype {values.dtype}")

    audio = np.nan_to_num(audio, nan=0.0, posinf=1.0, neginf=-1.0)
    audio = np.clip(audio, -1.0, 1.0)
    if int(sample_rate) != AUDIO_SAMPLE_RATE:
        divisor = math.gcd(int(sample_rate), AUDIO_SAMPLE_RATE)
        audio = signal.resample_poly(
            audio,
            AUDIO_SAMPLE_RATE // divisor,
            int(sample_rate) // divisor,
        )
    if audio.size == 0:
        raise ValueError(f"WAV file is empty: {path}")
    peak = float(np.max(np.abs(audio)))
    if peak > 0.98:
        audio *= 0.98 / peak
    pcm = np.rint(audio * 32767.0).clip(-32768, 32767).astype(np.int16)
    LOG.info(
        "Loaded %s: %.2f s, %d Hz -> %d Hz mono PCM",
        path,
        pcm.size / AUDIO_SAMPLE_RATE,
        sample_rate,
        AUDIO_SAMPLE_RATE,
    )
    return pcm


class AudioStreamSource:
    def __init__(self, stream_id: int, pcm: np.ndarray, repeat: bool = True) -> None:
        self.stream_id = stream_id
        self.pcm = np.asarray(pcm, dtype=np.int16).reshape(-1)
        self.repeat = repeat
        self.position = 0
        self.sequence = 0
        self.first_packet = True
        self.finished = False

    def next_packet(self) -> AudioPacket:
        frame = np.zeros(AUDIO_FRAME_SAMPLES, dtype=np.int16)
        flags = 0
        valid = 0

        if self.finished:
            flags |= FLAG_IDLE
        else:
            if self.first_packet:
                flags |= FLAG_START_OF_FILE
                self.first_packet = False
            remaining = self.pcm.size - self.position
            valid = min(AUDIO_FRAME_SAMPLES, max(0, remaining))
            if valid:
                frame[:valid] = self.pcm[self.position:self.position + valid]
                self.position += valid
            if self.position >= self.pcm.size:
                flags |= FLAG_END_OF_FILE
                if self.repeat:
                    self.position = 0
                    self.first_packet = True
                else:
                    self.finished = True

        packet = build_audio_packet(
            self.stream_id,
            self.sequence,
            frame,
            valid,
            flags,
        )
        self.sequence = (self.sequence + 1) & 0xFFFF
        return packet


class FourAudioSource:
    def __init__(self, pcm_streams: Sequence[np.ndarray], repeat: bool = True) -> None:
        if len(pcm_streams) != 4:
            raise ValueError("exactly four audio streams are required")
        self.sources = [AudioStreamSource(i, pcm_streams[i], repeat=repeat) for i in range(4)]

    def next_packets(self) -> List[AudioPacket]:
        return [source.next_packet() for source in self.sources]


# ---------------------------------------------------------------------------
# OFDM frame builder
# ---------------------------------------------------------------------------

class OfdmFrameBuilder:
    def __init__(self, fec: FecCodec) -> None:
        self.fec = fec
        self._pilot_values = PILOT_VALUES
        self.last_clip_fraction = 0.0
        self.last_output_papr_db = float("nan")
        self.last_unscaled_papr_db = float("nan")

    def packet_channel_bits(self, packet: AudioPacket) -> np.ndarray:
        uncoded = bytes_to_bits(packet.raw)
        coded = self.fec.encode(uncoded)
        if coded.size != CODED_PACKET_BITS:
            raise RuntimeError("unexpected coded packet length")
        return np.concatenate((coded, CHANNEL_PRBS[packet.stream_id])).astype(np.uint8)

    def build_unscaled_superframe(self, packets: Sequence[AudioPacket]) -> np.ndarray:
        if len(packets) != 4:
            raise ValueError("four packets are required for one OFDM superframe")
        by_stream = {packet.stream_id: packet for packet in packets}
        if set(by_stream) != {0, 1, 2, 3}:
            raise ValueError("one packet for each stream id 0..3 is required")

        grid = np.zeros((DATA_OFDM_SYMBOLS, NFFT), dtype=np.complex64)
        grid[:, PILOT_BINS] = self._pilot_values
        for channel in range(4):
            bits = self.packet_channel_bits(by_stream[channel])
            if bits.size != CHANNEL_CODED_BITS_PER_FRAME:
                raise RuntimeError("channel bit allocation mismatch")
            symbols = _qpsk_map(bits).reshape(DATA_OFDM_SYMBOLS, 48)
            grid[:, CHANNEL_DATA_BINS[channel]] = symbols

        scale = NFFT / math.sqrt(float(ACTIVE_BINS.size))
        useful = (np.fft.ifft(grid, axis=1) * scale).astype(np.complex64)
        data_time = np.concatenate((useful[:, -CP_LEN:], useful), axis=1).reshape(-1)
        active = np.concatenate((PREAMBLE_TIME, data_time)).astype(np.complex64)
        frame = np.zeros(SUPERFRAME_SAMPLES, dtype=np.complex64)
        start = LEADING_GUARD_SAMPLES
        frame[start:start + active.size] = active
        return frame

    def build_scaled_chunk(
        self,
        source: FourAudioSource,
        superframes: int = TX_SUPERFRAMES_PER_BUFFER,
        peak_target: float = TX_DIGITAL_PEAK_DEFAULT,
        rms_target: float = TX_DIGITAL_RMS_DEFAULT,
    ) -> Tuple[np.ndarray, List[List[AudioPacket]], float]:
        """Build a fixed-duration TX chunk with RMS normalization and rare peak limiting.

        Scaling an entire one-second OFDM chunk only by its single largest sample
        caused approximately 18 dB of average-power backoff in release 1.0.0.
        This version first sets a deterministic RMS level, then limits only samples
        whose complex magnitude exceeds ``peak_target``.  The default 2,000/6,000
        ratio gives about 9.5 dB output crest factor and clips roughly 0.05% of the
        bundled test waveform, while retaining substantial linearity.
        """
        frames: List[np.ndarray] = []
        packet_history: List[List[AudioPacket]] = []
        for _ in range(superframes):
            packets = source.next_packets()
            packet_history.append(packets)
            frames.append(self.build_unscaled_superframe(packets))
        chunk = np.concatenate(frames).astype(np.complex64, copy=False)
        magnitude = np.abs(chunk)
        peak = float(np.max(magnitude))
        unscaled_rms = float(np.sqrt(np.mean(magnitude ** 2)))
        if not np.isfinite(peak) or not np.isfinite(unscaled_rms) or unscaled_rms <= 0:
            raise RuntimeError("invalid OFDM chunk amplitude")
        if peak_target <= 0 or rms_target <= 0 or rms_target >= peak_target:
            raise ValueError("TX digital levels require 0 < rms_target < peak_target")

        self.last_unscaled_papr_db = 20.0 * math.log10(max(peak / unscaled_rms, 1.0))
        chunk *= float(rms_target) / unscaled_rms
        magnitude = np.abs(chunk)
        clipped = magnitude > float(peak_target)
        self.last_clip_fraction = float(np.mean(clipped))
        if np.any(clipped):
            chunk[clipped] *= (float(peak_target) / magnitude[clipped]).astype(np.float32)

        rms = float(np.sqrt(np.mean(np.abs(chunk) ** 2)))
        output_peak = float(np.max(np.abs(chunk)))
        self.last_output_papr_db = 20.0 * math.log10(max(output_peak / max(rms, 1e-12), 1.0))
        return chunk.astype(np.complex64, copy=False), packet_history, rms


# ---------------------------------------------------------------------------
# Streaming OFDM receiver
# ---------------------------------------------------------------------------

class StreamingOfdmReceiver:
    """Acquire and track fixed 20 ms OFDM superframes in a continuous stream.

    Acquisition uses an overlap-save normalized correlator that processes each
    newly received sample once.  Release 1.0.0 repeatedly correlated the entire
    rolling buffer while unlocked, which could not keep pace with 2 MSPS on a
    Raspberry Pi 4.  Once a valid packet establishes lock, a small predictive
    correlation window is used around each 20 ms frame boundary.
    """

    def __init__(
        self,
        fec: FecCodec,
        packet_callback: Callable[[AudioPacket, np.ndarray], None],
        diagnostics_callback: Optional[Callable[[OfdmFrameDiagnostics], None]] = None,
        correlation_threshold: float = 0.20,
        decode_mode: str = "rx2",
        channel_workers: int = CHANNEL_DECODE_WORKERS,
        metrics_interval: int = LIVE_METRICS_INTERVAL,
        channel_selector: Optional[Callable[[], Set[int]]] = None,
    ) -> None:
        if decode_mode not in {"rx2", "mrc"}:
            raise ValueError("decode_mode must be rx2 or mrc")
        self.fec = fec
        self.packet_callback = packet_callback
        self.diagnostics_callback = diagnostics_callback
        self.correlation_threshold = float(correlation_threshold)
        self.decode_mode = decode_mode
        self.channel_workers = max(1, min(4, int(channel_workers)))
        self.metrics_interval = max(1, int(metrics_interval))
        self.channel_selector = channel_selector
        self.stats = ReceiverStatistics()
        self._decoded_frame_times: Deque[float] = collections.deque(maxlen=512)

        self.buffer_rx2 = np.empty(0, dtype=np.complex64)
        self.buffer_rx1 = np.empty(0, dtype=np.complex64)
        self.buffer_global_start = 0
        self.total_samples = 0
        self.locked = False
        self.next_preamble_global: Optional[int] = None
        self.frames_since_acquisition = 0
        self._last_good_preamble_global: Optional[int] = None
        self.search_after_global = 0

        self._acquisition_tail = np.empty(0, dtype=np.complex64)
        self._acquisition_candidates: Deque[Tuple[int, float]] = collections.deque()
        self._acquisition_kernel_fft: Dict[int, np.ndarray] = {}
        self._acquisition_block_counter = 0
        self.acquisition_block_stride = ACQUISITION_BLOCK_STRIDE

    @staticmethod
    def _normalized_correlation(source: np.ndarray, template: np.ndarray) -> np.ndarray:
        """Normalized correlation for short tracking windows."""
        if source.size < template.size:
            return np.empty(0, dtype=np.float32)
        # Tracking windows are short (typically template + 256 samples).
        # NumPy's direct complex correlator is faster than constructing an FFT
        # convolution for this size and avoids repeated SciPy FFT-plan overhead.
        correlation = np.correlate(source, template, mode="valid")
        power = np.abs(source).astype(np.float64) ** 2
        cumulative = np.empty(power.size + 1, dtype=np.float64)
        cumulative[0] = 0.0
        np.cumsum(power, out=cumulative[1:])
        energy = cumulative[template.size:] - cumulative[:-template.size]
        metric = np.abs(correlation) ** 2 / (PREAMBLE_ENERGY * energy + 1e-30)
        return np.clip(metric.real, 0.0, 1.0).astype(np.float32)

    def _streaming_acquisition_metrics(
        self,
        block: np.ndarray,
        block_global_start: int,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Return one normalized metric per newly available template start.

        The previous ``M-1`` samples are prepended to the new block, where ``M``
        is the preamble length.  Consequently every possible start index is
        evaluated exactly once, including starts that cross an IIO block edge.
        """
        template_size = PREAMBLE_TIME.size
        source = np.concatenate((self._acquisition_tail, block)).astype(np.complex64, copy=False)
        source_global_start = block_global_start - self._acquisition_tail.size
        if source.size < template_size:
            self._acquisition_tail = source.copy()
            return np.empty(0, dtype=np.float32), np.empty(0, dtype=np.int64)

        full_length = source.size + template_size - 1
        fft_length = scipy_fft.next_fast_len(full_length)
        kernel_fft = self._acquisition_kernel_fft.get(fft_length)
        if kernel_fft is None:
            kernel = np.conj(PREAMBLE_TIME[::-1])
            kernel_fft = scipy_fft.fft(kernel, fft_length)
            self._acquisition_kernel_fft[fft_length] = kernel_fft
        correlation_full = scipy_fft.ifft(
            scipy_fft.fft(source, fft_length) * kernel_fft,
            fft_length,
        )[:full_length]
        correlation = correlation_full[template_size - 1:source.size]

        power = np.abs(source).astype(np.float64) ** 2
        cumulative = np.empty(power.size + 1, dtype=np.float64)
        cumulative[0] = 0.0
        np.cumsum(power, out=cumulative[1:])
        energy = cumulative[template_size:] - cumulative[:-template_size]
        metric = np.abs(correlation) ** 2 / (PREAMBLE_ENERGY * energy + 1e-30)
        metric = np.clip(metric.real, 0.0, 1.0).astype(np.float32)
        starts = source_global_start + np.arange(metric.size, dtype=np.int64)

        self._acquisition_tail = source[-(template_size - 1):].copy()
        return metric, starts

    def _update_acquisition_candidates(self, block: np.ndarray, block_global_start: int) -> None:
        self.stats.acquisition_searches += 1
        metric, starts = self._streaming_acquisition_metrics(block, block_global_start)
        if metric.size == 0:
            return
        self.stats.last_detector_peak = float(np.max(metric))
        peaks, _properties = signal.find_peaks(
            metric,
            height=self.correlation_threshold,
            distance=SUPERFRAME_SAMPLES // 2,
        )
        if metric[0] >= self.correlation_threshold:
            peaks = np.concatenate((np.array([0], dtype=int), peaks))
        for peak_value in peaks:
            index = int(peak_value)
            global_start = int(starts[index])
            if global_start < self.search_after_global:
                continue
            if self._acquisition_candidates and abs(
                global_start - self._acquisition_candidates[-1][0]
            ) < SUPERFRAME_SAMPLES // 2:
                if float(metric[index]) > self._acquisition_candidates[-1][1]:
                    self._acquisition_candidates[-1] = (global_start, float(metric[index]))
                continue
            self._acquisition_candidates.append((global_start, float(metric[index])))

    def _reset_acquisition_from_current_buffer(self) -> None:
        self._acquisition_candidates.clear()
        keep = PREAMBLE_TIME.size - 1
        if self.buffer_rx2.size:
            self._acquisition_tail = self.buffer_rx2[-keep:].copy()
        else:
            self._acquisition_tail = np.empty(0, dtype=np.complex64)

    def _acquire_candidate(self) -> Optional[Tuple[int, float, int]]:
        frame_samples = TOTAL_OFDM_SYMBOLS * OFDM_SYMBOL_SAMPLES
        while self._acquisition_candidates:
            global_start, value = self._acquisition_candidates[0]
            if global_start < max(self.search_after_global, self.buffer_global_start):
                self._acquisition_candidates.popleft()
                continue
            local_start = global_start - self.buffer_global_start
            if local_start + frame_samples > self.buffer_rx2.size:
                return None
            self._acquisition_candidates.popleft()
            return global_start, value, 0
        return None

    def _tracking_candidate(self) -> Optional[Tuple[int, float, int]]:
        if self.next_preamble_global is None:
            return None
        self.stats.tracking_searches += 1
        boundary = (self.frames_since_acquisition + 1) % TX_SUPERFRAMES_PER_BUFFER == 0
        radius = 4096 if boundary else 128
        expected_local = self.next_preamble_global - self.buffer_global_start
        window_start = max(0, expected_local - radius)
        window_end = min(
            self.buffer_rx2.size,
            expected_local + radius + PREAMBLE_TIME.size,
        )
        if window_end - window_start < PREAMBLE_TIME.size:
            return None
        metric = self._normalized_correlation(
            self.buffer_rx2[window_start:window_end], PREAMBLE_TIME
        )
        if metric.size == 0:
            return None
        best = int(np.argmax(metric))
        value = float(metric[best])
        self.stats.last_detector_peak = value
        if value < self.correlation_threshold:
            return None
        local_start = window_start + best
        timing_offset = int(local_start - expected_local)
        required = local_start + TOTAL_OFDM_SYMBOLS * OFDM_SYMBOL_SAMPLES
        if required > self.buffer_rx2.size:
            return None
        return self.buffer_global_start + local_start, value, timing_offset

    @staticmethod
    def _extract_frequency_symbols(
        raw: np.ndarray,
        cfo_hz: float,
    ) -> np.ndarray:
        """CFO-correct and FFT one superframe with a separable phasor.

        The original implementation evaluated almost twenty thousand complex
        exponentials per 20 ms frame.  Because the phase ramp is separable into
        an intra-symbol term and an OFDM-symbol term, only 288 + Nsym
        exponentials are required while producing the same correction.
        """
        matrix = raw.reshape(TOTAL_OFDM_SYMBOLS, OFDM_SYMBOL_SAMPLES)
        omega = np.float32(-2.0 * math.pi * cfo_hz / SAMPLE_RATE)
        within_phase = omega * np.arange(OFDM_SYMBOL_SAMPLES, dtype=np.float32)
        symbol_phase = (
            omega
            * np.float32(OFDM_SYMBOL_SAMPLES)
            * np.arange(TOTAL_OFDM_SYMBOLS, dtype=np.float32)
        )
        within = np.exp(1j * within_phase).astype(np.complex64)
        per_symbol = np.exp(1j * symbol_phase).astype(np.complex64)
        corrected = matrix * per_symbol[:, None]
        corrected *= within[None, :]
        useful = corrected[:, CP_LEN:]
        return np.fft.fft(useful, axis=1).astype(np.complex64)

    @staticmethod
    def _estimate_cfo(raw: np.ndarray) -> float:
        matrix = raw[: 2 * OFDM_SYMBOL_SAMPLES].reshape(2, OFDM_SYMBOL_SAMPLES)
        first = matrix[0, CP_LEN:]
        second = matrix[1, CP_LEN:]
        product = np.vdot(first, second)
        return float(np.angle(product) * SAMPLE_RATE / (2.0 * math.pi * OFDM_SYMBOL_SAMPLES))

    @staticmethod
    def _equalize_single(frequency: np.ndarray) -> Tuple[np.ndarray, np.ndarray, float]:
        training_rx = frequency[:2]
        h1 = training_rx[0, ACTIVE_BINS] / TRAINING_FREQUENCY[ACTIVE_BINS]
        h2 = training_rx[1, ACTIVE_BINS] / TRAINING_FREQUENCY[ACTIVE_BINS]
        channel = 0.5 * (h1 + h2)
        channel_power = np.abs(channel) ** 2
        safe = channel_power > 1e-8
        equalized_active = np.zeros((DATA_OFDM_SYMBOLS, ACTIVE_BINS.size), dtype=np.complex64)
        y = frequency[PREAMBLE_SYMBOLS:, ACTIVE_BINS]
        equalized_active[:, safe] = y[:, safe] / channel[safe]
        return equalized_active, channel, float(np.mean(np.abs(channel)))

    @staticmethod
    def _equalize_mrc(
        frequency_rx1: np.ndarray,
        frequency_rx2: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray, float]:
        def estimate(freq: np.ndarray) -> np.ndarray:
            h1 = freq[0, ACTIVE_BINS] / TRAINING_FREQUENCY[ACTIVE_BINS]
            h2 = freq[1, ACTIVE_BINS] / TRAINING_FREQUENCY[ACTIVE_BINS]
            return 0.5 * (h1 + h2)

        h0 = estimate(frequency_rx1)
        h1 = estimate(frequency_rx2)
        y0 = frequency_rx1[PREAMBLE_SYMBOLS:, ACTIVE_BINS]
        y1 = frequency_rx2[PREAMBLE_SYMBOLS:, ACTIVE_BINS]
        denominator = np.abs(h0) ** 2 + np.abs(h1) ** 2 + 1e-9
        equalized = (
            np.conj(h0)[None, :] * y0 + np.conj(h1)[None, :] * y1
        ) / denominator[None, :]
        combined_channel = np.sqrt(denominator)
        return equalized.astype(np.complex64), combined_channel.astype(np.complex64), float(np.mean(combined_channel))

    @staticmethod
    def _pilot_phase_correct(equalized_active: np.ndarray) -> np.ndarray:
        corrected = equalized_active.copy()
        pilots = corrected[:, PILOT_ACTIVE_POSITIONS]
        phase = np.angle(np.sum(pilots * np.conj(PILOT_VALUES), axis=1))
        corrected *= np.exp(-1j * phase).astype(np.complex64)[:, None]
        return corrected

    def active_decode_channels(self) -> Set[int]:
        """Return logical channels decoded on the current frame.

        The over-the-air waveform always carries all four streams.  In the
        selected-channel realtime mode, the Raspberry Pi only performs FEC and
        ADPCM work for channels currently requested by the listener.  When the
        selection is muted, channel 1 remains decoded as a low-cost health
        monitor so frame-rate and CRC status continue to advance.  Performance
        capture passes no selector and therefore decodes all channels.
        """
        if self.channel_selector is None or self.diagnostics_callback is not None:
            return {0, 1, 2, 3}
        try:
            selected = {int(value) for value in self.channel_selector()}
        except Exception:
            LOG.exception("Logical-channel selector failed; decoding channel 1")
            return {0}
        selected = {value for value in selected if 0 <= value < 4}
        return selected or {0}

    def active_decode_description(self) -> str:
        return ",".join(str(value + 1) for value in sorted(self.active_decode_channels()))

    def recent_frame_rate(self, window_seconds: float = 5.0) -> float:
        now = time.monotonic()
        while self._decoded_frame_times and now - self._decoded_frame_times[0] > window_seconds:
            self._decoded_frame_times.popleft()
        if len(self._decoded_frame_times) < 2:
            return 0.0
        duration = self._decoded_frame_times[-1] - self._decoded_frame_times[0]
        return (len(self._decoded_frame_times) - 1) / duration if duration > 0 else 0.0

    def _decode_one_channel(
        self,
        channel_index: int,
        symbols: np.ndarray,
        compute_metrics: bool,
        include_diagnostics: bool,
        make_waveform: bool,
    ) -> ChannelDecodeResult:
        """Decode one logical carrier group without mutating receiver state.

        This function is intentionally safe to execute in the shared channel
        worker pool.  The native FEC and ADPCM routines are re-entrant and use
        thread-local Viterbi workspaces.  Packet callbacks and statistics are
        committed later by the receiver thread in deterministic channel order.
        """
        soft = np.empty(symbols.size * 2, dtype=np.float32)
        soft[0::2] = symbols.real
        soft[1::2] = symbols.imag

        packet: Optional[AudioPacket] = None
        pcm: Optional[np.ndarray] = None
        packet_valid = False
        try:
            decoded_bits = self.fec.decode(soft[:CODED_PACKET_BITS])
            if decoded_bits.size != PACKET_BITS:
                raise ValueError("decoded packet bit length mismatch")
            packet = parse_audio_packet(bits_to_bytes(decoded_bits))
            if packet.stream_id != channel_index:
                raise ValueError("decoded stream id does not match carrier group")
            pcm = ima_adpcm_decode_block(packet.payload, packet.valid_samples)
            packet_valid = True
        except Exception:
            packet = None
            pcm = None

        raw_ber = float("nan")
        evm_db = float("nan")
        if compute_metrics:
            prbs_symbols = symbols[CODED_PACKET_BITS // 2:]
            hard_prbs = _bits_from_symbols(prbs_symbols)
            expected_prbs = CHANNEL_PRBS[channel_index]
            raw_ber = (
                float(np.mean(hard_prbs != expected_prbs))
                if expected_prbs.size else float("nan")
            )
            expected_prbs_symbols = CHANNEL_PRBS_SYMBOLS[channel_index]
            evm_rms = math.sqrt(
                float(np.mean(np.abs(prbs_symbols - expected_prbs_symbols) ** 2))
            )
            evm_db = 20.0 * math.log10(max(evm_rms, 1e-12))

        channel_time: Optional[np.ndarray] = None
        diagnostic_symbols: Optional[np.ndarray] = None
        if include_diagnostics:
            diagnostic_symbols = symbols
            if make_waveform:
                channel_grid = np.zeros((DATA_OFDM_SYMBOLS, NFFT), dtype=np.complex64)
                channel_grid[:, CHANNEL_DATA_BINS[channel_index]] = symbols.reshape(
                    DATA_OFDM_SYMBOLS, 48
                )
                scale = NFFT / math.sqrt(float(ACTIVE_BINS.size))
                channel_useful = (
                    np.fft.ifft(channel_grid, axis=1) * scale
                ).astype(np.complex64)
                channel_time = np.concatenate(
                    (channel_useful[:, -CP_LEN:], channel_useful), axis=1
                ).reshape(-1)

        return ChannelDecodeResult(
            stream_id=channel_index,
            packet_valid=packet_valid,
            packet=packet,
            pcm=pcm,
            evm_db=evm_db,
            raw_prbs_ber=raw_ber,
            equalized_symbols=diagnostic_symbols,
            reconstructed_waveform=channel_time,
        )

    def _decode_frame(
        self,
        preamble_global: int,
        correlation: float,
        timing_offset: int,
    ) -> bool:
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
        diagnostics: Dict[int, ChannelFrameDiagnostics] = {}
        at_least_one_packet = False

        include_diagnostics = self.diagnostics_callback is not None
        compute_metrics = (
            include_diagnostics
            or (self.stats.frames_detected % self.metrics_interval == 0)
        )
        active_channels = sorted(self.active_decode_channels())
        channel_symbols = {
            channel_index: equalized_active[:, CHANNEL_ACTIVE_POSITIONS[channel_index]]
            .reshape(-1)
            .astype(np.complex64)
            for channel_index in active_channels
        }
        make_waveforms = {channel_index: False for channel_index in active_channels}
        if include_diagnostics:
            needs_waveform = getattr(self.diagnostics_callback, "needs_waveform", None)
            make_waveforms = {
                channel_index: (
                    True if needs_waveform is None else bool(needs_waveform(channel_index))
                )
                for channel_index in active_channels
            }

        work = [
            (
                channel_index,
                channel_symbols[channel_index],
                compute_metrics,
                include_diagnostics,
                make_waveforms[channel_index],
            )
            for channel_index in active_channels
        ]
        if self.channel_workers > 1 and self.fec.backend == "native C":
            futures = [
                _CHANNEL_DECODE_EXECUTOR.submit(self._decode_one_channel, *item)
                for item in work
            ]
            results = [future.result() for future in futures]
        else:
            results = [self._decode_one_channel(*item) for item in work]

        for result in results:
            channel_index = result.stream_id
            packet = result.packet
            if result.packet_valid and packet is not None and result.pcm is not None:
                self.packet_callback(packet, result.pcm)
                at_least_one_packet = True
                self.stats.packet_valid[channel_index] += 1
                previous = self.stats.last_sequence[channel_index]
                if previous is not None:
                    expected = (previous + 1) & 0xFFFF
                    if packet.sequence != expected:
                        self.stats.sequence_gaps[channel_index] += (
                            packet.sequence - expected
                        ) & 0xFFFF
                self.stats.last_sequence[channel_index] = packet.sequence
            else:
                self.stats.packet_fail[channel_index] += 1

            if math.isfinite(result.evm_db):
                self.stats.last_evm_db[channel_index] = result.evm_db
            if math.isfinite(result.raw_prbs_ber):
                self.stats.last_prbs_ber[channel_index] = result.raw_prbs_ber

            diagnostics[channel_index] = ChannelFrameDiagnostics(
                stream_id=channel_index,
                packet_valid=result.packet_valid,
                sequence=packet.sequence if packet is not None else None,
                evm_db=result.evm_db,
                raw_prbs_ber=result.raw_prbs_ber,
                equalized_symbols=result.equalized_symbols,
                expected_symbols=None,
                reconstructed_waveform=result.reconstructed_waveform,
            )

        self.stats.frames_detected += 1
        if at_least_one_packet:
            self.stats.frames_decoded += 1
            self._decoded_frame_times.append(time.monotonic())
        self.stats.last_correlation = correlation
        self.stats.last_cfo_hz = cfo
        self.stats.last_timing_offset = timing_offset

        if self.diagnostics_callback is not None:
            mean_evm_linear = np.mean(
                [10.0 ** (diag.evm_db / 20.0) for diag in diagnostics.values()]
            )
            frame_evm_db = 20.0 * math.log10(max(float(mean_evm_linear), 1e-12))
            frame_diag = OfdmFrameDiagnostics(
                frame_start_global=preamble_global - LEADING_GUARD_SAMPLES,
                correlation=correlation,
                cfo_hz=cfo,
                timing_offset_samples=timing_offset,
                channel_magnitude_mean=channel_magnitude,
                noise_evm_db=frame_evm_db,
                channels=diagnostics,
            )
            self.diagnostics_callback(frame_diag)
        return at_least_one_packet

    def _trim_buffer(self) -> None:
        if self.locked and self.next_preamble_global is not None:
            retain_from_global = self.next_preamble_global - 8192
        else:
            retain_from_global = self.total_samples - 3 * SUPERFRAME_SAMPLES
        if self._acquisition_candidates:
            retain_from_global = min(
                retain_from_global,
                self._acquisition_candidates[0][0] - LEADING_GUARD_SAMPLES,
            )
        remove = max(0, retain_from_global - self.buffer_global_start)
        remove = min(remove, self.buffer_rx2.size)
        if remove > 0:
            self.buffer_rx2 = self.buffer_rx2[remove:]
            if self.buffer_rx1.size:
                self.buffer_rx1 = self.buffer_rx1[remove:]
            self.buffer_global_start += remove

    def feed(self, rx2: np.ndarray, rx1: Optional[np.ndarray] = None) -> int:
        channel2 = np.asarray(rx2, dtype=np.complex64).reshape(-1)
        channel2 = channel2 - np.mean(channel2)
        if self.decode_mode == "mrc":
            if rx1 is None:
                raise ValueError("MRC mode requires RX1 and RX2")
            channel1 = np.asarray(rx1, dtype=np.complex64).reshape(-1)
            if channel1.size != channel2.size:
                raise ValueError("RX channel lengths differ")
            channel1 = channel1 - np.mean(channel1)
        else:
            channel1 = np.empty(0, dtype=np.complex64)

        block_global_start = self.total_samples
        if not self.locked:
            # A full unlocked FFT search is the most expensive receiver task.
            # Search one of every N large IIO blocks; every searched block spans
            # several complete 20 ms frames, so acquisition opportunities remain
            # plentiful while average CPU use stays below the arrival rate.
            search_this_block = (
                self._acquisition_block_counter % self.acquisition_block_stride == 0
            )
            self._acquisition_block_counter += 1
            if search_this_block:
                self._update_acquisition_candidates(channel2, block_global_start)
            else:
                # Do not correlate across a deliberately skipped block.
                self._acquisition_tail = np.empty(0, dtype=np.complex64)

        self.stats.last_rx_rms = float(np.sqrt(np.mean(np.abs(channel2) ** 2)))
        self.stats.last_rx_peak = float(np.max(np.abs(channel2)))
        self.buffer_rx2 = np.concatenate((self.buffer_rx2, channel2))
        if self.decode_mode == "mrc":
            self.buffer_rx1 = np.concatenate((self.buffer_rx1, channel1))
        self.total_samples += channel2.size

        decoded_count = 0
        while True:
            candidate: Optional[Tuple[int, float, int]]
            was_locked = self.locked
            if self.locked:
                candidate = self._tracking_candidate()
                if candidate is None:
                    if self.next_preamble_global is not None:
                        expected_end = (
                            self.next_preamble_global
                            - self.buffer_global_start
                            + TOTAL_OFDM_SYMBOLS * OFDM_SYMBOL_SAMPLES
                        )
                        if expected_end > self.buffer_rx2.size:
                            break
                    self.stats.tracking_misses += 1
                    self.locked = False
                    self.stats.reacquisitions += 1
                    self.next_preamble_global = None
                    self._reset_acquisition_from_current_buffer()
                    self._acquisition_block_counter = 0
                    break
            else:
                candidate = self._acquire_candidate()
                if candidate is None:
                    break

            preamble_global, metric, timing_offset = candidate
            frame_valid = self._decode_frame(preamble_global, metric, timing_offset)
            if not frame_valid:
                self.search_after_global = preamble_global + SUPERFRAME_SAMPLES // 2
                if was_locked:
                    self.stats.tracking_misses += 1
                    self.locked = False
                    self.stats.reacquisitions += 1
                    self.next_preamble_global = None
                    self._reset_acquisition_from_current_buffer()
                    self._acquisition_block_counter = 0
                    break
                continue

            decoded_count += 1
            if not was_locked:
                self.frames_since_acquisition = 0
                self._acquisition_candidates.clear()
                self._acquisition_tail = np.empty(0, dtype=np.complex64)
                self._acquisition_block_counter = 0
            else:
                self.frames_since_acquisition += 1
            self.locked = True
            self._last_good_preamble_global = preamble_global
            self.search_after_global = preamble_global + SUPERFRAME_SAMPLES // 2
            self.next_preamble_global = preamble_global + SUPERFRAME_SAMPLES

            if (
                self.next_preamble_global
                - self.buffer_global_start
                + TOTAL_OFDM_SYMBOLS * OFDM_SYMBOL_SAMPLES
                > self.buffer_rx2.size
            ):
                break

        self._trim_buffer()
        return decoded_count
# ---------------------------------------------------------------------------
# Selectable four-channel audio sink
# ---------------------------------------------------------------------------

class SelectableAudioMixer:
    """Buffer four decoded streams and mix the user-selected channels to ALSA."""

    def __init__(
        self,
        enabled: bool,
        stop_event: threading.Event,
        selected_channels: Set[int],
        audio_device: str = "default",
        startup_buffer_ms: int = 1500,
        record_directory: Optional[Path] = None,
        runtime_stats: Optional[RuntimeStatistics] = None,
    ) -> None:
        self.enabled = enabled
        self.stop_event = stop_event
        self.audio_device = audio_device
        self.startup_frames = max(1, int(math.ceil(startup_buffer_ms / 20.0)))
        self.write_frames = AUDIO_WRITE_FRAMES
        self.record_directory = Path(record_directory) if record_directory else None
        self.runtime_stats = runtime_stats or RuntimeStatistics()
        self._selection_lock = threading.Lock()
        self._selected = set(selected_channels)
        self._queue: "queue.Queue[Tuple[AudioPacket, np.ndarray]]" = queue.Queue(maxsize=2000)
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen] = None
        self._pending: List[Dict[int, np.ndarray]] = [dict() for _ in range(4)]
        self._last_audio: List[np.ndarray] = [np.zeros(AUDIO_FRAME_SAMPLES, dtype=np.float32) for _ in range(4)]
        self._next_sequence: Optional[int] = None
        self._started = False
        self._restart_requested = threading.Event()
        self._record_handles: List[Optional[object]] = [None, None, None, None]

    def start(self) -> None:
        if self.record_directory:
            self.record_directory.mkdir(parents=True, exist_ok=True)
            import wave
            for channel in range(4):
                handle = wave.open(str(self.record_directory / f"received_stream_{channel + 1}.wav"), "wb")
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(AUDIO_SAMPLE_RATE)
                self._record_handles[channel] = handle
        self._thread = threading.Thread(target=self._run, name="ofdm-audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        if self._process is not None:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                self._process.terminate()
            except Exception:
                pass
            self._process = None
        for handle in self._record_handles:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass

    def submit(self, packet: AudioPacket, pcm: np.ndarray) -> None:
        try:
            self._queue.put_nowait((packet, np.asarray(pcm, dtype=np.int16).copy()))
        except queue.Full:
            self.runtime_stats.audio_drops += 1
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait((packet, np.asarray(pcm, dtype=np.int16).copy()))
            except queue.Full:
                self.runtime_stats.audio_drops += 1

    def set_selection(self, channels: Set[int]) -> None:
        if any(channel not in range(4) for channel in channels):
            raise ValueError("audio channel indices must be 0..3")
        with self._selection_lock:
            changed = set(channels) != self._selected
            self._selected = set(channels)
        if changed:
            # The newly selected stream was intentionally not decoded in the
            # low-load mode.  Restart from a fresh contiguous reservoir rather
            # than concealing thousands of historical sequence numbers.
            self._restart_requested.set()
        LOG.info("Listening selection changed to: %s", self.selection_description())

    def selected_channels(self) -> Set[int]:
        with self._selection_lock:
            return set(self._selected)

    def selection_description(self) -> str:
        selected = sorted(channel + 1 for channel in self.selected_channels())
        return "none" if not selected else ",".join(str(value) for value in selected)

    def _close_aplay(self) -> None:
        if self._process is None:
            return
        try:
            if self._process.stdin:
                self._process.stdin.close()
        except Exception:
            pass
        try:
            self._process.terminate()
            self._process.wait(timeout=1.0)
        except Exception:
            try:
                self._process.kill()
            except Exception:
                pass
        self._process = None

    def _restart_for_selection_change(self) -> None:
        self._restart_requested.clear()
        self._close_aplay()
        self._started = False
        self._next_sequence = None
        self._pending = [dict() for _ in range(4)]
        self._last_audio = [
            np.zeros(AUDIO_FRAME_SAMPLES, dtype=np.float32) for _ in range(4)
        ]
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        LOG.info(
            "Audio selection changed; rebuilding a contiguous %d-frame reservoir for %s",
            self.startup_frames,
            self.selection_description(),
        )

    def _open_aplay(self) -> None:
        if not self.enabled or self._process is not None:
            return
        executable = shutil.which("aplay")
        if executable is None:
            LOG.error("aplay was not found; audio playback disabled")
            self.enabled = False
            return
        self._process = subprocess.Popen(
            [
                executable,
                "-q",
                "-D", self.audio_device,
                "-t", "raw",
                "-f", "S16_LE",
                "-r", str(AUDIO_SAMPLE_RATE),
                "-c", "1",
            ],
            stdin=subprocess.PIPE,
            # Keep a substantial userspace/pipe reservoir.  The old unbuffered
            # 640-byte write every 20 ms exposed ALSA directly to Python/Linux
            # scheduling jitter and produced multi-second underruns.
            bufsize=65_536,
        )

    @staticmethod
    def _sequence_distance(newer: int, older: int) -> int:
        return (newer - older) & 0xFFFF

    def _drain_input(self) -> None:
        while True:
            try:
                packet, pcm = self._queue.get_nowait()
            except queue.Empty:
                break
            self._pending[packet.stream_id][packet.sequence] = pcm
            handle = self._record_handles[packet.stream_id]
            if handle is not None:
                handle.writeframesraw(np.asarray(pcm, dtype="<i2").tobytes())

    def _choose_start_sequence(self) -> Optional[int]:
        """Return the first sequence with a contiguous startup reservoir.

        Counting dictionary entries is insufficient after receive drops: 75
        packets can span several hundred sequence numbers.  Playback must not
        start until every selected stream has the same contiguous run.
        """
        selected = self.selected_channels()
        if not selected:
            return None
        if any(len(self._pending[channel]) < self.startup_frames for channel in selected):
            return None
        common = set(self._pending[next(iter(selected))].keys())
        for channel in selected:
            common.intersection_update(self._pending[channel].keys())
        for start in sorted(common):
            if all(
                all(((start + offset) & 0xFFFF) in self._pending[channel]
                    for offset in range(self.startup_frames))
                for channel in selected
            ):
                return start
        return None

    def _sequence_can_render(self, sequence: int) -> bool:
        """True when a sequence is present or is known to have been skipped."""
        selected = self.selected_channels()
        if not selected:
            return True
        for channel in selected:
            if sequence in self._pending[channel]:
                continue
            # A newer packet proves this sequence will not arrive; concealment
            # is then appropriate.  If no newer packet exists, wait for RX.
            has_newer = any(
                0 < self._sequence_distance(candidate, sequence) < 0x8000
                for candidate in self._pending[channel]
            )
            if not has_newer:
                return False
        return True

    def _write_all(self, data: bytes) -> None:
        if not self.enabled or self._process is None or self._process.stdin is None:
            return
        view = memoryview(data)
        while view and not self.stop_event.is_set():
            written = self._process.stdin.write(view)
            if written is None:
                written = 0
            if written <= 0:
                raise BrokenPipeError("ALSA pipe accepted no data")
            view = view[written:]
        try:
            self._process.stdin.flush()
        except Exception:
            pass

    def _conceal(self, channel: int) -> np.ndarray:
        self.runtime_stats.audio_concealed[channel] += 1
        replacement = self._last_audio[channel] * 0.92
        self._last_audio[channel] = replacement
        return replacement

    def _mix_sequence(self, sequence: int) -> np.ndarray:
        selected = self.selected_channels()
        if not selected:
            return np.zeros(AUDIO_FRAME_SAMPLES, dtype=np.float32)
        components: List[np.ndarray] = []
        for channel in range(4):
            if channel in selected:
                pcm = self._pending[channel].pop(sequence, None)
                if pcm is None:
                    audio = self._conceal(channel)
                else:
                    audio = pcm.astype(np.float32) / 32768.0
                    self._last_audio[channel] = audio
                components.append(audio)
            else:
                self._pending[channel].pop(sequence, None)
        if not components:
            return np.zeros(AUDIO_FRAME_SAMPLES, dtype=np.float32)
        mixed = np.sum(components, axis=0) / max(1, len(components))
        return np.clip(0.95 * mixed, -1.0, 1.0).astype(np.float32)

    def _run(self) -> None:
        block_period = AUDIO_FRAME_SECONDS * self.write_frames
        try:
            while not self.stop_event.is_set():
                if self._restart_requested.is_set():
                    self._restart_for_selection_change()
                self._drain_input()
                if not self._started:
                    start = self._choose_start_sequence()
                    if start is None:
                        self.stop_event.wait(0.01)
                        continue
                    self._next_sequence = start
                    self._started = True
                    self._open_aplay()
                    LOG.info(
                        "Audio playback started at sequence %d after %d contiguous frames; "
                        "writing %d-frame ALSA blocks; listening to %s",
                        start,
                        self.startup_frames,
                        self.write_frames,
                        self.selection_description(),
                    )

                assert self._next_sequence is not None
                block_frames: List[np.ndarray] = []
                for _ in range(self.write_frames):
                    while not self.stop_event.is_set():
                        self._drain_input()
                        if self._sequence_can_render(self._next_sequence):
                            break
                        self.stop_event.wait(0.005)
                    if self.stop_event.is_set():
                        break
                    block_frames.append(self._mix_sequence(self._next_sequence))
                    self._next_sequence = (self._next_sequence + 1) & 0xFFFF

                if not block_frames:
                    continue
                mixed_block = np.concatenate(block_frames)
                pcm16 = np.rint(mixed_block * 32767.0).clip(-32768, 32767).astype("<i2")
                if self.enabled and self._process is not None:
                    try:
                        self._write_all(pcm16.tobytes())
                    except (BrokenPipeError, OSError) as exc:
                        LOG.error("ALSA playback pipe failed: %s", exc)
                        self.enabled = False
                else:
                    self.stop_event.wait(block_period)

                # Discard stale frames to bound memory after losses or a
                # listening-selection change.
                for channel in range(4):
                    stale = [
                        seq for seq in self._pending[channel]
                        if 0 < self._sequence_distance(self._next_sequence, seq) < 0x8000
                    ]
                    for seq in stale:
                        self._pending[channel].pop(seq, None)
        finally:
            if self._process is not None:
                try:
                    if self._process.stdin:
                        self._process.stdin.close()
                    self._process.terminate()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Hardware configuration
# ---------------------------------------------------------------------------

def set_debug_attribute(sdr: object, name: str, value: str) -> None:
    try:
        sdr._ctrl.debug_attrs[name].value = value
    except Exception as exc:
        LOG.debug("AD9361 debug attribute %s unavailable: %s", name, exc)


def configure_pluto_a_ports(sdr: object) -> None:
    try:
        sdr.rx_rf_port_select = "A_BALANCED"
    except Exception as exc:
        LOG.warning("Could not force RX A port: %s", exc)
    try:
        sdr.tx_rf_port_select = "A"
    except Exception as exc:
        LOG.warning("Could not force TX A port: %s", exc)


def _coerce_iio_scalar(value: object, attribute: str) -> int:
    """Convert a pyadi/libiio numeric result to one integer.

    A transient RNDIS/libiio read error has been observed to make the pyadi
    number parser return a list instead of a scalar.  The stock AD936x
    sample-rate setter then raises ``TypeError`` while comparing that list with
    an integer.  Select the first plausible value and log the anomaly.
    """
    if isinstance(value, np.ndarray):
        values = value.reshape(-1).tolist()
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        return int(value)
    numeric: List[int] = []
    for item in values:
        try:
            numeric.append(int(item))
        except Exception:
            continue
    if not numeric:
        raise RuntimeError(f"IIO attribute {attribute} returned no numeric value: {value!r}")
    if "sampling_frequency" in attribute or "sample_rate" in attribute:
        plausible = [item for item in numeric if item > 100_000]
    elif "fir_en" in attribute:
        plausible = [item for item in numeric if item in (0, 1)]
    else:
        plausible = numeric
    selected = plausible[0] if plausible else numeric[0]
    LOG.warning("IIO attribute %s returned %r; using scalar %d", attribute, value, selected)
    return int(selected)


def set_ad9361_sample_rate_robust(sdr: object, rate: int) -> int:
    """Set a low AD9361 sample rate while tolerating malformed scalar reads.

    pyadi-iio's low-rate setter reads the current sampling frequency before
    reconfiguring the FIR.  The temporary getter wrapper normalizes any list
    result.  A direct 3 MSPS/FIR-disable reset mirrors the recovery sequence in
    the official pyadi setter and is used only after a failed attempt.
    """
    original_getter = sdr._get_iio_attr

    def scalar_getter(*getter_args, **getter_kwargs):
        result = original_getter(*getter_args, **getter_kwargs)
        attribute = str(getter_args[1]) if len(getter_args) > 1 else "unknown"
        return _coerce_iio_scalar(result, attribute) if isinstance(result, (list, tuple, np.ndarray)) else result

    sdr._get_iio_attr = scalar_getter
    try:
        last_error: Optional[BaseException] = None
        for attempt in range(1, 4):
            try:
                sdr.sample_rate = int(rate)
                actual = _coerce_iio_scalar(sdr.sample_rate, "sample_rate")
                return actual
            except (TypeError, OSError, RuntimeError, ValueError) as exc:
                last_error = exc
                LOG.warning(
                    "AD9361 sample-rate configuration attempt %d/3 failed: %s",
                    attempt,
                    exc,
                )
                try:
                    sdr._set_iio_attr("voltage0", "sampling_frequency", False, 3_000_000)
                    sdr._set_iio_attr("out", "voltage_filter_fir_en", False, 0)
                except Exception as reset_exc:
                    LOG.debug("AD9361 low-rate reset failed: %s", reset_exc)
                time.sleep(0.25 * attempt)
        raise RuntimeError(f"could not configure AD9361 sample rate {rate}: {last_error}")
    finally:
        sdr._get_iio_attr = original_getter


def load_calibration(phaser: object, directory: Optional[Path]) -> bool:
    if directory is None:
        return False
    paths = {
        "channel": directory / "channel_cal_val.pkl",
        "gain": directory / "gain_cal_val.pkl",
        "phase": directory / "phase_cal_val.pkl",
    }
    if not all(path.is_file() for path in paths.values()):
        LOG.warning("Calibration files incomplete in %s; using uncalibrated values", directory)
        return False
    phaser.load_channel_cal(str(paths["channel"]))
    phaser.load_gain_cal(str(paths["gain"]))
    phaser.load_phase_cal(str(paths["phase"]))
    LOG.info("Loaded CN0566 calibration from %s", directory)
    return True


def configure_hardware(args: argparse.Namespace) -> Tuple[object, object, Optional[int]]:
    try:
        import adi
        from adi.cn0566 import CN0566
    except ImportError as exc:
        raise RuntimeError("pyadi-iio/CN0566 support is not installed") from exc

    LOG.info("Connecting to CN0566 at %s", args.phaser_uri)
    phaser = CN0566(uri=args.phaser_uri)
    LOG.info("Connecting to PlutoSDR at %s", args.sdr_uri)
    sdr = adi.ad9361(uri=args.sdr_uri)
    phaser.sdr = sdr
    try:
        sdr._ctx.set_timeout(5000)
    except Exception:
        pass

    phaser.configure(device_mode="rx")
    set_debug_attribute(sdr, "adi,frequency-division-duplex-mode-enable", "1")
    set_debug_attribute(sdr, "adi,ensm-enable-txnrx-control-enable", "0")
    set_debug_attribute(sdr, "initialize", "1")
    time.sleep(0.25)
    configure_pluto_a_ports(sdr)

    if args.decode_mode == "mrc":
        sdr.rx_enabled_channels = [PLUTO_RX1_COMPLEX_CHANNEL, PLUTO_RX2_COMPLEX_CHANNEL]
    else:
        sdr.rx_enabled_channels = [PLUTO_RX2_COMPLEX_CHANNEL]
    sdr.tx_enabled_channels = [PLUTO_TX_COMPLEX_CHANNEL]
    try:
        sdr._rxadc.set_kernel_buffers_count(4)
    except Exception:
        pass
    try:
        sdr._txdac.set_kernel_buffers_count(8)
    except Exception:
        pass

    try:
        sdr.rx_destroy_buffer()
    except Exception:
        pass
    try:
        sdr.tx_destroy_buffer()
    except Exception:
        pass

    actual_sample_rate = set_ad9361_sample_rate_robust(sdr, SAMPLE_RATE)
    if abs(actual_sample_rate - SAMPLE_RATE) > 5_000:
        raise RuntimeError(f"Pluto sample-rate readback {actual_sample_rate} differs from requested {SAMPLE_RATE}")
    sdr.rx_lo = PLUTO_IF_HZ
    sdr.tx_lo = PLUTO_IF_HZ
    sdr.rx_rf_bandwidth = RF_BANDWIDTH_HZ
    sdr.tx_rf_bandwidth = RF_BANDWIDTH_HZ
    sdr.rx_buffer_size = RX_BUFFER_SAMPLES
    sdr.tx_cyclic_buffer = False

    sdr.gain_control_mode_chan0 = "manual"
    sdr.gain_control_mode_chan1 = "manual"
    sdr.rx_hardwaregain_chan0 = float(args.rx_gain)
    sdr.rx_hardwaregain_chan1 = float(args.rx_gain)
    sdr.tx_hardwaregain_chan0 = -88.0
    sdr.tx_hardwaregain_chan1 = float(args.tx_gain)
    try:
        sdr.dds_enabled = [0] * 8
        sdr.dds_scales = [0.0] * 8
    except Exception:
        pass

    phaser.freq_dev_range = 0
    phaser.freq_dev_step = 0
    phaser.freq_dev_time = 0
    phaser.ramp_mode = "disabled"
    try:
        phaser.powerdown = 0
    except Exception:
        pass
    try:
        phaser.enable = 0
    except Exception:
        pass
    phaser.lo = CN0566_LO_HZ
    phaser.set_tx_sw_div(0)
    phaser._gpios.gpio_tx_sw = CN0566_TX_SWITCH_GPIO_VALUE

    calibrated = load_calibration(phaser, args.calibration_dir)
    for element in range(8):
        phaser.set_chan_gain(element, int(args.array_gain), apply_cal=calibrated)
    phaser.set_beam_phase_diff(float(args.beam_phase_diff))

    lock: Optional[int] = None
    try:
        phaser.muxout_sel = 6
        time.sleep(0.2)
        lock = int(phaser._gpios.gpio_muxout)
    except Exception:
        pass

    LOG.info("RF port profile: %s", PORT_PROFILE_DESCRIPTION)
    LOG.info(
        "OFDM configured: Fs=%d, FFT=%d, CP=%d, data=%d, pilots=%d, occupied=%.4f MHz",
        actual_sample_rate,
        NFFT,
        CP_LEN,
        DATA_BINS.size,
        PILOT_BINS.size,
        OCCUPIED_BANDWIDTH_HZ / 1e6,
    )
    LOG.info(
        "IF=%.6f GHz, CN0566 LO=%.6f GHz, nominal RF center=%.6f GHz, TX_OUT_2",
        PLUTO_IF_HZ / 1e9,
        CN0566_LO_HZ / 1e9,
        NOMINAL_XBAND_RF_HZ / 1e9,
    )
    LOG.info(
        "Pluto channels: TX=%s, RX=%s; decode mode=%s; PLL lock=%s",
        list(sdr.tx_enabled_channels),
        list(sdr.rx_enabled_channels),
        args.decode_mode,
        "unavailable" if lock is None else str(lock),
    )
    return phaser, sdr, lock


# ---------------------------------------------------------------------------
# Runtime worker functions
# ---------------------------------------------------------------------------

def _normalize_rx_return(data: object, decode_mode: str) -> Tuple[np.ndarray, Optional[np.ndarray]]:
    if decode_mode == "mrc":
        if not isinstance(data, Sequence) or len(data) != 2:
            raise RuntimeError("MRC mode expected two Pluto RX arrays")
        rx1 = np.asarray(data[0], dtype=np.complex64).copy()
        rx2 = np.asarray(data[1], dtype=np.complex64).copy()
        return rx2, rx1
    if isinstance(data, Sequence) and not isinstance(data, np.ndarray):
        if len(data) != 1:
            raise RuntimeError("RX2 mode expected one Pluto RX array")
        return np.asarray(data[0], dtype=np.complex64).copy(), None
    return np.asarray(data, dtype=np.complex64).copy(), None


def rx_acquisition_worker(
    sdr: object,
    decode_mode: str,
    output_queue: "queue.Queue[Tuple[np.ndarray, Optional[np.ndarray]]]",
    stop_event: threading.Event,
    runtime_stats: RuntimeStatistics,
) -> None:
    try:
        while not stop_event.is_set():
            rx2, rx1 = _normalize_rx_return(sdr.rx(), decode_mode)
            item = (rx2, rx1)
            try:
                output_queue.put_nowait(item)
            except queue.Full:
                runtime_stats.rx_queue_drops += 1
                try:
                    output_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    output_queue.put_nowait(item)
                except queue.Full:
                    runtime_stats.rx_queue_drops += 1
    except Exception:
        LOG.exception("RX acquisition worker failed")
        stop_event.set()


def tx_producer_worker(
    builder: OfdmFrameBuilder,
    source: FourAudioSource,
    output_queue: "queue.Queue[np.ndarray]",
    stop_event: threading.Event,
    runtime_stats: RuntimeStatistics,
    peak_target: float,
    rms_target: float,
) -> None:
    try:
        while not stop_event.is_set():
            chunk, _packets, rms = builder.build_scaled_chunk(
                source,
                superframes=TX_SUPERFRAMES_PER_BUFFER,
                peak_target=peak_target,
                rms_target=rms_target,
            )
            while not stop_event.is_set():
                try:
                    output_queue.put(chunk, timeout=0.25)
                    break
                except queue.Full:
                    continue
            if stop_event.is_set():
                break
            runtime_stats.tx_chunks_built += 1
            runtime_stats.tx_queue_depth = output_queue.qsize()
            LOG.debug(
                "Built OFDM TX chunk: samples=%d RMS=%.1f clip=%.4f%% PAPR=%.2f dB",
                chunk.size,
                rms,
                100.0 * builder.last_clip_fraction,
                builder.last_output_papr_db,
            )
    except Exception:
        LOG.exception("TX producer worker failed")
        stop_event.set()


def tx_pusher_worker(
    sdr: object,
    input_queue: "queue.Queue[np.ndarray]",
    stop_event: threading.Event,
    runtime_stats: RuntimeStatistics,
) -> None:
    try:
        while not stop_event.is_set():
            try:
                chunk = input_queue.get(timeout=1.2)
            except queue.Empty:
                runtime_stats.tx_starves += 1
                continue
            sdr.tx(chunk)
            runtime_stats.tx_chunks_pushed += 1
            runtime_stats.tx_queue_depth = input_queue.qsize()
    except Exception:
        LOG.exception("TX pusher worker failed")
        stop_event.set()

# ---------------------------------------------------------------------------
# Selection, reporting and simulation helpers
# ---------------------------------------------------------------------------

def parse_listen_selection(text: str) -> Set[int]:
    value = text.strip().lower()
    if value in {"all", "1,2,3,4", "*"}:
        return {0, 1, 2, 3}
    if value in {"none", "off", "mute", ""}:
        return set()
    result: Set[int] = set()
    for token in value.replace("+", ",").split(","):
        token = token.strip()
        if not token:
            continue
        number = int(token)
        if number not in {1, 2, 3, 4}:
            raise ValueError("listen channels must be selected from 1,2,3,4")
        result.add(number - 1)
    return result


def profile_summary() -> str:
    return (
        f"profile={PROFILE_NAME}, Fs={SAMPLE_RATE/1e6:.3f} MSPS, FFT={NFFT}, CP={CP_LEN}, "
        f"data={DATA_BINS.size}, pilots={PILOT_BINS.size}, "
        f"raw data-carrier={RAW_DATA_CARRIER_BPS/1e6:.3f} Mbps, "
        f"rate-1/2 framed={NET_RATE_HALF_FEC_BPS/1e6:.3f} Mbps"
    )


def format_status(
    receiver: StreamingOfdmReceiver,
    runtime: RuntimeStatistics,
    elapsed: float,
    mixer: Optional[SelectableAudioMixer] = None,
) -> str:
    stats = receiver.stats
    rate = stats.frames_decoded / max(elapsed, 1e-9)
    rate_recent = receiver.recent_frame_rate()
    channel_parts = []
    for channel in range(4):
        evm = stats.last_evm_db[channel]
        ber = stats.last_prbs_ber[channel]
        channel_parts.append(
            f"ch{channel+1}:ok={stats.packet_valid[channel]} fail={stats.packet_fail[channel]} "
            f"gap={stats.sequence_gaps[channel]} EVM={'nan' if not math.isfinite(evm) else f'{evm:.1f}'}dB "
            f"BER={'nan' if not math.isfinite(ber) else f'{ber:.2e}'}"
        )
    listen = mixer.selection_description() if mixer is not None else "n/a"
    return (
        f"frames={stats.frames_decoded}/{stats.frames_detected} "
        f"rate={rate:.1f}/s rate5={rate_recent:.1f}/s decode={receiver.active_decode_description()} "
        f"lock={int(receiver.locked)} det={stats.last_detector_peak:.3f}/{receiver.correlation_threshold:.3f} "
        f"corr={stats.last_correlation:.3f} "
        f"CFO={'nan' if not math.isfinite(stats.last_cfo_hz) else f'{stats.last_cfo_hz:+.1f}'}Hz "
        f"RMS={stats.last_rx_rms:.2f} peak={stats.last_rx_peak:.1f} "
        f"host_drop={runtime.rx_queue_drops} tx={runtime.tx_chunks_pushed}/{runtime.tx_chunks_built} "
        f"tx_q={runtime.tx_queue_depth} tx_starve={runtime.tx_starves} "
        f"audio_drop={runtime.audio_drops} conceal={sum(runtime.audio_concealed)} "
        f"listen={listen} | "
        + " | ".join(channel_parts)
    )


def apply_fractional_delay(source: np.ndarray, delay: float, taps: int = 33) -> np.ndarray:
    """Apply a windowed-sinc fractional delay for software validation."""
    integer = int(math.floor(delay))
    fraction = delay - integer
    n = np.arange(taps, dtype=np.float64) - (taps - 1) / 2.0
    h = np.sinc(n - fraction) * np.hamming(taps)
    h /= np.sum(h)
    filtered = np.convolve(np.asarray(source, dtype=np.complex64), h, mode="full")
    output = np.concatenate((np.zeros(integer, dtype=np.complex64), filtered.astype(np.complex64)))
    return output


class DiagnosticCollector:
    """Bounded collection of per-channel symbols and waveforms for report plots."""

    def __init__(self, max_symbols_per_channel: int = 120_000) -> None:
        self.max_symbols = int(max_symbols_per_channel)
        self.symbols: List[List[np.ndarray]] = [[], [], [], []]
        self.waveforms: List[Optional[np.ndarray]] = [None, None, None, None]
        self.frame_diagnostics: List[OfdmFrameDiagnostics] = []
        self.packet_counts = [0, 0, 0, 0]
        self.frame_count = 0

    def __call__(self, diagnostics: OfdmFrameDiagnostics) -> None:
        self.frame_count += 1
        self.frame_diagnostics.append(diagnostics)
        if len(self.frame_diagnostics) > 2000:
            self.frame_diagnostics.pop(0)
        for channel, item in diagnostics.channels.items():
            if item.packet_valid:
                self.packet_counts[channel] += 1
            current = sum(block.size for block in self.symbols[channel])
            if item.equalized_symbols is not None and current < self.max_symbols:
                remaining = self.max_symbols - current
                self.symbols[channel].append(item.equalized_symbols[:remaining].copy())
            if self.waveforms[channel] is None and item.reconstructed_waveform is not None:
                self.waveforms[channel] = item.reconstructed_waveform.copy()

    def needs_waveform(self, channel: int) -> bool:
        return self.waveforms[channel] is None

    def concatenated_symbols(self, channel: int) -> np.ndarray:
        if not self.symbols[channel]:
            return np.empty(0, dtype=np.complex64)
        return np.concatenate(self.symbols[channel]).astype(np.complex64, copy=False)


def run_software_simulation(
    wav_paths: Sequence[Path],
    frames: int = 20,
    snr_db: float = 24.0,
    cfo_hz: float = 180.0,
    fractional_delay: float = 0.37,
    multipath: bool = True,
    decode_mode: str = "rx2",
    diagnostics_collector: Optional[DiagnosticCollector] = None,
) -> Dict[str, object]:
    if len(wav_paths) != 4:
        raise ValueError("four WAV paths are required")
    fec = FecCodec()
    LOG.info("FEC backend: %s", fec.backend)
    pcm = [load_wav_as_pcm16(Path(path)) for path in wav_paths]
    source = FourAudioSource(pcm, repeat=True)
    builder = OfdmFrameBuilder(fec)

    expected: Dict[Tuple[int, int], bytes] = {}
    tx_frames: List[np.ndarray] = []
    for _ in range(frames):
        packets = source.next_packets()
        for packet in packets:
            expected[(packet.stream_id, packet.sequence)] = packet.raw
        tx_frames.append(builder.build_unscaled_superframe(packets))
    tx = np.concatenate(tx_frames).astype(np.complex64)
    tx /= max(float(np.sqrt(np.mean(np.abs(tx) ** 2))), 1e-12)

    delayed = apply_fractional_delay(tx, fractional_delay)
    if multipath:
        channel = np.zeros(delayed.size + 13, dtype=np.complex64)
        channel[:delayed.size] += 0.82 * np.exp(1j * 0.4) * delayed
        channel[5:5 + delayed.size] += 0.24 * np.exp(-1j * 0.9) * delayed
        channel[13:13 + delayed.size] += 0.10 * np.exp(1j * 1.7) * delayed
    else:
        channel = 0.9 * np.exp(1j * 0.4) * delayed

    n = np.arange(channel.size, dtype=np.float64)
    channel *= np.exp(1j * 2.0 * math.pi * cfo_hz * n / SAMPLE_RATE).astype(np.complex64)
    signal_power = float(np.mean(np.abs(channel) ** 2))
    noise_power = signal_power / (10.0 ** (snr_db / 10.0))
    rng = np.random.default_rng(0xC0566F99)
    noise = math.sqrt(noise_power / 2.0) * (
        rng.standard_normal(channel.size) + 1j * rng.standard_normal(channel.size)
    )
    rx2 = (channel + noise).astype(np.complex64)
    rx1 = (0.55 * np.exp(-1j * 0.7) * channel + 1.2 * noise).astype(np.complex64)

    received: Dict[Tuple[int, int], bytes] = {}

    def packet_callback(packet: AudioPacket, _pcm: np.ndarray) -> None:
        received[(packet.stream_id, packet.sequence)] = packet.raw

    receiver = StreamingOfdmReceiver(
        fec=fec,
        packet_callback=packet_callback,
        diagnostics_callback=diagnostics_collector,
        correlation_threshold=0.20,
        decode_mode=decode_mode,
    )

    leading = np.zeros(7777, dtype=np.complex64)
    trailing = np.zeros(2 * SUPERFRAME_SAMPLES, dtype=np.complex64)
    rx2_stream = np.concatenate((leading, rx2, trailing))
    rx1_stream = np.concatenate((leading, rx1, trailing))
    for start in range(0, rx2_stream.size, RX_BUFFER_SAMPLES):
        block2 = rx2_stream[start:start + RX_BUFFER_SAMPLES]
        block1 = rx1_stream[start:start + RX_BUFFER_SAMPLES]
        receiver.feed(block2, block1 if decode_mode == "mrc" else None)

    matches = 0
    mismatches: List[Tuple[int, int]] = []
    for key, raw in expected.items():
        if received.get(key) == raw:
            matches += 1
        else:
            mismatches.append(key)
    result = {
        "frames": frames,
        "expected_packets": len(expected),
        "received_packets": len(received),
        "matching_packets": matches,
        "mismatches": mismatches,
        "receiver": receiver,
        "fec_backend": fec.backend,
        "snr_db": snr_db,
        "cfo_hz": cfo_hz,
        "fractional_delay": fractional_delay,
    }
    return result


__all__ = [name for name in globals() if not name.startswith("_")]
