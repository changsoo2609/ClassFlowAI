import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from PIL import Image

from app import ClassFlowAIApp
from modules.capture_order import (
    active_ordered_records,
    move_record,
    normalize_display_orders,
    restore_capture_order,
    restore_capture_order_if_confirmed,
)
from modules.flow_document import build_flow_document
from modules.storage import write_json_atomic


class CaptureOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.images = []
        for name, color in (("late.png", "red"), ("early.png", "blue"), ("middle.png", "green")):
            path = self.root / name
            Image.new("RGB", (8, 8), color).save(path)
            self.images.append(path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def legacy_records(self):
        return [
            {"record_id": "late", "image_path": str(self.images[0]), "created_at": "2026-01-01 10:03:00"},
            {"record_id": "early", "image_path": str(self.images[1]), "created_at": "2026-01-01 10:01:00"},
            {"record_id": "middle", "image_path": str(self.images[2]), "created_at": "2026-01-01 10:02:00"},
        ]

    def test_legacy_records_without_display_order_use_capture_time(self):
        records = self.legacy_records()
        self.assertEqual(
            [record["record_id"] for record in active_ordered_records(records)],
            ["early", "middle", "late"],
        )

    def test_move_up_down_and_boundaries(self):
        records = self.legacy_records()
        ordered = active_ordered_records(records)
        self.assertFalse(move_record(records, ordered[0], -1))
        self.assertFalse(move_record(records, ordered[-1], 1))
        self.assertTrue(move_record(records, ordered[1], -1))
        self.assertEqual([r["record_id"] for r in active_ordered_records(records)], ["middle", "early", "late"])
        self.assertTrue(move_record(records, records[2], 1))
        self.assertEqual([r["record_id"] for r in active_ordered_records(records)], ["early", "middle", "late"])

    def test_save_and_reload_keeps_display_order(self):
        records_path = self.root / "state" / "capture_records.json"
        app = ClassFlowAIApp.__new__(ClassFlowAIApp)
        app.paths = {"records": records_path}
        app.capture_records = self.legacy_records()
        app.current_record_index = -1
        move_record(app.capture_records, active_ordered_records(app.capture_records)[-1], -1)
        app.save_records()

        reloaded = ClassFlowAIApp.__new__(ClassFlowAIApp)
        reloaded.paths = {"records": records_path}
        reloaded.capture_records = []
        reloaded.current_record_index = -1
        reloaded.load_records()
        self.assertEqual([r["record_id"] for r in active_ordered_records(reloaded.capture_records)], ["early", "late", "middle"])
        self.assertEqual(json.loads(records_path.read_text(encoding="utf-8")), reloaded.capture_records)

    def test_restore_capture_order_and_cancel(self):
        records = self.legacy_records()
        normalize_display_orders(records)
        move_record(records, active_ordered_records(records)[-1], -1)
        before_cancel = copy.deepcopy(records)
        self.assertFalse(restore_capture_order_if_confirmed(records, lambda: False))
        self.assertEqual(records, before_cancel)
        self.assertTrue(restore_capture_order(records))
        self.assertEqual([r["record_id"] for r in active_ordered_records(records)], ["early", "middle", "late"])

    def test_new_capture_is_added_last(self):
        app = ClassFlowAIApp.__new__(ClassFlowAIApp)
        app.capture_records = self.legacy_records()
        normalize_display_orders(app.capture_records)
        app.capture_mode = "capture"
        app.current_record_index = -1
        app.save_records = Mock()
        new_path = self.root / "new.png"
        Image.new("RGB", (8, 8), "yellow").save(new_path)
        new_record = app.add_capture_record(new_path)
        self.assertIs(active_ordered_records(app.capture_records)[-1], new_record)

    def test_flow_document_follows_display_order(self):
        records = self.legacy_records()
        normalize_display_orders(records)
        records[0]["display_order"] = 0
        records[1]["display_order"] = 2
        records[2]["display_order"] = 1
        normalize_display_orders(records)
        expected = ["late", "middle", "early"]

        document = build_flow_document(records)
        capture_ids = [section["items"][0]["captureId"] for section in document["sections"]]
        self.assertEqual(capture_ids, expected)
        self.assertEqual([r["record_id"] for r in active_ordered_records(records)], expected)

    def test_reorder_preserves_original_identity_and_capture_metadata(self):
        records = self.legacy_records()
        before = [
            (id(record), record["record_id"], record["image_path"], record["created_at"])
            for record in records
        ]
        move_record(records, active_ordered_records(records)[-1], -1)
        after = [
            (id(record), record["record_id"], record["image_path"], record["created_at"])
            for record in records
        ]
        self.assertEqual(after, before)

    def test_duplicate_and_invalid_orders_are_normalized_safely(self):
        records = self.legacy_records()
        records[0]["display_order"] = 1
        records[1]["display_order"] = 1
        records[2]["display_order"] = "broken"
        normalize_display_orders(records)
        orders = [record["display_order"] for record in records]
        self.assertEqual(sorted(orders), [0, 1, 2])
        self.assertEqual(len(set(orders)), 3)

    def test_atomic_save_failure_preserves_existing_records(self):
        records_path = self.root / "state" / "capture_records.json"
        write_json_atomic(records_path, [{"record_id": "original"}])
        with patch("modules.storage.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                write_json_atomic(records_path, [{"record_id": "changed"}])
        self.assertEqual(
            json.loads(records_path.read_text(encoding="utf-8")),
            [{"record_id": "original"}],
        )


if __name__ == "__main__":
    unittest.main()
