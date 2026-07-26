#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
sudo apt update
sudo apt install -y \
  alsa-utils build-essential libopenblas0 libiio-utils \
  python3-venv python3-pip python3-numpy python3-scipy python3-matplotlib
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip setuptools wheel
python3 -m pip install --no-deps 'pylibiio>=0.25' 'pyadi-iio>=0.0.21'
./build_native.sh
./check_install.sh
