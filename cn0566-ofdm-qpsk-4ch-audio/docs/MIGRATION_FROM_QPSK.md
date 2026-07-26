# Relationship to the previous single-carrier QPSK project

This release is installed in a new folder and does not overwrite the previous `1.2.0-realtime-audio` project.

Reused concepts:

- Fixed CN0566/Pluto port map.
- 16 kHz mono WAV conversion.
- Independent 20 ms IMA ADPCM blocks.
- CRC-32 and sequence numbers.
- Rate-1/2 K=7 convolutional coding and native C decoding.
- One-second TX chunks and producer/pusher queues.
- RX2-only maximum-throughput mode.
- ALSA startup reservoir and packet-loss concealment.
- Conservative RF interlock and shutdown behavior.

Replaced signal-processing components:

- RRC single-carrier pulse shaping is replaced by 256-point OFDM IFFT/FFT processing.
- The single-carrier preamble is replaced by two identical training OFDM symbols.
- Fractional QPSK symbol timing search is replaced by superframe correlation and cyclic-prefix-tolerant FFT windowing.
- A single flat channel estimate is replaced by one complex estimate per active subcarrier.
- Four audio streams are assigned to four interleaved 48-carrier logical groups.

The old project remains useful as the stable single-carrier fallback and as an RF sanity reference.
