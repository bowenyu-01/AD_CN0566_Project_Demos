# Why this hotfix is required

The supplied live logs showed clean RF metrics (`corr≈0.96`, EVM near -15 dB,
raw PRBS BER 0) but only about 38–40 decoded frames/s and a growing
`host_drop` counter. Audio requires 50 frames/s because one frame contains
20 ms of PCM per stream.

The 2.2 benchmark measured about 0.408 s of TX generation and about 0.734 s of
four-channel RX processing separately. Their sum exceeds one CPU-second per
second before USB/IIO, logging, and ALSA are included. In playback mode the
user listened to channel 1, but the receiver still performed four independent
FEC and ADPCM decodes per frame.

Version 2.3 keeps all four streams on air and performs only the decodes needed
for the current listener selection. Selecting multiple channels increases the
load proportionally. `listen all` is supported but should be treated as a
stress condition on this Raspberry Pi; use the dedicated all-channel test to
measure it before relying on four-channel simultaneous playback.
