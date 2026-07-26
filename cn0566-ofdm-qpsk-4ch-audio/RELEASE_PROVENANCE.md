# Release provenance

This repository is based on the validated `2.3.0-selected-playback` source tree.
Version `2.3.1-github-release` changes repository packaging, public defaults,
documentation, CI and performance-tool integration; it does not intentionally
change the OFDM packet format or selected-playback runtime architecture.

The standard PRBS performance tool is integrated from the previously validated
four-channel performance package. Compiled shared libraries and physical-array
calibration files are intentionally excluded and must be built or supplied on
the target system.
