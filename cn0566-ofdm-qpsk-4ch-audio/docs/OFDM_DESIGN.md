# OFDM design — 2.1.1 realtime-stable

## Common PHY

```text
FFT=256, CP=32
192 QPSK data carriers, 16 BPSK pilots
4 logical streams, 48 interleaved carriers each
2 identical training symbols per 20 ms frame
rate-1/2 K=7 convolutional code
one 179-byte protected audio packet per stream per frame
```

## Profiles

| Profile | Fs | Symbols/frame | Data symbols | Occupied span | Net half-rate capacity |
|---|---:|---:|---:|---:|---:|
| stable_1m | 1.0 MSPS | 69 | 67 | 843.75 kHz | 643.2 kbit/s |
| balanced_1p5m | 1.5 MSPS | 104 | 102 | 1.265625 MHz | 979.2 kbit/s |
| high_2m | 2.0 MSPS | 138 | 136 | 1.6875 MHz | 1.3056 Mbit/s |

The four-stream packet requirement is 286.4 kbit/s before FEC and 575.2
kbit/s after FEC, so the stable profile carries all four audio streams.

## Realtime acquisition policy

Each profile uses 131,072-sample IIO RX blocks. While unlocked, a full FFT
preamble search is performed on selected blocks only:

```text
stable_1m:       every 2nd block
balanced_1p5m:   every 2nd block
high_2m:         every 3rd block
```

A searched block contains multiple complete 20 ms frames. Skipped blocks are
still acquired and counted but not subjected to the expensive full search.
After one valid frame, predictive tracking searches a small window around each
20 ms boundary.

## Acceptance rule

Calculated capacity does not establish a working profile. A profile is accepted
only when the Raspberry Pi benchmark, cyclic probe, and long no-play hardware
run all pass with zero sustained RX drops and TX starvation.
