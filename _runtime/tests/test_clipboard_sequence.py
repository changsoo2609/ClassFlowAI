import unittest

from modules.clipboard_watcher import clipboard_sequence_changed


class ClipboardSequenceTests(unittest.TestCase):
    def test_unchanged_windows_sequence_skips_expensive_image_read(self):
        self.assertFalse(clipboard_sequence_changed(42, 42))
        self.assertTrue(clipboard_sequence_changed(42, 43))

    def test_missing_sequence_uses_compatible_fallback(self):
        self.assertTrue(clipboard_sequence_changed(None, 43))
        self.assertTrue(clipboard_sequence_changed(43, None))


if __name__ == "__main__":
    unittest.main()
