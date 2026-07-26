# Performance output

`run_performance_sim.sh` writes to `performance_output/simulation`.

`run_performance_hardware.sh` writes to `performance_output/hardware`.

For each logical audio channel the test writes:

- `channel_N_constellation.png`: measured equalized QPSK carrier symbols.
- `channel_N_eye.png`: an analysis-only equivalent decision eye derived from the equalized OFDM symbols. OFDM does not have the same pulse-shaped eye as a single-carrier QPSK link.
- `channel_N_waveform.png`: reconstructed time-domain OFDM contribution after retaining only that channel's interleaved carrier group.
- `channel_N_spectrum.png`: spectrum of that reconstructed channel contribution.
- `channel_summary.csv`: packet count, EVM and raw PRBS BER by channel.
- `frame_metrics.csv`: frame-by-frame correlation, CFO, EVM and BER.
- `REPORT.txt`: text summary and plot interpretation.

Plots are generated only after the capture to avoid Matplotlib causing RX queue drops.
