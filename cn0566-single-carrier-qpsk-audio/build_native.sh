#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
CC_BIN="${CC:-gcc}"
command -v "$CC_BIN" >/dev/null 2>&1 || {
  echo "gcc not found. Install with: sudo apt install -y build-essential" >&2
  exit 1
}
"$CC_BIN" -O3 -std=c99 -fPIC -shared -Wall -Wextra \
  -o libcn0566_viterbi.so cn0566_viterbi.c
python3 - <<'PY'
import ctypes
from pathlib import Path
p = Path('libcn0566_viterbi.so').resolve()
lib = ctypes.CDLL(str(p))
getattr(lib, 'cn0566_viterbi_decode')
print('Native Viterbi load test passed:', p)
PY
