import json
import tempfile
import queue
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from app import ClassFlowAIApp
from modules.nvidia_cap_reasoner import (
    DEFAULT_CAP_MODEL,
    DEFAULT_CAP_PROMPT,
    DEFAULT_FLOW_INTERPRETATION_PROMPT,
    build_flow_interpretation_prompt,
    parse_flow_interpretation_result,
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
            "display_order": 0,
        }
        self.app = ClassFlowAIApp.__new__(ClassFlowAIApp)
        self.app.workspace = self.workspace
        self.app.capture_records = [self.record]
        self.app.config = {"nvidia_api_key": "test-key", "cap_reasoning_prompt": "CUSTOM_MANUAL_ONLY"}
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

    def test_queue_does_not_freeze_prompt_or_manual_cap_config(self):
        self.assertTrue(self.app.start_flow_interpretation_background(self.record))
        job = self.app.flow_interpretation_queue.get_nowait()
        self.assertNotIn("config", job)
        self.assertNotIn("prompt", job)
        self.assertEqual(self.app.config["cap_reasoning_prompt"], "CUSTOM_MANUAL_ONLY")
        self.assertNotEqual(DEFAULT_CAP_PROMPT, DEFAULT_FLOW_INTERPRETATION_PROMPT)

    def test_lesson_note_prompt_rejects_report_headings_and_empty_confirmation(self):
        prompt = build_flow_interpretation_prompt(
            "보조 OCR",
            capture_index=2,
            record_id="capture-2",
            previous={
                "record_id": "capture-1",
                "title": "첫 제목",
                "summary": "첫 학습 요약",
                "group_id": "group-1",
            },
        )
        self.assertIn('"continues_previous"', prompt)
        self.assertIn('"body_markdown"', prompt)
        self.assertIn("capture_index: 2", prompt)
        self.assertIn("record_id: capture-2", prompt)
        self.assertIn("title: 첫 제목", prompt)
        self.assertIn("summary:\n첫 학습 요약", prompt)
        self.assertIn("group_id: group-1", prompt)
        self.assertIn("JSON 객체만 반환", DEFAULT_FLOW_INTERPRETATION_PROMPT)

    def test_quick_ocr_is_immediately_visible_in_current_result(self):
        self.app.get_current_record = Mock(return_value=self.record)
        panel_text = ClassFlowAIApp.get_ocr_panel_text(self.app)
        self.assertIn("빠르게 추출된 텍스트", panel_text)

    @patch("app.append_event")
    def test_completion_updates_only_flow_interpretation(self, append_event):
        self.record["flow_interpretation_status"] = "running"
        self.app._after_flow_interpretation(
            self.record,
            {
                "title": "구체적인 제목",
                "continues_previous": False,
                "body_markdown": "학습 흐름 해설",
                "review_required": [],
                "parse_fallback": False,
            },
            self.workspace.resolve(),
        )
        self.assertEqual(self.record["ocr_interpretation_text"], "학습 흐름 해설")
        self.assertEqual(self.record["flow_interpretation_text"], "학습 흐름 해설")
        self.assertEqual(self.record["flow_title"], "구체적인 제목")
        self.assertEqual(self.record["group_id"], "capture-1")
        self.assertEqual(self.record["status"], "ocr_done")
        self.assertEqual(self.record["display_result_type"], "ocr")
        self.app.rebuild_outputs_from_records.assert_called_once_with(save_records=False)
        append_event.assert_called_once()

    def test_deleted_capture_ignores_late_completion(self):
        self.app.capture_records = []
        self.app._after_flow_interpretation(
            self.record,
            {"title": "늦음", "continues_previous": False, "body_markdown": "늦은 결과", "review_required": []},
            self.workspace.resolve(),
        )
        self.assertNotIn("ocr_interpretation_text", self.record)
        self.app.save_records.assert_not_called()

    def test_workspace_change_ignores_late_completion(self):
        old_workspace = self.workspace.resolve()
        self.app.workspace = self.workspace / "다른 수업"
        self.app._after_flow_interpretation(
            self.record,
            {"title": "늦음", "continues_previous": False, "body_markdown": "늦은 결과", "review_required": []},
            old_workspace,
        )
        self.assertNotIn("ocr_interpretation_text", self.record)
        self.app.save_records.assert_not_called()

    def test_duplicate_record_is_not_queued_twice(self):
        self.assertTrue(self.app.start_flow_interpretation_background(self.record))
        self.assertFalse(self.app.start_flow_interpretation_background(self.record))
        self.assertEqual(self.app.flow_interpretation_queue.qsize(), 1)

    @patch("app.append_event")
    def test_single_worker_builds_previous_context_at_execution_time(self, _append_event):
        second_path = self.workspace / "두 번째.png"
        Image.new("RGB", (20, 12), "black").save(second_path)
        second = {
            "record_id": "capture-2",
            "mode": "ocr",
            "image_path": str(second_path),
            "ocr_text": "두 번째 OCR",
            "status": "ocr_done",
            "display_result_type": "ocr",
            "display_order": 1,
        }
        self.app.capture_records.append(second)
        self.assertTrue(self.app.start_flow_interpretation_background(self.record))
        self.assertTrue(self.app.start_flow_interpretation_background(second))
        self.app.flow_interpretation_queue.put(None)
        self.app.root.after.side_effect = lambda _delay, callback: callback()
        active = 0
        maximum_active = 0
        calls = []
        prompts = []

        def analyze(path, config, on_retry=None):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            calls.append(Path(path).name)
            prompts.append(config["cap_reasoning_prompt"])
            active -= 1
            index = len(calls)
            return (
                '{"title":"첫 제목","continues_previous":false,'
                '"body_markdown":"첫 본문","review_required":[]}'
                if index == 1
                else '{"title":"둘째 제목","continues_previous":true,'
                '"body_markdown":"새로 추가된 본문","review_required":[]}'
            )

        with patch("app.analyze_capture_image", side_effect=analyze):
            self.app._flow_interpretation_worker_loop()
        self.assertEqual(maximum_active, 1)
        self.assertEqual(calls, [self.image_path.name, second_path.name])
        self.assertNotIn("첫 제목", prompts[0])
        self.assertIn("title: 첫 제목", prompts[1])
        self.assertIn("summary:\n첫 본문", prompts[1])
        self.assertIn("group_id: capture-1", prompts[1])
        self.assertNotIn("CUSTOM_MANUAL_ONLY", prompts[1])
        self.assertEqual(self.record["flow_interpretation_status"], "done")
        self.assertEqual(second["flow_interpretation_status"], "done")
        self.assertEqual(self.record["group_id"], "capture-1")
        self.assertEqual(second["group_id"], "capture-1")

    @patch("app.append_event")
    def test_worker_never_skips_invalid_immediate_previous_capture(self, _append_event):
        self.record.update({
            "flow_interpretation_status": "done",
            "flow_title": "첫 캡처 제목",
            "flow_interpretation_text": "첫 캡처 본문",
            "group_id": "capture-1",
        })
        second = {
            "record_id": "capture-2",
            "mode": "ocr",
            "image_path": str(self.image_path),
            "ocr_text": "실패 OCR",
            "flow_interpretation_status": "failed",
            "display_order": 1,
        }
        third = {
            "record_id": "capture-3",
            "mode": "ocr",
            "image_path": str(self.image_path),
            "ocr_text": "세 번째 OCR",
            "display_order": 2,
        }
        self.app.capture_records.extend([second, third])
        self.assertTrue(self.app.start_flow_interpretation_background(third))
        self.app.flow_interpretation_queue.put(None)
        self.app.root.after.side_effect = lambda _delay, callback: callback()
        prompts = []

        def analyze(_path, config, on_retry=None):
            prompts.append(config["cap_reasoning_prompt"])
            return '{"title":"셋째","continues_previous":true,"body_markdown":"셋째 본문","review_required":[]}'

        with patch("app.analyze_capture_image", side_effect=analyze):
            self.app._flow_interpretation_worker_loop()
        self.assertIn("exists: false", prompts[0])
        self.assertNotIn("첫 캡처 제목", prompts[0])
        self.assertFalse(third["flow_continues_previous"])
        self.assertEqual(third["group_id"], "capture-3")

    @patch("app.append_event")
    def test_worker_uses_order_changed_after_queueing(self, _append_event):
        second_path = self.workspace / "재정렬.png"
        Image.new("RGB", (10, 10), "blue").save(second_path)
        second = {
            "record_id": "capture-2",
            "mode": "ocr",
            "image_path": str(second_path),
            "ocr_text": "둘째 OCR",
            "display_order": 1,
        }
        self.app.capture_records.append(second)
        self.assertTrue(self.app.start_flow_interpretation_background(second))
        self.record["display_order"] = 1
        second["display_order"] = 0
        self.app.flow_interpretation_queue.put(None)
        self.app.root.after.side_effect = lambda _delay, callback: callback()
        prompts = []

        def analyze(_path, config, on_retry=None):
            prompts.append(config["cap_reasoning_prompt"])
            return '{"title":"재정렬","continues_previous":false,"body_markdown":"본문","review_required":[]}'

        with patch("app.analyze_capture_image", side_effect=analyze):
            self.app._flow_interpretation_worker_loop()
        self.assertIn("capture_index: 1", prompts[0])
        self.assertIn("exists: false", prompts[0])

    @patch("app.append_event")
    def test_worker_uses_current_selected_model_at_execution_time(self, _append_event):
        self.app.config["cap_reasoning_model"] = "custom/previous-model"
        self.assertTrue(self.app.start_flow_interpretation_background(self.record))
        self.app.config["cap_reasoning_model"] = DEFAULT_CAP_MODEL
        self.app.flow_interpretation_queue.put(None)
        self.app.root.after.side_effect = lambda _delay, callback: callback()
        models = []

        def analyze(_path, config, on_retry=None):
            models.append(config["cap_reasoning_model"])
            return '{"title":"현재 모델","continues_previous":false,"body_markdown":"본문","review_required":[]}'

        with patch("app.analyze_capture_image", side_effect=analyze):
            self.app._flow_interpretation_worker_loop()

        self.assertEqual(models, ["google/diffusiongemma-26b-a4b-it"])

    @patch("app.append_event")
    def test_worker_exception_does_not_stop_next_job(self, _append_event):
        second_path = self.workspace / "예외 다음.png"
        Image.new("RGB", (10, 10), "green").save(second_path)
        second = {
            "record_id": "capture-2",
            "mode": "ocr",
            "image_path": str(second_path),
            "ocr_text": "둘째 OCR",
            "display_order": 1,
        }
        self.app.capture_records.append(second)
        self.assertTrue(self.app.start_flow_interpretation_background(self.record))
        self.assertTrue(self.app.start_flow_interpretation_background(second))
        self.app.flow_interpretation_queue.put(None)
        self.app.root.after.side_effect = lambda _delay, callback: callback()
        results = [RuntimeError("첫 작업 예외"), '{"title":"둘째","continues_previous":false,"body_markdown":"둘째 본문","review_required":[]}']

        def analyze(_path, _config, on_retry=None):
            value = results.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with patch("app.analyze_capture_image", side_effect=analyze):
            self.app._flow_interpretation_worker_loop()
        self.assertEqual(self.record["flow_interpretation_status"], "failed")
        self.assertEqual(second["flow_interpretation_status"], "done")

    @patch("app.append_event")
    def test_group_assignment_is_persisted_for_both_records(self, _append_event):
        previous = self.record
        previous.update({
            "flow_interpretation_status": "done",
            "flow_title": "앞 단계",
            "flow_interpretation_text": "앞 설명",
        })
        second = dict(previous, record_id="capture-2", display_order=1, flow_interpretation_status="running")
        for key in ("group_id", "flow_title", "flow_interpretation_text"):
            second.pop(key, None)
        self.app.capture_records.append(second)
        records_path = self.workspace / "state" / "capture_records.json"
        self.app.paths["records"] = records_path
        self.app.save_records = ClassFlowAIApp.save_records.__get__(self.app, ClassFlowAIApp)
        self.app._after_flow_interpretation(
            second,
            {"title": "다음", "continues_previous": True, "body_markdown": "추가", "review_required": []},
            self.workspace.resolve(),
            previous_record_id="capture-1",
        )
        saved = json.loads(records_path.read_text(encoding="utf-8"))
        self.assertEqual([record["group_id"] for record in saved], ["capture-1", "capture-1"])

    @patch("app.append_event")
    def test_continuation_without_previous_group_assigns_previous_id_to_both(self, _append_event):
        previous = self.record
        previous["flow_interpretation_status"] = "done"
        previous["flow_title"] = "앞 단계"
        previous["flow_interpretation_text"] = "앞 설명"
        second = dict(previous, record_id="capture-2", flow_interpretation_status="running")
        for key in ("group_id", "flow_title", "flow_interpretation_text"):
            second.pop(key, None)
        self.app.capture_records.append(second)
        self.app._after_flow_interpretation(
            second,
            {"title": "다음 단계", "continues_previous": True, "body_markdown": "추가 설명", "review_required": ["결과 확인"]},
            self.workspace.resolve(),
            previous_record_id="capture-1",
        )
        self.assertEqual(previous["group_id"], "capture-1")
        self.assertEqual(second["group_id"], "capture-1")
        self.assertEqual(second["flow_review_required"], ["결과 확인"])

    @patch("app.append_event")
    def test_missing_previous_never_merges_even_if_model_says_continue(self, _append_event):
        self.record["flow_interpretation_status"] = "running"
        self.app._after_flow_interpretation(
            self.record,
            {"title": "독립", "continues_previous": True, "body_markdown": "본문", "review_required": []},
            self.workspace.resolve(),
            previous_record_id="deleted-record",
        )
        self.assertFalse(self.record["flow_continues_previous"])
        self.assertEqual(self.record["group_id"], "capture-1")

    def test_legacy_previous_interpretation_is_available_as_worker_context(self):
        previous = {
            "record_id": "legacy",
            "mode": "ocr",
            "ocr_interpretation_text": "## 레거시 제목\n레거시 본문",
        }
        context = self.app._flow_previous_context(previous)
        self.assertEqual(context["record_id"], "legacy")
        self.assertEqual(context["title"], "레거시 제목")
        self.assertIn("레거시 본문", context["summary"])

    def test_pending_or_failed_previous_interpretation_is_not_context(self):
        for status in ("queued", "running", "failed", "waiting_for_api_key"):
            previous = {
                "record_id": "previous",
                "mode": "ocr",
                "flow_interpretation_status": status,
                "ocr_interpretation_text": "사용하면 안 되는 본문",
            }
            with self.subTest(status=status):
                self.assertIsNone(self.app._flow_previous_context(previous))

    def test_cap_result_is_available_as_previous_context(self):
        previous = {
            "record_id": "cap-1",
            "mode": "capture",
            "cap_text": "## CAP 제목\nCAP 학습 설명",
            "group_id": "cap-group",
        }
        context = self.app._flow_previous_context(previous)
        self.assertEqual(context["record_id"], "cap-1")
        self.assertEqual(context["title"], "CAP 제목")
        self.assertEqual(context["group_id"], "cap-group")

    def test_edited_result_is_used_as_previous_context(self):
        previous = {
            "record_id": "edited-previous",
            "mode": "ocr",
            "flow_title": "원본 제목",
            "flow_title_edited": "수정 제목",
            "flow_interpretation_text": "원본 설명",
            "flow_interpretation_text_edited": "수정 설명",
            "flow_interpretation_status": "done",
        }

        context = self.app._flow_previous_context(previous)

        self.assertEqual(context["title"], "수정 제목")
        self.assertEqual(context["summary"], "수정 설명")

    @patch("app.append_event")
    def test_ocr_rerun_clears_all_previous_flow_fields(self, _append_event):
        self.record.update({
            "flow_title": "이전 제목",
            "flow_title_edited": "이전 수정 제목",
            "flow_interpretation_text": "이전 본문",
            "flow_interpretation_text_edited": "이전 수정 본문",
            "ocr_interpretation_text": "이전 본문",
            "analysis_edited_at": "2026-07-27 15:00:00",
            "flow_continues_previous": True,
            "flow_review_required": ["확인"],
            "flow_interpretation_parse_fallback": True,
            "flow_interpretation_error": "이전 오류",
            "flow_interpretation_status": "done",
            "group_id": "old-group",
        })
        self.app.stop_execution_timer = Mock(return_value=0.1)
        self.app.refresh_current_preview = Mock()
        self.app.update_mini_status = Mock()
        self.app.update_counter = Mock()
        self.app.start_flow_interpretation_background = Mock(return_value=True)
        self.app._after_ocr_record(self.record, "새 OCR", auto_copy=False)
        for key in (
            "flow_title", "flow_title_edited",
            "flow_interpretation_text", "flow_interpretation_text_edited",
            "ocr_interpretation_text", "analysis_edited_at",
            "flow_continues_previous", "flow_review_required",
            "flow_interpretation_parse_fallback", "flow_interpretation_error",
            "group_id",
        ):
            self.assertNotIn(key, self.record)
        self.assertNotIn("flow_interpretation_status", self.record)
        self.app.start_flow_interpretation_background.assert_called_once_with(self.record)

    @patch("app.append_event")
    def test_ocr_rerun_marks_running_interpretation_for_safe_requeue(self, _append_event):
        self.record["flow_interpretation_status"] = "running"
        self.record["flow_interpretation_text"] = "오래된 본문"
        self.app.stop_execution_timer = Mock(return_value=0.1)
        self.app.refresh_current_preview = Mock()
        self.app.update_mini_status = Mock()
        self.app.update_counter = Mock()
        self.app.start_flow_interpretation_background = Mock(return_value=True)
        self.app._after_ocr_record(self.record, "새 OCR", auto_copy=False)
        self.assertTrue(self.record["flow_interpretation_requeue"])
        self.assertEqual(self.record["flow_interpretation_status"], "running")
        self.assertNotIn("flow_interpretation_text", self.record)
        self.app.start_flow_interpretation_background.assert_not_called()

    @patch("app.append_event")
    def test_ocr_correction_clears_previous_flow_fields_before_requeue(self, _append_event):
        self.record.update({
            "flow_title": "이전 제목",
            "flow_title_edited": "이전 수정 제목",
            "flow_interpretation_text": "이전 본문",
            "flow_interpretation_text_edited": "이전 수정 본문",
            "ocr_interpretation_text": "이전 본문",
            "analysis_edited_at": "2026-07-27 15:00:00",
            "flow_continues_previous": True,
            "flow_review_required": ["확인"],
            "flow_interpretation_parse_fallback": True,
            "flow_interpretation_error": "이전 오류",
            "flow_interpretation_status": "done",
            "group_id": "old-group",
        })
        self.app.stop_execution_timer = Mock(return_value=0.1)
        self.app.refresh_current_preview = Mock()
        self.app.update_mini_status = Mock()
        self.app.update_counter = Mock()
        self.app.copy_text_to_clipboard = Mock(return_value=True)
        self.app.start_flow_interpretation_background = Mock(return_value=True)
        self.app._after_ocr_correction(self.record, "보정된 OCR")
        for key in (
            "flow_title", "flow_title_edited",
            "flow_interpretation_text", "flow_interpretation_text_edited",
            "ocr_interpretation_text", "analysis_edited_at",
            "flow_continues_previous", "flow_review_required",
            "flow_interpretation_parse_fallback", "flow_interpretation_error",
            "group_id", "flow_interpretation_status",
        ):
            self.assertNotIn(key, self.record)
        self.app.start_flow_interpretation_background.assert_called_once_with(self.record, force=True)

    @patch("app.append_event")
    def test_markdown_fallback_flag_is_stored_without_raw_response_in_event(self, append_event):
        self.record["flow_interpretation_status"] = "running"
        parsed = parse_flow_interpretation_result("## 레거시 제목\n레거시 본문")
        self.app._after_flow_interpretation(self.record, parsed, self.workspace.resolve())
        self.assertTrue(self.record["flow_interpretation_parse_fallback"])
        event = append_event.call_args.args[1]
        self.assertTrue(event["parse_fallback"])
        self.assertNotIn("레거시 본문", str(event))

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
            "정확한 복사",
        )
        self.app.cap_copy_button.config.assert_not_called()
        self.app.cap_copy_button.pack_forget.assert_called_once_with()


class FlowInterpretationParserTests(unittest.TestCase):
    def test_parses_json_and_cleans_review_items_and_body_title(self):
        parsed = parse_flow_interpretation_result(
            '{"title":"구체적인 제목","continues_previous":true,'
            '"body_markdown":"## 중복 제목\\n새로운 설명",'
            '"review_required":[" 확인 1 ","", "확인 2"]}'
        )
        self.assertEqual(parsed["title"], "구체적인 제목")
        self.assertTrue(parsed["continues_previous"])
        self.assertEqual(parsed["body_markdown"], "새로운 설명")
        self.assertEqual(parsed["review_required"], ["확인 1", "확인 2"])
        self.assertFalse(parsed["parse_fallback"])

    def test_json_body_keeps_non_title_subheading(self):
        parsed = parse_flow_interpretation_result(
            '{"title":"제목","continues_previous":false,'
            '"body_markdown":"### 핵심\\n중요 설명","review_required":[]}'
        )
        self.assertEqual(parsed["body_markdown"], "### 핵심\n중요 설명")

    def test_parses_json_fence_and_removes_think_block(self):
        parsed = parse_flow_interpretation_result(
            '<think>내부 분석</think>\n```json\n'
            '{"title":"제목","continues_previous":false,'
            '"body_markdown":"본문","review_required":[]}\n```'
        )
        self.assertEqual(parsed["title"], "제목")
        self.assertEqual(parsed["body_markdown"], "본문")

    def test_json_title_does_not_keep_markdown_heading_marker(self):
        parsed = parse_flow_interpretation_result(
            '{"title":"## 구체적인 제목","continues_previous":false,'
            '"body_markdown":"실제 본문","review_required":[]}'
        )
        self.assertEqual(parsed["title"], "구체적인 제목")

    def test_rejects_invalid_json_field_types_and_empty_required_values(self):
        invalid_values = [
            '{"title":"제목","continues_previous":"true","body_markdown":"본문","review_required":[]}',
            '{"title":"","continues_previous":false,"body_markdown":"본문","review_required":[]}',
            '{"title":"제목","continues_previous":false,"body_markdown":"","review_required":[]}',
            '{"title":"제목","continues_previous":false,"body_markdown":"본문","review_required":[1]}',
        ]
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_flow_interpretation_result(value)

    def test_legacy_markdown_fallback_splits_title_and_never_continues(self):
        parsed = parse_flow_interpretation_result("## 레거시 제목\n레거시 본문")
        self.assertEqual(parsed["title"], "레거시 제목")
        self.assertEqual(parsed["body_markdown"], "레거시 본문")
        self.assertFalse(parsed["continues_previous"])
        self.assertEqual(parsed["review_required"], [])
        self.assertTrue(parsed["parse_fallback"])

    def test_fallback_requires_heading_and_body_and_limits_long_json_title(self):
        with self.assertRaises(ValueError):
            parse_flow_interpretation_result("제목 없는 한 줄")
        parsed = parse_flow_interpretation_result(
            '{"title":"' + ("가" * 200) + '","continues_previous":false,'
            '"body_markdown":"본문","review_required":[]}'
        )
        self.assertLessEqual(len(parsed["title"]), 80)

    def test_empty_and_failure_responses_do_not_fallback(self):
        for value in (
            "",
            "CAP 분석 실패\n\n오류",
            "수업 흐름 해석 실패\n\n오류",
            "<think>분석만 있음</think>",
            "## 제목만 있음",
            "API 오류: 인증에 실패했습니다.",
            '{"title": "깨진 JSON"',
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_flow_interpretation_result(value)


if __name__ == "__main__":
    unittest.main()
