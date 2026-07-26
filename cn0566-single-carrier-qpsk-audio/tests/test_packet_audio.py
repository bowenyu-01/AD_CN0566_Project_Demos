import unittest
import numpy as np
import cn0566_qpsk_audio as q

class PacketAudioTests(unittest.TestCase):
    def test_adpcm_geometry(self):
        pcm = q.generate_test_pcm(0.020)[:q.AUDIO_FRAME_SAMPLES]
        payload = q.ima_adpcm_encode_block(pcm)
        self.assertEqual(len(payload), q.PAYLOAD_BYTES)
        decoded = q.ima_adpcm_decode_block(payload)
        self.assertEqual(decoded.shape, pcm.shape)
        self.assertEqual(decoded.dtype, np.int16)
        self.assertLess(float(np.mean(np.abs(decoded.astype(np.float64)-pcm.astype(np.float64)))), 4000)

    def test_packet_crc(self):
        pcm = np.arange(q.AUDIO_FRAME_SAMPLES, dtype=np.int16)
        packet = q.build_packet(513, pcm, 319, q.FLAG_START_OF_FILE)
        seq, valid, flags, payload = q.parse_packet(packet.raw)
        self.assertEqual((seq, valid, flags), (513, 319, q.FLAG_START_OF_FILE))
        self.assertEqual(payload, packet.payload)
        corrupted = bytearray(packet.raw)
        corrupted[20] ^= 0x01
        with self.assertRaises(ValueError):
            q.parse_packet(bytes(corrupted))

if __name__ == '__main__':
    unittest.main()
