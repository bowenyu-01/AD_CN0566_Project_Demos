#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f config.env || ! -f _run_common.sh ]]; then
    echo "Run this script from the active CN0566_OFDM_4CH_RPi4_Release directory." >&2
    exit 1
fi
# shellcheck disable=SC1091
source ./_run_common.sh
label="${1:-simulation_check}"
python3 cn0566_ofdm_qpsk_pattern_performance.py \
    --mode simulate \
    --simulation-frames 80 \
    --max-frames 60 \
    --output-root qpsk_pattern_performance_runs \
    --label "$label" \
    --log-level "$LOG_LEVEL"
