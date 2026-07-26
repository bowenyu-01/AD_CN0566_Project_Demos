# Deterministic QPSK performance test

The measurement tool bypasses live audio scheduling and repeatedly transmits a
known QPSK frame. This allows exact pre-FEC BER/SER and EVM evaluation.

Simulation:

```bash
./run_qpsk_performance_simulation.sh
```

Hardware:

```bash
./run_qpsk_performance.sh
```

Results are written to timestamped folders under `qpsk_performance_runs/`.
Plotting occurs after hardware capture so Matplotlib does not interfere with the
realtime IIO read loop.
