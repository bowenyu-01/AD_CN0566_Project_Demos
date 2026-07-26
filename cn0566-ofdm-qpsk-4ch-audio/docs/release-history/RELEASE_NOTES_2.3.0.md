# Release 2.3.0 — selected-channel realtime playback

This release addresses Raspberry Pi 4 host drops observed while only channel 1
was audible but all four logical channels were still Viterbi-decoded on every
20 ms OFDM frame.

## Main changes

- All four audio streams remain present in every transmitted OFDM superframe.
- `DECODE_SCOPE="selected"` decodes only the logical channels currently chosen
  by `listen ...`; a muted receiver decodes channel 1 as a low-load health
  monitor.
- Interactive selection changes close the old ALSA process, clear stale packet
  state, and rebuild a contiguous reservoir for the new channel set.
- The all-channel stress test remains available as
  `run_ofdm_all_channels_test.sh`.
- RX application queue capacity increases from 16 to 32 blocks.
- Default native decoder workers reduce from 4 to 2 so the RX acquisition,
  OFDM FFT/equalizer, TX producer, and operating system retain CPU time.
- Live BER/EVM calculations run every 25th frame rather than every fifth frame.
- Audio startup reservoir increases to 2 seconds.
- Continuous WAV recording is disabled by default; set `RECORD_DIR` to enable
  it deliberately.
- Status now reports both total-average `rate` and rolling five-second `rate5`,
  plus the logical channels currently decoded.
- The realtime benchmark includes selected-channel and concurrent TX/RX CPU
  tests; the older benchmark measured TX and RX separately and could pass even
  when their combined live workload was excessive.

## RF and protocol compatibility

No RF port, frequency, OFDM carrier allocation, packet, FEC, ADPCM, gain, or
sample-rate change is made. Existing four-stream transmitters and waveform
settings remain compatible.
