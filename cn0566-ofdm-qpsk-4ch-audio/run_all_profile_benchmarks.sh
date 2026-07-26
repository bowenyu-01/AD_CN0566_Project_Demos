#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
for profile in stable_1m balanced_1p5m high_2m; do
    echo "===== $profile benchmark ====="
    CN0566_OFDM_PROFILE="$profile" \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 \
    CN0566_DISABLE_NUMBA=1 \
    python3 benchmark_rpi.py \
        --wav1 sample_1.wav --wav2 sample_2.wav \
        --wav3 sample_3.wav --wav4 sample_4.wav \
        --tx-digital-rms 2000 --tx-digital-peak 6000
done
