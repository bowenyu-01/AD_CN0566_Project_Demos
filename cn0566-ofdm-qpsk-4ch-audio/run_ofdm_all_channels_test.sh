#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"
ensure_radio_idle
for file in "$WAV1" "$WAV2" "$WAV3" "$WAV4"; do
    [[ -f "$file" ]] || { echo "Missing $file" >&2; exit 1; }
done
read -r -p "Type OFDM_ALL_TEST to decode all four channels: " answer
[[ "$answer" == "OFDM_ALL_TEST" ]] || { echo "Cancelled"; exit 1; }
python3 cn0566_ofdm_4audio.py \
    --mode transceive --enable-tx --no-play --no-interactive \
    --wav1 "$WAV1" --wav2 "$WAV2" --wav3 "$WAV3" --wav4 "$WAV4" \
    --phaser-uri "$PHASER_URI" --sdr-uri "$SDR_URI" \
    --tx-gain "$TX_GAIN" --rx-gain "$RX_GAIN" --array-gain "$ARRAY_GAIN" \
    --beam-phase-diff "$BEAM_PHASE_DIFF" --calibration-dir "$CAL_DIR" \
    --decode-mode "$DECODE_MODE" --decode-scope all \
    --correlation-threshold "$CORRELATION_THRESHOLD" \
    --tx-digital-rms "$TX_DIGITAL_RMS" --tx-digital-peak "$TX_DIGITAL_PEAK" \
    --run-seconds "$OFDM_TEST_SECONDS" --listen all --log-level "$LOG_LEVEL"
