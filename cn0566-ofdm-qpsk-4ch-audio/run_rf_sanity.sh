#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"
ensure_radio_idle
echo "This emits the $OFDM_PROFILE profile-specific cyclic diagnostic tone through TX_OUT_2."
read -r -p "Type OFDM_RF_TEST to continue: " answer
[[ "$answer" == "OFDM_RF_TEST" ]] || { echo "Cancelled"; exit 1; }
python3 cn0566_rf_sanity_ofdm.py \
    --enable-tx \
    --phaser-uri "$PHASER_URI" --sdr-uri "$SDR_URI" \
    --tx-gain "$TX_GAIN" --rx-gain "$RX_GAIN" --array-gain "$ARRAY_GAIN" \
    --beam-phase-diff "$BEAM_PHASE_DIFF" \
    --calibration-dir "$CAL_DIR" \
    --decode-mode "$DECODE_MODE" \
    --tx-digital-rms "$TX_DIGITAL_RMS" --tx-digital-peak "$TX_DIGITAL_PEAK"
