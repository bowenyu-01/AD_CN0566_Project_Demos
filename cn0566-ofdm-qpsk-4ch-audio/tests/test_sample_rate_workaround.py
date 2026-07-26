import unittest

from cn0566_ofdm_core import SAMPLE_RATE, set_ad9361_sample_rate_robust


class FakeAd9361:
    def __init__(self):
        self._sample_rate = 1_999_999
        self._fir = 1
        self._get_iio_attr = self._getter

    def _getter(self, channel, attribute, output):
        if attribute == "sampling_frequency":
            return [self._sample_rate, -32]
        if attribute == "voltage_filter_fir_en":
            return [self._fir, -32]
        return 0

    def _set_iio_attr(self, channel, attribute, output, value):
        if attribute == "sampling_frequency":
            self._sample_rate = int(value)
        elif attribute == "voltage_filter_fir_en":
            self._fir = int(value)

    @property
    def sample_rate(self):
        return [self._sample_rate, -32]

    @sample_rate.setter
    def sample_rate(self, value):
        current = self._get_iio_attr("voltage0", "sampling_frequency", False)
        fir = self._get_iio_attr("out", "voltage_filter_fir_en", False)
        if fir and current <= (25_000_000 // 12):
            self._set_iio_attr("voltage0", "sampling_frequency", False, 3_000_000)
        self._set_iio_attr("out", "voltage_filter_fir_en", False, 1)
        self._set_iio_attr("voltage0", "sampling_frequency", False, int(value) - 1)


class SampleRateWorkaroundTests(unittest.TestCase):
    def test_list_readback_is_scalarized(self):
        fake = FakeAd9361()
        actual = set_ad9361_sample_rate_robust(fake, SAMPLE_RATE)
        self.assertEqual(actual, SAMPLE_RATE - 1)


if __name__ == "__main__":
    unittest.main()
