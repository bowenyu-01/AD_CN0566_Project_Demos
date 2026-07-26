#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f config.env || ! -f _run_common.sh ]]; then
    echo "Run this script from the active CN0566_OFDM_4CH_RPi4_Release directory." >&2
    exit 1
fi
# shellcheck disable=SC1091
source ./_run_common.sh

active="$(ps -eo pid=,comm=,args= | awk '$2 ~ /^python/ && $0 ~ /cn0566_(ofdm_4audio|ofdm_performance|ofdm_probe|rf_sanity_ofdm|ofdm_qpsk_pattern_performance|ofdm_qpsk_performance_report)\.py/ {print}' || true)"
if [[ -n "$active" ]]; then
    echo "Another CN0566/Pluto process is still running:" >&2
    echo "$active" >&2
    exit 1
fi
sleep 1

: "${PATTERN_PERFORMANCE_SECONDS:=8}"
: "${PATTERN_PERFORMANCE_MAX_FRAMES:=300}"
label="${1:-}"

cat <<EOF
This test does not read sample_*.wav.
It transmits four deterministic standard PRBS QPSK patterns:
  Channel 1: PRBS7
  Channel 2: PRBS9
  Channel 3: PRBS15
  Channel 4: PRBS23
Capture duration: ${PATTERN_PERFORMANCE_SECONDS} seconds
Results root: qpsk_pattern_performance_runs/
EOF
read -r -p "Type QPSK_PATTERN_PERF to enable the cyclic RF test: " answer
[[ "$answer" == "QPSK_PATTERN_PERF" ]] || { echo "Cancelled"; exit 1; }

args=(
    --mode hardware
    --enable-tx
    --capture-seconds "$PATTERN_PERFORMANCE_SECONDS"
    --max-frames "$PATTERN_PERFORMANCE_MAX_FRAMES"
    --output-root qpsk_pattern_performance_runs
    --phaser-uri "$PHASER_URI"
    --sdr-uri "$SDR_URI"
    --tx-gain "$TX_GAIN"
    --rx-gain "$RX_GAIN"
    --array-gain "$ARRAY_GAIN"
    --beam-phase-diff "$BEAM_PHASE_DIFF"
    --decode-mode "$DECODE_MODE"
    --correlation-threshold "$CORRELATION_THRESHOLD"
    --tx-digital-rms "$TX_DIGITAL_RMS"
    --tx-digital-peak "$TX_DIGITAL_PEAK"
    --log-level "$LOG_LEVEL"
)
if [[ -n "${CAL_DIR:-}" ]]; then
    args+=(--calibration-dir "$CAL_DIR")
fi
if [[ -n "$label" ]]; then
    args+=(--label "$label")
fi

python3 cn0566_ofdm_qpsk_pattern_performance.py "${args[@]}"
echo "Latest results:"
find qpsk_pattern_performance_runs -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
    | sort -nr | head -1 | cut -d' ' -f2-
