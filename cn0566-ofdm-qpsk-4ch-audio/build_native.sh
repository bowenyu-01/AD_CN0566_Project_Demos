#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
CC="${CC:-gcc}"
"$CC" -O3 -std=gnu11 -fPIC -shared -Wall -Wextra \
    -o libcn0566_fec.so cn0566_fec.c
python3 - <<'PY'
import ctypes
from pathlib import Path
p = Path('libcn0566_fec.so').resolve()
lib = ctypes.CDLL(str(p))
for name in ('cn0566_conv_encode', 'cn0566_viterbi_decode', 'cn0566_ima_adpcm_encode', 'cn0566_ima_adpcm_decode'):
    getattr(lib, name)
print(f'Native FEC/ADPCM load test passed: {p}')
PY
