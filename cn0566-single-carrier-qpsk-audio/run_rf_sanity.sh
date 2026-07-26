#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"
read -r -p 'Type QPSK_RF_TEST to transmit the +187.5 kHz cyclic test tone: ' answer
[[ "$answer" == "QPSK_RF_TEST" ]] || { echo 'Cancelled.'; exit 1; }
exec python3 cn0566_rf_sanity.py \
  --enable-tx --phaser-uri "$PHASER_URI" --sdr-uri "$SDR_URI" --cal-dir "$CAL_DIR" \
  --tx-output "$TX_OUTPUT" --tx-gain "$TX_GAIN" --rx-gain "$RX_GAIN" \
  --array-gain "$ARRAY_GAIN" --beam-phase-diff "$BEAM_PHASE_DIFF" \
  --decode-channel "$DECODE_CHANNEL"
