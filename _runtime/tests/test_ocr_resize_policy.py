import unittest

from modules.ocr_engine import _ocr_resize_scale


class OcrResizePolicyTests(unittest.TestCase):
    def test_large_capture_is_not_upscaled(self):
        self.assertEqual(_ocr_resize_scale(3000, True, 2.5, 2800, 3600), 1.0)

    def test_small_capture_is_upscaled_with_factor_cap(self):
        self.assertEqual(_ocr_resize_scale(1000, True, 2.5, 2800, 3600), 2.5)

    def test_maximum_resolution_is_preserved(self):
        self.assertAlmostEqual(_ocr_resize_scale(2000, True, 2.5, 2800, 2400), 1.2)


if __name__ == "__main__":
    unittest.main()
