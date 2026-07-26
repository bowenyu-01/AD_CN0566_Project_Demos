#!/usr/bin/env bash
set -euo pipefail
sudo apt update
sudo apt install -y python3-matplotlib
source "$(dirname "$0")/_run_common.sh"
python3 - <<'PY'
import matplotlib
print('Matplotlib', matplotlib.__version__)
PY
