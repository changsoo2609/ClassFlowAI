import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import ClassFlowAIApp
from modules.flow_document import (
    build_flow_document,
)


class ResultEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_flow_document_uses_edits_without_mutating_originals(self):
        (self.root / "cap.png").write_bytes(b"cap")
        (self.root / "ocr.png").write_bytes(b"ocr")
        cap = {
            "record_id": "cap",
            "mode": "capture",
            "image_path": str(self.root / "cap.png"),
            "cap_text": "원본 CAP 오타",
            "cap_text_edited": "수정된 CAP 설명",
            "display_order": 0,
        }
        ocr = {
            "record_id": "ocr",
            "mode": "ocr",
            "image_path": str(self.root / "ocr.png"),
            "flow_title": "원본 제목",
            "flow_title_edited": "수정 제목",
            "flow_interpretation_text": "원본 흐름 오타",
            "flow_interpretation_text_edited": "수정된 흐름 설명",
            "flow_interpretation_status": "done",
            "display_order": 1,
        }
        before = copy.deepcopy([cap, ocr])

        document = build_flow_document([cap, ocr])
        rendered = json.dumps(document, ensure_ascii=False)

        self.assertIn("수정된 CAP 설명", rendered)
        self.assertIn("수정된 흐름 설명", rendered)
        self.assertIn("수정 제목", rendered)
        self.assertNotIn("원본 CAP 오타", rendered)
        self.assertNotIn("원본 흐름 오타", rendered)
        self.assertEqual([cap, ocr], before)

    def make_app(self, record):
        app = ClassFlowAIApp.__new__(ClassFlowAIApp)
        app.capture_records = [record]
        app.current_record_index = 0
        app.paths = {
            "records": self.root / "state" / "capture_records.json",
        }
        app.rebuild_outputs_from_records = Mock()
        app.refresh_current_preview = Mock()
        app.update_ocr_panel = Mock()
        app.update_result_action_buttons = Mock()
        app.set_status = Mock()
        return app

    @patch("app.append_event")
    def test_new_cap_analysis_clears_stale_edit(self, _append_event):
        record = {
            "record_id": "cap-rerun",
            "mode": "capture",
            "cap_text": "이전 모델 원본",
            "cap_text_edited": "이전 사용자 수정",
            "analysis_edited_at": "2026-07-27 15:00:00",
        }
        app = self.make_app(record)
        app.config = {"cap_reasoning_model": "test/model"}
        app.paths["events"] = self.root / "events.jsonl"
        app.stop_execution_timer = Mock(return_value=0.2)
        app.update_mini_status = Mock()
        app.update_counter = Mock()

        app._after_cap_reasoning_record(record, "새 모델 원본")

        self.assertEqual(record["cap_text"], "새 모델 원본")
        self.assertNotIn("cap_text_edited", record)
        self.assertNotIn("analysis_edited_at", record)


if __name__ == "__main__":
    unittest.main()
