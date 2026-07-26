#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"
exec python3 cn0566_qpsk_audio.py \
  --mode simulate --wav "$WAV" \
  --correlation-threshold "$CORRELATION_THRESHOLD" \
  --log-level "$LOG_LEVEL"
