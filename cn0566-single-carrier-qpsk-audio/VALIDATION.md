# Validation — 1.3.0 GitHub release

Validated on 2026-07-26 in the packaging environment:

```text
Native Viterbi C build/load: passed
Python syntax: passed
Unit/integration tests: 7/7 passed
End-to-end simulation: 12/12 packet payloads exact
Simulation correlation: approximately 0.927
Simulation EVM: approximately -32.3 dB
Simulation residual CFO: approximately +6.9 Hz
Deterministic performance simulation: report and five plots generated
```

The hardware path was not opened during packaging. RF validation remains an
operator test using `run_rf_sanity.sh`, `run_qpsk_no_play.sh` and
`run_qpsk_audio.sh`.
