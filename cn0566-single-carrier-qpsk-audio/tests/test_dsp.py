import math
import unittest
import numpy as np
import cn0566_qpsk_audio as q

class DspTests(unittest.TestCase):
    def test_qpsk_mapping(self):
        bits = np.array([0,0, 0,1, 1,0, 1,1], dtype=np.uint8)
        symbols = q.qpsk_map(bits)
        expected = np.array([1+1j, 1-1j, -1+1j, -1-1j], dtype=np.complex64)/math.sqrt(2)
        np.testing.assert_allclose(symbols, expected, atol=1e-6)
        np.testing.assert_allclose(np.abs(symbols), 1.0, atol=1e-6)

    def test_rrc_geometry_and_energy(self):
        self.assertEqual(len(q.RRC_TAPS), 41)
        self.assertEqual(q.RRC_GROUP_DELAY, 20)
        self.assertAlmostEqual(float(np.sum(q.RRC_TAPS**2)), 1.0, places=5)

    def test_frame_geometry(self):
        self.assertEqual(q.FRAME_PERIOD_SAMPLES, 20000)
        self.assertEqual(q.TOTAL_FRAME_SYMBOLS, 4862)
        pcm = q.generate_test_pcm(0.020)[:q.AUDIO_FRAME_SAMPLES]
        packet = q.build_packet(1, pcm, q.AUDIO_FRAME_SAMPLES, 0)
        frame = q.packet_to_radio_frame(packet)
        self.assertEqual(frame.shape, (20000,))
        self.assertEqual(frame.dtype, np.complex64)
        self.assertTrue(np.all(frame[:q.LEADING_GUARD_SAMPLES] == 0))

if __name__ == '__main__':
    unittest.main()
