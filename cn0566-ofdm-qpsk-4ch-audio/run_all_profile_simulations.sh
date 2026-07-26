#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
for profile in stable_1m balanced_1p5m high_2m; do
    echo "===== $profile simulation ====="
    CN0566_OFDM_PROFILE="$profile" \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 \
    CN0566_DISABLE_NUMBA=1 \
    .venv/bin/python3 cn0566_ofdm_4audio.py \
        --mode simulate \
        --wav1 sample_1.wav --wav2 sample_2.wav \
        --wav3 sample_3.wav --wav4 sample_4.wav \
        --decode-mode rx2 --log-level INFO
done
