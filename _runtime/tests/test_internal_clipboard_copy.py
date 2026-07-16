import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from app import ClassFlowAIApp
from modules.clipboard_watcher import image_hash


class InternalClipboardCopyTests(unittest.TestCase):
    def make_app(self):
        app = ClassFlowAIApp.__new__(ClassFlowAIApp)
        app.root = Mock()
        app.root.winfo_id.return_value = 321
        app.internal_clipboard_write_lock = threading.Lock()
        app.internal_clipboard_write_active = False
        app.ignored_clipboard_sequences = set()
        app.last_clipboard_sequence = 40
        app.last_hash = "before"
        app.capture_records = [{"record_id": "existing"}]
        return app

    @patch("app.get_clipboard_sequence_number", return_value=41)
    @patch("app.copy_pil_image_to_clipboard")
    def test_internal_image_is_written_once_and_sequence_hash_are_ignored(self, copy_image, _sequence):
        app = self.make_app()
        image = Image.new("RGB", (80, 40), "red")
        before_count = len(app.capture_records)
        app.handle_new_clipboard_image = Mock()
        app.run_ocr_for_record_async = Mock()
        app.run_cap_reasoning_for_record_async = Mock()
        app.start_flow_interpretation_background = Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            captures = Path(temp_dir) / "captures"
            captures.mkdir()
            app.paths = {"captures": captures}
            app.copy_internal_image_to_clipboard(image)
            self.assertEqual(list(captures.iterdir()), [])
        copy_image.assert_called_once()
        self.assertEqual(app.ignored_clipboard_sequences, {41})
        self.assertEqual(app.last_clipboard_sequence, 41)
        self.assertEqual(app.last_hash, image_hash(image))
        self.assertFalse(app.internal_clipboard_write_active)
        self.assertEqual(len(app.capture_records), before_count)
        app.handle_new_clipboard_image.assert_not_called()
        app.run_ocr_for_record_async.assert_not_called()
        app.run_cap_reasoning_for_record_async.assert_not_called()
        app.start_flow_interpretation_background.assert_not_called()

    @patch("app.copy_pil_image_to_clipboard", side_effect=OSError("busy"))
    def test_internal_copy_failure_always_releases_active_state(self, _copy_image):
        app = self.make_app()
        with self.assertRaises(OSError):
            app.copy_internal_image_to_clipboard(Image.new("RGB", (20, 10)))
        self.assertFalse(app.internal_clipboard_write_active)
        self.assertEqual(app.ignored_clipboard_sequences, set())
        self.assertEqual(app.last_clipboard_sequence, 40)
        self.assertEqual(app.last_hash, "before")

    @patch("app.time.sleep")
    @patch("app.get_clipboard_sequence_number", return_value=41)
    def test_watch_loop_discards_exact_internal_sequence_without_capture(self, _sequence, sleep):
        app = self.make_app()
        app.running = True
        app.paused = False
        app.config = {"poll_interval_sec": 0}
        app.paths = {"events": Path(tempfile.gettempdir()) / "unused-events.jsonl"}
        app.ignored_clipboard_sequences = {41}
        app.handle_new_clipboard_image = Mock()
        sleep.side_effect = lambda *_args: setattr(app, "running", False)
        app.clipboard_watch_loop()
        app.handle_new_clipboard_image.assert_not_called()
        self.assertEqual(app.ignored_clipboard_sequences, set())

    @patch("app.time.sleep")
    @patch("app.get_clipboard_sequence_number", return_value=42)
    @patch("app.get_clipboard_image")
    def test_external_image_immediately_after_internal_copy_is_detected(self, get_image, _sequence, sleep):
        app = self.make_app()
        app.running = True
        app.paused = False
        app.config = {"poll_interval_sec": 0}
        app.paths = {"events": Path(tempfile.gettempdir()) / "unused-events.jsonl"}
        app.last_clipboard_sequence = 41
        app.ignored_clipboard_sequences = {41}
        external = Image.new("RGB", (33, 22), "blue")
        get_image.return_value = external
        app.handle_new_clipboard_image = Mock(side_effect=lambda _image: setattr(app, "running", False))
        sleep.side_effect = lambda *_args: None
        app.clipboard_watch_loop()
        app.handle_new_clipboard_image.assert_called_once_with(external)
        self.assertEqual(app.last_hash, image_hash(external))

    @patch("app.time.sleep")
    @patch("app.get_clipboard_sequence_number", side_effect=[50, 55, 55])
    @patch("app.get_clipboard_image")
    def test_internal_copy_finishing_while_watcher_reads_is_not_captured(self, get_image, _sequence, sleep):
        app = self.make_app()
        app.running = True
        app.paused = False
        app.config = {"poll_interval_sec": 0}
        app.paths = {"events": Path(tempfile.gettempdir()) / "unused-events.jsonl"}
        app.last_clipboard_sequence = 49
        internal = Image.new("RGB", (44, 22), "green")

        def finish_internal_copy():
            with app.internal_clipboard_write_lock:
                app.internal_clipboard_write_active = True
                app.last_hash = image_hash(internal)
                app.ignored_clipboard_sequences.add(55)
            return internal

        get_image.side_effect = finish_internal_copy
        app.handle_new_clipboard_image = Mock()
        sleep.side_effect = lambda *_args: setattr(app, "running", False)
        app.clipboard_watch_loop()
        app.handle_new_clipboard_image.assert_not_called()

if __name__ == "__main__":
    unittest.main()
