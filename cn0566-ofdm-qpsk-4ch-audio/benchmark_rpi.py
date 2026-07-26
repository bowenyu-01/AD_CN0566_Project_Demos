#!/usr/bin/env python3
"""Software-only real-time budget benchmark for the selected OFDM profile."""

from __future__ import annotations

import argparse
import statistics
import threading
import time
from pathlib import Path

import numpy as np

from cn0566_ofdm_core import (
    ACQUISITION_BLOCK_STRIDE,
    AUDIO_WRITE_FRAMES,
    CHANNEL_DECODE_WORKERS,
    LIVE_METRICS_INTERVAL,
    FecCodec,
    FourAudioSource,
    OfdmFrameBuilder,
    PROFILE_NAME,
    RX_BUFFER_SAMPLES,
    SAMPLE_RATE,
    SUPERFRAME_SAMPLES,
    StreamingOfdmReceiver,
    TX_DIGITAL_PEAK_DEFAULT,
    TX_DIGITAL_RMS_DEFAULT,
    load_wav_as_pcm16,
    profile_summary,
)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Benchmark TX generation, unlocked acquisition, and locked decoding without RF"
    )
    for index in range(1, 5):
        p.add_argument(f"--wav{index}", type=Path, default=Path(f"sample_{index}.wav"))
    p.add_argument("--chunks", type=int, default=3)
    p.add_argument("--rx-blocks", type=int, default=12)
    p.add_argument("--tx-digital-rms", type=float, default=TX_DIGITAL_RMS_DEFAULT)
    p.add_argument("--tx-digital-peak", type=float, default=TX_DIGITAL_PEAK_DEFAULT)
    return p


def main() -> int:
    args = parser().parse_args()
    paths = [args.wav1, args.wav2, args.wav3, args.wav4]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing WAV files: " + ", ".join(missing))
    if args.chunks <= 0 or args.rx_blocks <= 0:
        raise ValueError("benchmark counts must be positive")

    codec = FecCodec()
    print(f"Profile: {profile_summary()}")
    print(f"FEC backend: {codec.backend}")
    print(f"IMA-ADPCM backend: {codec.audio_backend}")
    print(
        f"Realtime controls: channel_workers={CHANNEL_DECODE_WORKERS}, "
        f"metrics_interval={LIVE_METRICS_INTERVAL}, "
        f"audio_write_frames={AUDIO_WRITE_FRAMES}"
    )
    pcm = [load_wav_as_pcm16(path) for path in paths]
    source = FourAudioSource(pcm, repeat=True)
    builder = OfdmFrameBuilder(codec)

    build_times = []
    last_chunk = None
    for index in range(args.chunks):
        started = time.perf_counter()
        chunk, _history, rms = builder.build_scaled_chunk(
            source,
            peak_target=args.tx_digital_peak,
            rms_target=args.tx_digital_rms,
        )
        elapsed = time.perf_counter() - started
        build_times.append(elapsed)
        last_chunk = chunk
        print(
            f"TX chunk {index + 1}/{args.chunks}: {elapsed:.3f} s to build "
            f"{chunk.size / SAMPLE_RATE:.1f} s, RMS={rms:.1f}, "
            f"peak={np.max(np.abs(chunk)):.1f}, clip={100*builder.last_clip_fraction:.4f}%, "
            f"PAPR={builder.last_output_papr_db:.2f} dB"
        )

    rng = np.random.default_rng(0xC0566)
    noise = (
        rng.standard_normal(RX_BUFFER_SAMPLES)
        + 1j * rng.standard_normal(RX_BUFFER_SAMPLES)
    ).astype(np.complex64)
    receiver = StreamingOfdmReceiver(
        codec, lambda _packet, _pcm: None, correlation_threshold=0.20
    )
    all_rx_times = []
    search_times = []
    skip_times = []
    for _ in range(args.rx_blocks):
        before = receiver.stats.acquisition_searches
        started = time.perf_counter()
        receiver.feed(noise)
        elapsed = time.perf_counter() - started
        all_rx_times.append(elapsed)
        if receiver.stats.acquisition_searches > before:
            search_times.append(elapsed)
        else:
            skip_times.append(elapsed)

    if last_chunk is None:
        raise RuntimeError("TX benchmark did not build a chunk")

    # Feed one clean one-second waveform to measure the all-channel locked path.
    locked_packets = 0

    def packet_callback(_packet, _pcm) -> None:
        nonlocal locked_packets
        locked_packets += 1

    locked_receiver = StreamingOfdmReceiver(
        codec, packet_callback, correlation_threshold=0.20
    )
    stream = np.concatenate(
        (
            np.zeros(7777, dtype=np.complex64),
            last_chunk,
            np.zeros(2 * SUPERFRAME_SAMPLES, dtype=np.complex64),
        )
    )
    locked_started = time.perf_counter()
    for start in range(0, stream.size, RX_BUFFER_SAMPLES):
        locked_receiver.feed(stream[start : start + RX_BUFFER_SAMPLES])
    locked_elapsed = time.perf_counter() - locked_started
    locked_signal_seconds = stream.size / SAMPLE_RATE

    # Realtime playback normally decodes only the selected logical channel.
    selected_packets = 0

    def selected_callback(_packet, _pcm) -> None:
        nonlocal selected_packets
        selected_packets += 1

    selected_receiver = StreamingOfdmReceiver(
        codec,
        selected_callback,
        correlation_threshold=0.20,
        channel_selector=lambda: {0},
    )
    selected_started = time.perf_counter()
    for start in range(0, stream.size, RX_BUFFER_SAMPLES):
        selected_receiver.feed(stream[start : start + RX_BUFFER_SAMPLES])
    selected_elapsed = time.perf_counter() - selected_started

    # Exercise TX generation at the same time as selected-channel RX decoding.
    concurrent_pcm = [load_wav_as_pcm16(path) for path in paths]
    concurrent_source = FourAudioSource(concurrent_pcm, repeat=True)
    concurrent_builder = OfdmFrameBuilder(codec)
    start_gate = threading.Event()
    tx_result = {}

    def concurrent_tx_build() -> None:
        start_gate.wait()
        started_tx = time.perf_counter()
        chunk_value, _history, _rms = concurrent_builder.build_scaled_chunk(
            concurrent_source,
            peak_target=args.tx_digital_peak,
            rms_target=args.tx_digital_rms,
        )
        tx_result["elapsed"] = time.perf_counter() - started_tx
        tx_result["samples"] = int(chunk_value.size)

    concurrent_packets = 0

    def concurrent_callback(_packet, _pcm) -> None:
        nonlocal concurrent_packets
        concurrent_packets += 1

    concurrent_receiver = StreamingOfdmReceiver(
        codec,
        concurrent_callback,
        correlation_threshold=0.20,
        channel_selector=lambda: {0},
    )
    worker = threading.Thread(target=concurrent_tx_build, daemon=True)
    worker.start()
    concurrent_started = time.perf_counter()
    start_gate.set()
    for start in range(0, stream.size, RX_BUFFER_SAMPLES):
        concurrent_receiver.feed(stream[start : start + RX_BUFFER_SAMPLES])
    worker.join()
    concurrent_elapsed = time.perf_counter() - concurrent_started

    block_seconds = RX_BUFFER_SAMPLES / SAMPLE_RATE
    build_max = max(build_times)
    rx_mean = statistics.mean(all_rx_times)
    rx_max = max(all_rx_times)
    search_max = max(search_times) if search_times else 0.0
    print()
    print(
        f"TX build median/max: {statistics.median(build_times):.3f}/{build_max:.3f} s "
        "per 1.000 s chunk"
    )
    print(
        f"RX block: {RX_BUFFER_SAMPLES} samples = {block_seconds:.6f} s; "
        f"unlocked acquisition stride={ACQUISITION_BLOCK_STRIDE}"
    )
    print(
        f"Unlocked feed mean/max: {rx_mean:.6f}/{rx_max:.6f} s per incoming block; "
        f"search max={search_max:.6f} s"
    )
    print(
        f"Locked all-channel stream: {locked_elapsed:.3f} s processing for "
        f"{locked_signal_seconds:.3f} s of samples; packets={locked_packets}/200"
    )
    print(
        f"Locked selected-channel stream: {selected_elapsed:.3f} s processing for "
        f"{locked_signal_seconds:.3f} s of samples; packets={selected_packets}/50"
    )
    print(
        f"Concurrent TX-build + selected RX: {concurrent_elapsed:.3f} s wall time; "
        f"TX={tx_result.get('elapsed', float('nan')):.3f} s, "
        f"packets={concurrent_packets}/50"
    )

    ok = True
    if codec.backend != "native C" or codec.audio_backend != "native C":
        print("FAIL: native FEC and ADPCM are required for the Pi real-time profile")
        ok = False
    if build_max >= 0.85:
        print("WARN: TX generation has less than 15% reserve")
        if build_max >= 1.0:
            ok = False
    # Average work must stay below the stream arrival rate. A single acquisition
    # search may be longer than one block because deliberately skipped blocks
    # provide the corresponding reserve.
    if rx_mean >= 0.80 * block_seconds:
        print("FAIL: average unlocked detector work has insufficient stream reserve")
        ok = False
    if search_max >= 0.90 * ACQUISITION_BLOCK_STRIDE * block_seconds:
        print("WARN: acquisition search has limited reserve before the next scheduled search")
        if search_max >= ACQUISITION_BLOCK_STRIDE * block_seconds:
            ok = False
    if locked_elapsed >= 0.90 * locked_signal_seconds:
        print("WARN: all-channel decoder has less than 10% real-time reserve")
    if locked_packets < 196:
        print("FAIL: all-channel clean-stream benchmark did not recover essentially all 200 packets")
        ok = False
    if selected_elapsed >= 0.75 * locked_signal_seconds:
        print("FAIL: selected-channel playback decoder has insufficient reserve")
        ok = False
    if selected_packets < 49:
        print("FAIL: selected-channel benchmark did not recover essentially all 50 packets")
        ok = False
    if concurrent_elapsed >= 0.90 * locked_signal_seconds:
        print("FAIL: concurrent TX generation and selected RX have insufficient reserve")
        ok = False
    if concurrent_packets < 49:
        print("FAIL: concurrent benchmark did not recover essentially all selected packets")
        ok = False

    if ok:
        print(f"PASS: {PROFILE_NAME} software-only real-time budget checks passed")
        return 0
    print(f"FAIL: {PROFILE_NAME} requires a lower profile or further optimization before RF streaming")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
