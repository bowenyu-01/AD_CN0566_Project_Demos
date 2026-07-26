#!/usr/bin/env python3
from pathlib import Path
import numpy as np
from scipy.io import wavfile

FS = 16_000
DURATION = 8.0
FREQUENCIES = (440.0, 554.37, 659.25, 783.99)

def main() -> None:
    t = np.arange(int(FS * DURATION), dtype=np.float64) / FS
    for index, frequency in enumerate(FREQUENCIES, start=1):
        envelope = np.minimum(1.0, t / 0.05) * np.minimum(1.0, (DURATION - t) / 0.05)
        signal = 0.42 * envelope * np.sin(2.0 * np.pi * frequency * t)
        signal += 0.12 * envelope * np.sin(2.0 * np.pi * 2.0 * frequency * t)
        pcm = np.rint(np.clip(signal, -0.95, 0.95) * 32767.0).astype(np.int16)
        path = Path(f"sample_{index}.wav")
        wavfile.write(path, FS, pcm)
        print(f"Wrote {path}: {DURATION:.1f} s, {FS} Hz, tone {frequency:.2f} Hz")

if __name__ == "__main__":
    main()
