#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"
rm -rf performance_output/simulation
python3 cn0566_ofdm_performance.py \
    --mode simulate --frames 100 \
    --wav1 "$WAV1" --wav2 "$WAV2" --wav3 "$WAV3" --wav4 "$WAV4" \
    --decode-mode "$DECODE_MODE" \
    --output-dir performance_output/simulation \
    --log-level "$LOG_LEVEL"
echo "Plots and CSV files: performance_output/simulation"
