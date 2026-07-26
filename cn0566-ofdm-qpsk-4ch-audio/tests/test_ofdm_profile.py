import unittest
import numpy as np

from cn0566_ofdm_core import (
    CHANNEL_DATA_BINS,
    DATA_BINS,
    DATA_OFDM_SYMBOLS,
    NET_RATE_HALF_FEC_BPS,
    OCCUPIED_BANDWIDTH_HZ,
    PROFILE_NAME,
    RAW_DATA_CARRIER_BPS,
    SAMPLE_RATE,
    SUPERFRAME_SAMPLES,
)


class ProfileTests(unittest.TestCase):
    def test_capacity_and_allocation(self):
        expected = {
            "stable_1m": (1_000_000, 20_000, 67, 1_333_333.3333333333, 643_200.0, 850_000),
            "balanced_1p5m": (1_500_000, 30_000, 102, 2_000_000.0, 979_200.0, 1_270_000),
            "high_2m": (2_000_000, 40_000, 136, 2_666_666.6666666665, 1_305_600.0, 1_700_000),
        }
        fs, frame, data_symbols, raw, net, occupied_limit = expected[PROFILE_NAME]
        self.assertEqual(SAMPLE_RATE, fs)
        self.assertEqual(SUPERFRAME_SAMPLES, frame)
        self.assertEqual(DATA_OFDM_SYMBOLS, data_symbols)
        self.assertEqual(DATA_BINS.size, 192)
        combined = np.concatenate(CHANNEL_DATA_BINS)
        self.assertEqual(np.unique(combined).size, 192)
        self.assertTrue(all(group.size == 48 for group in CHANNEL_DATA_BINS))
        self.assertAlmostEqual(RAW_DATA_CARRIER_BPS, raw, places=3)
        self.assertAlmostEqual(NET_RATE_HALF_FEC_BPS, net, places=3)
        self.assertLess(OCCUPIED_BANDWIDTH_HZ, occupied_limit)
        # Four 179-byte packets every 20 ms require 286.4 kbit/s before FEC.
        self.assertGreater(NET_RATE_HALF_FEC_BPS, 286_400.0)


if __name__ == "__main__":
    unittest.main()
