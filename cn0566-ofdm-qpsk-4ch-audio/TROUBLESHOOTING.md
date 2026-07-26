# Troubleshooting — 2.3.0 selected-playback

| Symptom | Meaning | Corrective action |
|---|---|---|
| Clean corr/EVM/BER but `rate5` below 50/s | Pi is doing more logical-channel work than the selected playback requires | Confirm `DECODE_SCOPE=selected`; start with `listen 1` |
| `host_drop` rises while listening to one channel | RX consumer still cannot sustain the sample stream | Use two decoder workers, metrics interval 25 or greater, no recording, and inspect Pi throttling |
| `listen all` causes drops | Four simultaneous FEC/ADPCM decodes exceed this Pi's live margin | Listen to fewer channels or use the all-channel test as a non-playback stress test |
| Selection change briefly stops audio | New channel has no historical decoded reservoir | Expected: ALSA restarts after a fresh contiguous buffer is assembled |
| Audio remains intermittent with `rate5≈50`, `host_drop=0` | Audio-device scheduling or reservoir issue | Test `aplay` directly; raise `AUDIO_BUFFER_MS` and `AUDIO_WRITE_FRAMES` |
| Audio recording increases drops | Four WAV writes add storage I/O | Leave `RECORD_DIR` empty for live playback |

## Earlier failure signatures

| Symptom | Meaning | Corrective action |
|---|---|---|
| `rate` near 15-20/s, `host_drop` rising | Receiver cannot process 50 frames/s | Install 2.2.0, rebuild native library, run realtime benchmark |
| ALSA multi-second `underrun!!!` | Audio writer is not receiving/scheduling PCM fast enough | Confirm rate near 50/s and host_drop=0; use batched ALSA path |
| `gap` rises on all four channels together | Entire OFDM frames are being lost | Inspect host_drop, lock and TX starvation before RF gain |
| Good corr/EVM followed by bad EVM and BER≈0.5 | Frame alignment lost, usually after dropped samples | Fix host throughput; reacquire with no queue drops |
| `tx_starve=0`, `tx_q` grows | TX producer is healthy | Do not change TX architecture |
| `host_drop=0` but EVM remains worse than about -7 dB | RF margin/equalization is inadequate | Run RF sanity/probe; adjust one RF gain at a time |
| Benchmark fails locked stream | Per-frame FFT/FEC path is still too slow | Verify four workers, native backend, no throttling; keep stable_1m |
| `Viterbi backend` not native C | Slow fallback active | Run `./build_native.sh` and verify `.so` beside Python file |
| `READ LINE: -32` | Pluto RNDIS/IIO failure | Stop all processes, reconnect/power-cycle, run diagnose_iio |
| Audio starts despite sparse packets | Old audio reservoir logic | Install 2.2.0; it requires contiguous startup sequences |

Check Pi health:

```bash
vcgencmd get_throttled
vcgencmd measure_temp
```

A normal non-throttled result is `throttled=0x0`.
