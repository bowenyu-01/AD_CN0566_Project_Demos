# Operations procedure — 2.2.0 realtime-parallel

## 1. Software acceptance

```bash
./build_native.sh
./run_tests.sh
./run_simulation.sh
./run_realtime_benchmark.sh
```

Require native FEC/ADPCM, four channel workers, 200/200 benchmark packets, and a
locked processing time below the signal duration.

## 2. Pi health

```bash
vcgencmd get_throttled
vcgencmd measure_temp
```

Close browsers and plotting programs during live streaming.

## 3. RF validation

```bash
./diagnose_iio.sh
./run_rf_sanity.sh
./run_ofdm_probe.sh
./run_ofdm_no_play.sh
```

Run no-play for at least 60 seconds.  Require about 50 frames/s,
`host_drop=0`, and `tx_starve=0` before speaker playback.

## 4. Audio

```bash
./run_ofdm_audio.sh
```

Playback begins only after a contiguous reservoir and is written to ALSA in
200 ms blocks by default.  Use `listen 1`, `listen 2,4`, `listen all`, `status`,
and `quit`.

## 5. Shutdown

Use `quit` or Ctrl-C and wait for IIO buffers to close.  Never run two Pluto
applications concurrently.
