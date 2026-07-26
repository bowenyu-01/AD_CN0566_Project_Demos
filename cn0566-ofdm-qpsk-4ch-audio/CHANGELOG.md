# Changelog

## 2.3.1-github-release — 2026-07-26

- Prepared a clean public repository from the validated 2.3.0 selected-playback
  runtime.
- Added GitHub metadata, CI, citation, security and contribution documents.
- Added conservative public RF defaults and retained a labeled measured example.
- Integrated the deterministic four-channel PRBS QPSK-OFDM performance tool.
- Moved historical hotfix documentation into `docs/release-history/`.
- Excluded compiled shared libraries, calibration pickles and runtime output.

## 2.3.0-selected-playback

- Transmit all four streams while decoding only selected audible channels.
- Reduced Raspberry Pi realtime load and added five-second rate reporting.
- Preserved the stable 1 MSPS profile and interactive channel selection.
