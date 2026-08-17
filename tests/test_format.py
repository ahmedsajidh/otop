import unittest

from otop import format as fmt


class HumanTest(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(fmt.human_bytes(None), "N/A")
        self.assertEqual(fmt.human_bytes(0), "0 B")
        self.assertEqual(fmt.human_bytes(512), "512 B")
        self.assertEqual(fmt.human_bytes(1024), "1.0 KB")
        self.assertEqual(fmt.human_bytes(1536), "1.5 KB")
        self.assertEqual(fmt.human_bytes(1024 ** 3 * 1.5), "1.5 GB")
        self.assertEqual(fmt.human_bytes(1024 ** 4), "1.0 TB")
        self.assertEqual(fmt.human_bytes("nonsense"), "N/A")

    def test_rate_and_percent(self):
        self.assertEqual(fmt.human_rate(None), "N/A")
        self.assertTrue(fmt.human_rate(2048).endswith("/s"))
        self.assertEqual(fmt.human_percent(None), "N/A")
        self.assertEqual(fmt.human_percent(12.4), "12%")
        self.assertEqual(fmt.human_percent(12.44, digits=1), "12.4%")

    def test_seconds(self):
        self.assertEqual(fmt.human_seconds(None), "N/A")
        self.assertEqual(fmt.human_seconds(3.2), "3.2s")
        self.assertEqual(fmt.human_seconds(42), "42s")
        self.assertEqual(fmt.human_seconds(125), "2m05s")
        self.assertEqual(fmt.human_seconds(3725), "1h02m")
        self.assertEqual(fmt.human_seconds(90000), "1d01h")


class GaugeTest(unittest.TestCase):
    def test_bar_width_and_fill(self):
        self.assertEqual(len(fmt.bar(50, 12)), 12)
        self.assertEqual(fmt.bar(0, 12), "[" + " " * 10 + "]")
        self.assertEqual(fmt.bar(100, 12), "[" + "|" * 10 + "]")
        self.assertEqual(fmt.bar(None, 12).count("?"), 1)
        self.assertEqual(fmt.bar(50, 2), "")

    def test_bar_clamps_out_of_range(self):
        self.assertEqual(fmt.bar(500, 10), fmt.bar(100, 10))
        self.assertEqual(fmt.bar(-20, 10), fmt.bar(0, 10))

    def test_level_thresholds(self):
        self.assertEqual(fmt.level(None), "dim")
        self.assertEqual(fmt.level(10), "good")
        self.assertEqual(fmt.level(80), "warn")
        self.assertEqual(fmt.level(95), "crit")

    def test_fit(self):
        self.assertEqual(fmt.fit("abcdef", 10), "abcdef")
        self.assertEqual(fmt.fit("abcdef", 4), "abc~")
        self.assertEqual(fmt.fit("abcdef", 0), "")


if __name__ == "__main__":
    unittest.main()
