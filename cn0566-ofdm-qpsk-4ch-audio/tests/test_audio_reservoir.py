import threading
import unittest

import numpy as np

from cn0566_ofdm_core import AUDIO_FRAME_SAMPLES, RuntimeStatistics, SelectableAudioMixer


class AudioReservoirTests(unittest.TestCase):
    def make_mixer(self):
        return SelectableAudioMixer(
            enabled=False,
            stop_event=threading.Event(),
            selected_channels={0},
            startup_buffer_ms=100,  # five 20 ms frames
            runtime_stats=RuntimeStatistics(),
        )

    def test_start_requires_contiguous_sequences(self):
        mixer = self.make_mixer()
        audio = np.zeros(AUDIO_FRAME_SAMPLES, dtype=np.int16)
        for sequence in (0, 2, 3, 4, 5):
            mixer._pending[0][sequence] = audio.copy()
        self.assertIsNone(mixer._choose_start_sequence())
        mixer._pending[0][1] = audio.copy()
        self.assertEqual(mixer._choose_start_sequence(), 0)

    def test_missing_sequence_waits_until_future_packet_proves_gap(self):
        mixer = self.make_mixer()
        audio = np.zeros(AUDIO_FRAME_SAMPLES, dtype=np.int16)
        self.assertFalse(mixer._sequence_can_render(10))
        mixer._pending[0][11] = audio
        self.assertTrue(mixer._sequence_can_render(10))


if __name__ == "__main__":
    unittest.main()
