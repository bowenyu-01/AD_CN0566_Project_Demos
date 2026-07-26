# Architecture

## Transmitter

1. Load a WAV file, average stereo to mono, remove DC and resample to 16 kHz.
2. Form independent 320-sample / 20 ms audio blocks.
3. Encode each block as a 164-byte IMA-ADPCM payload.
4. Add packet header, sequence, valid-sample count, flags and CRC-32.
5. Apply the rate-1/2 K=7 convolutional encoder and termination bits.
6. Map dibits to unit-energy QPSK.
7. Prepend the 256-symbol preamble and append deterministic QPSK filler.
8. Upsample by four and filter with a 41-tap root-raised-cosine FIR.
9. Insert the pulse-shaped waveform after a 512-sample zero guard.
10. Multiply by the +187.5 kHz complex NCO.
11. Build 50 frames into a one-second complex64 TX chunk.
12. A producer queue feeds a separate IIO TX thread.

## Receiver

1. Read RX2A by default, or RX1A/RX2A in MRC mode.
2. Remove per-block DC and digitally remove the +187.5 kHz offset.
3. Acquire the repeated preamble through normalized correlation.
4. Matched-filter and search fractional timing.
5. Estimate residual CFO from repeated 64-symbol preamble blocks.
6. Estimate the flat complex channel from the known preamble.
7. Equalize RX2 or coherently combine both analog subarray sums.
8. Use I/Q values as soft evidence for the native Viterbi decoder.
9. Validate CRC-32 and sequence continuity.
10. Decode independent IMA-ADPCM blocks.
11. Use a contiguous startup reservoir and faded-frame loss concealment.
12. Write mono PCM to `aplay`.

The waveform has a gross QPSK line rate of 500 kbit/s. The audio payload is
65.6 kbit/s; the remaining capacity is deliberately used for robust framing,
FEC and deterministic high-duty-cycle filler.
