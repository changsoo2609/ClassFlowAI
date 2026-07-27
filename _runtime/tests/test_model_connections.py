import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from PIL import Image

import modules.nvidia_cap_reasoner as cap_reasoner
from modules.nvidia_cap_reasoner import (
    DEFAULT_CAP_MODEL,
    RETIRED_QWEN_CAP_MODEL,
    analyze_capture_image,
)
from modules.ocr_engine import extract_text_from_image


def response(
    status_code: int,
    text: str = "",
    payload: dict | None = None,
    headers: dict | None = None,
):
    return SimpleNamespace(
        status_code=status_code,
        text=text,
        headers=headers or {},
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
            "cap_reasoning_model": DEFAULT_CAP_MODEL,
            "cap_reasoning_api_base": "https://example.test/cap",
            "cap_reasoning_connect_timeout_sec": 1,
            "cap_reasoning_timeout_sec": 1,
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

    @patch("modules.model_retry.time.sleep")
    @patch("requests.post")
    def test_cap_timeout_retries_then_reports_timeout(self, post, _sleep):
        post.side_effect = [
            requests.exceptions.Timeout("slow"),
            requests.exceptions.Timeout("slow"),
        ]
        result = analyze_capture_image(self.image_path, self.cap_config)
        self.assertIn("시간이 초과", result)
        self.assertEqual(post.call_count, 2)

    @patch("modules.model_retry.time.sleep")
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
        post.return_value = response(400, "invalid model")
        model_result = analyze_capture_image(self.image_path, self.cap_config)
        self.assertIn("API가 오류", model_result)
        self.assertEqual(post.call_count, 1)
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("chat_template_kwargs", payload)

    @patch("requests.post")
    def test_cap_http_410_reports_unavailable_model_without_retry(self, post):
        post.return_value = response(
            410,
            payload={
                "status": 410,
                "title": "Gone",
                "detail": (
                    "The model 'qwen/qwen3.5-397b-a17b' has reached its end of life "
                    "and is no longer available."
                ),
            },
            headers={
                "Content-Type": "application/problem+json",
                "NVCF-REQID": "req-test-410",
            },
        )
        diagnostic_log = Path(self.temp_dir.name) / "model_request_diagnostics.jsonl"

        with patch.object(
            cap_reasoner,
            "MODEL_DIAGNOSTIC_LOG_PATH",
            diagnostic_log,
            create=True,
        ):
            result = analyze_capture_image(
                self.image_path,
                dict(self.cap_config, cap_reasoning_model=RETIRED_QWEN_CAP_MODEL),
            )

        self.assertIn("현재 NVIDIA API 키로 사용할 수 없습니다", result)
        self.assertIn("HTTP 410", result)
        self.assertIn(RETIRED_QWEN_CAP_MODEL, result)
        self.assertEqual(post.call_count, 1)

        entries = [
            json.loads(line)
            for line in diagnostic_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([entry["event"] for entry in entries], ["request", "http_error"])
        request_entry, error_entry = entries
        self.assertEqual(request_entry["endpoint"], self.cap_config["cap_reasoning_api_base"])
        self.assertEqual(request_entry["model_repr"], repr(RETIRED_QWEN_CAP_MODEL))
        self.assertEqual(request_entry["model_length"], len(RETIRED_QWEN_CAP_MODEL))
        self.assertEqual(request_entry["message_roles"], ["user"])
        self.assertEqual(request_entry["message_content_types"], [["text", "image_url"]])
        self.assertTrue(request_entry["has_image_url"])
        self.assertEqual(request_entry["data_url_mime_type"], "image/png")
        self.assertGreater(request_entry["image_bytes"], 0)
        self.assertEqual(
            request_entry["payload_keys"],
            sorted(post.call_args.kwargs["json"].keys()),
        )
        self.assertEqual(request_entry["timeout"], [1, 1])
        self.assertIn("request_at", request_entry)
        self.assertEqual(error_entry["status_code"], 410)
        self.assertEqual(error_entry["content_type"], "application/problem+json")
        self.assertEqual(error_entry["error_code"], "410")
        self.assertIn("has reached its end of life", error_entry["error_message"])
        self.assertEqual(error_entry["request_id"], "req-test-410")

        logged = diagnostic_log.read_text(encoding="utf-8")
        self.assertNotIn(self.cap_config["nvidia_api_key"], logged)
        self.assertNotIn("data:image/png;base64", logged)

    @patch("requests.post")
    def test_cap_default_config_uses_current_default_vlm(self, post):
        post.return_value = response(
            200,
            payload={"choices": [{"message": {"content": "ok"}}]},
        )
        config = dict(self.cap_config)
        config.pop("cap_reasoning_model")

        self.assertEqual(analyze_capture_image(self.image_path, config), "ok")
        self.assertEqual(post.call_args.kwargs["json"]["model"], DEFAULT_CAP_MODEL)
        self.assertEqual(DEFAULT_CAP_MODEL, "google/diffusiongemma-26b-a4b-it")

    @patch("requests.post")
    def test_cap_selected_qwen_and_custom_models_reach_payload_unchanged(self, post):
        post.return_value = response(
            200,
            payload={"choices": [{"message": {"content": "ok"}}]},
        )
        for model in (
            "qwen/qwen3.5-397b-a17b",
            "custom/provider-model",
        ):
            with self.subTest(model=model):
                config = dict(self.cap_config, cap_reasoning_model=model)
                self.assertEqual(analyze_capture_image(self.image_path, config), "ok")
                self.assertEqual(post.call_args.kwargs["json"]["model"], model)

    @patch("requests.post")
    def test_fast_default_request_exception_does_not_fallback(self, post):
        post.side_effect = requests.exceptions.RequestException("permanent")
        config = dict(self.cap_config, cap_reasoning_model=DEFAULT_CAP_MODEL)

        result = analyze_capture_image(self.image_path, config)

        self.assertIn("요청에 실패", result)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["model"], DEFAULT_CAP_MODEL)


if __name__ == "__main__":
    unittest.main()
