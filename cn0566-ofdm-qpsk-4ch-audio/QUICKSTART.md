# Quick start

```bash
chmod +x *.sh *.py
./setup_rpi.sh
./run_tests.sh
./run_simulation.sh
./run_realtime_benchmark.sh
```

Then inspect `config.env` and run, in order:

```bash
./diagnose_iio.sh
./run_rf_sanity.sh
./run_ofdm_probe.sh
./run_ofdm_no_play.sh
./run_ofdm_audio.sh
```

Use `stable_1m` until a long no-play run has zero `host_drop` and zero
`tx_starve`. Stop with Ctrl-C once and allow IIO cleanup to finish.
