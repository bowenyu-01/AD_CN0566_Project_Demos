# CN0566 four-channel QPSK-OFDM — 2.3.0 selected-playback

This release transmits all four 16 kHz IMA-ADPCM streams on every OFDM
superframe, but the Raspberry Pi decodes only the logical channel or channels
currently selected for listening.  The previous 2.2.0 audio run listened to
channel 1 while still performing four Viterbi/ADPCM decodes per 20 ms frame.
Its RF metrics were clean, yet live throughput settled around 38–40 frames/s
and `host_drop` increased.  Continuous audio requires 50 frames/s.

The default realtime controls are now:

```text
DECODE_SCOPE=selected
CHANNEL_DECODE_WORKERS=2
METRICS_INTERVAL=25
AUDIO_BUFFER_MS=2000
AUDIO_WRITE_FRAMES=10
RX_QUEUE_BUFFERS=32
RECORD_DIR=empty
```

Use `run_ofdm_no_play.sh` to test the currently selected channel with no ALSA.
Use `run_ofdm_all_channels_test.sh` only as an all-channel Raspberry Pi stress
test.  `listen 1,3` decodes channels 1 and 3; `listen all` requests all four and
may exceed the Pi's live budget.  A selection change deliberately closes ALSA
and rebuilds a fresh contiguous reservoir for the new channel set.

The status line reports `rate5`, the rolling five-second frame rate.  This is
more useful than the total-average `rate` during startup.

## Version history and retained design

This release streams four independent 16 kHz mono IMA-ADPCM audio files over one
QPSK-OFDM waveform using:

```text
Pluto TX2A -> CN0566 TX_IN -> CN0566 TX_OUT_2 -> Vivaldi
CN0566 RX2 -> Pluto RX2A              (default realtime receive path)
CN0566 RX1 -> Pluto RX1A              (optional MRC experiment)
```

The release is a continuation of the previous project. It preserves the packet
format, native rate-1/2 convolutional codec, native IMA-ADPCM codec, four logical
carrier groups, interactive listening, performance plots, and hardware profile.

## Why 2.2.0 exists

A physical `stable_1m` run decoded only about 15.8 OFDM frames/s while the audio
source requires 50 frames/s.  `host_drop` increased from 0 to 74, sequence gaps
rose rapidly, and ALSA reported multi-second underruns.  The RF link itself was
usable whenever the receiver was aligned (`corr` about 0.90, EVM about -9 to
-10 dB, and low raw BER).  The dominant fault was single-core receive
processing, followed by an audio sink that delayed playback but did not preload
ALSA with contiguous audio.

Version 2.2.0:

- replaces the generic native Viterbi loop with a fixed-trellis implementation;
- reuses thread-local Viterbi workspaces instead of allocating them for every
  packet;
- decodes the four logical carrier groups concurrently on the Raspberry Pi 4
  cores;
- uses a separable CFO phasor, reducing complex exponential evaluations from
  roughly 20,000 to about 360 per frame;
- uses direct correlation for the short locked tracking window;
- refreshes live BER/EVM every fifth frame rather than on every frame;
- requires a genuinely contiguous audio startup reservoir;
- writes 200 ms PCM blocks to a buffered ALSA pipe so the reservoir protects
  the speaker from Linux scheduling jitter;
- adds `audio_drop` and `conceal` to the live status line.

The RF gain and OFDM framing are unchanged.  Apply this release before changing
TX or RX gain.

## Why 2.1.2 exists

A previous 2.1 overlay could leave a mixed source tree when applied on top of
2.0.1: the stability-profile core and the 2.0.1 compatibility tests did not
refer to the same API set. Symptoms included missing `FecCodec.audio_backend`,
`set_ad9361_sample_rate_robust`, and `TX_DIGITAL_RMS_DEFAULT` during tests even
though profile simulations passed.

Version 2.1.2 contains one internally consistent tree. Its patch installer
replaces the complete `tests/` directory rather than merging it, removes stale
Python bytecode, retains the 2.0.1 native ADPCM/IIO/RMS fixes, and retains the
2.1 realtime-stability profiles and acquisition scheduling.

## Why 2.1.1 exists

The Raspberry Pi benchmark for release 2.0.1 measured:

```text
TX generation:            0.758/0.760 s per one-second chunk  -> passes
RX block duration:        0.065536 s at 2 MSPS
Unlocked detector:        0.106476/0.125816 s per RX block    -> fails
```

The detector was 1.63 times slower than realtime at the median and 1.92 times
slower at the maximum. A 2 MSPS live run would therefore build RX backlog and
increase `host_drop`, even when RF quality was good.

Version 2.1.2 makes `stable_1m` the default, retains native ADPCM/FEC, keeps the
large 131,072-sample RX block, and schedules an expensive acquisition search on
one of every two blocks while unlocked. Every searched block spans several
20 ms frames, so frame-acquisition opportunities remain abundant. Once a frame
is decoded, the receiver returns to small predictive tracking windows.

## Selectable profiles

| Profile | Fs | Raw QPSK data carriers | Rate-1/2 framed information | RX block duration | Acquisition stride | Status |
|---|---:|---:|---:|---:|---:|---|
| `stable_1m` | 1.0 MSPS | 1.333 Mbit/s | 643.2 kbit/s | 131.072 ms | 2 | Default |
| `balanced_1p5m` | 1.5 MSPS | 2.000 Mbit/s | 979.2 kbit/s | 87.381 ms | 2 | Benchmark after stable pass |
| `high_2m` | 2.0 MSPS | 2.667 Mbit/s | 1.3056 Mbit/s | 65.536 ms | 3 | Experimental |

The four protected audio packets require about 286.4 kbit/s before FEC and
575.2 kbit/s after the existing rate-1/2 encoder. All four streams therefore fit
inside `stable_1m`.

## OFDM structure

```text
FFT / CP:                  256 / 32 samples
Active carriers:           208
Data / pilots:             192 / 16
Logical audio streams:     4
Data carriers per stream:  48, interleaved across the occupied band
Modulation:                QPSK
FEC:                       rate-1/2, K=7, 0o133/0o171
Audio packet:              one independent 20 ms IMA-ADPCM packet per stream
```

## Installation in a new folder

```bash
cd ~/Desktop/QPSK_test
unzip CN0566_OFDM_4CH_RPi4_Release_2_1_1_stable.zip
cd CN0566_OFDM_4CH_RPi4_Release_2_1_1_stable
chmod +x *.sh
./setup_rpi.sh
```

Replace the bundled test WAV files as needed, retaining the names:

```text
sample_1.wav
sample_2.wav
sample_3.wav
sample_4.wav
```

## Required acceptance sequence

```bash
./run_tests.sh
./run_simulation.sh
./run_realtime_benchmark.sh
./diagnose_iio.sh
./run_rf_sanity.sh
./run_ofdm_probe.sh
./run_ofdm_no_play.sh
./run_ofdm_audio.sh
```

Do not proceed to RF when the realtime benchmark fails.

### Realtime benchmark pass conditions

The selected profile must report:

```text
FEC backend: native C
IMA-ADPCM backend: native C
TX build maximum < 1.0 s per 1.0 s chunk
average unlocked feed work below the RX arrival interval
acquisition search completes before its next scheduled search
locked clean stream processed faster than realtime
200/200 clean-stream packets recovered
```

The benchmark now measures both unlocked acquisition and a one-second locked
stream. A profile that passes simulation but fails this benchmark is not accepted
for live RF streaming.

## Configuration

Edit `config.env`:

```bash
OFDM_PROFILE="stable_1m"
TX_GAIN="-40"
RX_GAIN="20"
ARRAY_GAIN="127"
BEAM_PHASE_DIFF="0.0"
TX_DIGITAL_RMS="2000"
TX_DIGITAL_PEAK="6000"
CORRELATION_THRESHOLD="0.20"
DECODE_MODE="rx2"
```

`-40 dB` is the previously established direct-path setting, not a universal
safe starting value for a different geometry. Use the RF sanity test and change
only one gain at a time.

## RF sanity tones

| Profile | Tone |
|---|---:|
| `stable_1m` | +125 kHz |
| `balanced_1p5m` | +187.5 kHz |
| `high_2m` | +250 kHz |

The narrow FFT `tone/median` value includes coherent processing gain. Judge the
wideband link primarily from coherent `CW_SNR`, estimated OFDM SNR, ADC headroom,
and later the OFDM probe results.

## Cyclic OFDM probe

`run_ofdm_probe.sh` repeats a fixed, valid OFDM waveform in Pluto's cyclic TX
buffer. It separates RF/acquisition/equalization performance from live TX
producer scheduling. The probe must decode frames on all four logical channels
before the live no-play run.

## Interactive playback

Run:

```bash
./run_ofdm_audio.sh
```

Commands while running:

```text
listen 1
listen 2,4
listen all
listen none
status
help
quit
```

All four channels remain decoded. Selected channels are averaged into one mono
ALSA output. Recovered streams can also be written to `received_audio/`.

## Escalating the profile

A profile is accepted only after:

1. `run_realtime_benchmark.sh` passes on the Raspberry Pi.
2. RF sanity gives useful margin without clipping.
3. The cyclic probe decodes continuously.
4. A no-play run of at least 10–30 minutes has `host_drop=0` and `tx_starve=0`.
5. All four packet counters rise at approximately 50 packets/s per stream.

Then change only:

```bash
OFDM_PROFILE="balanced_1p5m"
```

Repeat every test. Treat `high_2m` as experimental even when its software-only
benchmark passes; USB/IIO full-duplex behavior must also pass on the physical
Pi/Pluto configuration.

## Important metrics

```text
rate           decoded OFDM frames per second; target about 50/s
lock           receiver tracking state
corr / det     successful-frame correlation / current detector peak
host_drop      RX buffers lost by the application queue; target 0
tx_starve      TX pusher lacked a prepared chunk; target 0
tx_q           prepared TX chunks waiting
EVM            equalized QPSK error vector magnitude; more negative is better
BER            raw known-PRBS bit error rate per logical channel
```

## Safe shutdown and IIO recovery

Use `quit` or Ctrl-C and wait for cleanup. After `READ LINE: -32`, a broken pipe,
or interrupted cleanup:

```bash
pkill -f cn0566_ofdm || true
sleep 3
# reconnect or power-cycle the supported assembly when needed
./diagnose_iio.sh
```

Do not run RF sanity, probe, no-play, audio, or hardware performance capture at
the same time.
