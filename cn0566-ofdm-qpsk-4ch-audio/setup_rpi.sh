#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

sudo apt-get update
sudo apt-get install -y \
    python3 python3-pip python3-venv python3-dev \
    python3-numpy python3-scipy python3-matplotlib \
    build-essential alsa-utils libgfortran5 libiio-utils libiio0

sudo apt-get install -y libopenblas0-pthread || sudo apt-get install -y libopenblas0
sudo ldconfig

if [[ ! -d .venv ]]; then
    python3 -m venv --system-site-packages .venv
fi
source .venv/bin/activate
python3 -m pip install --upgrade pip setuptools wheel

# Preserve the operating system's matched NumPy/SciPy/Matplotlib packages.
python3 -m pip install --prefer-binary "pylibiio>=0.25" "pyadi-iio>=0.0.21"

chmod +x build_native.sh run_*.sh check_install.sh diagnose_iio.sh generate_test_audio.py benchmark_rpi.py cn0566_ofdm_probe.py cn0566_ofdm_qpsk_pattern_performance.py cn0566_ofdm_qpsk_performance_report.py
./build_native.sh
python3 verify_environment.py
python3 -m unittest discover -s tests -v

echo
echo "Installation completed. Run ./run_simulation.sh before enabling RF."
