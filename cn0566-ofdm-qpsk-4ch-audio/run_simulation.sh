#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"
python3 cn0566_ofdm_4audio.py \
    --mode simulate \
    --wav1 "$WAV1" --wav2 "$WAV2" --wav3 "$WAV3" --wav4 "$WAV4" \
    --simulation-frames 30 \
    --decode-mode "$DECODE_MODE" \
    --log-level "$LOG_LEVEL"
