#!/usr/bin/env python3
"""Four-audio-stream QPSK-OFDM application for CN0566 + Pluto + Raspberry Pi 4."""

from __future__ import annotations

import argparse
import logging
import math
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from cn0566_ofdm_core import (
    AUDIO_FRAME_SECONDS,
    AUDIO_WRITE_FRAMES,
    CHANNEL_DECODE_WORKERS,
    LIVE_METRICS_INTERVAL,
    DECODE_SCOPE_DEFAULT,
    CN0566_TX_OUTPUT_NUMBER,
    FecCodec,
    FourAudioSource,
    OfdmFrameBuilder,
    RX_QUEUE_BUFFERS,
    RuntimeStatistics,
    SAMPLE_RATE,
    SelectableAudioMixer,
    StreamingOfdmReceiver,
    TX_DIGITAL_PEAK_DEFAULT,
    TX_DIGITAL_RMS_DEFAULT,
    TX_INITIAL_PREFILL_CHUNKS,
    TX_PREFETCH_CHUNKS,
    configure_hardware,
    format_status,
    load_wav_as_pcm16,
    parse_listen_selection,
    profile_summary,
    run_software_simulation,
    rx_acquisition_worker,
    tx_producer_worker,
    tx_pusher_worker,
)

LOG = logging.getLogger("cn0566-ofdm-app")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stream four independent 16 kHz IMA-ADPCM audio files over one "
            "selectable-profile QPSK-OFDM link using CN0566 and PlutoSDR."
        )
    )
    parser.add_argument("--mode", choices=("simulate", "transceive", "tx", "rx"), default="simulate")
    parser.add_argument("--wav1", type=Path, default=Path("sample_1.wav"))
    parser.add_argument("--wav2", type=Path, default=Path("sample_2.wav"))
    parser.add_argument("--wav3", type=Path, default=Path("sample_3.wav"))
    parser.add_argument("--wav4", type=Path, default=Path("sample_4.wav"))
    parser.add_argument("--phaser-uri", default="ip:localhost")
    parser.add_argument("--sdr-uri", default="ip:192.168.2.1")
    parser.add_argument("--enable-tx", action="store_true", help="required RF-transmit interlock")
    parser.add_argument("--once", action="store_true", help="do not loop the WAV files")
    parser.add_argument("--tx-gain", type=float, default=-40.0, help="Pluto TX2 attenuation/gain in dB")
    parser.add_argument("--rx-gain", type=float, default=20.0)
    parser.add_argument("--array-gain", type=int, default=127)
    parser.add_argument("--beam-phase-diff", type=float, default=0.0)
    parser.add_argument("--calibration-dir", type=Path)
    parser.add_argument("--decode-mode", choices=("rx2", "mrc"), default="rx2")
    parser.add_argument(
        "--decode-scope",
        choices=("selected", "all"),
        default=DECODE_SCOPE_DEFAULT,
        help=(
            "selected decodes only currently audible logical channels; all "
            "decodes all four and is intended for validation/performance capture"
        ),
    )
    parser.add_argument("--correlation-threshold", type=float, default=0.20)
    parser.add_argument("--tx-digital-rms", type=float, default=TX_DIGITAL_RMS_DEFAULT)
    parser.add_argument("--tx-digital-peak", type=float, default=TX_DIGITAL_PEAK_DEFAULT)
    parser.add_argument("--listen", default="1", help="initial audible channels: 1, 1,3, all, or none")
    parser.add_argument("--no-play", action="store_true")
    parser.add_argument("--no-interactive", action="store_true")
    parser.add_argument("--audio-device", default="default")
    parser.add_argument("--audio-buffer-ms", type=int, default=1500)
    parser.add_argument("--record-dir", type=Path)
    parser.add_argument("--run-seconds", type=float, default=0.0)
    parser.add_argument("--simulation-frames", type=int, default=20)
    parser.add_argument("--simulation-snr-db", type=float, default=24.0)
    parser.add_argument("--simulation-cfo-hz", type=float, default=180.0)
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.tx_gain < -88.0 or args.tx_gain > 0.0:
        raise ValueError("--tx-gain must be in [-88, 0] dB")
    if not 0 <= args.array_gain <= 127:
        raise ValueError("--array-gain must be in 0..127")
    if not 0.05 <= args.correlation_threshold <= 0.95:
        raise ValueError("--correlation-threshold must be in [0.05, 0.95]")
    if not 500.0 <= args.tx_digital_peak <= 12_000.0:
        raise ValueError("--tx-digital-peak must be in [500, 12000]")
    if not 250.0 <= args.tx_digital_rms < args.tx_digital_peak:
        raise ValueError("--tx-digital-rms must be at least 250 and below --tx-digital-peak")
    if args.audio_buffer_ms < 100:
        raise ValueError("--audio-buffer-ms must be at least 100")
    if args.run_seconds < 0:
        raise ValueError("--run-seconds cannot be negative")
    parse_listen_selection(args.listen)


def wav_paths(args: argparse.Namespace):
    return [args.wav1, args.wav2, args.wav3, args.wav4]


def command_worker(
    mixer: SelectableAudioMixer,
    stop_event: threading.Event,
    status_callback: Callable[[], str],
) -> None:
    help_text = (
        "Commands: listen 1 | listen 1,3 | listen all | listen none | "
        "status | help | quit"
    )
    print(help_text, flush=True)
    while not stop_event.is_set():
        try:
            line = sys.stdin.readline()
        except Exception:
            return
        if line == "":
            return
        command = line.strip()
        if not command:
            continue
        lower = command.lower()
        try:
            if lower.startswith("listen "):
                mixer.set_selection(parse_listen_selection(command.split(None, 1)[1]))
            elif lower == "status":
                print(status_callback(), flush=True)
            elif lower in {"help", "?"}:
                print(help_text, flush=True)
            elif lower in {"quit", "exit", "q"}:
                stop_event.set()
                return
            else:
                print(f"Unknown command: {command}. {help_text}", flush=True)
        except Exception as exc:
            print(f"Command error: {exc}", flush=True)


def run_simulation(args: argparse.Namespace) -> int:
    result = run_software_simulation(
        wav_paths(args),
        frames=args.simulation_frames,
        snr_db=args.simulation_snr_db,
        cfo_hz=args.simulation_cfo_hz,
        decode_mode=args.decode_mode,
    )
    receiver = result["receiver"]
    LOG.info(
        "Simulation expected=%d received=%d exact=%d FEC=%s",
        result["expected_packets"],
        result["received_packets"],
        result["matching_packets"],
        result["fec_backend"],
    )
    LOG.info(
        "Simulation receiver frames=%d corr=%.3f CFO=%+.1f Hz EVM=%s",
        receiver.stats.frames_decoded,
        receiver.stats.last_correlation,
        receiver.stats.last_cfo_hz,
        ",".join(
            "nan" if not math.isfinite(value) else f"{value:.1f}dB"
            for value in receiver.stats.last_evm_db
        ),
    )
    if result["matching_packets"] != result["expected_packets"]:
        LOG.error("Simulation mismatches: %s", result["mismatches"][:20])
        return 2
    LOG.info("Simulation passed: all four-stream packet payloads match exactly")
    return 0


def run_hardware(args: argparse.Namespace) -> int:
    if args.mode in {"transceive", "tx"} and not args.enable_tx:
        raise RuntimeError(
            "RF transmission is disabled. Re-run with --enable-tx only after "
            "connecting TX_OUT_2 to the Vivaldi antenna and confirming safe/authorized operation."
        )

    stop_event = threading.Event()

    def request_stop(_signum=None, _frame=None):
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    runtime = RuntimeStatistics()
    fec = FecCodec()
    LOG.info("FEC backend: %s; IMA-ADPCM backend: %s", fec.backend, fec.audio_backend)
    LOG.info(
        "Realtime engine: decode scope=%s, channel workers=%d, "
        "live metrics every %d frame(s), ALSA write block=%d frames (%.0f ms)",
        args.decode_scope,
        CHANNEL_DECODE_WORKERS,
        LIVE_METRICS_INTERVAL,
        AUDIO_WRITE_FRAMES,
        1000.0 * AUDIO_WRITE_FRAMES * AUDIO_FRAME_SECONDS,
    )
    if fec.backend != "native C":
        LOG.warning("Native FEC is required for reliable Raspberry Pi real-time operation; run ./build_native.sh")

    source: Optional[FourAudioSource] = None
    builder: Optional[OfdmFrameBuilder] = None
    if args.mode in {"transceive", "tx"}:
        missing = [str(path) for path in wav_paths(args) if not path.is_file()]
        if missing:
            raise FileNotFoundError("Missing WAV files: " + ", ".join(missing))
        pcm = [load_wav_as_pcm16(path) for path in wav_paths(args)]
        source = FourAudioSource(pcm, repeat=not args.once)
        builder = OfdmFrameBuilder(fec)

    mixer = SelectableAudioMixer(
        enabled=(args.mode in {"transceive", "rx"} and not args.no_play),
        stop_event=stop_event,
        selected_channels=parse_listen_selection(args.listen),
        audio_device=args.audio_device,
        startup_buffer_ms=args.audio_buffer_ms,
        record_directory=args.record_dir,
        runtime_stats=runtime,
    )

    receiver: Optional[StreamingOfdmReceiver] = None
    if args.mode in {"transceive", "rx"}:
        receiver = StreamingOfdmReceiver(
            fec=fec,
            packet_callback=mixer.submit,
            correlation_threshold=args.correlation_threshold,
            decode_mode=args.decode_mode,
            channel_selector=(
                mixer.selected_channels if args.decode_scope == "selected" else None
            ),
        )

    phaser = None
    sdr = None
    threads: list[threading.Thread] = []
    rx_queue: "queue.Queue[tuple[np.ndarray, Optional[np.ndarray]]]" = queue.Queue(maxsize=RX_QUEUE_BUFFERS)
    tx_queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=TX_PREFETCH_CHUNKS)

    try:
        phaser, sdr, lock = configure_hardware(args)
        if lock == 0:
            raise RuntimeError("CN0566 PLL is not locked")
        mixer.start()

        if receiver is not None:
            for _ in range(4):
                _ = sdr.rx()
            thread = threading.Thread(
                target=rx_acquisition_worker,
                args=(sdr, args.decode_mode, rx_queue, stop_event, runtime),
                name="pluto-rx",
                daemon=True,
            )
            thread.start()
            threads.append(thread)

        if source is not None and builder is not None:
            # Prefill multiple one-second chunks before TX starts.  This gives the
            # Raspberry Pi producer a scheduling reservoir and prevents the immediate
            # queue-empty condition observed in release 1.0.0.
            prefill = min(TX_INITIAL_PREFILL_CHUNKS, TX_PREFETCH_CHUNKS)
            for index in range(prefill):
                chunk, _history, chunk_rms = builder.build_scaled_chunk(
                    source,
                    peak_target=args.tx_digital_peak,
                    rms_target=args.tx_digital_rms,
                )
                tx_queue.put(chunk)
                runtime.tx_chunks_built += 1
                runtime.tx_queue_depth = tx_queue.qsize()
                LOG.info(
                    "Prepared TX chunk %d/%d: %.1f s, %d samples, RMS=%.1f, "
                    "peak=%.1f, clip=%.4f%%, PAPR=%.2f dB",
                    index + 1,
                    prefill,
                    chunk.size / SAMPLE_RATE,
                    chunk.size,
                    chunk_rms,
                    float(np.max(np.abs(chunk))),
                    100.0 * builder.last_clip_fraction,
                    builder.last_output_papr_db,
                )
            producer = threading.Thread(
                target=tx_producer_worker,
                args=(
                    builder,
                    source,
                    tx_queue,
                    stop_event,
                    runtime,
                    args.tx_digital_peak,
                    args.tx_digital_rms,
                ),
                name="ofdm-tx-producer",
                daemon=True,
            )
            pusher = threading.Thread(
                target=tx_pusher_worker,
                args=(sdr, tx_queue, stop_event, runtime),
                name="ofdm-tx-pusher",
                daemon=True,
            )
            producer.start()
            pusher.start()
            threads.extend((producer, pusher))

        started = time.monotonic()
        last_report = started

        def current_status() -> str:
            if receiver is None:
                return (
                    f"TX chunks={runtime.tx_chunks_pushed}/{runtime.tx_chunks_built} "
                    f"queue={runtime.tx_queue_depth} starve={runtime.tx_starves}"
                )
            return format_status(receiver, runtime, time.monotonic() - started, mixer)

        if (
            not args.no_interactive
            and args.mode in {"transceive", "rx"}
            and sys.stdin.isatty()
        ):
            interactive = threading.Thread(
                target=command_worker,
                args=(mixer, stop_event, current_status),
                name="interactive-control",
                daemon=True,
            )
            interactive.start()
            threads.append(interactive)

        LOG.info("Running: %s", profile_summary())
        LOG.info("Initial listening selection: %s", mixer.selection_description())

        while not stop_event.is_set():
            now = time.monotonic()
            if args.run_seconds > 0 and now - started >= args.run_seconds:
                break
            if receiver is not None:
                try:
                    rx2, rx1 = rx_queue.get(timeout=0.05)
                    receiver.feed(rx2, rx1)
                except queue.Empty:
                    pass
            else:
                stop_event.wait(0.05)
            if now - last_report >= 1.0:
                runtime.tx_queue_depth = tx_queue.qsize()
                LOG.info(current_status())
                last_report = now
    finally:
        stop_event.set()
        mixer.stop()
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
                sdr.tx_hardwaregain_chan0 = -88.0
                sdr.tx_hardwaregain_chan1 = -88.0
            except Exception:
                pass
        del sdr
        del phaser
        time.sleep(0.25)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
    )
    try:
        validate_args(args)
        LOG.info("Selected OFDM profile: %s", profile_summary())
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
