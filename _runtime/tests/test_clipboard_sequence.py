import unittest
from unittest.mock import Mock, patch

from PIL import Image

from modules.clipboard_watcher import (
    clipboard_sequence_changed,
    copy_pil_image_to_clipboard,
    image_to_dib_bytes,
)


class ClipboardSequenceTests(unittest.TestCase):
    def test_unchanged_windows_sequence_skips_expensive_image_read(self):
        self.assertFalse(clipboard_sequence_changed(42, 42))
        self.assertTrue(clipboard_sequence_changed(42, 43))

    def test_missing_sequence_uses_compatible_fallback(self):
        self.assertTrue(clipboard_sequence_changed(None, 43))
        self.assertTrue(clipboard_sequence_changed(43, None))

    def test_in_memory_combined_image_uses_native_dib_clipboard(self):
        image = Image.new("RGB", (120, 80), "white")
        user32 = Mock()
        user32.RegisterClipboardFormatW.return_value = 49321
        with (
            patch("modules.clipboard_watcher.ctypes.WinDLL", return_value=user32),
            patch("modules.clipboard_watcher._set_windows_clipboard_formats") as set_formats,
        ):
            copy_pil_image_to_clipboard(image, owner_hwnd=99)
        formats = set_formats.call_args.args[0]
        self.assertEqual([value[0] for value in formats], [49321, 8])
        self.assertTrue(formats[0][1].startswith(b"\x89PNG"))
        self.assertEqual(formats[1][1], image_to_dib_bytes(image))
        self.assertEqual(set_formats.call_args.kwargs["owner_hwnd"], 99)


if __name__ == "__main__":
    unittest.main()
