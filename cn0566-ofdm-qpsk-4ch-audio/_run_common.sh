#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f config.env ]]; then
    echo "Missing config.env" >&2
    exit 1
fi
# shellcheck disable=SC1091
source config.env
: "${OFDM_PROFILE:=stable_1m}"
export CN0566_OFDM_PROFILE="$OFDM_PROFILE"
: "${CHANNEL_DECODE_WORKERS:=2}"
: "${METRICS_INTERVAL:=25}"
: "${AUDIO_WRITE_FRAMES:=10}"
export CN0566_OFDM_CHANNEL_WORKERS="$CHANNEL_DECODE_WORKERS"
export CN0566_OFDM_METRICS_INTERVAL="$METRICS_INTERVAL"
export CN0566_OFDM_AUDIO_WRITE_FRAMES="$AUDIO_WRITE_FRAMES"
: "${DECODE_SCOPE:=selected}"
export CN0566_OFDM_DECODE_SCOPE="$DECODE_SCOPE"
if [[ -d .venv ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
else
    echo "Note: .venv not found; using the active system/CI Python environment." >&2
fi
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export CN0566_DISABLE_NUMBA=1

ensure_radio_idle() {
    local active
    active="$(pgrep -af 'cn0566_(ofdm_4audio|ofdm_performance|ofdm_probe|rf_sanity_ofdm|ofdm_qpsk_pattern_performance|ofdm_qpsk_performance_report)\.py' || true)"
    if [[ -n "$active" ]]; then
        echo "Another CN0566/Pluto process is still running:" >&2
        echo "$active" >&2
        echo "Stop it and wait two seconds before opening new IIO buffers." >&2
        exit 1
    fi
    # Give libiio/Pluto a short release interval after a preceding process.
    sleep 1
}
