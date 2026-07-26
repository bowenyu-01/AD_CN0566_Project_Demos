#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import importlib
import platform
import shutil
import sys
from pathlib import Path


def check_import(name: str):
    module = importlib.import_module(name)
    version = getattr(module, "__version__", "not exposed")
    print(f"PASS import {name}: {version}")
    return module


def main() -> int:
    print("Python:", sys.version.replace("\n", " "))
    print("Architecture:", platform.machine())
    check_import("numpy")
    check_import("scipy")
    check_import("matplotlib")
    adi = check_import("adi")
    check_import("iio")
    from adi.cn0566 import CN0566  # noqa: F401
    assert hasattr(adi, "ad9361")
    print("PASS pyadi-iio AD9361 and CN0566 classes")

    library = Path(__file__).resolve().with_name("libcn0566_fec.so")
    loaded = ctypes.CDLL(str(library))
    for symbol in (
        "cn0566_conv_encode",
        "cn0566_viterbi_decode",
        "cn0566_ima_adpcm_encode",
        "cn0566_ima_adpcm_decode",
    ):
        getattr(loaded, symbol)
    print("PASS native FEC/ADPCM:", library)

    from cn0566_ofdm_core import (
        CHANNEL_DATA_BINS,
        DATA_BINS,
        NET_RATE_HALF_FEC_BPS,
        RAW_DATA_CARRIER_BPS,
        SAMPLE_RATE,
        PROFILE_NAME,
        FecCodec,
        RX_BUFFER_SAMPLES,
        TX_DIGITAL_RMS_DEFAULT,
        TX_DIGITAL_PEAK_DEFAULT,
    )
    assert PROFILE_NAME in {"stable_1m", "balanced_1p5m", "high_2m"}
    assert SAMPLE_RATE in {1_000_000, 1_500_000, 2_000_000}
    assert len(DATA_BINS) == 192
    assert all(len(group) == 48 for group in CHANNEL_DATA_BINS)
    assert RX_BUFFER_SAMPLES == 131_072
    assert 0 < TX_DIGITAL_RMS_DEFAULT < TX_DIGITAL_PEAK_DEFAULT
    codec = FecCodec()
    assert codec.backend == "native C"
    assert codec.audio_backend == "native C"
    print(
        f"PASS OFDM profile {PROFILE_NAME}: raw={RAW_DATA_CARRIER_BPS/1e6:.3f} Mbps, "
        f"net={NET_RATE_HALF_FEC_BPS/1e6:.3f} Mbps, "
        f"RX block={RX_BUFFER_SAMPLES} samples"
    )
    print(f"PASS native runtime backends: FEC={codec.backend}, ADPCM={codec.audio_backend}")

    for utility in ("gcc", "aplay", "iio_info"):
        print(f"{'PASS' if shutil.which(utility) else 'WARN'} utility {utility}: {shutil.which(utility)}")
    print("Environment check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
