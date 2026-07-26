import unittest
from pathlib import Path
import numpy as np

from cn0566_ofdm_core import (
    FecCodec,
    FourAudioSource,
    OfdmFrameBuilder,
    RX_BUFFER_SAMPLES,
    StreamingOfdmReceiver,
    load_wav_as_pcm16,
)


class TxBoundaryGapTests(unittest.TestCase):
    def test_reacquires_after_one_second_gap(self):
        root = Path(__file__).resolve().parents[1]
        fec = FecCodec()
        pcm = [load_wav_as_pcm16(root / f"sample_{i}.wav") for i in range(1, 5)]
        source = FourAudioSource(pcm)
        builder = OfdmFrameBuilder(fec)
        expected = {}
        frames = []
        for _ in range(55):
            packets = source.next_packets()
            for packet in packets:
                expected[(packet.stream_id, packet.sequence)] = packet.raw
            frames.append(builder.build_unscaled_superframe(packets))
        stream = np.concatenate(
            (
                np.zeros(5000, dtype=np.complex64),
                np.concatenate(frames[:50]),
                np.zeros(1000, dtype=np.complex64),
                np.concatenate(frames[50:]),
                np.zeros(80_000, dtype=np.complex64),
            )
        )
        received = {}

        def callback(packet, _pcm):
            received[(packet.stream_id, packet.sequence)] = packet.raw

        receiver = StreamingOfdmReceiver(fec, callback, correlation_threshold=0.20)
        for start in range(0, stream.size, RX_BUFFER_SAMPLES):
            receiver.feed(stream[start:start + RX_BUFFER_SAMPLES])
        self.assertEqual(len(received), len(expected))
        self.assertTrue(all(received[key] == value for key, value in expected.items()))
        self.assertGreaterEqual(receiver.stats.reacquisitions, 1)


if __name__ == "__main__":
    unittest.main()
