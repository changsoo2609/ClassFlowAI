import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from app import ClassFlowAIApp, gpt_zip_user_error_message
from modules.chatgpt_handoff_exporter import build_chatgpt_prompt, export_chatgpt_handoff_zip


class _ImmediateThread:
    def __init__(self, target, daemon=False):
        self.target = target

    def start(self):
        self.target()


class _Root:
    def __init__(self):
        self.alive = True
        self.destroy_count = 0
        self.cursor_history = []

    def winfo_exists(self):
        return self.alive

    def after(self, _delay, callback):
        callback()

    def config(self, **kwargs):
        if "cursor" in kwargs:
            self.cursor_history.append(kwargs["cursor"])

    def destroy(self):
        self.destroy_count += 1
        self.alive = False


class _Button:
    def __init__(self):
        self.states = []

    def config(self, **kwargs):
        if "state" in kwargs:
            self.states.append(kwargs["state"])


class _Var:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value


class _Widget:
    def config(self, **_kwargs):
        return None


class GptZipExporterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "한글 상위 폴더" / "공백 포함"
        self.root.mkdir(parents=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _image(self, name, color="blue"):
        path = self.root / name
        Image.new("RGB", (12, 12), color).save(path)
        return path

    def test_normal_zip_is_utf8_valid_portable_and_display_ordered(self):
        first = self._image("첫 캡처.png", "red")
        second = self._image("second.png", "green")
        records = [
            {"record_id": "first", "image_path": str(first), "created_at": "2026-01-01 10:00:00", "mode": "ocr", "ocr_text": "한글 OCR", "display_order": 1},
            {"record_id": "second", "image_path": str(second), "created_at": "2026-01-01 10:01:00", "mode": "capture", "cap_text": "CAP 결과", "display_order": 0},
        ]

        zip_path, prompt = export_chatgpt_handoff_zip(records, self.root / "ZIP 결과")

        with zipfile.ZipFile(zip_path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertTrue(all("\\" not in name and ":" not in name and not name.startswith("/") for name in archive.namelist()))
            timeline = archive.read("CAPTURE_TIMELINE.md").decode("utf-8")
            preview = archive.read("html_flow_preview.html").decode("utf-8")
            stored_prompt = archive.read("PROMPT_FOR_CHATGPT.txt").decode("utf-8")
            self.assertLess(timeline.index("10:01:00"), timeline.index("10:00:00"))
            self.assertIn('src="images/capture_001.png"', preview)
            self.assertNotIn("data:image", preview)
            self.assertEqual(stored_prompt.replace("\r\n", "\n"), prompt.replace("\r\n", "\n"))

    def test_empty_and_missing_images_leave_a_valid_zip_without_images(self):
        missing = self.root / "missing.png"
        for records in ([], [{"record_id": "missing", "image_path": str(missing)}]):
            zip_path, _ = export_chatgpt_handoff_zip(records, self.root / "empty")
            with zipfile.ZipFile(zip_path) as archive:
                self.assertIsNone(archive.testzip())
                self.assertFalse(any(name.startswith("images/") for name in archive.namelist()))

    def test_failed_atomic_replace_preserves_existing_zip_and_cleans_temporary_files(self):
        image = self._image("one.png")
        out_dir = self.root / "locked"
        destination = []

        def fail_replace(_source, target):
            target = Path(target)
            target.write_bytes(b"existing-good-zip")
            destination.append(target)
            raise PermissionError("locked")

        with patch("modules.chatgpt_handoff_exporter.os.replace", side_effect=fail_replace):
            with self.assertRaises(PermissionError):
                export_chatgpt_handoff_zip([{"image_path": str(image)}], out_dir)

        self.assertEqual(destination[0].read_bytes(), b"existing-good-zip")
        self.assertEqual(list(out_dir.glob("*.tmp")), [])
        self.assertEqual([path for path in out_dir.iterdir() if path.is_dir()], [])

    def test_prompt_special_characters_are_not_interpreted_as_a_format_template(self):
        template = r'BAT %~dp0, Python {value}, backslash C:\\not-a-user-path'
        prompt = build_chatgpt_prompt(prompt_template=template)
        self.assertIn("%~dp0", prompt)
        self.assertIn("{value}", prompt)
        self.assertIn(r"C:\\not-a-user-path", prompt)


class GptZipUiBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_dir = Path(self.temp_dir.name)
        self.image = self.root_dir / "capture.png"
        Image.new("RGB", (8, 8), "blue").save(self.image)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _app(self):
        instance = ClassFlowAIApp.__new__(ClassFlowAIApp)
        instance.root = _Root()
        instance.gpt_zip_button = _Button()
        instance.closing = False
        instance.gpt_zip_generation_active = False
        instance.processing_state = "OCR 완료"
        instance.gpt_zip_previous_processing_state = instance.processing_state
        instance.pending_capture_updates = 0
        instance.execution_started_at = None
        instance.lesson_switch_lock = threading.RLock()
        instance.capture_records = [{"record_id": "one", "image_path": str(self.image), "created_at": "2026-01-01 10:00:00"}]
        instance.config = {"html_flow_subject": "테스트", "html_flow_prompt_template": ""}
        instance.paths = {"gpt_handoff": self.root_dir / "exports", "events": self.root_dir / "events.jsonl"}
        instance.set_status = Mock()
        instance.update_mini_status = Mock()
        instance.copy_text_to_clipboard = Mock(return_value=True)
        instance._ensure_records_for_capture_files = Mock()
        instance.run_nvidia_ocr_for_zip = Mock()
        instance.rebuild_outputs_from_records = Mock()
        instance.on_close = Mock()
        return instance

    def test_success_restores_state_and_allows_two_sequential_generations(self):
        instance = self._app()
        zip_path = self.root_dir / "exports" / "result.zip"
        with patch("app.threading.Thread", _ImmediateThread), patch(
            "app.export_chatgpt_handoff_zip", return_value=(zip_path, "prompt")
        ) as exporter, patch("app.messagebox.showinfo"), patch("app.os.startfile", create=True):
            instance.export_chatgpt_handoff_zip_ui()
            instance.export_chatgpt_handoff_zip_ui()

        self.assertEqual(exporter.call_count, 2)
        self.assertFalse(instance.gpt_zip_generation_active)
        self.assertEqual(instance.processing_state, "OCR 완료")
        self.assertEqual(instance.gpt_zip_button.states, ["disabled", "normal", "disabled", "normal"])
        self.assertEqual(instance.root.cursor_history, ["watch", "", "watch", ""])
        self.assertTrue(instance.root.alive)
        self.assertEqual(instance.root.destroy_count, 0)
        instance.on_close.assert_not_called()

    def test_worker_failure_logs_traceback_but_keeps_app_and_retry_ready(self):
        instance = self._app()
        with patch("app.threading.Thread", _ImmediateThread), patch(
            "app.export_chatgpt_handoff_zip", side_effect=PermissionError("private path must not be shown")
        ), patch("app.messagebox.showerror") as showerror:
            instance.export_chatgpt_handoff_zip_ui()

        self.assertFalse(instance.gpt_zip_generation_active)
        self.assertEqual(instance.processing_state, "OCR 완료")
        self.assertEqual(instance.gpt_zip_button.states[-1], "normal")
        self.assertTrue(instance.root.alive)
        self.assertEqual(instance.root.destroy_count, 0)
        instance.on_close.assert_not_called()
        showerror.assert_called_once()
        self.assertNotIn("private path", showerror.call_args.args[1])
        event = json.loads(instance.paths["events"].read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(event["type"], "gpt_zip_generation_failed")
        self.assertEqual(event["error_type"], "PermissionError")
        self.assertIn("Traceback", event["traceback"])

    def test_duplicate_click_is_ignored_without_starting_another_worker(self):
        instance = self._app()
        instance.gpt_zip_generation_active = True
        with patch("app.threading.Thread") as thread:
            instance.export_chatgpt_handoff_zip_ui()
        thread.assert_not_called()
        instance.set_status.assert_called_once_with("GPT ZIP을 이미 생성 중입니다.")

    def test_thread_start_failure_is_caught_and_restores_ui(self):
        instance = self._app()
        with patch("app.threading.Thread", side_effect=RuntimeError("thread unavailable")), patch(
            "app.messagebox.showerror"
        ) as showerror:
            instance.export_chatgpt_handoff_zip_ui()
        showerror.assert_called_once()
        self.assertFalse(instance.gpt_zip_generation_active)
        self.assertEqual(instance.processing_state, "OCR 완료")
        self.assertEqual(instance.gpt_zip_button.states[-1], "normal")
        self.assertTrue(instance.root.alive)

    def test_no_capture_and_missing_capture_restore_ui_without_closing(self):
        for records in ([], [{"record_id": "missing", "image_path": str(self.root_dir / "missing.png")}]):
            instance = self._app()
            instance.capture_records = records
            with patch("app.threading.Thread", _ImmediateThread), patch("app.messagebox.showwarning") as warning:
                instance.export_chatgpt_handoff_zip_ui()
            warning.assert_called_once()
            self.assertFalse(instance.gpt_zip_generation_active)
            self.assertTrue(instance.root.alive)
            self.assertEqual(instance.gpt_zip_button.states[-1], "normal")

    def test_active_work_blocks_duplicate_generation_before_ui_state_changes(self):
        instance = self._app()
        instance.pending_capture_updates = 1
        with patch("app.threading.Thread") as thread:
            instance.export_chatgpt_handoff_zip_ui()
        thread.assert_not_called()
        self.assertFalse(instance.gpt_zip_generation_active)
        self.assertEqual(instance.gpt_zip_button.states, [])

    def test_mini_widget_shows_zip_during_work_and_restores_ocr_mode(self):
        instance = ClassFlowAIApp.__new__(ClassFlowAIApp)
        instance.mini_state_var = _Var()
        instance.mini_mode_var = _Var()
        instance.mini_frame = _Widget()
        instance.mini_mode_label = _Widget()
        instance.mini_state_label = _Widget()
        instance.paused = False
        instance.capture_mode = "ocr"
        instance.processing_state = "GPT ZIP 생성 중"
        instance.gpt_zip_generation_active = True

        instance.update_mini_status()
        self.assertEqual(instance.mini_mode_var.value, "ZIP")
        self.assertEqual(instance.mini_state_var.value, "진행")

        instance.gpt_zip_generation_active = False
        instance.processing_state = "OCR 완료"
        instance.update_mini_status()
        self.assertEqual(instance.mini_mode_var.value, "OCR")
        self.assertEqual(instance.mini_state_var.value, "완료")

    def test_user_error_categories_do_not_expose_exception_detail(self):
        locked = OSError("secret detail")
        locked.winerror = 32
        self.assertIn("다른 프로그램에서 사용 중", gpt_zip_user_error_message(locked))
        self.assertIn("저장 공간이 부족", gpt_zip_user_error_message(OSError(28, "secret")))
        self.assertNotIn("secret", gpt_zip_user_error_message(locked))


if __name__ == "__main__":
    unittest.main()
