import unittest
import numpy as np

from cn0566_ofdm_core import (
    AUDIO_FRAME_SAMPLES,
    build_audio_packet,
    ima_adpcm_decode_block,
    parse_audio_packet,
)


class PacketAudioTests(unittest.TestCase):
    def test_packet_crc_and_audio(self):
        t = np.arange(AUDIO_FRAME_SAMPLES) / 16000.0
        pcm = np.rint(0.6 * np.sin(2 * np.pi * 880 * t) * 32767).astype(np.int16)
        packet = build_audio_packet(2, 123, pcm, AUDIO_FRAME_SAMPLES, 0)
        parsed = parse_audio_packet(packet.raw)
        self.assertEqual(parsed.stream_id, 2)
        self.assertEqual(parsed.sequence, 123)
        decoded = ima_adpcm_decode_block(parsed.payload, parsed.valid_samples)
        error = np.sqrt(np.mean((decoded.astype(np.float64) - pcm.astype(np.float64)) ** 2))
        self.assertLess(error, 8000.0)

    def test_crc_rejects_corruption(self):
        pcm = np.zeros(AUDIO_FRAME_SAMPLES, dtype=np.int16)
        packet = bytearray(build_audio_packet(0, 0, pcm, AUDIO_FRAME_SAMPLES, 0).raw)
        packet[20] ^= 0x40
        with self.assertRaises(ValueError):
            parse_audio_packet(bytes(packet))


if __name__ == "__main__":
    unittest.main()
