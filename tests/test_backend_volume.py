import unittest

from backend import backend


class VolumeCurveTests(unittest.TestCase):
    def test_low_percentages_map_to_audible_gain(self):
        for percent, gain in ((0, 0), (7, 20), (15, 32), (50, 66), (100, 100)):
            with self.subTest(percent=percent):
                self.assertAlmostEqual(backend.volume_percent_to_gain(percent), gain, delta=1)

    def test_every_ui_percentage_survives_a_round_trip_through_mpv(self):
        for percent in range(101):
            gain = backend.volume_percent_to_gain(percent)
            self.assertEqual(backend.gain_to_volume_percent(gain), percent)

    def test_out_of_range_values_are_clamped(self):
        self.assertEqual(backend.volume_percent_to_gain(-5), 0)
        self.assertEqual(backend.volume_percent_to_gain(150), 100)
        self.assertEqual(backend.gain_to_volume_percent(130), 100)


if __name__ == "__main__":
    unittest.main()
