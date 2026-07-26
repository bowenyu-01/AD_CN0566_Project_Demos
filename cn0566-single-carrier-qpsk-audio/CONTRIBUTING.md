# Contributing

Contributions should preserve the fixed hardware map and distinguish protocol
changes from implementation changes.

Before opening a pull request:

```bash
./build_native.sh
./run_tests.sh
./run_simulation.sh
./run_qpsk_performance_simulation.sh
```

Do not commit:

- `.venv/`, compiled `.so` files or Python bytecode;
- hardware-specific calibration pickle files;
- raw I/Q captures, performance output or received audio;
- private credentials or device identifiers.

RF changes must document safe starting gain, expected bandwidth, center
frequency, port routing and cleanup behavior.
