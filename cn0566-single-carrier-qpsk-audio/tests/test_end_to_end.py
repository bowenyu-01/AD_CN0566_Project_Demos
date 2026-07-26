import argparse
import unittest
import cn0566_qpsk_audio as q

class EndToEndTests(unittest.TestCase):
    def test_simulation(self):
        args = argparse.Namespace(
            wav='sample_test.wav', simulation_frames=6,
            correlation_threshold=q.CORRELATION_THRESHOLD_DEFAULT,
        )
        self.assertEqual(q.run_simulation(args), 0)

if __name__ == '__main__':
    unittest.main()
