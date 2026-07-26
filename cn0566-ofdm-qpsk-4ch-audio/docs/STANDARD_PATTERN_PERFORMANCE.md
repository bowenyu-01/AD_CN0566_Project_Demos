# Standard-pattern QPSK-OFDM PHY performance test

This integrated tool does not read `sample_1.wav` through `sample_4.wav`. It
transmits four deterministic pseudo-random QPSK test streams on the same four
interleaved logical OFDM carrier groups used by the audio application:

| Logical channel | Test sequence |
|---|---|
| 1 | PRBS7: x^7 + x^6 + 1 |
| 2 | PRBS9: x^9 + x^5 + 1 |
| 3 | PRBS15: x^15 + x^14 + 1 |
| 4 | PRBS23: x^23 + x^18 + 1 |

The known bits permit direct BER, SER and EVM calculation without audio codec,
packet or FEC effects. Plot generation occurs after capture so Matplotlib does
not slow the realtime IIO path.

## Simulation

```bash
./run_standard_pattern_performance_sim.sh
```

## Hardware

Stop other CN0566/Pluto applications, inspect `config.env`, and run:

```bash
./run_standard_pattern_performance_hardware.sh reflector_test
```

The launcher requires the exact confirmation text `QPSK_PATTERN_PERF`.

Each run creates a timestamped folder under
`qpsk_pattern_performance_runs/` containing:

- `REPORT.txt`
- channel and frame CSV/JSON data
- per-channel equalized-symbol arrays
- constellation, derived eye, waveform and PSD figures
- overall OFDM waveform/PSD and EVM stability plots

The generated eye is a derived QPSK decision eye from equalized subcarrier
symbols. It is not a conventional raw OFDM time-domain eye.
