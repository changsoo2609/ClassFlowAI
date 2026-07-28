import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from app import ClassFlowAIApp


class AccurateOcrCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.image_path = self.workspace / "수업 화면.png"
        Image.new("RGB", (20, 12), "white").save(self.image_path)
        self.record = {
            "record_id": "ocr-1",
            "mode": "ocr",
            "image_path": str(self.image_path),
            "display_order": 0,
        }
        self.app = ClassFlowAIApp.__new__(ClassFlowAIApp)
        self.app.capture_records = [self.record]
        self.app.current_record_index = 0
        self.app.config = {
            "nvidia_api_key": "test-key",
            "copy_ocr_to_clipboard_on_done": True,
            "cap_reasoning_model": "test/model",
        }
        self.app.paths = {
            "records": self.workspace / "capture_records.json",
            "events": self.workspace / "events.jsonl",
        }
        self.app.stop_execution_timer = Mock(return_value=0.2)
        self.app.save_records = Mock()
        self.app.rebuild_outputs_from_records = Mock()
        self.app.refresh_current_preview = Mock()
        self.app.update_mini_status = Mock()
        self.app.update_counter = Mock()
        self.app.update_result_action_buttons = Mock()
        self.app.set_status = Mock()
        self.app.copy_text_to_clipboard = Mock(return_value=True)
        self.app.start_flow_interpretation_background = Mock(return_value=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("app.append_event")
    def test_ocr_completion_starts_correction_without_copying_raw_text(self, _append_event):
        self.app.run_ocr_correction_for_record_async = Mock(return_value=True)

        self.app._after_ocr_record(self.record, "추출 원문", auto_copy=True)

        self.assertEqual(self.record["ocr_text"], "추출 원문")
        self.app.copy_text_to_clipboard.assert_not_called()
        self.app.run_ocr_correction_for_record_async.assert_called_once_with(
            self.record,
            auto_copy=True,
        )
        self.app.start_flow_interpretation_background.assert_not_called()

    @patch("app.append_event")
    def test_successful_correction_copies_only_corrected_text_once(self, _append_event):
        self.record.update({"ocr_text": "추출 오타", "status": "ocr_correction_running"})

        self.app._after_ocr_correction(
            self.record,
            "보정된 최종 텍스트",
            auto_copy=True,
        )

        self.app.copy_text_to_clipboard.assert_called_once_with("보정된 최종 텍스트")
        self.assertEqual(self.record["ocr_text"], "추출 오타")
        self.assertEqual(self.record["ocr_corrected_text"], "보정된 최종 텍스트")
        self.app.start_flow_interpretation_background.assert_called_once_with(
            self.record,
            force=True,
        )

    @patch("app.messagebox.showerror")
    @patch("app.append_event")
    def test_failed_correction_never_copies_raw_text(self, _append_event, _showerror):
        self.record.update({"ocr_text": "추출 원문", "status": "ocr_correction_running"})

        self.app._after_ocr_correction(
            self.record,
            "OCR 보정 실패\n\n일시적 오류",
            auto_copy=True,
        )

        self.app.copy_text_to_clipboard.assert_not_called()
        self.assertEqual(self.record["status"], "ocr_done")
        self.assertIn("ocr_correction_error", self.record)

    def test_existing_raw_ocr_starts_correction_instead_of_copying(self):
        self.record["ocr_text"] = "저장된 원문"
        self.app.run_ocr_correction_for_record_async = Mock(return_value=True)

        self.app.run_ocr_for_record_async(self.record, auto_copy=True, force=False)

        self.app.copy_text_to_clipboard.assert_not_called()
        self.app.run_ocr_correction_for_record_async.assert_called_once_with(
            self.record,
            auto_copy=True,
        )

    def test_existing_corrected_ocr_copies_final_text_without_new_request(self):
        self.record.update({
            "ocr_text": "저장된 원문",
            "ocr_corrected_text": "저장된 보정본",
        })
        self.app.run_ocr_correction_for_record_async = Mock(return_value=True)

        self.app.run_ocr_for_record_async(self.record, auto_copy=True, force=False)

        self.app.copy_text_to_clipboard.assert_called_once_with("저장된 보정본")
        self.app.run_ocr_correction_for_record_async.assert_not_called()

    def test_correction_failure_keeps_only_reanalysis_action(self):
        self.record.update({
            "ocr_text": "보존된 OCR 원문",
            "ocr_correction_error": "OCR 보정 실패",
            "status": "ocr_done",
        })
        self.app.get_current_record = Mock(return_value=self.record)
        self.app.result_actions = Mock()
        self.app.result_actions.winfo_manager.return_value = "pack"
        self.app.result_edit_button = Mock()
        self.app.result_edit_button.winfo_manager.return_value = "pack"

        ClassFlowAIApp.update_result_action_buttons(self.app)

        action = self.app.result_edit_button.config.call_args.kwargs
        self.assertEqual(action["text"], "결과 다시 수정")
        self.assertEqual(action["command"], self.app.reanalyze_current_result)
        self.assertEqual(action["state"], "normal")


if __name__ == "__main__":
    unittest.main()
