import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from PIL import Image

from modules.nvidia_cap_reasoner import analyze_capture_image
from modules.ocr_engine import extract_text_from_image


def response(status_code: int, text: str = "", payload: dict | None = None):
    return SimpleNamespace(
        status_code=status_code,
        text=text,
        json=lambda: payload or {},
    )


class ModelConnectionErrorPathTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.image_path = Path(self.temp_dir.name) / "connection.png"
        Image.new("RGB", (32, 32), "white").save(self.image_path)
        self.ocr_config = {
            "nvidia_api_key": "test-only-key",
            "nvidia_api_base": "https://example.test/ocr",
            "nvidia_ocr_model": "nvidia/nemotron-ocr-v2",
            "nvidia_ocr_timeout_sec": 1,
            "ocr_upscale_enabled": False,
            "ocr_preprocess_mode": "none",
            "ocr_post_cleanup_enabled": False,
        }
        self.cap_config = {
            "nvidia_api_key": "test-only-key",
            "cap_reasoning_model": "qwen/qwen3.5-397b-a17b",
            "cap_reasoning_api_base": "https://example.test/cap",
            "cap_reasoning_connect_timeout_sec": 1,
            "cap_reasoning_timeout_sec": 1,
            "cap_reasoning_retry_count": 1,
            "cap_reasoning_max_tokens": 64,
            "cap_reasoning_max_long_side": 64,
            "cap_reasoning_prompt": "test",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("requests.post")
    def test_ocr_authentication_and_model_errors(self, post):
        post.return_value = response(401, "unauthorized")
        auth_result = extract_text_from_image(self.image_path, self.ocr_config)
        self.assertIn("인증에 실패", auth_result)
        self.assertEqual(post.call_count, 1)

        post.reset_mock()
        post.return_value = response(400, "invalid model")
        model_result = extract_text_from_image(self.image_path, self.ocr_config)
        self.assertIn("OCR API가 오류", model_result)
        self.assertEqual(post.call_count, 1)

    @patch("modules.nvidia_cap_reasoner.time.sleep")
    @patch("requests.post")
    def test_cap_timeout_retries_then_reports_timeout(self, post, _sleep):
        post.side_effect = [
            requests.exceptions.Timeout("slow"),
            requests.exceptions.Timeout("slow"),
        ]
        result = analyze_capture_image(self.image_path, self.cap_config)
        self.assertIn("시간이 초과", result)
        self.assertEqual(post.call_count, 2)

    @patch("modules.nvidia_cap_reasoner.time.sleep")
    @patch("requests.post")
    def test_cap_transient_server_error_retries_successfully(self, post, _sleep):
        post.side_effect = [
            response(500, "temporary"),
            response(
                200,
                payload={"choices": [{"message": {"content": "retry success"}}]},
            ),
        ]
        result = analyze_capture_image(self.image_path, self.cap_config)
        self.assertEqual(result, "retry success")
        self.assertEqual(post.call_count, 2)

    @patch("requests.post")
    def test_cap_authentication_and_model_errors(self, post):
        post.return_value = response(401, "unauthorized")
        auth_result = analyze_capture_image(self.image_path, self.cap_config)
        self.assertIn("인증에 실패", auth_result)
        self.assertEqual(post.call_count, 1)

        post.reset_mock()
        self.cap_config["cap_reasoning_retry_count"] = 0
        post.return_value = response(400, "invalid model")
        model_result = analyze_capture_image(self.image_path, self.cap_config)
        self.assertIn("API가 오류", model_result)
        self.assertEqual(post.call_count, 1)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(
            payload.get("chat_template_kwargs"),
            {"enable_thinking": False},
        )


if __name__ == "__main__":
    unittest.main()
