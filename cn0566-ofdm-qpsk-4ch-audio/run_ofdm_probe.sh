#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"
ensure_radio_idle
echo "This repeats a fixed valid OFDM waveform in Pluto's cyclic TX buffer."
echo "It separates RF/synchronization performance from live TX generation."
read -r -p "Type OFDM_PROBE to continue: " answer
[[ "$answer" == "OFDM_PROBE" ]] || { echo "Cancelled"; exit 1; }
python3 cn0566_ofdm_probe.py \
    --enable-tx \
    --phaser-uri "$PHASER_URI" --sdr-uri "$SDR_URI" \
    --tx-gain "$TX_GAIN" --rx-gain "$RX_GAIN" --array-gain "$ARRAY_GAIN" \
    --beam-phase-diff "$BEAM_PHASE_DIFF" --calibration-dir "$CAL_DIR" \
    --decode-mode "$DECODE_MODE" --correlation-threshold "$CORRELATION_THRESHOLD" \
    --tx-digital-peak "$TX_DIGITAL_PEAK" --run-seconds 10 \
    --log-level "$LOG_LEVEL"
