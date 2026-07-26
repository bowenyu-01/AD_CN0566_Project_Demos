#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"
read -r -p 'Type QPSK_MEASURE to transmit the deterministic QPSK probe: ' answer
[[ "$answer" == "QPSK_MEASURE" ]] || { echo 'Cancelled.'; exit 1; }
exec python3 cn0566_qpsk_performance.py --mode hardware --enable-tx \
  --phaser-uri "$PHASER_URI" --sdr-uri "$SDR_URI" --cal-dir "$CAL_DIR" \
  --tx-output "$TX_OUTPUT" --tx-gain "$TX_GAIN" --rx-gain "$RX_GAIN" \
  --array-gain "$ARRAY_GAIN" --beam-phase-diff "$BEAM_PHASE_DIFF" \
  --decode-channel "$DECODE_CHANNEL" --correlation-threshold "$CORRELATION_THRESHOLD" \
  --output-root qpsk_performance_runs --label hardware --log-level "$LOG_LEVEL"
