# v1.3.0-github-release

GitHub-ready source release for the CN0566/Pluto/Raspberry Pi single-carrier
QPSK audio link. Includes the realtime modem, native Viterbi source, RF sanity
test, deterministic performance test, synthetic test WAV, tests, CI and
complete operating documentation.

No compiled shared library or hardware-specific calibration pickle is included;
run `./build_native.sh` on the target Raspberry Pi.
