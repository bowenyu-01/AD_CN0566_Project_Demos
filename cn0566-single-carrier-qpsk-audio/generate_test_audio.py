#!/usr/bin/env python3
from pathlib import Path
import wave
import numpy as np

RATE = 16000
SECONDS = 8
n = np.arange(RATE * SECONDS, dtype=np.float64)
audio = 0.42 * np.sin(2*np.pi*440*n/RATE) + 0.16*np.sin(2*np.pi*880*n/RATE)
pcm = np.clip(np.rint(audio * 32767), -32768, 32767).astype('<i2')
for name in ('sample.wav', 'sample_test.wav'):
    with wave.open(str(Path(__file__).with_name(name)), 'wb') as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(RATE)
        handle.writeframes(pcm.tobytes())
    print('Wrote', name)
