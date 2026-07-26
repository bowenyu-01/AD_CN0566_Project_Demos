#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"
printf 'RF profile: Pluto TX2A -> CN0566 TX_IN -> TX_OUT_%s -> Vivaldi; CN0566 RX2 -> Pluto RX2A\n' "$TX_OUTPUT"
read -r -p 'Type QPSK_TX to enable the single-carrier transmitter: ' answer
[[ "$answer" == "QPSK_TX" ]] || { echo 'Cancelled.'; exit 1; }
exec python3 cn0566_qpsk_audio.py \
  --mode transceive --enable-tx --no-play --wav "$WAV" \
  --phaser-uri "$PHASER_URI" --sdr-uri "$SDR_URI" --cal-dir "$CAL_DIR" \
  --tx-output "$TX_OUTPUT" --tx-gain "$TX_GAIN" --rx-gain "$RX_GAIN" \
  --array-gain "$ARRAY_GAIN" --beam-phase-diff "$BEAM_PHASE_DIFF" \
  --decode-channel "$DECODE_CHANNEL" --correlation-threshold "$CORRELATION_THRESHOLD" \
  --audio-device "$AUDIO_DEVICE" --audio-buffer-ms "$AUDIO_BUFFER_MS" \
  --log-level "$LOG_LEVEL"
