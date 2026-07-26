import unittest
from pathlib import Path

from cn0566_ofdm_core import run_software_simulation


class EndToEndTests(unittest.TestCase):
    def test_four_stream_exact_packets(self):
        root = Path(__file__).resolve().parents[1]
        paths = [root / f"sample_{index}.wav" for index in range(1, 5)]
        result = run_software_simulation(paths, frames=4, snr_db=24.0, cfo_hz=180.0)
        self.assertEqual(result["matching_packets"], result["expected_packets"])
        self.assertEqual(result["expected_packets"], 16)


if __name__ == "__main__":
    unittest.main()
