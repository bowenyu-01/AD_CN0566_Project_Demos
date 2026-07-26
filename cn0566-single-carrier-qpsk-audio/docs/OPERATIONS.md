# Operations

## Non-RF checks

```bash
./check_install.sh
./run_tests.sh
./run_simulation.sh
```

## Safe RF bring-up

1. Confirm the Vivaldi is on `TX_OUT_2` and the two receive cables match
   `PORT_PROFILE.md`.
2. Keep antennas separated and directional.
3. Confirm no stale process owns Pluto:

   ```bash
   pgrep -af 'cn0566_(qpsk_audio|rf_sanity|qpsk_performance)\.py'
   ```

4. Run the cyclic tone test.
5. Run the modem without playback for at least 60 seconds.
6. Enable playback only when frame rate is near 50/s and drop counters remain
   zero.

## Shutdown

Press Ctrl-C once. After an abnormal stop:

```bash
pkill -TERM -f 'cn0566_(qpsk_audio|rf_sanity|qpsk_performance)\.py'
pkill -TERM -x aplay
sleep 3
iio_info -s
```
