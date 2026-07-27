import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from modules.nvidia_cap_reasoner import (
    DEFAULT_CAP_MODEL,
    DEFAULT_CAP_PROMPT,
    DEFAULT_FLOW_INTERPRETATION_PROMPT,
    DEFAULT_MANUAL_CAP_PROMPT,
    LEGACY_CAP_REPORT_PROMPT,
    LEGACY_SHARED_CAP_PROMPT,
    RETIRED_QWEN_CAP_MODEL,
    build_cap_prompt,
)


class CapPromptMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.user_config = self.root / "settings.json"
        self.user_secret = self.root / "secrets.json"
        self.packaged_config = self.root / "config.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _load(self, values: dict) -> dict:
        self.user_config.write_text(
            json.dumps(values, ensure_ascii=False),
            encoding="utf-8",
        )
        with (
            patch.object(app, "USER_CONFIG_PATH", self.user_config),
            patch.object(app, "USER_SECRET_PATH", self.user_secret),
            patch.object(app, "CONFIG_PATH", self.packaged_config),
        ):
            return app.load_config()

    def _save(self, values: dict) -> dict:
        with (
            patch.object(app, "USER_CONFIG_PATH", self.user_config),
            patch.object(app, "USER_SECRET_PATH", self.user_secret),
            patch.object(app, "CONFIG_PATH", self.packaged_config),
        ):
            app.save_config(values)
        return json.loads(self.user_config.read_text(encoding="utf-8"))

    def test_manual_and_flow_prompts_are_separate(self):
        self.assertEqual(DEFAULT_CAP_PROMPT, DEFAULT_MANUAL_CAP_PROMPT)
        self.assertNotEqual(DEFAULT_CAP_PROMPT, DEFAULT_FLOW_INTERPRETATION_PROMPT)

    def test_manual_cap_uses_user_prompt(self):
        self.assertEqual(
            build_cap_prompt({"cap_reasoning_prompt": "사용자 CAP 전용"}),
            "사용자 CAP 전용",
        )

    def test_legacy_report_prompt_is_replaced_for_existing_users(self):
        config = self._load({
            "settings_schema_version": 6,
            "cap_reasoning_prompt": LEGACY_CAP_REPORT_PROMPT,
        })
        self.assertEqual(config["cap_reasoning_prompt"], DEFAULT_MANUAL_CAP_PROMPT)
        saved = json.loads(self.user_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["settings_schema_version"], 10)
        self.assertEqual(saved["cap_reasoning_prompt"], DEFAULT_MANUAL_CAP_PROMPT)

    def test_legacy_shared_flow_prompt_is_replaced_for_existing_users(self):
        config = self._load({
            "settings_schema_version": 6,
            "cap_reasoning_prompt": LEGACY_SHARED_CAP_PROMPT,
        })
        self.assertEqual(config["cap_reasoning_prompt"], DEFAULT_MANUAL_CAP_PROMPT)
        saved = json.loads(self.user_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["settings_schema_version"], 10)

    def test_empty_prompt_uses_new_manual_default(self):
        config = self._load({
            "settings_schema_version": 6,
            "cap_reasoning_prompt": "",
        })
        self.assertEqual(config["cap_reasoning_prompt"], DEFAULT_MANUAL_CAP_PROMPT)
        saved = json.loads(self.user_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["cap_reasoning_prompt"], DEFAULT_MANUAL_CAP_PROMPT)

    def test_custom_prompt_is_preserved_during_schema_migration(self):
        config = self._load({
            "settings_schema_version": 6,
            "cap_reasoning_prompt": "내가 작성한 사용자 프롬프트",
        })
        self.assertEqual(config["cap_reasoning_prompt"], "내가 작성한 사용자 프롬프트")
        saved = json.loads(self.user_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["settings_schema_version"], 10)
        self.assertEqual(saved["cap_reasoning_prompt"], "내가 작성한 사용자 프롬프트")

    def test_legacy_prompt_with_user_added_newline_is_not_overwritten(self):
        customized = LEGACY_SHARED_CAP_PROMPT + "\n"
        config = self._load({
            "settings_schema_version": 6,
            "cap_reasoning_prompt": customized,
        })
        self.assertEqual(config["cap_reasoning_prompt"], customized)
        saved = json.loads(self.user_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["cap_reasoning_prompt"], customized)

    def test_schema_10_custom_prompt_is_idempotent(self):
        custom = "사용자 프롬프트\n줄바꿈 유지"
        first = self._load({
            "settings_schema_version": 10,
            "cap_reasoning_prompt": custom,
        })
        first_saved = self.user_config.read_text(encoding="utf-8")
        second = self._load(json.loads(first_saved))
        self.assertEqual(first["cap_reasoning_prompt"], custom)
        self.assertEqual(second["cap_reasoning_prompt"], custom)
        self.assertEqual(self.user_config.read_text(encoding="utf-8"), first_saved)

    def test_secret_api_key_is_unchanged_by_prompt_migration(self):
        self.user_secret.write_text(
            json.dumps({"nvidia_api_key": "test-secret"}),
            encoding="utf-8",
        )
        config = self._load({
            "settings_schema_version": 6,
            "cap_reasoning_prompt": LEGACY_SHARED_CAP_PROMPT,
        })
        self.assertEqual(config["nvidia_api_key"], "test-secret")
        self.assertEqual(
            json.loads(self.user_secret.read_text(encoding="utf-8")),
            {"nvidia_api_key": "test-secret"},
        )

    def test_schema_8_qwen_cap_model_migrates_to_fast_default(self):
        config = self._load({
            "settings_schema_version": 8,
            "cap_reasoning_model": RETIRED_QWEN_CAP_MODEL,
            "cap_reasoning_model_url": f"https://build.nvidia.com/{RETIRED_QWEN_CAP_MODEL}",
            "cap_reasoning_prompt": "사용자 프롬프트 유지",
        })

        self.assertEqual(config["cap_reasoning_model"], DEFAULT_CAP_MODEL)
        self.assertEqual(
            config["cap_reasoning_model_url"],
            f"https://build.nvidia.com/{DEFAULT_CAP_MODEL}",
        )
        saved = json.loads(self.user_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["settings_schema_version"], 10)
        self.assertEqual(saved["cap_reasoning_model"], DEFAULT_CAP_MODEL)
        self.assertEqual(saved["cap_reasoning_prompt"], "사용자 프롬프트 유지")

    def test_schema_8_custom_cap_model_is_preserved(self):
        custom_model = "meta/llama-3.2-90b-vision-instruct"
        config = self._load({
            "settings_schema_version": 8,
            "cap_reasoning_model": custom_model,
            "cap_reasoning_model_url": f"https://build.nvidia.com/{custom_model}",
        })

        self.assertEqual(config["cap_reasoning_model"], custom_model)
        saved = json.loads(self.user_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["settings_schema_version"], 10)
        self.assertEqual(saved["cap_reasoning_model"], custom_model)

    def test_diffusiongemma_is_the_fast_cap_default(self):
        self.assertEqual(DEFAULT_CAP_MODEL, "google/diffusiongemma-26b-a4b-it")

    def test_new_install_uses_fast_default(self):
        with (
            patch.object(app, "USER_CONFIG_PATH", self.user_config),
            patch.object(app, "USER_SECRET_PATH", self.user_secret),
            patch.object(app, "CONFIG_PATH", self.packaged_config),
        ):
            config = app.load_config()

        self.assertEqual(config["cap_reasoning_model"], DEFAULT_CAP_MODEL)
        self.assertEqual(
            config["cap_reasoning_model_url"],
            f"https://build.nvidia.com/{DEFAULT_CAP_MODEL}",
        )

    def test_missing_and_blank_models_use_fast_default(self):
        for values in (
            {"settings_schema_version": 8},
            {"settings_schema_version": 8, "cap_reasoning_model": "  \n"},
        ):
            with self.subTest(values=values):
                config = self._load(values)
                self.assertEqual(config["cap_reasoning_model"], DEFAULT_CAP_MODEL)
                saved = json.loads(self.user_config.read_text(encoding="utf-8"))
                self.assertEqual(saved["cap_reasoning_model"], DEFAULT_CAP_MODEL)
                self.assertEqual(saved["settings_schema_version"], 10)

    def test_schema_9_retired_qwen_migrates_to_fast_default(self):
        config = self._load({
            "settings_schema_version": 9,
            "cap_reasoning_model": RETIRED_QWEN_CAP_MODEL,
        })
        self.assertEqual(config["cap_reasoning_model"], DEFAULT_CAP_MODEL)
        self.assertEqual(config["settings_schema_version"], 10)
        saved = json.loads(self.user_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["settings_schema_version"], 10)
        self.assertEqual(saved["cap_reasoning_model"], DEFAULT_CAP_MODEL)

    def test_latest_schema_blank_model_uses_fast_default_without_rewriting_settings(self):
        values = {
            "settings_schema_version": 10,
            "cap_reasoning_model": "  ",
            "workspace_dir": "user-workspace",
        }
        config = self._load(values)

        self.assertEqual(config["cap_reasoning_model"], DEFAULT_CAP_MODEL)
        self.assertEqual(
            json.loads(self.user_config.read_text(encoding="utf-8")),
            values,
        )

    def test_latest_schema_custom_model_is_not_remigrated(self):
        values = {
            "settings_schema_version": 10,
            "cap_reasoning_model": "custom/provider-model",
            "workspace_dir": "user-workspace",
        }
        config = self._load(values)

        self.assertEqual(config["cap_reasoning_model"], "custom/provider-model")
        self.assertEqual(
            json.loads(self.user_config.read_text(encoding="utf-8")),
            values,
        )

    def test_fast_default_and_custom_models_round_trip_through_save_and_load(self):
        for model in (
            DEFAULT_CAP_MODEL,
            "custom/provider-model",
        ):
            with self.subTest(model=model):
                saved = self._save({
                    "settings_schema_version": 10,
                    "cap_reasoning_model": model,
                    "workspace_dir": "사용자 작업공간",
                    "nvidia_api_key": "test-secret",
                })
                self.assertEqual(saved["cap_reasoning_model"], model)
                self.assertEqual(saved["workspace_dir"], "사용자 작업공간")
                loaded = self._load(saved)
                self.assertEqual(loaded["cap_reasoning_model"], model)

    def test_ocr_default_is_unchanged(self):
        config = self._load({"settings_schema_version": 10})
        self.assertEqual(config["nvidia_ocr_model"], "nvidia/nemotron-ocr-v2")


if __name__ == "__main__":
    unittest.main()
