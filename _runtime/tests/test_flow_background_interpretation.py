import tempfile
import queue
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from app import ClassFlowAIApp
from modules.nvidia_cap_reasoner import (
    DEFAULT_CAP_PROMPT,
    DEFAULT_FLOW_INTERPRETATION_PROMPT,
    build_flow_interpretation_prompt,
)


class FlowBackgroundInterpretationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.image_path = self.workspace / "캡처 화면.png"
        Image.new("RGB", (18, 12), "white").save(self.image_path)
        self.record = {
            "record_id": "capture-1",
            "mode": "ocr",
            "image_path": str(self.image_path),
            "ocr_text": "빠르게 추출된 텍스트",
            "status": "ocr_done",
            "display_result_type": "ocr",
        }
        self.app = ClassFlowAIApp.__new__(ClassFlowAIApp)
        self.app.workspace = self.workspace
        self.app.capture_records = [self.record]
        self.app.config = {"nvidia_api_key": "test-key", "cap_reasoning_prompt": "설명"}
        self.app.root = Mock()
        self.app.save_records = Mock()
        self.app.update_result_action_buttons = Mock()
        self.app.rebuild_outputs_from_records = Mock()
        self.app.paths = {"events": self.workspace / "events.jsonl"}
        self.app.set_status = Mock()
        self.app.running = True
        self.app.flow_interpretation_queue = queue.Queue()
        self.app.flow_interpretation_pending = set()
        self.app.flow_interpretation_lock = threading.RLock()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_start_does_not_replace_quick_ocr_display_or_status(self):
        started = self.app.start_flow_interpretation_background(self.record)
        self.assertTrue(started)
        self.assertEqual(self.record["flow_interpretation_status"], "queued")
        self.assertEqual(self.record["status"], "ocr_done")
        self.assertEqual(self.record["display_result_type"], "ocr")
        self.assertEqual(self.app.flow_interpretation_queue.qsize(), 1)

    def test_background_job_uses_lesson_note_prompt_not_manual_cap_prompt(self):
        self.assertTrue(self.app.start_flow_interpretation_background(self.record))
        prompt = self.app.flow_interpretation_queue.get_nowait()["config"]["cap_reasoning_prompt"]
        self.assertIn("학생이 복습하기 좋은 수업 노트", prompt)
        self.assertIn("빠르게 추출된 텍스트", prompt)
        self.assertNotEqual(prompt, self.app.config["cap_reasoning_prompt"])
        self.assertEqual(DEFAULT_CAP_PROMPT, DEFAULT_FLOW_INTERPRETATION_PROMPT)

    def test_lesson_note_prompt_rejects_report_headings_and_empty_confirmation(self):
        prompt = build_flow_interpretation_prompt("보조 OCR")
        for heading in (
            "화면 해석",
            "화면 유형",
            "화면에서 확인되는 근거",
            "구조 또는 흐름",
            "학습·활용 포인트",
            "이미지 분석",
            "분석 결과",
            "관찰 내용",
        ):
            self.assertIn(heading, prompt)
        self.assertIn("제목이나 메타 항목으로 출력하지 않는다", prompt)
        self.assertIn("확인 필요: 없음", prompt)
        self.assertIn("절대 출력하지 않는다", prompt)
        self.assertIn("코드 화면", DEFAULT_FLOW_INTERPRETATION_PROMPT)
        self.assertIn("오류 화면", DEFAULT_FLOW_INTERPRETATION_PROMPT)
        self.assertIn("표·다이어그램 화면", DEFAULT_FLOW_INTERPRETATION_PROMPT)
        for capture_rule in (
            "개념 화면",
            "처리 흐름 화면",
            "장단점 화면",
            "코드 일부가 잘렸거나",
            "이미지와 OCR이 충돌하면 이미지를 우선",
            "내용이 짧으면 2~3개 섹션만 사용",
        ):
            self.assertIn(capture_rule, DEFAULT_FLOW_INTERPRETATION_PROMPT)

    def test_quick_ocr_is_immediately_visible_in_current_result(self):
        self.app.get_current_record = Mock(return_value=self.record)
        panel_text = ClassFlowAIApp.get_ocr_panel_text(self.app)
        self.assertIn("빠르게 추출된 텍스트", panel_text)

    @patch("app.append_event")
    def test_completion_updates_only_flow_interpretation(self, append_event):
        self.record["flow_interpretation_status"] = "running"
        self.app._after_flow_interpretation(self.record, "학습 흐름 해설", self.workspace.resolve())
        self.assertEqual(self.record["ocr_interpretation_text"], "학습 흐름 해설")
        self.assertEqual(self.record["status"], "ocr_done")
        self.assertEqual(self.record["display_result_type"], "ocr")
        self.app.rebuild_outputs_from_records.assert_called_once_with(save_records=False)
        append_event.assert_called_once()

    def test_deleted_capture_ignores_late_completion(self):
        self.app.capture_records = []
        self.app._after_flow_interpretation(self.record, "늦은 결과", self.workspace.resolve())
        self.assertNotIn("ocr_interpretation_text", self.record)
        self.app.save_records.assert_not_called()

    def test_duplicate_record_is_not_queued_twice(self):
        self.assertTrue(self.app.start_flow_interpretation_background(self.record))
        self.assertFalse(self.app.start_flow_interpretation_background(self.record))
        self.assertEqual(self.app.flow_interpretation_queue.qsize(), 1)

    @patch("app.append_event")
    def test_single_worker_processes_continuous_captures_sequentially(self, _append_event):
        second_path = self.workspace / "두 번째.png"
        Image.new("RGB", (20, 12), "black").save(second_path)
        second = {
            "record_id": "capture-2",
            "mode": "ocr",
            "image_path": str(second_path),
            "ocr_text": "두 번째 OCR",
            "status": "ocr_done",
            "display_result_type": "ocr",
        }
        self.app.capture_records.append(second)
        self.assertTrue(self.app.start_flow_interpretation_background(self.record))
        self.assertTrue(self.app.start_flow_interpretation_background(second))
        self.app.flow_interpretation_queue.put(None)
        self.app.root.after.side_effect = lambda _delay, callback: callback()
        active = 0
        maximum_active = 0
        calls = []

        def analyze(path, _config, on_retry=None):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            calls.append(Path(path).name)
            active -= 1
            return f"{Path(path).stem} 해설"

        with patch("app.analyze_capture_image", side_effect=analyze):
            self.app._flow_interpretation_worker_loop()
        self.assertEqual(maximum_active, 1)
        self.assertEqual(calls, [self.image_path.name, second_path.name])
        self.assertEqual(self.record["flow_interpretation_status"], "done")
        self.assertEqual(second["flow_interpretation_status"], "done")

    def test_ocr_keeps_correction_button_but_hides_background_interpret_button(self):
        self.app.ocr_refine_button = Mock()
        self.app.ocr_refine_button.winfo_manager.return_value = "pack"
        self.app.cap_copy_button = Mock()
        self.app.result_actions = Mock()
        self.app.result_actions.winfo_manager.return_value = "pack"
        self.app.get_current_record = Mock(return_value=self.record)
        ClassFlowAIApp.update_result_action_buttons(self.app)
        self.app.result_actions.pack_forget.assert_not_called()
        self.app.ocr_refine_button.config.assert_called_once()
        self.assertEqual(
            self.app.ocr_refine_button.config.call_args.kwargs["text"],
            "OCR 보정 후 복사",
        )
        self.app.cap_copy_button.config.assert_not_called()
        self.app.cap_copy_button.pack_forget.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
