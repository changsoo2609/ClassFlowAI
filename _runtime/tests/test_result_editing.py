import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import ClassFlowAIApp
from modules.flow_document import (
    apply_analysis_edit,
    build_flow_document,
    editable_analysis_text,
    effective_analysis_text,
    effective_section_title,
    is_analysis_edited,
    restore_analysis_original,
)


class ResultEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cap_edit_is_separate_and_restore_reveals_original(self):
        record = {
            "record_id": "cap-1",
            "mode": "capture",
            "cap_text": "## 원본\n마묶으로 구성됩니다.",
        }

        apply_analysis_edit(
            record,
            "## 원본\n옥텟 네 개로 구성됩니다.",
            edited_at="2026-07-27 15:00:00",
        )

        self.assertEqual(record["cap_text"], "## 원본\n마묶으로 구성됩니다.")
        self.assertEqual(
            effective_analysis_text(record),
            "## 원본\n옥텟 네 개로 구성됩니다.",
        )
        self.assertTrue(is_analysis_edited(record))
        self.assertTrue(restore_analysis_original(record))
        self.assertEqual(
            effective_analysis_text(record),
            "## 원본\n마묶으로 구성됩니다.",
        )
        self.assertFalse(is_analysis_edited(record))

    def test_ocr_flow_edit_keeps_original_title_and_body(self):
        record = {
            "record_id": "ocr-1",
            "mode": "ocr",
            "flow_title": "IPv4 주소 구조",
            "flow_interpretation_text": "마묶으로 구성됩니다.",
            "ocr_interpretation_text": "마묶으로 구성됩니다.",
            "flow_interpretation_status": "done",
        }

        apply_analysis_edit(
            record,
            "## IPv4 주소의 구조와 특징\n8비트짜리 옥텟 네 개로 구성됩니다.",
            edited_at="2026-07-27 15:01:00",
        )

        self.assertEqual(record["flow_title"], "IPv4 주소 구조")
        self.assertEqual(record["flow_interpretation_text"], "마묶으로 구성됩니다.")
        self.assertEqual(effective_section_title(record), "IPv4 주소의 구조와 특징")
        self.assertEqual(effective_analysis_text(record), "8비트짜리 옥텟 네 개로 구성됩니다.")
        self.assertEqual(
            editable_analysis_text(record),
            "## IPv4 주소의 구조와 특징\n8비트짜리 옥텟 네 개로 구성됩니다.",
        )

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

    def test_edit_persists_and_cap_copy_uses_edited_text(self):
        record = {
            "record_id": "cap-1",
            "mode": "capture",
            "cap_text": "원본 오타",
        }
        app = self.make_app(record)

        self.assertTrue(app.save_record_analysis_edit(record, "수정한 설명"))
        saved = json.loads(app.paths["records"].read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["cap_text"], "원본 오타")
        self.assertEqual(saved[0]["cap_text_edited"], "수정한 설명")

        app.copy_text_to_clipboard = Mock(return_value=True)
        app.copy_current_cap_result()
        app.copy_text_to_clipboard.assert_called_once_with("수정한 설명")

    def test_save_failure_restores_record_and_reports_error(self):
        record = {
            "record_id": "cap-1",
            "mode": "capture",
            "cap_text": "원본 유지",
        }
        app = self.make_app(record)
        app.save_records = Mock(side_effect=OSError("disk unavailable"))
        before = copy.deepcopy(record)

        with patch("app.messagebox.showerror") as showerror:
            self.assertFalse(app.save_record_analysis_edit(record, "저장되면 안 됨"))

        self.assertEqual(record, before)
        showerror.assert_called_once()

    def test_restore_original_persists_without_changing_capture_identity(self):
        record = {
            "record_id": "cap-restore",
            "mode": "capture",
            "captured_at": "2026-07-27 14:00:00",
            "image_path": str(self.root / "original.png"),
            "display_order": 4,
            "cap_text": "모델 원본",
            "cap_text_edited": "사용자 수정",
            "analysis_edited_at": "2026-07-27 15:00:00",
        }
        immutable = {
            key: record[key]
            for key in ("record_id", "captured_at", "image_path", "display_order")
        }
        app = self.make_app(record)

        self.assertTrue(app.restore_record_analysis_edit(record))
        saved = json.loads(app.paths["records"].read_text(encoding="utf-8"))[0]

        self.assertEqual(effective_analysis_text(record), "모델 원본")
        self.assertNotIn("cap_text_edited", saved)
        self.assertEqual({key: record[key] for key in immutable}, immutable)

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
