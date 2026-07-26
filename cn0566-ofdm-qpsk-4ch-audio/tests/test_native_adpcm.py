import unittest
import numpy as np

import cn0566_ofdm_core as modem


class NativeAdpcmTests(unittest.TestCase):
    def test_native_matches_python_reference(self):
        fec = modem.FecCodec()
        self.assertEqual(fec.audio_backend, "native C")
        native_library = modem._NATIVE_ADPCM_LIBRARY
        rng = np.random.default_rng(0xAD0C)
        for _ in range(25):
            pcm = rng.integers(
                -32768,
                32768,
                size=modem.AUDIO_FRAME_SAMPLES,
                dtype=np.int16,
            )
            modem._NATIVE_ADPCM_LIBRARY = None
            expected_payload = modem.ima_adpcm_encode_block(pcm)
            expected_pcm = modem.ima_adpcm_decode_block(
                expected_payload,
                modem.AUDIO_FRAME_SAMPLES,
            )
            modem._NATIVE_ADPCM_LIBRARY = native_library
            actual_payload = modem.ima_adpcm_encode_block(pcm)
            actual_pcm = modem.ima_adpcm_decode_block(
                actual_payload,
                modem.AUDIO_FRAME_SAMPLES,
            )
            self.assertEqual(actual_payload, expected_payload)
            np.testing.assert_array_equal(actual_pcm, expected_pcm)
        modem._NATIVE_ADPCM_LIBRARY = native_library


if __name__ == "__main__":
    unittest.main()
