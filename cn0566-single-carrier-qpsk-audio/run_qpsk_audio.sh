#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"
read -r -p 'Type QPSK_AUDIO to enable RF and live audio playback: ' answer
[[ "$answer" == "QPSK_AUDIO" ]] || { echo 'Cancelled.'; exit 1; }
exec python3 cn0566_qpsk_audio.py \
  --mode transceive --enable-tx --wav "$WAV" \
  --phaser-uri "$PHASER_URI" --sdr-uri "$SDR_URI" --cal-dir "$CAL_DIR" \
  --tx-output "$TX_OUTPUT" --tx-gain "$TX_GAIN" --rx-gain "$RX_GAIN" \
  --array-gain "$ARRAY_GAIN" --beam-phase-diff "$BEAM_PHASE_DIFF" \
  --decode-channel "$DECODE_CHANNEL" --correlation-threshold "$CORRELATION_THRESHOLD" \
  --audio-device "$AUDIO_DEVICE" --audio-buffer-ms "$AUDIO_BUFFER_MS" \
  --log-level "$LOG_LEVEL"
