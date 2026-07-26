# Validation — 2.3.1 GitHub release

Validated on 2026-07-26 in the packaging environment:

```text
Native FEC/IMA-ADPCM C build/load: passed
Unit/integration tests: 12/12 passed
Four-stream simulation: 120/120 packet payloads exact
Simulation EVM: approximately -22 dB on all four streams
Stable-profile realtime benchmark: passed
  TX build max: 0.145 s per 1.0 s chunk
  Locked selected-channel RX: 50/50 packets in 0.086 s for 1.048 s samples
  Concurrent TX + selected RX: 50/50 packets
Standard PRBS performance simulation: 60 accepted frames
  PRBS7/9/15/23 BER and SER: 0
  Median/mean EVM: approximately -20.4 to -20.5 dB
```

No RF hardware was opened during this packaging validation. Hardware acceptance
still requires the documented sanity, probe, no-play and audio sequence.
