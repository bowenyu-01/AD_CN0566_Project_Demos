#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"
ensure_radio_idle
for file in "$WAV1" "$WAV2" "$WAV3" "$WAV4"; do
    [[ -f "$file" ]] || { echo "Missing $file" >&2; exit 1; }
done
read -r -p "Type OFDM_PERF to run the RF performance capture: " answer
[[ "$answer" == "OFDM_PERF" ]] || { echo "Cancelled"; exit 1; }
rm -rf performance_output/hardware
python3 cn0566_ofdm_performance.py \
    --mode hardware --enable-tx --run-seconds "$PERFORMANCE_SECONDS" \
    --wav1 "$WAV1" --wav2 "$WAV2" --wav3 "$WAV3" --wav4 "$WAV4" \
    --phaser-uri "$PHASER_URI" --sdr-uri "$SDR_URI" \
    --tx-gain "$TX_GAIN" --rx-gain "$RX_GAIN" --array-gain "$ARRAY_GAIN" \
    --beam-phase-diff "$BEAM_PHASE_DIFF" --calibration-dir "$CAL_DIR" \
    --decode-mode "$DECODE_MODE" --correlation-threshold "$CORRELATION_THRESHOLD" \
    --tx-digital-rms "$TX_DIGITAL_RMS" --tx-digital-peak "$TX_DIGITAL_PEAK" \
    --output-dir performance_output/hardware \
    --log-level "$LOG_LEVEL"
echo "Plots and CSV files: performance_output/hardware"
