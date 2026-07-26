#!/usr/bin/env python3
from __future__ import annotations
import ctypes
import importlib
import shutil
from pathlib import Path

required = ('numpy', 'scipy')
for name in required:
    module = importlib.import_module(name)
    print(f'{name}: {getattr(module, "__version__", "unknown")}')
try:
    import iio  # noqa: F401
    import adi  # noqa: F401
    from adi.cn0566 import CN0566  # noqa: F401
    print('pyadi-iio / pylibiio: import passed')
except Exception as exc:
    print('Hardware bindings unavailable:', exc)
lib = Path(__file__).with_name('libcn0566_viterbi.so')
if not lib.is_file():
    raise SystemExit('Missing libcn0566_viterbi.so; run ./build_native.sh')
handle = ctypes.CDLL(str(lib.resolve()))
getattr(handle, 'cn0566_viterbi_decode')
print('native Viterbi symbol: passed')
print('aplay:', shutil.which('aplay') or 'not found (use --no-play)')
print('Environment check passed')
