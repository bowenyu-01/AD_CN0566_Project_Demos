#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
if [[ -f config.env ]]; then
  # shellcheck disable=SC1091
  source config.env
fi
if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
export CN0566_DISABLE_NUMBA=1
: "${PHASER_URI:=ip:localhost}"
: "${SDR_URI:=ip:192.168.2.1}"
: "${CAL_DIR:=calibration}"
: "${WAV:=sample.wav}"
: "${TX_OUTPUT:=2}"
: "${TX_GAIN:=-60}"
: "${RX_GAIN:=20}"
: "${ARRAY_GAIN:=127}"
: "${BEAM_PHASE_DIFF:=0.0}"
: "${DECODE_CHANNEL:=rx2}"
: "${CORRELATION_THRESHOLD:=0.18}"
: "${AUDIO_DEVICE:=default}"
: "${AUDIO_BUFFER_MS:=1500}"
: "${LOG_LEVEL:=INFO}"
