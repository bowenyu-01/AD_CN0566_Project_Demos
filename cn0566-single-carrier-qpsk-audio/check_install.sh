#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"
python3 -m py_compile cn0566_qpsk_audio.py cn0566_rf_sanity.py cn0566_qpsk_performance.py verify_environment.py
./build_native.sh
python3 verify_environment.py
if command -v iio_info >/dev/null 2>&1; then iio_info -s || true; fi
./run_tests.sh
./run_simulation.sh
