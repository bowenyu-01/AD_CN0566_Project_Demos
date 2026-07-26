import unittest
import numpy as np
import cn0566_qpsk_audio as q

class FecTests(unittest.TestCase):
    def test_native_or_numpy_roundtrip(self):
        rng = np.random.default_rng(3)
        bits = rng.integers(0, 2, 500, dtype=np.uint8)
        coded = q.convolutional_encode(bits)
        symbols = 1.0 - 2.0*coded.astype(np.float32)
        decoded = q.ViterbiDecoder().decode(symbols)
        np.testing.assert_array_equal(decoded[:bits.size], bits)
        np.testing.assert_array_equal(decoded[bits.size:], np.zeros(q.CONV_TAIL_BITS, dtype=np.uint8))

if __name__ == '__main__':
    unittest.main()
