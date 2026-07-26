#!/usr/bin/env python3
"""Cyclic OFDM probe separating RF/synchronization from live TX generation.

The probe builds ten valid four-stream OFDM superframes from silence, repeats
that fixed waveform in Pluto's cyclic TX buffer, and runs the normal receiver.
It is diagnostic only: repeated sequence numbers are expected at each cyclic
wrap, and no audio is played.
"""

from __future__ import annotations

import argparse
import logging
import math
import time
from pathlib import Path

import numpy as np

from cn0566_ofdm_core import (
    AUDIO_FRAME_SAMPLES,
    PROFILE_NAME,
    RX_BUFFER_SAMPLES,
    SAMPLE_RATE,
    FecCodec,
    FourAudioSource,
    OfdmFrameBuilder,
    StreamingOfdmReceiver,
    configure_hardware,
    profile_summary,
)

LOG = logging.getLogger("cn0566-ofdm-probe")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Cyclic four-stream OFDM RF/synchronization probe")
    p.add_argument("--phaser-uri", default="ip:localhost")
    p.add_argument("--sdr-uri", default="ip:192.168.2.1")
    p.add_argument("--enable-tx", action="store_true")
    p.add_argument("--tx-gain", type=float, default=-45.0)
    p.add_argument("--rx-gain", type=float, default=20.0)
    p.add_argument("--array-gain", type=int, default=127)
    p.add_argument("--beam-phase-diff", type=float, default=0.0)
    p.add_argument("--calibration-dir", type=Path)
    p.add_argument("--decode-mode", choices=("rx2", "mrc"), default="rx2")
    p.add_argument("--correlation-threshold", type=float, default=0.25)
    p.add_argument("--tx-digital-peak", type=float, default=8000.0)
    p.add_argument("--run-seconds", type=float, default=10.0)
    p.add_argument("--probe-frames", type=int, default=10)
    p.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return p


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
    )
    if not args.enable_tx:
        raise SystemExit("RF transmission disabled; add --enable-tx only after checking TX_OUT_2 and the antenna")
    if args.probe_frames < 2:
        raise SystemExit("--probe-frames must be at least 2")

    fec = FecCodec()
    silence = [np.zeros(AUDIO_FRAME_SAMPLES * args.probe_frames, dtype=np.int16) for _ in range(4)]
    source = FourAudioSource(silence, repeat=True)
    builder = OfdmFrameBuilder(fec)
    waveform, _history, rms = builder.build_scaled_chunk(
        source,
        superframes=args.probe_frames,
        peak_target=args.tx_digital_peak,
    )
    peak = float(np.max(np.abs(waveform)))
    crest_db = 20.0 * math.log10(max(peak / max(rms, 1e-12), 1.0))
    LOG.info("Probe profile: %s", profile_summary())
    LOG.info(
        "Cyclic probe: %d frames, %.3f s, %d samples, RMS=%.1f, peak=%.1f, crest=%.1f dB",
        args.probe_frames,
        waveform.size / SAMPLE_RATE,
        waveform.size,
        rms,
        peak,
        crest_db,
    )

    packets = [0, 0, 0, 0]

    def packet_callback(packet, _pcm):
        packets[packet.stream_id] += 1

    receiver = StreamingOfdmReceiver(
        fec=fec,
        packet_callback=packet_callback,
        correlation_threshold=args.correlation_threshold,
        decode_mode=args.decode_mode,
    )

    phaser = None
    sdr = None
    try:
        phaser, sdr, lock = configure_hardware(args)
        if lock == 0:
            raise RuntimeError("CN0566 PLL is not locked")
        sdr.tx_destroy_buffer()
        sdr.tx_cyclic_buffer = True
        sdr.tx(waveform)
        LOG.info("Cyclic OFDM probe started; profile=%s", PROFILE_NAME)
        time.sleep(0.25)
        for _ in range(4):
            _ = sdr.rx()

        started = time.monotonic()
        last_report = started
        slow_feeds = 0
        feed_count = 0
        buffer_period = RX_BUFFER_SAMPLES / SAMPLE_RATE
        while time.monotonic() - started < args.run_seconds:
            data = sdr.rx()
            if args.decode_mode == "mrc":
                rx1 = np.asarray(data[0], dtype=np.complex64)
                rx2 = np.asarray(data[1], dtype=np.complex64)
            else:
                if isinstance(data, (list, tuple)):
                    data = data[0]
                rx2 = np.asarray(data, dtype=np.complex64)
                rx1 = None
            t0 = time.monotonic()
            receiver.feed(rx2, rx1)
            elapsed = time.monotonic() - t0
            feed_count += 1
            if elapsed > buffer_period:
                slow_feeds += 1
            now = time.monotonic()
            if now - last_report >= 1.0:
                evm = ",".join(
                    "nan" if not math.isfinite(value) else f"{value:.1f}"
                    for value in receiver.stats.last_evm_db
                )
                ber = ",".join(
                    "nan" if not math.isfinite(value) else f"{value:.2e}"
                    for value in receiver.stats.last_prbs_ber
                )
                LOG.info(
                    "frames=%d/%d rate=%.1f/s lock=%d corr=%.3f CFO=%sHz "
                    "RMS=%.2f peak=%.1f packets=%s EVM=%s dB BER=%s "
                    "feed=%.1fms/%0.1fms slow=%d/%d",
                    receiver.stats.frames_decoded,
                    receiver.stats.frames_detected,
                    receiver.stats.frames_decoded / max(now - started, 1e-9),
                    int(receiver.locked),
                    receiver.stats.last_correlation,
                    "nan" if not math.isfinite(receiver.stats.last_cfo_hz) else f"{receiver.stats.last_cfo_hz:+.1f}",
                    receiver.stats.last_rx_rms,
                    receiver.stats.last_rx_peak,
                    packets,
                    evm,
                    ber,
                    elapsed * 1000.0,
                    buffer_period * 1000.0,
                    slow_feeds,
                    feed_count,
                )
                last_report = now

        if receiver.stats.frames_decoded == 0:
            LOG.error(
                "Probe decoded no frames. This isolates the failure to RF margin, "
                "OFDM acquisition/equalization, or receive processing—not live TX generation."
            )
            return 2
        LOG.info(
            "Probe completed: %d decoded frames; per-channel packets=%s; slow feeds=%d/%d",
            receiver.stats.frames_decoded,
            packets,
            slow_feeds,
            feed_count,
        )
        return 0
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


if __name__ == "__main__":
    raise SystemExit(main())
