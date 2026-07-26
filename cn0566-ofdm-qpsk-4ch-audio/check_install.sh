#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
python3 -m py_compile cn0566_ofdm_core.py cn0566_ofdm_4audio.py cn0566_ofdm_performance.py cn0566_ofdm_probe.py cn0566_rf_sanity_ofdm.py benchmark_rpi.py verify_environment.py
./build_native.sh
python3 verify_environment.py
python3 -m unittest discover -s tests -v
if command -v iio_info >/dev/null 2>&1; then
    echo "IIO contexts:"
    iio_info -s || true
fi
