# v2.3.1-github-release

GitHub-ready source release of the CN0566/Pluto/Raspberry Pi four-channel
QPSK-OFDM audio link. Based on the validated 2.3.0 selected-playback runtime.

Highlights:

- four independent IMA-ADPCM audio streams in one OFDM waveform;
- 192 QPSK data carriers, 16 pilots and four interleaved 48-carrier groups;
- selected-channel realtime decoding and interactive playback;
- stable 1 MSPS profile plus 1.5/2 MSPS benchmark profiles;
- native FEC/ADPCM source, tests and realtime benchmark;
- cyclic RF sanity and OFDM probe utilities;
- audio-based and deterministic PRBS performance reports;
- CI and public repository documentation.

Compile `libcn0566_fec.so` on the target machine with `./build_native.sh`.
