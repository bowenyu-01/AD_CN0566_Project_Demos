#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_run_common.sh"
./build_native.sh
python3 -m unittest discover -s tests -v
