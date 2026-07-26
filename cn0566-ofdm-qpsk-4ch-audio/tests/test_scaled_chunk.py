import unittest
from pathlib import Path

import numpy as np

from cn0566_ofdm_core import (
    FecCodec,
    FourAudioSource,
    OfdmFrameBuilder,
    RX_BUFFER_SAMPLES,
    StreamingOfdmReceiver,
    TX_DIGITAL_PEAK_DEFAULT,
    TX_DIGITAL_RMS_DEFAULT,
    load_wav_as_pcm16,
)


class ScaledChunkTests(unittest.TestCase):
    def test_rms_limited_chunk_decodes_exactly(self):
        root = Path(__file__).resolve().parents[1]
        fec = FecCodec()
        pcm = [load_wav_as_pcm16(root / f"sample_{i}.wav") for i in range(1, 5)]
        source = FourAudioSource(pcm)
        builder = OfdmFrameBuilder(fec)
        chunk, history, rms = builder.build_scaled_chunk(
            source,
            superframes=5,
            rms_target=TX_DIGITAL_RMS_DEFAULT,
            peak_target=TX_DIGITAL_PEAK_DEFAULT,
        )
        self.assertLessEqual(float(np.max(np.abs(chunk))), TX_DIGITAL_PEAK_DEFAULT + 1.0)
        self.assertGreater(rms, 0.95 * TX_DIGITAL_RMS_DEFAULT)
        self.assertLess(builder.last_clip_fraction, 0.002)
        expected = {
            (packet.stream_id, packet.sequence): packet.raw
            for packets in history
            for packet in packets
        }
        received = {}
        receiver = StreamingOfdmReceiver(
            fec,
            lambda packet, _pcm: received.__setitem__(
                (packet.stream_id, packet.sequence), packet.raw
            ),
            correlation_threshold=0.20,
        )
        stream = np.concatenate(
            (
                np.zeros(7777, dtype=np.complex64),
                chunk,
                np.zeros(100_000, dtype=np.complex64),
            )
        )
        for start in range(0, stream.size, RX_BUFFER_SAMPLES):
            receiver.feed(stream[start:start + RX_BUFFER_SAMPLES])
        self.assertEqual(received, expected)


if __name__ == "__main__":
    unittest.main()
