# Validation record — 2.2.0 realtime-parallel

The release passed:

- 11 unit/integration tests;
- native Viterbi comparison with the Python reference;
- native IMA-ADPCM comparison;
- four-stream exact-packet simulation;
- RMS/peak-limited waveform round trip;
- one-second TX boundary reacquisition;
- malformed `[value, -32]` IIO readback handling;
- contiguous audio-reservoir tests;
- stable, balanced, and high profile simulations;
- 200/200 clean packets in the locked software benchmark.

On the packaging host, `stable_1m` processed about 1.048 seconds of locked
samples in about 0.10 to 0.12 seconds.  Raspberry Pi performance must be
confirmed with `./run_realtime_benchmark.sh`; RF use requires a subsequent
no-play run with `host_drop=0` and `tx_starve=0`.
