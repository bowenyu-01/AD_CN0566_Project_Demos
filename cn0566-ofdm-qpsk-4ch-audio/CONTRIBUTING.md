# Contributing

Before opening a pull request:

```bash
./build_native.sh
./run_tests.sh
./run_simulation.sh
./run_realtime_benchmark.sh
./run_standard_pattern_performance_sim.sh
```

Protocol changes must document carrier allocation, packet format, pilots,
training symbols, cyclic prefix, profile capacity and backwards compatibility.
Realtime changes must include Raspberry Pi timing measurements.

Do not commit `.venv/`, compiled `.so` files, calibration pickle files, raw I/Q,
received audio, timestamped performance output or private device identifiers.
