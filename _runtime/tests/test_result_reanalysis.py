import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import ClassFlowAIApp
from modules.nvidia_cap_reasoner import build_cap_revision_prompt
from modules.result_reanalysis import (
    apply_reanalysis_failure,
    apply_reanalysis_success,
    begin_reanalysis,
    current_result,
    is_current_reanalysis,
    restore_previous_result,
)


class ResultReanalysisStateTests(unittest.TestCase):
    def test_cap_success_uses_latest_result_and_preserves_original_and_previous(self):
        record = {
            "record_id": "cap-1",
            "mode": "capture",
            "cap_text": "최초 모델 결과",
            "cap_text_edited": "1차 성공 결과",
            "captured_at": "2026-07-28 09:00:00",
            "display_order": 3,
        }
        immutable = {
            key: record[key]
            for key in ("record_id", "captured_at", "display_order")
        }
        job = begin_reanalysis(
            record,
            workspace=Path("C:/lesson one"),
            job_id="job-2",
            started_at="2026-07-28 10:00:00",
        )

        self.assertEqual(job["current_result"], "1차 성공 결과")
        self.assertTrue(
            apply_reanalysis_success(
                record,
                job,
                "2차 성공 결과",
                completed_at="2026-07-28 10:01:00",
            )
        )

        self.assertEqual(record["cap_text"], "최초 모델 결과")
        self.assertEqual(record["cap_text_previous"], "1차 성공 결과")
        self.assertEqual(record["cap_text_edited"], "2차 성공 결과")
        self.assertEqual(current_result(record), "2차 성공 결과")
        self.assertEqual({key: record[key] for key in immutable}, immutable)
        self.assertEqual(record["result_reanalysis_status"], "done")

    def test_ocr_success_preserves_raw_and_previous_corrected_result(self):
        record = {
            "record_id": "ocr-1",
            "mode": "ocr",
            "ocr_text": "최초 OCR",
            "ocr_corrected_text": "1차 보정",
        }
        job = begin_reanalysis(
            record,
            workspace=Path("C:/lesson"),
            job_id="ocr-job",
            started_at="2026-07-28 11:00:00",
        )

        self.assertTrue(apply_reanalysis_success(record, job, "2차 보정"))

        self.assertEqual(record["ocr_text"], "최초 OCR")
        self.assertEqual(record["ocr_previous_corrected_text"], "1차 보정")
        self.assertEqual(record["ocr_corrected_text"], "2차 보정")
        self.assertEqual(record["display_result_type"], "ocr_corrected")

    def test_ocr_success_marks_inflight_flow_for_safe_requeue(self):
        record = {
            "record_id": "ocr-flow-running",
            "mode": "ocr",
            "ocr_text": "원본 OCR",
            "ocr_corrected_text": "현재 OCR",
            "flow_interpretation_status": "running",
            "flow_interpretation_text": "오래된 흐름",
        }
        job = begin_reanalysis(record, Path("C:/lesson"), "flow-requeue-job")

        self.assertTrue(apply_reanalysis_success(record, job, "새 OCR"))

        self.assertTrue(record["flow_interpretation_requeue"])
        self.assertNotIn("flow_interpretation_text", record)

    def test_failure_keeps_every_existing_result_unchanged(self):
        record = {
            "record_id": "cap-fail",
            "mode": "capture",
            "cap_text": "최초 결과",
            "cap_text_edited": "현재 정상 결과",
        }
        job = begin_reanalysis(
            record,
            workspace=Path("C:/lesson"),
            job_id="fail-job",
            started_at="2026-07-28 12:00:00",
        )
        result_fields_before = {
            "cap_text": record["cap_text"],
            "cap_text_edited": record["cap_text_edited"],
        }

        self.assertTrue(apply_reanalysis_failure(record, job, "HTTP 503"))

        self.assertEqual(
            {key: record[key] for key in result_fields_before},
            result_fields_before,
        )
        self.assertEqual(record["result_reanalysis_status"], "failed")
        self.assertEqual(record["result_reanalysis_error"], "HTTP 503")

    def test_stale_job_cannot_overwrite_newer_result(self):
        record = {
            "record_id": "cap-stale",
            "mode": "capture",
            "cap_text": "최초",
        }
        stale_job = begin_reanalysis(
            record,
            workspace=Path("C:/lesson"),
            job_id="old-job",
            started_at="2026-07-28 13:00:00",
        )
        record["result_reanalysis_status"] = "done"
        current_job = begin_reanalysis(
            record,
            workspace=Path("C:/lesson"),
            job_id="new-job",
            started_at="2026-07-28 13:01:00",
        )
        self.assertTrue(is_current_reanalysis(record, current_job))

        self.assertFalse(apply_reanalysis_success(record, stale_job, "오래된 응답"))

        self.assertEqual(record["cap_text"], "최초")
        self.assertNotIn("cap_text_edited", record)
        self.assertEqual(record["result_reanalysis_job_id"], "new-job")

    def test_same_record_cannot_start_duplicate_running_job(self):
        record = {
            "record_id": "duplicate",
            "mode": "capture",
            "cap_text": "현재 결과",
        }
        begin_reanalysis(record, Path("C:/lesson"), "first-job")

        with self.assertRaisesRegex(ValueError, "이미 결과 재분석"):
            begin_reanalysis(record, Path("C:/lesson"), "second-job")

    def test_deleted_capture_rejects_late_result(self):
        record = {
            "record_id": "deleted-cap",
            "mode": "capture",
            "cap_text": "기존 결과",
        }
        job = begin_reanalysis(record, Path("C:/lesson"), "deleted-job")
        record["deleted"] = True

        self.assertFalse(apply_reanalysis_success(record, job, "늦게 도착한 결과"))
        self.assertEqual(record["cap_text"], "기존 결과")
        self.assertNotIn("cap_text_edited", record)

    def test_restore_previous_result_swaps_without_losing_latest_result(self):
        record = {
            "record_id": "restore-cap",
            "mode": "capture",
            "cap_text": "최초 결과",
            "cap_text_previous": "직전 결과",
            "cap_text_edited": "현재 결과",
        }

        self.assertTrue(restore_previous_result(record))

        self.assertEqual(current_result(record), "직전 결과")
        self.assertEqual(record["cap_text_previous"], "현재 결과")
        self.assertEqual(record["cap_text"], "최초 결과")


class ResultReanalysisPromptTests(unittest.TestCase):
    def test_cap_revision_prompt_keeps_base_prompt_and_compares_current_result(self):
        prompt = build_cap_revision_prompt(
            {"cap_reasoning_prompt": "기존 CAP 분석 지침"},
            "현재 저장된 해석",
        )

        self.assertIn("기존 CAP 분석 지침", prompt)
        self.assertIn("현재 저장된 해석", prompt)
        self.assertIn("원본 이미지를 다시 분석", prompt)
        self.assertIn("그대로 신뢰하지", prompt)
        self.assertIn("추측해서 추가하지", prompt)
        self.assertIn("완성된 최종 해석 결과만", prompt)


class ResultActionLayoutTests(unittest.TestCase):
    def test_result_panel_builds_only_one_right_aligned_reanalysis_button(self):
        buttons = []

        def widget_factory(*args, **kwargs):
            widget = Mock()
            widget.parent = args[0] if args else None
            widget.creation_options = kwargs
            return widget

        def button_factory(*args, **kwargs):
            button = widget_factory(*args, **kwargs)
            buttons.append(button)
            return button

        app = ClassFlowAIApp.__new__(ClassFlowAIApp)
        app.root = Mock()
        app.lesson_location_text = Mock(return_value="테스트 수업")
        app.get_ocr_panel_text = Mock(return_value="현재 결과")

        with patch.multiple(
            "app.tk",
            Frame=widget_factory,
            LabelFrame=widget_factory,
            Label=widget_factory,
            Button=button_factory,
            Text=widget_factory,
            Listbox=widget_factory,
            Scrollbar=widget_factory,
            StringVar=widget_factory,
        ):
            app.build_ui()

        result_buttons = [
            button for button in buttons
            if button.parent is app.result_actions
        ]
        self.assertEqual(len(result_buttons), 1)
        button = result_buttons[0]
        self.assertEqual(button.creation_options["text"], "결과 다시 수정")
        self.assertEqual(button.creation_options["command"], app.reanalyze_current_result)
        self.assertEqual(button.pack.call_args.kwargs["side"], "right")
        self.assertEqual(app.result_actions.pack.call_args.kwargs["fill"], "x")
        button.place.assert_not_called()
        app.result_actions.place.assert_not_called()


class ResultReanalysisAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.record = {
            "record_id": "cap-ui",
            "mode": "capture",
            "image_path": str(self.workspace / "capture.png"),
            "cap_text": "현재 결과",
        }
        (self.workspace / "capture.png").write_bytes(b"image")
        self.app = ClassFlowAIApp.__new__(ClassFlowAIApp)
        self.app.workspace = self.workspace
        self.app.capture_records = [self.record]
        self.app.current_record_index = 0
        self.app.set_status = Mock()
        self.app.start_result_reanalysis = Mock(return_value=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("app.tk.Toplevel")
    def test_button_handler_starts_ai_reanalysis_without_opening_modal(self, toplevel):
        self.app.reanalyze_current_result()

        self.app.start_result_reanalysis.assert_called_once_with(self.record)
        toplevel.assert_not_called()

    def test_running_job_shows_non_blocking_message_without_erasing_record(self):
        before = copy.deepcopy(self.record)
        self.record["result_reanalysis_status"] = "running"

        text = self.app.get_current_result_text(self.record)

        self.assertIn("결과 다시 확인 중", text)
        self.assertIn("원본 이미지를 다시 분석", text)
        self.assertEqual(self.record["cap_text"], before["cap_text"])

    def test_failure_category_uses_existing_http_error_classification(self):
        self.assertEqual(
            self.app._result_reanalysis_error_category("CAP 분석 실패 HTTP 503"),
            "모델 서버 일시 오류",
        )

    def test_result_button_is_disabled_only_while_reanalysis_is_running(self):
        self.record["result_reanalysis_status"] = "running"
        self.app.get_current_record = Mock(return_value=self.record)
        self.app.result_actions = Mock()
        self.app.result_actions.winfo_manager.return_value = "pack"
        self.app.result_edit_button = Mock()
        self.app.result_edit_button.winfo_manager.return_value = "pack"

        ClassFlowAIApp.update_result_action_buttons(self.app)
        running_config = self.app.result_edit_button.config.call_args.kwargs
        self.assertEqual(running_config["state"], "disabled")
        self.assertEqual(running_config["text"], "결과 다시 수정 중...")

        self.record["result_reanalysis_status"] = "failed"
        ClassFlowAIApp.update_result_action_buttons(self.app)
        failed_config = self.app.result_edit_button.config.call_args.kwargs
        self.assertEqual(failed_config["state"], "normal")
        self.assertEqual(failed_config["text"], "결과 다시 수정")

        self.record["status"] = "cap_running"
        ClassFlowAIApp.update_result_action_buttons(self.app)
        processing_config = self.app.result_edit_button.config.call_args.kwargs
        self.assertEqual(processing_config["state"], "disabled")
        self.assertEqual(processing_config["text"], "결과 다시 수정")

    def make_running_app(self, record, workspace=None):
        workspace = Path(workspace or self.workspace)
        app = ClassFlowAIApp.__new__(ClassFlowAIApp)
        app.workspace = workspace
        app.capture_records = [record]
        app.current_record_index = 0
        app.paths = {
            "records": workspace / "state" / "capture_records.json",
            "flow_document": workspace / "state" / "flow_document.json",
        }
        app.config = {"cap_reasoning_model": "test/model"}
        app.result_reanalysis_jobs = {}
        app.result_reanalysis_lock = __import__("threading").RLock()
        app.save_records = Mock()
        app.rebuild_outputs_from_records = Mock()
        app.refresh_current_preview = Mock()
        app.update_ocr_panel = Mock()
        app.update_mini_status = Mock()
        app.update_counter = Mock()
        app.update_result_action_buttons = Mock()
        app.set_status = Mock()
        app.copy_text_to_clipboard = Mock(return_value=True)
        app.start_flow_interpretation_background = Mock(return_value=True)
        return app

    def test_completion_after_lesson_switch_updates_only_original_workspace(self):
        original = self.workspace / "원래 수업"
        current = self.workspace / "현재 수업"
        (original / "state").mkdir(parents=True)
        (original / "captures").mkdir(parents=True)
        (current / "state").mkdir(parents=True)
        image_path = original / "captures" / "capture.png"
        image_path.write_bytes(b"image")
        original_record = {
            "record_id": "original-cap",
            "mode": "capture",
            "image_path": str(image_path),
            "cap_text": "기존 해석",
        }
        job = begin_reanalysis(
            original_record,
            workspace=original,
            job_id="switched-job",
            started_at="2026-07-28 14:00:00",
        )
        (original / "state" / "capture_records.json").write_text(
            json.dumps([original_record], ensure_ascii=False),
            encoding="utf-8",
        )
        current_record = {
            "record_id": "current-cap",
            "mode": "capture",
            "cap_text": "현재 수업 결과",
        }
        app = self.make_running_app(current_record, workspace=current)
        app.copy_text_to_clipboard = Mock(return_value=True)

        app._complete_result_reanalysis(job, "원래 수업의 새 해석")

        saved = json.loads(
            (original / "state" / "capture_records.json").read_text(encoding="utf-8")
        )[0]
        self.assertEqual(saved["cap_text"], "기존 해석")
        self.assertEqual(saved["cap_text_edited"], "원래 수업의 새 해석")
        self.assertEqual(current_record["cap_text"], "현재 수업 결과")
        app.refresh_current_preview.assert_not_called()
        app.set_status.assert_not_called()
        app.copy_text_to_clipboard.assert_not_called()

    def test_current_cap_success_copies_exact_saved_display_result(self):
        record = {
            "record_id": "cap-copy",
            "mode": "capture",
            "cap_text": "기존 CAP 결과",
        }
        job = begin_reanalysis(record, self.workspace, "cap-copy-job")
        app = self.make_running_app(record)
        app.copy_text_to_clipboard = Mock(return_value=True)

        app._complete_result_reanalysis(job, "  최신 CAP 결과  ")

        displayed = app.get_current_result_text(record)
        self.assertEqual(displayed, "최신 CAP 결과")
        app.copy_text_to_clipboard.assert_called_once_with(displayed)

    def test_current_ocr_success_copies_exact_saved_display_result(self):
        record = {
            "record_id": "ocr-copy",
            "mode": "ocr",
            "ocr_text": "OCR 원문",
            "ocr_corrected_text": "기존 보정 결과",
        }
        job = begin_reanalysis(record, self.workspace, "ocr-copy-job")
        app = self.make_running_app(record)
        app.copy_text_to_clipboard = Mock(return_value=True)

        app._complete_result_reanalysis(job, "  최신 OCR 결과  ")

        displayed = app.get_current_result_text(record)
        self.assertEqual(displayed, "최신 OCR 결과")
        app.copy_text_to_clipboard.assert_called_once_with(displayed)

    def test_same_lesson_selection_change_does_not_refresh_or_copy_late_result(self):
        target = {
            "record_id": "target-cap",
            "mode": "capture",
            "cap_text": "기존 대상 결과",
        }
        current = {
            "record_id": "current-cap",
            "mode": "capture",
            "cap_text": "현재 보고 있는 결과",
        }
        job = begin_reanalysis(target, self.workspace, "late-cap-job")
        app = self.make_running_app(target)
        app.capture_records = [target, current]
        app.current_record_index = 1
        app.copy_text_to_clipboard = Mock(return_value=True)

        app._complete_result_reanalysis(job, "늦게 완료된 대상 결과")

        self.assertEqual(current_result(target), "늦게 완료된 대상 결과")
        self.assertEqual(current_result(current), "현재 보고 있는 결과")
        app.refresh_current_preview.assert_not_called()
        app.update_ocr_panel.assert_not_called()
        app.copy_text_to_clipboard.assert_not_called()

    def test_api_failure_keeps_result_and_clipboard_unchanged(self):
        record = {
            "record_id": "cap-api-fail",
            "mode": "capture",
            "cap_text": "기존 정상 결과",
        }
        job = begin_reanalysis(record, self.workspace, "cap-api-fail-job")
        app = self.make_running_app(record)
        app.copy_text_to_clipboard = Mock(return_value=True)

        app._complete_result_reanalysis(job, "CAP 분석 실패\n\nHTTP 503")

        self.assertEqual(current_result(record), "기존 정상 결과")
        app.copy_text_to_clipboard.assert_not_called()

    def test_empty_response_keeps_result_and_clipboard_unchanged(self):
        record = {
            "record_id": "cap-empty",
            "mode": "capture",
            "cap_text": "기존 정상 결과",
        }
        job = begin_reanalysis(record, self.workspace, "cap-empty-job")
        app = self.make_running_app(record)

        app._complete_result_reanalysis(job, "   ")

        self.assertEqual(current_result(record), "기존 정상 결과")
        app.copy_text_to_clipboard.assert_not_called()

    def test_clipboard_failure_does_not_roll_back_saved_result(self):
        record = {
            "record_id": "cap-clipboard-fail",
            "mode": "capture",
            "cap_text": "기존 정상 결과",
        }
        job = begin_reanalysis(record, self.workspace, "cap-clipboard-fail-job")
        app = self.make_running_app(record)
        app.copy_text_to_clipboard = Mock(return_value=False)

        app._complete_result_reanalysis(job, "저장된 새 결과")

        self.assertEqual(current_result(record), "저장된 새 결과")
        app.copy_text_to_clipboard.assert_called_once_with("저장된 새 결과")
        self.assertIn("클립보드 복사에 실패", app.set_status.call_args.args[0])

    def test_save_failure_after_selection_change_does_not_refresh_current_capture(self):
        target = {
            "record_id": "save-target",
            "mode": "capture",
            "cap_text": "기존 대상 결과",
        }
        current = {
            "record_id": "save-current",
            "mode": "capture",
            "cap_text": "현재 보고 있는 결과",
        }
        job = begin_reanalysis(target, self.workspace, "save-target-job")
        app = self.make_running_app(target)
        app.capture_records = [target, current]
        app.current_record_index = 1
        app.save_records = Mock(side_effect=OSError("disk unavailable"))

        app._complete_result_reanalysis(job, "저장되면 안 되는 결과")

        self.assertEqual(current_result(target), "기존 대상 결과")
        self.assertEqual(current_result(current), "현재 보고 있는 결과")
        app.refresh_current_preview.assert_not_called()
        app.update_ocr_panel.assert_not_called()
        app.set_status.assert_not_called()
        app.copy_text_to_clipboard.assert_not_called()

    def test_save_failure_rolls_back_new_result_and_reenables_retry_state(self):
        record = {
            "record_id": "save-fail",
            "mode": "capture",
            "cap_text": "기존 정상 결과",
        }
        job = begin_reanalysis(
            record,
            workspace=self.workspace,
            job_id="save-fail-job",
            started_at="2026-07-28 15:00:00",
        )
        app = self.make_running_app(record)
        app.save_records = Mock(side_effect=OSError("disk unavailable"))
        app.copy_text_to_clipboard = Mock(return_value=True)

        app._complete_result_reanalysis(job, "저장되면 안 되는 새 결과")

        self.assertEqual(record["cap_text"], "기존 정상 결과")
        self.assertNotIn("cap_text_edited", record)
        self.assertEqual(record["result_reanalysis_status"], "failed")
        app.set_status.assert_called_once()
        app.copy_text_to_clipboard.assert_not_called()

    @patch("app.messagebox.showinfo")
    @patch("app.messagebox.askyesno")
    def test_previous_result_restore_is_one_click_and_modal_free(self, askyesno, showinfo):
        record = {
            "record_id": "restore-ui",
            "mode": "capture",
            "cap_text": "최초 결과",
            "cap_text_previous": "직전 결과",
            "cap_text_edited": "현재 결과",
        }
        app = self.make_running_app(record)

        self.assertTrue(app.restore_previous_reanalysis_result())

        self.assertEqual(current_result(record), "직전 결과")
        app.save_records.assert_called_once()
        app.rebuild_outputs_from_records.assert_called_once_with(save_records=False)
        askyesno.assert_not_called()
        showinfo.assert_not_called()


class ImmediateThread:
    def __init__(self, target, daemon=True):
        self.target = target

    def start(self):
        self.target()


class ResultReanalysisRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.image_path = self.workspace / "capture.png"
        self.image_path.write_bytes(b"image")

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_app(self, record):
        app = ClassFlowAIApp.__new__(ClassFlowAIApp)
        app.workspace = self.workspace
        app.paths = {"records": self.workspace / "records.json"}
        app.capture_records = [record]
        app.current_record_index = 0
        app.config = {"cap_reasoning_model": "test/model"}
        app.root = Mock()
        app.root.after.side_effect = lambda _delay, callback: callback()
        app.has_nvidia_api_key = Mock(return_value=True)
        app.save_records = Mock()
        app.refresh_current_preview = Mock()
        app.update_ocr_panel = Mock()
        app.update_result_action_buttons = Mock()
        app.set_status = Mock()
        app._complete_result_reanalysis = Mock()
        return app

    @patch("app.threading.Thread", ImmediateThread)
    @patch("app.analyze_capture_image")
    @patch("app.correct_ocr_with_image", return_value="OCR 최신 결과")
    def test_ocr_reanalysis_uses_ocr_correction_path_and_latest_result(
        self,
        correct_ocr,
        analyze_cap,
    ):
        record = {
            "record_id": "ocr-route",
            "mode": "ocr",
            "image_path": str(self.image_path),
            "ocr_text": "최초 OCR",
            "ocr_corrected_text": "직전 OCR 결과",
        }
        app = self.make_app(record)

        self.assertTrue(app.start_result_reanalysis(record))

        self.assertEqual(correct_ocr.call_args.kwargs["ocr_text"], "직전 OCR 결과")
        analyze_cap.assert_not_called()
        completed_job = app._complete_result_reanalysis.call_args.args[0]
        self.assertEqual(completed_job["result_type"], "ocr")

    @patch("app.threading.Thread", ImmediateThread)
    @patch("app.correct_ocr_with_image")
    @patch("app.analyze_capture_image", return_value="CAP 최신 결과")
    def test_cap_reanalysis_uses_cap_path_with_current_result(
        self,
        analyze_cap,
        correct_ocr,
    ):
        record = {
            "record_id": "cap-route",
            "mode": "capture",
            "image_path": str(self.image_path),
            "cap_text": "최초 CAP 결과",
            "cap_text_edited": "직전 CAP 결과",
        }
        app = self.make_app(record)

        self.assertTrue(app.start_result_reanalysis(record))

        self.assertEqual(analyze_cap.call_args.kwargs["current_result"], "직전 CAP 결과")
        correct_ocr.assert_not_called()
        completed_job = app._complete_result_reanalysis.call_args.args[0]
        self.assertEqual(completed_job["result_type"], "cap")

    def test_existing_ocr_correction_is_blocked_during_result_reanalysis(self):
        record = {
            "record_id": "ocr-blocked",
            "mode": "ocr",
            "image_path": str(self.image_path),
            "ocr_text": "OCR 결과",
            "status": "ocr_corrected",
            "result_reanalysis_status": "running",
        }
        app = self.make_app(record)
        app.start_execution_timer = Mock()
        app.update_mini_status = Mock()
        app.update_counter = Mock()

        self.assertFalse(app.run_ocr_correction_for_record_async(record))

        app.start_execution_timer.assert_not_called()

    def test_abandoned_flow_job_is_marked_for_refresh_in_original_workspace(self):
        original = self.workspace / "original lesson"
        current = self.workspace / "current lesson"
        (original / "state").mkdir(parents=True)
        (current / "state").mkdir(parents=True)
        record = {
            "record_id": "ocr-flow",
            "mode": "ocr",
            "ocr_text": "최신 OCR",
            "flow_interpretation_status": "queued",
        }
        records_path = original / "state" / "capture_records.json"
        records_path.write_text(json.dumps([record], ensure_ascii=False), encoding="utf-8")
        app = self.make_app({"record_id": "current", "mode": "capture", "cap_text": "current"})
        app.workspace = current

        app._defer_flow_interpretation_for_workspace(original, "ocr-flow")

        saved = json.loads(records_path.read_text(encoding="utf-8"))[0]
        self.assertNotIn("flow_interpretation_status", saved)
        self.assertTrue(saved["flow_interpretation_needs_refresh"])


if __name__ == "__main__":
    unittest.main()
