#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"
exec python3 cn0566_qpsk_performance.py --mode simulate \
  --output-root qpsk_performance_runs --label software_validation \
  --correlation-threshold "$CORRELATION_THRESHOLD" --log-level "$LOG_LEVEL"
