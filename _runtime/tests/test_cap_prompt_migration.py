import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from modules.nvidia_cap_reasoner import (
    DEFAULT_CAP_PROMPT,
    LEGACY_CAP_REPORT_PROMPT,
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

    def test_legacy_report_prompt_is_replaced_for_existing_users(self):
        config = self._load({
            "settings_schema_version": 5,
            "cap_reasoning_prompt": LEGACY_CAP_REPORT_PROMPT,
        })
        self.assertEqual(config["cap_reasoning_prompt"], DEFAULT_CAP_PROMPT)
        saved = json.loads(self.user_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["settings_schema_version"], 6)
        self.assertEqual(saved["cap_reasoning_prompt"], DEFAULT_CAP_PROMPT)

    def test_custom_prompt_is_preserved_during_schema_migration(self):
        config = self._load({
            "settings_schema_version": 5,
            "cap_reasoning_prompt": "내가 작성한 사용자 프롬프트",
        })
        self.assertEqual(config["cap_reasoning_prompt"], "내가 작성한 사용자 프롬프트")
        saved = json.loads(self.user_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["settings_schema_version"], 6)
        self.assertEqual(saved["cap_reasoning_prompt"], "내가 작성한 사용자 프롬프트")


if __name__ == "__main__":
    unittest.main()
