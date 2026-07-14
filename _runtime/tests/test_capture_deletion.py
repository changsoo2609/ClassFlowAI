import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from app import ClassFlowAIApp
from modules.capture_deletion import CaptureDeletionError, delete_capture_files


class CaptureDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name) / "lesson"
        self.captures = self.workspace / "captures"
        self.state = self.workspace / "state"
        self.captures.mkdir(parents=True)
        self.state.mkdir()
        self.original = self.captures / "capture.png"
        self.thumbnail = self.captures / "capture_thumb.png"
        Image.new("RGB", (8, 8), "red").save(self.original)
        Image.new("RGB", (4, 4), "blue").save(self.thumbnail)

    def tearDown(self):
        self.temp_dir.cleanup()

    def record(self):
        return {
            "record_id": "capture",
            "image_path": str(self.original),
            "thumbnail_path": str(self.thumbnail),
            "ocr_text": "인라인 OCR",
            "display_order": 0,
        }

    def test_original_and_capture_owned_related_files_are_deleted(self):
        record = self.record()
        result = delete_capture_files(self.workspace, record, [record])
        self.assertFalse(self.original.exists())
        self.assertFalse(self.thumbnail.exists())
        self.assertEqual(len(result["deleted"]), 2)

    def test_shared_related_file_is_not_deleted(self):
        record = self.record()
        other = {
            "record_id": "other",
            "image_path": str(self.captures / "other.png"),
            "thumbnail_path": str(self.thumbnail),
        }
        Image.new("RGB", (8, 8), "green").save(other["image_path"])
        result = delete_capture_files(self.workspace, record, [record, other])
        self.assertFalse(self.original.exists())
        self.assertTrue(self.thumbnail.exists())
        self.assertEqual(result["retained_shared"], [self.thumbnail.resolve()])

    def test_external_path_is_rejected_before_any_deletion(self):
        record = self.record()
        external = Path(self.temp_dir.name) / "outside.png"
        Image.new("RGB", (3, 3), "black").save(external)
        record["temp_path"] = str(external)
        with self.assertRaises(CaptureDeletionError):
            delete_capture_files(self.workspace, record, [record])
        self.assertTrue(self.original.exists())
        self.assertTrue(external.exists())

    def test_relative_capture_path_is_resolved_inside_lesson(self):
        record = self.record()
        record["image_path"] = str(Path("captures") / self.original.name)
        record.pop("thumbnail_path")
        delete_capture_files(self.workspace, record, [record])
        self.assertFalse(self.original.exists())

    def test_original_delete_failure_keeps_file(self):
        record = self.record()
        original_unlink = Path.unlink

        def fail_original(path, *args, **kwargs):
            if path.resolve() == self.original.resolve():
                raise PermissionError("locked")
            return original_unlink(path, *args, **kwargs)

        with patch("pathlib.Path.unlink", autospec=True, side_effect=fail_original):
            with self.assertRaises(CaptureDeletionError):
                delete_capture_files(self.workspace, record, [record])
        self.assertTrue(self.original.exists())

    def test_deleting_last_capture_leaves_empty_usable_lesson(self):
        app = ClassFlowAIApp.__new__(ClassFlowAIApp)
        record = self.record()
        app.workspace = self.workspace
        app.paths = {
            "records": self.state / "capture_records.json",
            "events": self.workspace / "logs" / "events.jsonl",
        }
        app.capture_records = [record]
        app.current_record_index = 0
        app.lesson_switch_lock = threading.RLock()
        app.execution_record = None
        app.pending_capture_updates = 0
        app.rebuild_outputs_from_records = Mock()
        app.refresh_current_preview = Mock()
        app.set_status = Mock()
        with patch("app.messagebox.askyesno", return_value=True):
            app.delete_current_record()
        self.assertEqual(app.capture_records, [])
        self.assertEqual(app.current_record_index, -1)
        self.assertFalse(self.original.exists())
        self.assertEqual(json.loads(app.paths["records"].read_text(encoding="utf-8")), [])
        app.refresh_current_preview.assert_called_once()

    def test_original_failure_keeps_ui_record_and_metadata(self):
        app = ClassFlowAIApp.__new__(ClassFlowAIApp)
        record = self.record()
        app.workspace = self.workspace
        app.paths = {
            "records": self.state / "capture_records.json",
            "events": self.workspace / "logs" / "events.jsonl",
        }
        app.capture_records = [record]
        app.current_record_index = 0
        app.lesson_switch_lock = threading.RLock()
        app.execution_record = None
        app.pending_capture_updates = 0
        app.rebuild_outputs_from_records = Mock()
        app.refresh_current_preview = Mock()
        app.set_status = Mock()
        app.save_records()
        error = CaptureDeletionError("locked", self.original)
        with (
            patch("app.messagebox.askyesno", return_value=True),
            patch("app.messagebox.showerror") as showerror,
            patch("app.delete_capture_files", side_effect=error),
        ):
            app.delete_current_record()
        self.assertEqual(app.capture_records, [record])
        self.assertTrue(self.original.exists())
        self.assertEqual(len(json.loads(app.paths["records"].read_text(encoding="utf-8"))), 1)
        showerror.assert_called_once()
        app.rebuild_outputs_from_records.assert_not_called()


if __name__ == "__main__":
    unittest.main()
