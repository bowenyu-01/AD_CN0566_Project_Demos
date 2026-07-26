import unittest
import numpy as np

from cn0566_ofdm_core import FecCodec, reference_conv_encode, reference_viterbi_decode


class FecTests(unittest.TestCase):
    def test_native_matches_reference(self):
        codec = FecCodec()
        rng = np.random.default_rng(1234)
        for length in (1, 17, 1432):
            bits = rng.integers(0, 2, length, dtype=np.uint8)
            expected = reference_conv_encode(bits)
            actual = codec.encode(bits)
            np.testing.assert_array_equal(actual, expected)
            soft = (1.0 - 2.0 * actual.astype(np.float32)) * 1.7
            decoded = codec.decode(soft)
            np.testing.assert_array_equal(decoded, bits)
            np.testing.assert_array_equal(reference_viterbi_decode(soft), bits)


if __name__ == "__main__":
    unittest.main()
