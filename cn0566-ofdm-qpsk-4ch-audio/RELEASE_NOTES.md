# Release notes — 2.3.1-github-release

This release packages the validated 2.3.0 selected-playback source tree for
public GitHub distribution. The OFDM packet format and selected-channel realtime
architecture are unchanged.

Added:

- public repository metadata, CI, license proposal and citation file;
- conservative `TX_GAIN=-60` public configuration;
- labeled `config.measured-direct-path.env` with the previously working -40 dB
  laboratory value;
- integrated standard PRBS7/9/15/23 QPSK-OFDM performance measurement;
- refreshed README, quick start, contribution and security guidance;
- source-only release policy for native binaries and calibration data.

See `docs/release-history/` for the full 2.3.0 hotfix history.
