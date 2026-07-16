import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from app import ClassFlowAIApp, listbox_index_at_y
from modules.clipboard_watcher import open_clipboard_with_retry


class _Root:
    def winfo_id(self):
        return 123


class _Menu:
    def __init__(self, *_args, **_kwargs):
        self.items = []
        self.popup_at = None

    def add_command(self, label, command):
        self.items.append((label, command))

    def tk_popup(self, x, y):
        self.popup_at = (x, y)

    def grab_release(self):
        pass


class _Listbox:
    def __init__(self, size=1, bbox=(0, 0, 200, 20)):
        self._size = size
        self._bbox = bbox
        self.selected = []

    def size(self):
        return self._size

    def nearest(self, _y):
        return 0

    def bbox(self, _index):
        return self._bbox

    def selection_clear(self, *_args):
        self.selected = []

    def selection_set(self, index):
        self.selected = [index]

    def activate(self, _index):
        pass


class CaptureImageContextMenuTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.image_path = Path(self.temp_dir.name) / "original.png"
        Image.new("RGB", (64, 48), "purple").save(self.image_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_app(self, mode="capture"):
        app = ClassFlowAIApp.__new__(ClassFlowAIApp)
        app.root = _Root()
        app.last_hash = None
        app.last_clipboard_sequence = None
        app.internal_clipboard_write_lock = threading.Lock()
        app.internal_clipboard_write_active = False
        app.ignored_clipboard_sequences = set()
        app.paths = {"events": Path(self.temp_dir.name) / "events.jsonl"}
        app.set_status = Mock()
        record = {
            "record_id": "original",
            "image_path": str(self.image_path),
            "mode": mode,
            "display_order": 4,
            "ocr_text": "kept ocr",
            "cap_text": "kept cap",
        }
        app.capture_records = [record]
        app.current_record_index = 0
        return app, record

    def test_original_path_is_used_for_ocr_and_cap_without_record_changes(self):
        for mode in ("ocr", "capture"):
            with self.subTest(mode=mode):
                app, record = self.make_app(mode)
                before = copy.deepcopy(record)
                app.current_preview = object()
                app.copy_internal_image_to_clipboard = Mock()
                with patch("app.copy_pil_image_to_clipboard") as copy_image:
                    self.assertTrue(app.copy_record_original_image(record))
                copy_image.assert_not_called()
                app.copy_internal_image_to_clipboard.assert_called_once()
                copied = app.copy_internal_image_to_clipboard.call_args.args[0]
                self.assertEqual(copied.size, (64, 48))
                self.assertEqual(record, before)

    def test_missing_original_is_safe(self):
        app, record = self.make_app("ocr")
        record["image_path"] = str(Path(self.temp_dir.name) / "missing.png")
        app.copy_internal_image_to_clipboard = Mock()
        with patch("app.messagebox.showwarning") as warning:
            self.assertFalse(app.copy_record_original_image(record))
        app.copy_internal_image_to_clipboard.assert_not_called()
        warning.assert_called_once()

    def test_damaged_original_is_safe(self):
        app, record = self.make_app("capture")
        damaged = Path(self.temp_dir.name) / "damaged.png"
        damaged.write_bytes(b"not an image")
        record["image_path"] = str(damaged)
        app.copy_internal_image_to_clipboard = Mock()
        with patch("app.messagebox.showerror") as error:
            self.assertFalse(app.copy_record_original_image(record))
        app.copy_internal_image_to_clipboard.assert_not_called()
        error.assert_called_once()

    def test_existing_cap_copy_action_reuses_common_copy(self):
        app, record = self.make_app("capture")
        app.copy_record_original_image = Mock(return_value=True)
        app.get_current_record = Mock(return_value=record)
        app.copy_current_cap_image()
        app.copy_record_original_image.assert_called_once_with(record)

    def test_right_click_selects_clicked_record_and_menu_uses_it(self):
        app, record = self.make_app("ocr")
        app.capture_listbox = _Listbox()
        app.capture_list_record_indices = [0]
        app.refresh_current_preview = Mock()
        event = SimpleNamespace(y=10, x_root=30, y_root=40)
        with patch("app.tk.Menu", _Menu):
            self.assertEqual(app.show_capture_list_context_menu(event), "break")
        self.assertEqual(app.current_record_index, 0)
        self.assertEqual(app.capture_context_menu.items[0][0], "원본 이미지 복사")
        with patch.object(app, "copy_record_original_image") as copy_original:
            app.capture_context_menu.items[0][1]()
        copy_original.assert_called_once_with(record)

    def test_empty_list_area_does_not_open_menu_or_copy(self):
        app, _record = self.make_app("capture")
        app.capture_listbox = _Listbox(bbox=(0, 0, 200, 20))
        app.capture_list_record_indices = [0]
        app.copy_record_original_image = Mock()
        event = SimpleNamespace(y=80, x_root=30, y_root=40)
        with patch("app.tk.Menu", _Menu) as menu:
            self.assertEqual(app.show_capture_list_context_menu(event), "break")
        app.copy_record_original_image.assert_not_called()
        self.assertFalse(hasattr(app, "capture_context_menu"))
        self.assertIsNone(listbox_index_at_y(app.capture_listbox, 80))

    def test_clipboard_lock_retries_then_succeeds(self):
        opener = Mock(side_effect=[False, False, True])
        sleep = Mock()
        self.assertTrue(open_clipboard_with_retry(opener, 123, max_attempts=5, retry_delay=0.01, sleep=sleep))
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_clipboard_retry_limit_returns_safe_failure(self):
        opener = Mock(return_value=False)
        sleep = Mock()
        self.assertFalse(open_clipboard_with_retry(opener, 123, max_attempts=3, retry_delay=0.01, sleep=sleep))
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
