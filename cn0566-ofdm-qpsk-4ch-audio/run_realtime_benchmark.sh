#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"
python3 benchmark_rpi.py \
    --wav1 "$WAV1" --wav2 "$WAV2" --wav3 "$WAV3" --wav4 "$WAV4" \
    --tx-digital-rms "$TX_DIGITAL_RMS" --tx-digital-peak "$TX_DIGITAL_PEAK"
