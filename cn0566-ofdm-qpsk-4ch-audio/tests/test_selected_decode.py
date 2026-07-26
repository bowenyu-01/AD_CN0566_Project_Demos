import unittest
from pathlib import Path

import numpy as np

from cn0566_ofdm_core import (
    FecCodec,
    FourAudioSource,
    OfdmFrameBuilder,
    RX_BUFFER_SAMPLES,
    StreamingOfdmReceiver,
    SUPERFRAME_SAMPLES,
    load_wav_as_pcm16,
)


class SelectedDecodeTests(unittest.TestCase):
    def test_only_selected_logical_channel_is_decoded(self):
        root = Path(__file__).resolve().parents[1]
        pcm = [load_wav_as_pcm16(root / f"sample_{index}.wav") for index in range(1, 5)]
        codec = FecCodec()
        source = FourAudioSource(pcm, repeat=True)
        builder = OfdmFrameBuilder(codec)
        frames = []
        for _ in range(6):
            frames.append(builder.build_unscaled_superframe(source.next_packets()))
        stream = np.concatenate(
            (
                np.zeros(7777, dtype=np.complex64),
                np.concatenate(frames),
                np.zeros(2 * SUPERFRAME_SAMPLES, dtype=np.complex64),
            )
        )
        selected = {0}
        received = []
        receiver = StreamingOfdmReceiver(
            codec,
            lambda packet, _pcm: received.append((packet.stream_id, packet.sequence)),
            correlation_threshold=0.20,
            channel_selector=lambda: set(selected),
        )
        for start in range(0, stream.size, RX_BUFFER_SAMPLES):
            receiver.feed(stream[start : start + RX_BUFFER_SAMPLES])
        self.assertEqual(len(received), 6)
        self.assertEqual({stream_id for stream_id, _sequence in received}, {0})
        self.assertEqual(receiver.stats.packet_valid[0], 6)
        self.assertEqual(receiver.stats.packet_valid[1:], [0, 0, 0])


if __name__ == "__main__":
    unittest.main()
