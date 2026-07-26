#!/usr/bin/env python3
"""Real-time single-carrier QPSK audio link for CN0566 + PlutoSDR.

Hardware profile
----------------
Pluto TX2A      -> CN0566 TX_IN
CN0566 TX_OUT_2 -> external Vivaldi TX antenna
CN0566 RX1      -> Pluto RX1A
CN0566 RX2      -> Pluto RX2A

The default transceive mode runs transmit, receive, decoding, and playback on
one Raspberry Pi. The waveform and packet format preserve the documented
1.2.0-realtime-audio project behavior while this GitHub consolidation adds
repository tests and packaging metadata.

This is prototype software, not regulatory authorization. Transmit only in a
shielded setup or where the selected X-band emission is permitted. Begin with
high Pluto TX attenuation and verify that the receive chain is not compressed.
"""
from __future__ import annotations

import argparse
import collections
import ctypes
import dataclasses
import logging
import math
import os
import queue
import shutil
import signal as os_signal
import struct
import subprocess
import sys
import threading
import time
import warnings
import wave
import zlib
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from scipy import signal
from scipy.io import wavfile

LOG = logging.getLogger("cn0566-qpsk")

# ---------------------------------------------------------------------------
# Fixed modem profile
# ---------------------------------------------------------------------------

SAMPLE_RATE = 1_000_000
SYMBOL_RATE = 250_000
SAMPLES_PER_SYMBOL = 4
RRC_ROLLOFF = 0.35
RRC_SPAN_SYMBOLS = 10
DIGITAL_OFFSET_HZ = 187_500
PLUTO_IF_HZ = 2_200_000_000
CN0566_LO_HZ = 12_400_000_000
NOMINAL_XBAND_RF_HZ = CN0566_LO_HZ - (PLUTO_IF_HZ + DIGITAL_OFFSET_HZ)
RF_BANDWIDTH_HZ = 1_000_000

AUDIO_SAMPLE_RATE = 16_000
AUDIO_FRAME_SAMPLES = 320
AUDIO_FRAME_SECONDS = AUDIO_FRAME_SAMPLES / AUDIO_SAMPLE_RATE
FRAME_PERIOD_SAMPLES = int(round(SAMPLE_RATE * AUDIO_FRAME_SECONDS))

RX_BUFFER_SAMPLES = 32_768
RX_QUEUE_BUFFERS = 32
TX_FRAMES_PER_BUFFER = 50
TX_PREFETCH_CHUNKS = 6
TX_INITIAL_PREFILL_CHUNKS = 2
TX_DIGITAL_AMPLITUDE = 18_000.0
LEADING_GUARD_SAMPLES = 512

MAGIC = b"QA"
PROTOCOL_VERSION = 1
HEADER_FORMAT = ">2sBHHB"  # magic, version, sequence, valid samples, flags
HEADER_BYTES = struct.calcsize(HEADER_FORMAT)
CRC_BYTES = 4
ADPCM_HEADER_BYTES = 4
ADPCM_CODE_BYTES = math.ceil((AUDIO_FRAME_SAMPLES - 1) / 2)
PAYLOAD_BYTES = ADPCM_HEADER_BYTES + ADPCM_CODE_BYTES
PACKET_BYTES = HEADER_BYTES + PAYLOAD_BYTES + CRC_BYTES
UNCODED_PACKET_BITS = PACKET_BYTES * 8

FLAG_START_OF_FILE = 0x01
FLAG_END_OF_FILE = 0x02
FLAG_IDLE = 0x04

CONSTRAINT_LENGTH = 7
CONV_TAIL_BITS = CONSTRAINT_LENGTH - 1
CONV_G0 = 0o133
CONV_G1 = 0o171
CONV_STATES = 1 << (CONSTRAINT_LENGTH - 1)
_CONV_PARITY = tuple(bin(v).count("1") & 1 for v in range(1 << CONSTRAINT_LENGTH))
CODED_PACKET_BITS = 2 * (UNCODED_PACKET_BITS + CONV_TAIL_BITS)
DATA_QPSK_SYMBOLS = CODED_PACKET_BITS // 2

CORRELATION_THRESHOLD_DEFAULT = 0.18
TRACK_SEARCH_RADIUS_SAMPLES = 64
TRACK_TX_BOUNDARY_RADIUS_SAMPLES = 4096
TRACK_MAX_CONSECUTIVE_MISSES = 3
RECENT_SEQUENCE_WINDOW = 256

if SAMPLE_RATE != SYMBOL_RATE * SAMPLES_PER_SYMBOL:
    raise RuntimeError("sample rate must equal symbol rate times samples per symbol")


# ---------------------------------------------------------------------------
# Data classes and runtime statistics
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class EncodedPacket:
    sequence: int
    valid_samples: int
    flags: int
    payload: bytes
    raw: bytes


@dataclasses.dataclass(frozen=True)
class DecodedPacket:
    sequence: int
    valid_samples: int
    flags: int
    payload: bytes
    correlation: float
    evm_db: float
    residual_cfo_hz: float
    timing_offset_samples: float


@dataclasses.dataclass
class ReceiverStats:
    detected_candidates: int = 0
    valid_packets: int = 0
    crc_or_decode_failures: int = 0
    duplicate_packets: int = 0
    host_queue_drops: int = 0
    audio_queue_drops: int = 0
    tracked_packets: int = 0
    acquired_packets: int = 0
    tracking_misses: int = 0
    reacquisitions: int = 0
    last_correlation: float = 0.0
    strongest_detector_metric: float = 0.0
    last_evm_db: float = float("nan")
    last_cfo_hz: float = float("nan")
    last_timing_offset: float = float("nan")
    largest_rx_magnitude: float = 0.0
    last_rms_rx1: float = 0.0
    last_rms_rx2: float = 0.0
    first_valid_monotonic: Optional[float] = None


@dataclasses.dataclass
class TxStats:
    chunks_encoded: int = 0
    chunks_pushed: int = 0
    queue_starvations: int = 0


@dataclasses.dataclass
class AudioStats:
    concealed_frames: int = 0
    queue_drops: int = 0


# ---------------------------------------------------------------------------
# DSP primitives
# ---------------------------------------------------------------------------

def root_raised_cosine_taps(alpha: float, samples_per_symbol: int, span_symbols: int) -> np.ndarray:
    """Return an odd-length, unit-energy root-raised-cosine FIR."""
    count = span_symbols * samples_per_symbol
    if count % 2:
        raise ValueError("span_symbols * samples_per_symbol must be even")
    t = np.arange(-count / 2, count / 2 + 1, dtype=np.float64) / samples_per_symbol
    taps = np.empty_like(t)
    for i, value in enumerate(t):
        if abs(value) < 1e-12:
            taps[i] = 1.0 + alpha * (4.0 / math.pi - 1.0)
        elif alpha > 0.0 and abs(abs(value) - 1.0 / (4.0 * alpha)) < 1e-10:
            taps[i] = (alpha / math.sqrt(2.0)) * (
                (1.0 + 2.0 / math.pi) * math.sin(math.pi / (4.0 * alpha))
                + (1.0 - 2.0 / math.pi) * math.cos(math.pi / (4.0 * alpha))
            )
        else:
            numerator = math.sin(math.pi * value * (1.0 - alpha))
            numerator += 4.0 * alpha * value * math.cos(math.pi * value * (1.0 + alpha))
            denominator = math.pi * value * (1.0 - (4.0 * alpha * value) ** 2)
            taps[i] = numerator / denominator
    taps /= math.sqrt(float(np.sum(taps * taps)))
    return taps.astype(np.float32)


RRC_TAPS = root_raised_cosine_taps(RRC_ROLLOFF, SAMPLES_PER_SYMBOL, RRC_SPAN_SYMBOLS)
RRC_GROUP_DELAY = (len(RRC_TAPS) - 1) // 2


def qpsk_map(bits: np.ndarray) -> np.ndarray:
    """Gray-compatible mapping: bit 0 controls I and bit 1 controls Q."""
    b = np.asarray(bits, dtype=np.uint8).reshape(-1)
    if b.size % 2:
        raise ValueError("QPSK mapper needs an even number of bits")
    i = 1.0 - 2.0 * b[0::2].astype(np.float32)
    q = 1.0 - 2.0 * b[1::2].astype(np.float32)
    return ((i + 1j * q) / math.sqrt(2.0)).astype(np.complex64)


def pulse_shape(symbols: np.ndarray) -> np.ndarray:
    symbols = np.asarray(symbols, dtype=np.complex64).reshape(-1)
    up = np.zeros(symbols.size * SAMPLES_PER_SYMBOL, dtype=np.complex64)
    up[::SAMPLES_PER_SYMBOL] = symbols
    return np.convolve(up, RRC_TAPS, mode="full").astype(np.complex64)


_PREAMBLE_RNG = np.random.default_rng(0xC0566)
_a_bits = _PREAMBLE_RNG.integers(0, 2, 64 * 2, dtype=np.uint8)
_b_bits = _PREAMBLE_RNG.integers(0, 2, 64 * 2, dtype=np.uint8)
_a_symbols = qpsk_map(_a_bits)
_b_symbols = qpsk_map(_b_bits)
PREAMBLE_SYMBOLS = np.concatenate((_a_symbols, _a_symbols, _a_symbols, _b_symbols)).astype(np.complex64)
PREAMBLE_SYMBOL_COUNT = PREAMBLE_SYMBOLS.size
DETECTOR_SYMBOL_COUNT = 192
TOTAL_FRAME_SYMBOLS = (FRAME_PERIOD_SAMPLES - LEADING_GUARD_SAMPLES - (len(RRC_TAPS) - 1)) // SAMPLES_PER_SYMBOL
FILLER_SYMBOL_COUNT = TOTAL_FRAME_SYMBOLS - PREAMBLE_SYMBOL_COUNT - DATA_QPSK_SYMBOLS
if TOTAL_FRAME_SYMBOLS != 4862 or FILLER_SYMBOL_COUNT <= 0:
    raise RuntimeError("unexpected frame geometry")
_FILLER_RNG = np.random.default_rng(0x5150534B)
FILLER_SYMBOLS = qpsk_map(_FILLER_RNG.integers(0, 2, FILLER_SYMBOL_COUNT * 2, dtype=np.uint8))

_TX_CARRIER = np.exp(
    1j * 2.0 * math.pi * DIGITAL_OFFSET_HZ * np.arange(FRAME_PERIOD_SAMPLES) / SAMPLE_RATE
).astype(np.complex64)
_RX_DOWNMIX = np.conj(
    np.exp(1j * 2.0 * math.pi * DIGITAL_OFFSET_HZ * np.arange(RX_BUFFER_SAMPLES) / SAMPLE_RATE)
).astype(np.complex64)

# Time-domain acquisition template after intentional digital downconversion.
_DETECTOR_TEMPLATE = pulse_shape(PREAMBLE_SYMBOLS[:DETECTOR_SYMBOL_COUNT])
_DETECTOR_TEMPLATE_ENERGY = float(np.vdot(_DETECTOR_TEMPLATE, _DETECTOR_TEMPLATE).real)


# ---------------------------------------------------------------------------
# IMA-ADPCM block codec
# ---------------------------------------------------------------------------

_IMA_INDEX_TABLE = np.array(
    [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8], dtype=np.int16
)
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
    ], dtype=np.int32
)


def _ima_decode_nibble(code: int, predictor: int, index: int) -> Tuple[int, int]:
    step = int(_IMA_STEP_TABLE[index])
    delta = step >> 3
    if code & 1:
        delta += step >> 2
    if code & 2:
        delta += step >> 1
    if code & 4:
        delta += step
    predictor += -delta if code & 8 else delta
    predictor = max(-32768, min(32767, predictor))
    index = max(0, min(88, index + int(_IMA_INDEX_TABLE[code & 0xF])))
    return predictor, index


def ima_adpcm_encode_block(samples: np.ndarray) -> bytes:
    pcm = np.asarray(samples, dtype=np.int16).reshape(-1)
    if pcm.size != AUDIO_FRAME_SAMPLES:
        raise ValueError(f"expected {AUDIO_FRAME_SAMPLES} PCM samples")
    predictor = int(pcm[0])
    index = 0
    nibbles: List[int] = []
    for value in pcm[1:]:
        target = int(value)
        step = int(_IMA_STEP_TABLE[index])
        diff = target - predictor
        code = 8 if diff < 0 else 0
        magnitude = abs(diff)
        estimate = step >> 3
        if magnitude >= step:
            code |= 4
            magnitude -= step
            estimate += step
        if magnitude >= (step >> 1):
            code |= 2
            magnitude -= step >> 1
            estimate += step >> 1
        if magnitude >= (step >> 2):
            code |= 1
            estimate += step >> 2
        predictor += -estimate if code & 8 else estimate
        predictor = max(-32768, min(32767, predictor))
        index = max(0, min(88, index + int(_IMA_INDEX_TABLE[code])))
        nibbles.append(code & 0xF)
    packed = bytearray(ADPCM_CODE_BYTES)
    for i, code in enumerate(nibbles):
        if i & 1:
            packed[i // 2] |= (code & 0xF) << 4
        else:
            packed[i // 2] = code & 0xF
    return struct.pack("<hBB", int(pcm[0]), 0, 0) + bytes(packed)


def ima_adpcm_decode_block(payload: bytes) -> np.ndarray:
    if len(payload) != PAYLOAD_BYTES:
        raise ValueError(f"expected {PAYLOAD_BYTES} ADPCM bytes")
    predictor, index, _reserved = struct.unpack("<hBB", payload[:4])
    out = np.empty(AUDIO_FRAME_SAMPLES, dtype=np.int16)
    out[0] = predictor
    sample_index = 1
    for byte in payload[4:]:
        for code in (byte & 0xF, (byte >> 4) & 0xF):
            if sample_index >= out.size:
                break
            predictor, index = _ima_decode_nibble(code, predictor, index)
            out[sample_index] = predictor
            sample_index += 1
    return out


# ---------------------------------------------------------------------------
# Packet and FEC
# ---------------------------------------------------------------------------

def build_packet(sequence: int, pcm: np.ndarray, valid_samples: int, flags: int) -> EncodedPacket:
    payload = ima_adpcm_encode_block(pcm)
    header = struct.pack(
        HEADER_FORMAT,
        MAGIC,
        PROTOCOL_VERSION,
        sequence & 0xFFFF,
        int(valid_samples) & 0xFFFF,
        flags & 0xFF,
    )
    body = header + payload
    raw = body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    if len(raw) != PACKET_BYTES:
        raise RuntimeError("packet geometry mismatch")
    return EncodedPacket(sequence & 0xFFFF, valid_samples, flags, payload, raw)


def parse_packet(raw: bytes) -> Tuple[int, int, int, bytes]:
    if len(raw) != PACKET_BYTES:
        raise ValueError("incorrect packet size")
    body, crc_bytes = raw[:-4], raw[-4:]
    expected_crc = struct.unpack(">I", crc_bytes)[0]
    if zlib.crc32(body) & 0xFFFFFFFF != expected_crc:
        raise ValueError("CRC mismatch")
    magic, version, sequence, valid_samples, flags = struct.unpack(HEADER_FORMAT, body[:HEADER_BYTES])
    if magic != MAGIC or version != PROTOCOL_VERSION:
        raise ValueError("packet header mismatch")
    return sequence, valid_samples, flags, body[HEADER_BYTES:]


def convolutional_encode(bits: np.ndarray) -> np.ndarray:
    b = np.asarray(bits, dtype=np.uint8).reshape(-1)
    terminated = np.concatenate((b, np.zeros(CONV_TAIL_BITS, dtype=np.uint8)))
    out = np.empty(2 * terminated.size, dtype=np.uint8)
    state = 0
    for i, bit in enumerate(terminated):
        register = ((state << 1) | int(bit)) & 0x7F
        out[2 * i] = _CONV_PARITY[register & CONV_G0]
        out[2 * i + 1] = _CONV_PARITY[register & CONV_G1]
        state = register & 0x3F
    return out


class ViterbiDecoder:
    def __init__(self) -> None:
        self.backend = "NumPy fallback"
        self._lib = None
        path = Path(__file__).with_name("libcn0566_viterbi.so")
        if path.is_file():
            try:
                lib = ctypes.CDLL(str(path))
                fn = lib.cn0566_viterbi_decode
                fn.argtypes = [
                    ctypes.POINTER(ctypes.c_float), ctypes.c_int,
                    ctypes.POINTER(ctypes.c_uint8), ctypes.c_int,
                ]
                fn.restype = ctypes.c_int
                self._lib = lib
                self._fn = fn
                self.backend = "native C"
            except Exception as exc:
                LOG.warning("native Viterbi unavailable: %s", exc)

    def decode(self, soft: np.ndarray) -> np.ndarray:
        values = np.ascontiguousarray(np.asarray(soft, dtype=np.float32).reshape(-1))
        if values.size % 2:
            raise ValueError("coded soft vector must have even length")
        nbits = values.size // 2
        if self._lib is not None:
            out = np.empty(nbits, dtype=np.uint8)
            result = self._fn(
                values.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                int(values.size),
                out.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
                int(nbits),
            )
            if result != 0:
                raise RuntimeError(f"native Viterbi failed with status {result}")
            return out
        return self._decode_numpy(values)

    @staticmethod
    def _decode_numpy(soft: np.ndarray) -> np.ndarray:
        nbits = soft.size // 2
        inf = 1e30
        metric = np.full(CONV_STATES, inf, dtype=np.float64)
        metric[0] = 0.0
        pred_state = np.empty((nbits, CONV_STATES), dtype=np.uint8)
        pred_bit = np.empty((nbits, CONV_STATES), dtype=np.uint8)
        for t in range(nbits):
            next_metric = np.full(CONV_STATES, inf, dtype=np.float64)
            r0, r1 = float(soft[2 * t]), float(soft[2 * t + 1])
            for state in range(CONV_STATES):
                pm = metric[state]
                if pm >= inf / 2:
                    continue
                for bit in (0, 1):
                    reg = (state << 1) | bit
                    o0 = _CONV_PARITY[reg & CONV_G0]
                    o1 = _CONV_PARITY[reg & CONV_G1]
                    e0 = 1.0 if o0 == 0 else -1.0
                    e1 = 1.0 if o1 == 0 else -1.0
                    candidate = pm + (r0 - e0) ** 2 + (r1 - e1) ** 2
                    ns = reg & 0x3F
                    if candidate < next_metric[ns]:
                        next_metric[ns] = candidate
                        pred_state[t, ns] = state
                        pred_bit[t, ns] = bit
            metric = next_metric
        state = 0 if np.isfinite(metric[0]) else int(np.argmin(metric))
        out = np.empty(nbits, dtype=np.uint8)
        for t in range(nbits - 1, -1, -1):
            out[t] = pred_bit[t, state]
            state = int(pred_state[t, state])
        return out


# ---------------------------------------------------------------------------
# Radio frame construction
# ---------------------------------------------------------------------------

def packet_to_radio_frame(packet: EncodedPacket) -> np.ndarray:
    packet_bits = np.unpackbits(np.frombuffer(packet.raw, dtype=np.uint8), bitorder="big")
    coded = convolutional_encode(packet_bits)
    if coded.size != CODED_PACKET_BITS:
        raise RuntimeError("coded packet length mismatch")
    data_symbols = qpsk_map(coded)
    symbols = np.concatenate((PREAMBLE_SYMBOLS, data_symbols, FILLER_SYMBOLS)).astype(np.complex64)
    shaped = pulse_shape(symbols)
    if shaped.size != FRAME_PERIOD_SAMPLES - LEADING_GUARD_SAMPLES:
        raise RuntimeError(f"pulse-shaped frame has {shaped.size} samples")
    frame = np.zeros(FRAME_PERIOD_SAMPLES, dtype=np.complex64)
    frame[LEADING_GUARD_SAMPLES:] = shaped
    frame *= np.float32(TX_DIGITAL_AMPLITUDE)
    frame *= _TX_CARRIER
    return frame


# ---------------------------------------------------------------------------
# Audio source and playback
# ---------------------------------------------------------------------------

def load_wav_as_pcm16(path: Path) -> np.ndarray:
    sample_rate, raw = wavfile.read(path)
    data = np.asarray(raw)
    if data.ndim == 2:
        data = data.astype(np.float64).mean(axis=1)
    elif data.ndim != 1:
        raise ValueError(f"unsupported WAV shape {data.shape}")
    if np.issubdtype(data.dtype, np.floating):
        audio = data.astype(np.float64)
    elif data.dtype == np.uint8:
        audio = (data.astype(np.float64) - 128.0) / 128.0
    elif np.issubdtype(data.dtype, np.integer):
        info = np.iinfo(data.dtype)
        audio = data.astype(np.float64) / max(abs(info.min), info.max)
    else:
        raise ValueError(f"unsupported WAV dtype {data.dtype}")
    audio = np.nan_to_num(audio)
    audio -= float(np.mean(audio))
    if int(sample_rate) != AUDIO_SAMPLE_RATE:
        divisor = math.gcd(int(sample_rate), AUDIO_SAMPLE_RATE)
        audio = signal.resample_poly(audio, AUDIO_SAMPLE_RATE // divisor, int(sample_rate) // divisor)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0:
        audio *= 0.92 / peak
    pcm = np.clip(np.rint(audio * 32767.0), -32768, 32767).astype(np.int16)
    if pcm.size == 0:
        raise ValueError("WAV is empty")
    return pcm


class AudioFrameSource:
    def __init__(self, pcm: np.ndarray, loop: bool = True) -> None:
        self.pcm = np.asarray(pcm, dtype=np.int16).reshape(-1)
        self.loop = loop
        self.position = 0
        self.sequence = 0
        self.started = False
        self.finished = False

    def next_packet(self) -> Optional[EncodedPacket]:
        if self.finished:
            return None
        frame = np.zeros(AUDIO_FRAME_SAMPLES, dtype=np.int16)
        flags = 0
        if not self.started:
            flags |= FLAG_START_OF_FILE
            self.started = True
        valid = 0
        while valid < AUDIO_FRAME_SAMPLES:
            available = self.pcm.size - self.position
            take = min(AUDIO_FRAME_SAMPLES - valid, available)
            frame[valid:valid + take] = self.pcm[self.position:self.position + take]
            valid += take
            self.position += take
            if self.position == self.pcm.size:
                flags |= FLAG_END_OF_FILE
                if self.loop:
                    self.position = 0
                    self.started = False
                    if valid < AUDIO_FRAME_SAMPLES:
                        flags &= ~FLAG_END_OF_FILE
                else:
                    self.finished = True
                    break
        packet = build_packet(self.sequence, frame, valid, flags)
        self.sequence = (self.sequence + 1) & 0xFFFF
        return packet

    def idle_packet(self) -> EncodedPacket:
        frame = np.zeros(AUDIO_FRAME_SAMPLES, dtype=np.int16)
        packet = build_packet(self.sequence, frame, AUDIO_FRAME_SAMPLES, FLAG_IDLE)
        self.sequence = (self.sequence + 1) & 0xFFFF
        return packet


class AlsaAudioSink(threading.Thread):
    def __init__(
        self,
        packet_queue: "queue.Queue[DecodedPacket]",
        stop_event: threading.Event,
        device: str,
        buffer_ms: int,
        stats: AudioStats,
        enabled: bool = True,
    ) -> None:
        super().__init__(name="qpsk-audio", daemon=True)
        self.packet_queue = packet_queue
        self.stop_event = stop_event
        self.device = device
        self.buffer_frames = max(1, int(math.ceil(buffer_ms / 20.0)))
        self.stats = stats
        self.enabled = enabled
        self.pending: Dict[int, np.ndarray] = {}
        self.expected: Optional[int] = None
        self.last_audio = np.zeros(AUDIO_FRAME_SAMPLES, dtype=np.int16)

    def _open_player(self) -> Optional[subprocess.Popen]:
        if not self.enabled:
            return None
        command = [
            "aplay", "-q", "-D", self.device,
            "-t", "raw", "-f", "S16_LE", "-r", str(AUDIO_SAMPLE_RATE), "-c", "1",
        ]
        return subprocess.Popen(command, stdin=subprocess.PIPE, bufsize=0)

    @staticmethod
    def _seq_distance(a: int, b: int) -> int:
        return (a - b) & 0xFFFF

    def run(self) -> None:
        player: Optional[subprocess.Popen] = None
        try:
            while not self.stop_event.is_set():
                try:
                    packet = self.packet_queue.get(timeout=0.05)
                    if packet.flags & FLAG_IDLE:
                        continue
                    self.pending[packet.sequence] = ima_adpcm_decode_block(packet.payload)
                except queue.Empty:
                    pass

                if self.expected is None:
                    if len(self.pending) < self.buffer_frames:
                        continue
                    start = min(self.pending)
                    contiguous = 0
                    seq = start
                    while seq in self.pending:
                        contiguous += 1
                        seq = (seq + 1) & 0xFFFF
                    if contiguous < self.buffer_frames:
                        continue
                    self.expected = start
                    player = self._open_player()
                    LOG.info(
                        "Audio playback started at sequence %d after %d contiguous frames",
                        start, self.buffer_frames,
                    )

                assert self.expected is not None
                if self.expected in self.pending:
                    audio = self.pending.pop(self.expected)
                    self.last_audio = audio
                else:
                    # Wait until future packets prove the expected sequence is missing.
                    future = [s for s in self.pending if 0 < self._seq_distance(s, self.expected) < 0x8000]
                    if not future:
                        continue
                    fade = max(0.0, 1.0 - min(self.stats.concealed_frames + 1, 8) / 9.0)
                    audio = np.rint(self.last_audio.astype(np.float32) * fade).astype(np.int16)
                    self.stats.concealed_frames += 1
                self.expected = (self.expected + 1) & 0xFFFF

                if player is not None and player.stdin is not None:
                    try:
                        player.stdin.write(audio.astype("<i2", copy=False).tobytes())
                    except (BrokenPipeError, OSError):
                        LOG.exception("ALSA pipe failed")
                        player = None
                elif not self.enabled:
                    self.stop_event.wait(AUDIO_FRAME_SECONDS)
        finally:
            if player is not None:
                try:
                    if player.stdin:
                        player.stdin.close()
                    player.terminate()
                    player.wait(timeout=1.0)
                except Exception:
                    try:
                        player.kill()
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# Streaming receiver
# ---------------------------------------------------------------------------

def _sliding_energy(values: np.ndarray, length: int) -> np.ndarray:
    power = np.abs(values).astype(np.float64) ** 2
    csum = np.empty(power.size + 1, dtype=np.float64)
    csum[0] = 0.0
    np.cumsum(power, out=csum[1:])
    return csum[length:] - csum[:-length]


def _catmull_rom(values: np.ndarray, positions: np.ndarray) -> np.ndarray:
    base = np.floor(positions).astype(np.int64)
    frac = positions - base
    base = np.clip(base, 1, values.size - 3)
    p0 = values[base - 1]
    p1 = values[base]
    p2 = values[base + 1]
    p3 = values[base + 2]
    a = 2.0 * p1
    b = -p0 + p2
    c = 2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3
    d = -p0 + 3.0 * p1 - 3.0 * p2 + p3
    return 0.5 * (a + b * frac + c * frac ** 2 + d * frac ** 3)


class StreamingQpskReceiver:
    def __init__(
        self,
        packet_callback: Callable[[DecodedPacket], None],
        correlation_threshold: float = CORRELATION_THRESHOLD_DEFAULT,
        decode_channel: str = "rx2",
        viterbi: Optional[ViterbiDecoder] = None,
    ) -> None:
        if decode_channel not in {"rx2", "mrc"}:
            raise ValueError("decode_channel must be rx2 or mrc")
        self.packet_callback = packet_callback
        self.threshold = float(correlation_threshold)
        self.decode_channel = decode_channel
        self.viterbi = viterbi or ViterbiDecoder()
        self.stats = ReceiverStats()
        self._buf0 = np.empty(0, dtype=np.complex64)
        self._buf1 = np.empty(0, dtype=np.complex64)
        self._global_start = 0
        self._total_samples = 0
        self._locked = False
        self._predicted_active_start: Optional[int] = None
        self._last_sequence: Optional[int] = None
        self._consecutive_misses = 0
        self._last_timing = 0.0
        self._recent_sequences: Deque[int] = collections.deque(maxlen=RECENT_SEQUENCE_WINDOW)
        self._rx_nco_phase_index = 0

    @property
    def locked(self) -> bool:
        return self._locked

    def _downmix(self, values: np.ndarray) -> np.ndarray:
        # RX_BUFFER_SAMPLES contains an integer number of NCO cycles, but this
        # general form also remains continuous for different block lengths.
        index = self._rx_nco_phase_index + np.arange(values.size, dtype=np.float64)
        osc = np.exp(-1j * 2.0 * math.pi * DIGITAL_OFFSET_HZ * index / SAMPLE_RATE)
        self._rx_nco_phase_index += values.size
        return (values * osc).astype(np.complex64)

    def feed(self, rx0: np.ndarray, rx1: Optional[np.ndarray] = None) -> List[DecodedPacket]:
        a = np.asarray(rx0, dtype=np.complex64).reshape(-1)
        b = a if rx1 is None else np.asarray(rx1, dtype=np.complex64).reshape(-1)
        if a.size != b.size:
            raise ValueError("RX channels have unequal sizes")
        if a.size == 0:
            return []
        a = a - np.mean(a)
        b = b - np.mean(b)
        self.stats.last_rms_rx1 = float(np.sqrt(np.mean(np.abs(a) ** 2)))
        self.stats.last_rms_rx2 = float(np.sqrt(np.mean(np.abs(b) ** 2)))
        self.stats.largest_rx_magnitude = max(
            self.stats.largest_rx_magnitude,
            float(np.max(np.abs(a))),
            float(np.max(np.abs(b))),
        )
        a = self._downmix(a)
        # Downmix second channel with same phase by deriving from already advanced index.
        idx0 = self._rx_nco_phase_index - b.size
        idx = idx0 + np.arange(b.size, dtype=np.float64)
        b = (b * np.exp(-1j * 2.0 * math.pi * DIGITAL_OFFSET_HZ * idx / SAMPLE_RATE)).astype(np.complex64)

        self._buf0 = np.concatenate((self._buf0, a))
        self._buf1 = np.concatenate((self._buf1, b))
        self._total_samples += a.size
        decoded: List[DecodedPacket] = []

        # Decode all ready predicted frames while locked.
        if self._locked and self._predicted_active_start is not None:
            while True:
                local_pred = self._predicted_active_start - self._global_start
                radius = TRACK_SEARCH_RADIUS_SAMPLES
                if self._last_sequence is not None and (self._last_sequence + 1) % TX_FRAMES_PER_BUFFER == 0:
                    radius = TRACK_TX_BOUNDARY_RADIUS_SAMPLES
                if local_pred + (FRAME_PERIOD_SAMPLES - LEADING_GUARD_SAMPLES) + radius + 8 > self._buf1.size:
                    break
                candidate, metric = self._local_search(local_pred, radius)
                if candidate is not None:
                    packet = self._attempt_decode(candidate, metric, tracked=True)
                    if packet is not None:
                        decoded.append(packet)
                        self._predicted_active_start = self._global_start + candidate + FRAME_PERIOD_SAMPLES
                        self._consecutive_misses = 0
                        continue
                self._consecutive_misses += 1
                self.stats.tracking_misses += 1
                self._predicted_active_start += FRAME_PERIOD_SAMPLES
                if self._consecutive_misses >= TRACK_MAX_CONSECUTIVE_MISSES:
                    self._locked = False
                    self.stats.reacquisitions += 1
                    self._predicted_active_start = None
                    break

        if not self._locked:
            acquired = self._acquire_candidates()
            for candidate, metric in acquired:
                packet = self._attempt_decode(candidate, metric, tracked=False)
                if packet is not None:
                    decoded.append(packet)
                    self._locked = True
                    self._consecutive_misses = 0
                    self._predicted_active_start = self._global_start + candidate + FRAME_PERIOD_SAMPLES
                    break

        self._trim()
        return decoded

    def _detection_source(self) -> np.ndarray:
        if self.decode_channel == "rx2":
            return self._buf1
        p0 = float(np.vdot(self._buf0, self._buf0).real)
        p1 = float(np.vdot(self._buf1, self._buf1).real)
        return self._buf0 if p0 >= p1 else self._buf1

    def _normalized_metric(self, source: np.ndarray, starts: np.ndarray) -> np.ndarray:
        out = np.zeros(starts.size, dtype=np.float64)
        t = _DETECTOR_TEMPLATE
        te = _DETECTOR_TEMPLATE_ENERGY
        for i, start in enumerate(starts):
            if start < 0 or start + t.size > source.size:
                continue
            segment = source[start:start + t.size]
            energy = float(np.vdot(segment, segment).real)
            if energy > 1e-20:
                out[i] = abs(np.vdot(t, segment)) ** 2 / (te * energy)
        return out

    def _local_search(self, predicted: int, radius: int) -> Tuple[Optional[int], float]:
        source = self._detection_source()
        starts = np.arange(max(0, predicted - radius), min(source.size - _DETECTOR_TEMPLATE.size, predicted + radius) + 1)
        if starts.size == 0:
            return None, 0.0
        metric = self._normalized_metric(source, starts)
        best_i = int(np.argmax(metric))
        best = float(metric[best_i])
        self.stats.strongest_detector_metric = max(self.stats.strongest_detector_metric, best)
        if best < self.threshold:
            return None, best
        return int(starts[best_i]), best

    def _acquire_candidates(self) -> List[Tuple[int, float]]:
        source = self._detection_source()
        need = _DETECTOR_TEMPLATE.size + (FRAME_PERIOD_SAMPLES - LEADING_GUARD_SAMPLES) + 8
        if source.size < need:
            return []
        corr = signal.fftconvolve(source, np.conj(_DETECTOR_TEMPLATE[::-1]), mode="valid")
        energy = _sliding_energy(source, _DETECTOR_TEMPLATE.size)
        metric = np.zeros(corr.size, dtype=np.float64)
        valid = energy > max(float(np.max(energy)) * 1e-12, 1e-20)
        metric[valid] = np.abs(corr[valid]) ** 2 / (_DETECTOR_TEMPLATE_ENERGY * energy[valid])
        np.clip(metric, 0.0, 1.0, out=metric)
        best = float(np.max(metric)) if metric.size else 0.0
        self.stats.strongest_detector_metric = max(self.stats.strongest_detector_metric, best)
        peaks, properties = signal.find_peaks(metric, height=self.threshold, distance=FRAME_PERIOD_SAMPLES // 2)
        order = sorted(peaks.tolist(), key=lambda p: float(metric[p]), reverse=True)
        ready_limit = source.size - (FRAME_PERIOD_SAMPLES - LEADING_GUARD_SAMPLES) - 8
        return [(int(p), float(metric[p])) for p in order[:8] if p <= ready_limit]

    def _symbol_stream(self, channel: np.ndarray, active_start: int, timing: float) -> np.ndarray:
        active_len = FRAME_PERIOD_SAMPLES - LEADING_GUARD_SAMPLES
        segment = channel[active_start:active_start + active_len]
        matched = np.convolve(segment, RRC_TAPS, mode="full")
        positions = 2 * RRC_GROUP_DELAY + timing + np.arange(TOTAL_FRAME_SYMBOLS) * SAMPLES_PER_SYMBOL
        if positions[-1] >= matched.size - 2 or positions[0] < 1:
            raise ValueError("timing positions outside matched-filter vector")
        return _catmull_rom(matched, positions).astype(np.complex64)

    @staticmethod
    def _estimate_cfo_and_channel(symbols: np.ndarray) -> Tuple[float, complex, np.ndarray, float]:
        a0 = symbols[0:64]
        a1 = symbols[64:128]
        a2 = symbols[128:192]
        cross = np.vdot(a0, a1) + np.vdot(a1, a2)
        rad_per_symbol = float(np.angle(cross)) / 64.0
        index = np.arange(symbols.size, dtype=np.float64)
        corrected = symbols * np.exp(-1j * rad_per_symbol * index)
        pre = corrected[:PREAMBLE_SYMBOL_COUNT]
        h = np.vdot(PREAMBLE_SYMBOLS, pre) / np.vdot(PREAMBLE_SYMBOLS, PREAMBLE_SYMBOLS)
        error = pre - h * PREAMBLE_SYMBOLS
        mse = float(np.mean(np.abs(error) ** 2) / max(abs(h) ** 2, 1e-12))
        return rad_per_symbol, complex(h), corrected.astype(np.complex64), mse

    def _attempt_decode(self, active_start: int, correlation: float, tracked: bool) -> Optional[DecodedPacket]:
        self.stats.detected_candidates += 1
        timings: Iterable[float]
        if tracked:
            timings = (self._last_timing,)
        else:
            timings = np.arange(-2.0, 2.0001, 0.125)
        best = None
        for timing in timings:
            try:
                s1 = self._symbol_stream(self._buf1, active_start, float(timing))
                r1, h1, c1, mse1 = self._estimate_cfo_and_channel(s1)
                if self.decode_channel == "mrc":
                    s0 = self._symbol_stream(self._buf0, active_start, float(timing))
                    r0, h0, c0, mse0 = self._estimate_cfo_and_channel(s0)
                    rad = 0.5 * (r0 + r1)
                    idx = np.arange(s0.size)
                    # Re-derotate with common estimate for coherent combining.
                    c0 = s0 * np.exp(-1j * rad * idx)
                    c1 = s1 * np.exp(-1j * rad * idx)
                    h0 = np.vdot(PREAMBLE_SYMBOLS, c0[:PREAMBLE_SYMBOL_COUNT]) / PREAMBLE_SYMBOL_COUNT
                    h1 = np.vdot(PREAMBLE_SYMBOLS, c1[:PREAMBLE_SYMBOL_COUNT]) / PREAMBLE_SYMBOL_COUNT
                    denom = abs(h0) ** 2 + abs(h1) ** 2
                    combined = (np.conj(h0) * c0 + np.conj(h1) * c1) / max(denom, 1e-12)
                    quality = mse0 + mse1
                else:
                    rad = r1
                    combined = c1 / (h1 if abs(h1) > 1e-12 else 1.0)
                    quality = mse1
                if best is None or quality < best[0]:
                    best = (quality, float(timing), float(rad), combined.astype(np.complex64))
            except Exception:
                continue
        if best is None and tracked:
            # Locked timing failed; one full timing sweep is cheaper than losing lock.
            for timing in np.arange(-2.0, 2.0001, 0.125):
                try:
                    s1 = self._symbol_stream(self._buf1, active_start, float(timing))
                    rad, h, corrected, quality = self._estimate_cfo_and_channel(s1)
                    combined = corrected / (h if abs(h) > 1e-12 else 1.0)
                    if best is None or quality < best[0]:
                        best = (quality, float(timing), float(rad), combined.astype(np.complex64))
                except Exception:
                    continue
        if best is None:
            self.stats.crc_or_decode_failures += 1
            return None

        _quality, timing, rad_per_symbol, symbols = best
        data = symbols[PREAMBLE_SYMBOL_COUNT:PREAMBLE_SYMBOL_COUNT + DATA_QPSK_SYMBOLS]
        soft = np.empty(CODED_PACKET_BITS, dtype=np.float32)
        soft[0::2] = data.real * math.sqrt(2.0)
        soft[1::2] = data.imag * math.sqrt(2.0)
        try:
            decoded = self.viterbi.decode(soft)
            packet_bits = decoded[:UNCODED_PACKET_BITS]
            raw = np.packbits(packet_bits, bitorder="big").tobytes()
            sequence, valid_samples, flags, payload = parse_packet(raw)
        except Exception:
            self.stats.crc_or_decode_failures += 1
            return None

        if sequence in self._recent_sequences:
            self.stats.duplicate_packets += 1
            return None
        self._recent_sequences.append(sequence)

        # EVM against re-encoded decoded packet symbols.
        expected_coded = convolutional_encode(packet_bits)
        expected_symbols = qpsk_map(expected_coded)
        evm = math.sqrt(float(np.mean(np.abs(data - expected_symbols) ** 2)))
        evm_db = 20.0 * math.log10(max(evm, 1e-12))
        cfo_hz = rad_per_symbol * SYMBOL_RATE / (2.0 * math.pi)
        packet = DecodedPacket(
            sequence=sequence,
            valid_samples=valid_samples,
            flags=flags,
            payload=payload,
            correlation=float(correlation),
            evm_db=evm_db,
            residual_cfo_hz=cfo_hz,
            timing_offset_samples=timing,
        )
        self.stats.valid_packets += 1
        if self.stats.first_valid_monotonic is None:
            self.stats.first_valid_monotonic = time.monotonic()
        self.stats.last_correlation = correlation
        self.stats.last_evm_db = evm_db
        self.stats.last_cfo_hz = cfo_hz
        self.stats.last_timing_offset = timing
        self._last_timing = timing
        self._last_sequence = sequence
        if tracked:
            self.stats.tracked_packets += 1
        else:
            self.stats.acquired_packets += 1
        self.packet_callback(packet)
        return packet

    def _trim(self) -> None:
        keep = max(4 * FRAME_PERIOD_SAMPLES, FRAME_PERIOD_SAMPLES + TRACK_TX_BOUNDARY_RADIUS_SAMPLES + 4096)
        if self._buf1.size <= keep:
            return
        remove = self._buf1.size - keep
        if self._predicted_active_start is not None:
            predicted_local = self._predicted_active_start - self._global_start
            remove = min(remove, max(0, predicted_local - TRACK_TX_BOUNDARY_RADIUS_SAMPLES - 2048))
        if remove <= 0:
            return
        self._buf0 = self._buf0[remove:]
        self._buf1 = self._buf1[remove:]
        self._global_start += remove


# ---------------------------------------------------------------------------
# Hardware configuration and workers
# ---------------------------------------------------------------------------

def _scalar_iio_value(value: object, name: str) -> int:
    if isinstance(value, (list, tuple, np.ndarray)):
        values = [int(v) for v in value if isinstance(v, (int, float, np.integer, np.floating)) and int(v) >= 0]
        if not values:
            raise RuntimeError(f"invalid {name} readback: {value!r}")
        LOG.warning("IIO attribute %s returned %r; using scalar %d", name, value, values[0])
        return values[0]
    return int(value)  # type: ignore[arg-type]


def set_sample_rate_robust(sdr: object, requested: int) -> int:
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            setattr(sdr, "sample_rate", int(requested))
            actual = _scalar_iio_value(getattr(sdr, "sample_rate"), "sample_rate")
            if abs(actual - requested) > max(5, requested * 0.002):
                raise RuntimeError(f"sample-rate readback {actual} does not match {requested}")
            return actual
        except Exception as exc:
            last_error = exc
            LOG.warning("sample-rate configuration attempt %d failed: %s", attempt + 1, exc)
            time.sleep(0.2)
    raise RuntimeError("unable to configure Pluto sample rate") from last_error


def _load_calibration(phaser: object, directory: Optional[Path]) -> bool:
    if directory is None:
        return False
    required = {
        "channel": directory / "channel_cal_val.pkl",
        "gain": directory / "gain_cal_val.pkl",
        "phase": directory / "phase_cal_val.pkl",
    }
    if not all(path.is_file() for path in required.values()):
        LOG.warning("calibration files incomplete in %s; using driver defaults", directory)
        return False
    phaser.load_channel_cal(str(required["channel"]))
    phaser.load_gain_cal(str(required["gain"]))
    phaser.load_phase_cal(str(required["phase"]))
    LOG.info("Loaded CN0566 calibration from %s", directory)
    return True


def configure_hardware(args: argparse.Namespace) -> Tuple[object, object, Optional[int]]:
    try:
        import adi
        from adi.cn0566 import CN0566
    except ImportError as exc:
        raise RuntimeError("pyadi-iio is required for hardware modes") from exc

    LOG.info("Connecting to CN0566 at %s", args.phaser_uri)
    phaser = CN0566(uri=args.phaser_uri)
    LOG.info("Connecting to PlutoSDR at %s", args.sdr_uri)
    sdr = adi.ad9361(uri=args.sdr_uri)
    phaser.sdr = sdr
    phaser.configure(device_mode="rx")
    calibrated = _load_calibration(phaser, args.cal_dir)

    phaser.ramp_mode = "disabled"
    phaser.lo = CN0566_LO_HZ
    phaser.powerdown = 0
    phaser.enable = 0
    phaser.set_tx_sw_div(0)
    phaser._gpios.gpio_tx_sw = 0 if args.tx_output == 2 else 1
    for channel in range(8):
        phaser.set_chan_gain(channel, int(args.array_gain), apply_cal=calibrated)
    phaser.set_beam_phase_diff(float(args.beam_phase_diff))

    # Explicit fixed physical ports.
    try:
        sdr.rx_rf_port_select = "A_BALANCED"
        sdr.tx_rf_port_select = "A"
    except Exception as exc:
        LOG.warning("RF port-select attributes unavailable: %s", exc)

    sdr.tx_enabled_channels = [1]
    sdr.rx_enabled_channels = [1] if args.decode_channel == "rx2" else [0, 1]
    actual_rate = set_sample_rate_robust(sdr, SAMPLE_RATE)
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
        sdr._rxadc.set_kernel_buffers_count(4)
    except Exception:
        pass
    try:
        sdr.dds_enabled = [0] * 8
    except Exception:
        try:
            sdr.disable_dds()
        except Exception:
            pass

    lock: Optional[int] = None
    try:
        lock = int(phaser._gpios.gpio_muxout)
    except Exception:
        pass
    LOG.info(
        "Configured: Fs=%d, QPSK=%d sym/s, IF=%.6f GHz, offset=%.1f kHz",
        actual_rate, SYMBOL_RATE, PLUTO_IF_HZ / 1e9, DIGITAL_OFFSET_HZ / 1e3,
    )
    LOG.info(
        "CN0566 LO=%.6f GHz; nominal RF=%.6f GHz; TX_OUT_%d; RX=%s; PLL=%s",
        CN0566_LO_HZ / 1e9, NOMINAL_XBAND_RF_HZ / 1e9, args.tx_output,
        args.decode_channel, "n/a" if lock is None else lock,
    )
    return phaser, sdr, lock


def _normalize_rx_return(received: object, decode_channel: str) -> Tuple[np.ndarray, np.ndarray]:
    if decode_channel == "rx2":
        if isinstance(received, (list, tuple)):
            values = np.asarray(received[-1], dtype=np.complex64)
        else:
            values = np.asarray(received, dtype=np.complex64)
        return values, values
    if not isinstance(received, (list, tuple)) or len(received) != 2:
        raise RuntimeError("MRC mode expected two Pluto receive channels")
    return np.asarray(received[0], dtype=np.complex64), np.asarray(received[1], dtype=np.complex64)


def rx_acquisition_worker(
    sdr: object,
    decode_channel: str,
    output_queue: "queue.Queue[Tuple[np.ndarray, np.ndarray]]",
    stop_event: threading.Event,
    stats: ReceiverStats,
) -> None:
    try:
        while not stop_event.is_set():
            rx0, rx1 = _normalize_rx_return(sdr.rx(), decode_channel)
            try:
                output_queue.put_nowait((rx0, rx1))
            except queue.Full:
                stats.host_queue_drops += 1
    except Exception:
        LOG.exception("RX acquisition stopped")
        stop_event.set()


def tx_encode_worker(
    source: AudioFrameSource,
    output_queue: "queue.Queue[Tuple[np.ndarray, bool]]",
    stop_event: threading.Event,
    stats: TxStats,
) -> None:
    try:
        while not stop_event.is_set():
            frames: List[np.ndarray] = []
            final = False
            for _ in range(TX_FRAMES_PER_BUFFER):
                packet = source.next_packet()
                if packet is None:
                    packet = source.idle_packet()
                    final = True
                frames.append(packet_to_radio_frame(packet))
            chunk = np.concatenate(frames).astype(np.complex64, copy=False)
            while not stop_event.is_set():
                try:
                    output_queue.put((chunk, final), timeout=0.1)
                    stats.chunks_encoded += 1
                    break
                except queue.Full:
                    continue
            if final and not source.loop:
                return
    except Exception:
        LOG.exception("TX encoder stopped")
        stop_event.set()


def tx_worker(
    sdr: object,
    input_queue: "queue.Queue[Tuple[np.ndarray, bool]]",
    stop_event: threading.Event,
    stats: TxStats,
) -> None:
    stream_started = False
    try:
        while not stop_event.is_set():
            try:
                chunk, final = input_queue.get(timeout=0.1)
            except queue.Empty:
                if stream_started:
                    stats.queue_starvations += 1
                continue
            sdr.tx(chunk)
            stream_started = True
            stats.chunks_pushed += 1
            if final:
                return
    except Exception:
        LOG.exception("TX pusher stopped")
        stop_event.set()


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def _fractional_delay(values: np.ndarray, delay: float, taps: int = 33) -> np.ndarray:
    half = taps // 2
    n = np.arange(-half, half + 1, dtype=np.float64)
    h = np.sinc(n - delay) * np.hamming(taps)
    h /= np.sum(h)
    return np.convolve(values, h, mode="full").astype(np.complex64)


def run_simulation(args: argparse.Namespace) -> int:
    wav = Path(args.wav)
    pcm = load_wav_as_pcm16(wav) if wav.is_file() else generate_test_pcm(3.0)
    source = AudioFrameSource(pcm, loop=True)
    expected: Dict[int, bytes] = {}
    frames: List[np.ndarray] = []
    for _ in range(int(args.simulation_frames)):
        packet = source.next_packet()
        assert packet is not None
        expected[packet.sequence] = packet.payload
        frames.append(packet_to_radio_frame(packet))
    transmitted = np.concatenate(frames)
    delayed = _fractional_delay(transmitted, 0.43)
    delayed = np.concatenate((np.zeros(7321, dtype=np.complex64), delayed))
    n = np.arange(delayed.size, dtype=np.float64)
    delayed *= np.exp(1j * 2.0 * math.pi * 7.0 * n / SAMPLE_RATE).astype(np.complex64)
    rng = np.random.default_rng(42)
    noise0 = (rng.normal(0.0, 18.0, delayed.size) + 1j * rng.normal(0.0, 18.0, delayed.size)).astype(np.complex64)
    noise1 = (rng.normal(0.0, 18.0, delayed.size) + 1j * rng.normal(0.0, 18.0, delayed.size)).astype(np.complex64)
    ch0 = (0.070 + 0.025j) * delayed + noise0
    ch1 = (-0.020 + 0.095j) * delayed + noise1
    pad = (-ch0.size) % RX_BUFFER_SAMPLES
    if pad:
        ch0 = np.pad(ch0, (0, pad))
        ch1 = np.pad(ch1, (0, pad))

    received: Dict[int, bytes] = {}
    receiver = StreamingQpskReceiver(
        packet_callback=lambda packet: received.setdefault(packet.sequence, packet.payload),
        correlation_threshold=float(args.correlation_threshold),
        decode_channel="mrc",
    )
    for start in range(0, ch0.size, RX_BUFFER_SAMPLES):
        receiver.feed(ch0[start:start + RX_BUFFER_SAMPLES], ch1[start:start + RX_BUFFER_SAMPLES])
    mismatched = [seq for seq, payload in expected.items() if received.get(seq) != payload]
    LOG.info(
        "Simulation expected=%d received=%d exact=%d FEC=%s corr=%.3f EVM=%.1f dB CFO=%+.1f Hz",
        len(expected), len(received), len(expected) - len(mismatched), receiver.viterbi.backend,
        receiver.stats.last_correlation, receiver.stats.last_evm_db, receiver.stats.last_cfo_hz,
    )
    if mismatched:
        LOG.error("Simulation mismatch sequences: %s", mismatched)
        return 1
    LOG.info("Simulation passed: all packet payloads match exactly")
    return 0


def generate_test_pcm(seconds: float = 8.0) -> np.ndarray:
    n = np.arange(int(round(seconds * AUDIO_SAMPLE_RATE)))
    audio = 0.45 * np.sin(2 * math.pi * 440.0 * n / AUDIO_SAMPLE_RATE)
    audio += 0.18 * np.sin(2 * math.pi * 880.0 * n / AUDIO_SAMPLE_RATE)
    return np.rint(audio * 32767.0).astype(np.int16)


# ---------------------------------------------------------------------------
# Live application
# ---------------------------------------------------------------------------

def format_status(rx: ReceiverStats, tx: TxStats, audio: AudioStats, tx_queue: queue.Queue) -> str:
    rate = 0.0
    if rx.first_valid_monotonic is not None:
        elapsed = max(time.monotonic() - rx.first_valid_monotonic, 1e-6)
        rate = max(0, rx.valid_packets - 1) / elapsed
    return (
        f"RX valid={rx.valid_packets} rate={rate:.1f}/s fail={rx.crc_or_decode_failures} "
        f"duplicate={rx.duplicate_packets} det={rx.strongest_detector_metric:.3f} "
        f"okcorr={rx.last_correlation:.3f} EVM={rx.last_evm_db:.1f}dB "
        f"CFO={rx.last_cfo_hz:+.1f}Hz RMS={rx.last_rms_rx1:.2f}/{rx.last_rms_rx2:.2f} "
        f"lock={int(rx.tracked_packets > 0)} track={rx.tracked_packets}/{rx.acquired_packets} "
        f"miss={rx.tracking_misses} reacq={rx.reacquisitions} host_drop={rx.host_queue_drops} "
        f"tx_q={tx_queue.qsize()} tx={tx.chunks_pushed}/{tx.chunks_encoded} "
        f"tx_starve={tx.queue_starvations} conceal={audio.concealed_frames}"
    )


def run_hardware(args: argparse.Namespace) -> int:
    if args.mode in {"transceive", "tx"} and not args.enable_tx:
        raise RuntimeError("RF transmission requires --enable-tx")
    wav = Path(args.wav)
    if args.mode in {"transceive", "tx"} and not wav.is_file():
        raise FileNotFoundError(wav)
    pcm = load_wav_as_pcm16(wav) if wav.is_file() else generate_test_pcm(8.0)
    LOG.info("Loaded %s: %.2f s after conversion to 16 kHz mono PCM", wav, pcm.size / AUDIO_SAMPLE_RATE)
    viterbi = ViterbiDecoder()
    LOG.info("Viterbi decoder ready (%s)", viterbi.backend)
    phaser = None
    sdr = None
    stop_event = threading.Event()
    rx_stats = ReceiverStats()
    tx_stats = TxStats()
    audio_stats = AudioStats()
    rx_queue: "queue.Queue[Tuple[np.ndarray, np.ndarray]]" = queue.Queue(maxsize=RX_QUEUE_BUFFERS)
    tx_queue: "queue.Queue[Tuple[np.ndarray, bool]]" = queue.Queue(maxsize=TX_PREFETCH_CHUNKS)
    audio_queue: "queue.Queue[DecodedPacket]" = queue.Queue(maxsize=500)
    threads: List[threading.Thread] = []

    def stop_handler(_signum: int, _frame: object) -> None:
        stop_event.set()

    os_signal.signal(os_signal.SIGINT, stop_handler)
    os_signal.signal(os_signal.SIGTERM, stop_handler)

    try:
        phaser, sdr, _lock = configure_hardware(args)
        receiver = StreamingQpskReceiver(
            packet_callback=lambda p: _enqueue_audio(audio_queue, p, audio_stats),
            correlation_threshold=args.correlation_threshold,
            decode_channel=args.decode_channel,
            viterbi=viterbi,
        )
        if args.mode in {"transceive", "rx"}:
            acquisition = threading.Thread(
                target=rx_acquisition_worker,
                name="qpsk-rx-io",
                daemon=True,
                args=(sdr, args.decode_channel, rx_queue, stop_event, rx_stats),
            )
            acquisition.start()
            threads.append(acquisition)
            player = AlsaAudioSink(
                audio_queue, stop_event, args.audio_device, args.audio_buffer_ms,
                audio_stats, enabled=not args.no_play,
            )
            player.start()
            threads.append(player)

        if args.mode in {"transceive", "tx"}:
            source = AudioFrameSource(pcm, loop=not args.once)
            encoder = threading.Thread(
                target=tx_encode_worker, name="qpsk-tx-encode", daemon=True,
                args=(source, tx_queue, stop_event, tx_stats),
            )
            encoder.start()
            threads.append(encoder)
            deadline = time.monotonic() + 30.0
            while tx_queue.qsize() < min(TX_INITIAL_PREFILL_CHUNKS, TX_PREFETCH_CHUNKS):
                if stop_event.is_set() or time.monotonic() > deadline:
                    raise RuntimeError("TX prefill timed out")
                time.sleep(0.05)
            pusher = threading.Thread(
                target=tx_worker, name="qpsk-tx-io", daemon=True,
                args=(sdr, tx_queue, stop_event, tx_stats),
            )
            pusher.start()
            threads.append(pusher)

        LOG.info("Running; press Ctrl-C to stop")
        started = time.monotonic()
        next_log = started + 1.0
        while not stop_event.is_set():
            if args.run_seconds > 0 and time.monotonic() - started >= args.run_seconds:
                stop_event.set()
                break
            try:
                rx0, rx1 = rx_queue.get(timeout=0.05)
                packets = receiver.feed(rx0, rx1)
                # Receiver object owns its stats; merge host drops maintained by I/O.
                host_drops = rx_stats.host_queue_drops
                rx_stats = receiver.stats
                rx_stats.host_queue_drops = host_drops
            except queue.Empty:
                pass
            now = time.monotonic()
            if now >= next_log:
                LOG.info(format_status(rx_stats, tx_stats, audio_stats, tx_queue))
                next_log = now + 1.0
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=2.0)
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
                sdr.tx_hardwaregain_chan1 = -88.0
            except Exception:
                pass
        # Keep phaser referenced until buffers close.
        del sdr
        del phaser
    return 0


def _enqueue_audio(output: "queue.Queue[DecodedPacket]", packet: DecodedPacket, stats: AudioStats) -> None:
    try:
        output.put_nowait(packet)
    except queue.Full:
        stats.queue_drops += 1
        try:
            output.get_nowait()
            output.put_nowait(packet)
        except queue.Empty:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CN0566/Pluto real-time single-carrier QPSK audio link")
    parser.add_argument("--mode", choices=("simulate", "transceive", "tx", "rx"), default="simulate")
    parser.add_argument("--wav", default="sample.wav")
    parser.add_argument("--phaser-uri", default="ip:localhost")
    parser.add_argument("--sdr-uri", default="ip:192.168.2.1")
    parser.add_argument("--cal-dir", type=Path)
    parser.add_argument("--tx-output", type=int, choices=(1, 2), default=2)
    parser.add_argument("--enable-tx", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--tx-gain", type=float, default=-40.0)
    parser.add_argument("--rx-gain", type=float, default=20.0)
    parser.add_argument("--array-gain", type=int, default=127)
    parser.add_argument("--beam-phase-diff", type=float, default=0.0)
    parser.add_argument("--decode-channel", choices=("rx2", "mrc"), default="rx2")
    parser.add_argument("--correlation-threshold", type=float, default=CORRELATION_THRESHOLD_DEFAULT)
    parser.add_argument("--audio-device", default="default")
    parser.add_argument("--audio-buffer-ms", type=int, default=1500)
    parser.add_argument("--no-play", action="store_true")
    parser.add_argument("--run-seconds", type=float, default=0.0)
    parser.add_argument("--simulation-frames", type=int, default=12)
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if not -88.0 <= args.tx_gain <= 0.0:
        raise ValueError("--tx-gain must be in [-88, 0] dB")
    if not -3.0 <= args.rx_gain <= 71.0:
        raise ValueError("--rx-gain must be in [-3, 71] dB at the 2.2 GHz IF")
    if not 0 <= args.array_gain <= 127:
        raise ValueError("--array-gain must be in [0, 127]")
    if not 0.0 < args.correlation_threshold < 1.0:
        raise ValueError("--correlation-threshold must be between zero and one")
    if args.audio_buffer_ms < 20:
        raise ValueError("--audio-buffer-ms must be at least 20")


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
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
        LOG.info("Interrupted")
        return 130
    except Exception as exc:
        LOG.exception("Fatal error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
