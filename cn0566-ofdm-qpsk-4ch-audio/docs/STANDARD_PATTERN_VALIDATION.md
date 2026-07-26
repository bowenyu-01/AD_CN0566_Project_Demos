# Validation

The tool was checked against the `stable_1m` profile from the 2.3.0 selected-playback release.

Software simulation configuration:

```text
40 generated test frames
22 dB simulated SNR
120 Hz simulated CFO
three-tap simulated multipath channel
RX2 processing
```

Observed smoke-test result:

```text
accepted frames: 20/20 analysis limit
accepted frame spacing: 50.000 frames/s
all four BER values: 0
all four SER values: 0
median EVM: approximately -20.3 to -20.4 dB
TX RMS: approximately 2000
TX peak: 6000
limited sample fraction: approximately 0.005%
output PAPR: approximately 9.54 dB
```

The generated output contained the requested report, CSV/JSON/NPZ data, four constellation plots, four derived eye plots, four channel waveform comparisons, four channel PSD plots, a full-waveform comparison, a full PSD comparison, and an EVM-stability plot.

Physical CN0566/Pluto performance must be measured on the target Raspberry Pi and RF geometry.
