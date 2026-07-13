import re
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from modules.chatgpt_handoff_exporter import (
    build_chatgpt_prompt,
    export_chatgpt_handoff_zip,
)


EXPECTED_PACKAGE_FILES = (
    "notion_ready.html",
    "notion_ready.md",
    "COPY_TO_NOTION.bat",
    "copy_to_notion.py",
    "README.txt",
)


class NotionPackagePromptTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "한글 사용자" / "공백 폴더"
        self.root.mkdir(parents=True)
        self.image_path = self.root / "capture.png"
        Image.new("RGB", (16, 16), "white").save(self.image_path)
        self.records = [
            {
                "record_id": "capture",
                "image_path": str(self.image_path),
                "created_at": "2026-07-13 10:00:00",
                "display_order": 0,
            }
        ]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_exported_gpt_zip_contains_windows_safe_package_rules(self):
        zip_path, _ = export_chatgpt_handoff_zip(self.records, self.root / "GPT 전달 ZIP")
        extract_dir = self.root / "압축 해제 결과"
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extract_dir)
        prompt = (extract_dir / "PROMPT_FOR_CHATGPT.txt").read_text(encoding="utf-8")
        guide = (extract_dir / "CAPTURE_FIRST_GUIDE.md").read_text(encoding="utf-8")

        for filename in EXPECTED_PACKAGE_FILES:
            self.assertIn(filename, prompt)
            self.assertIn(filename, guide)
        self.assertNotIn("COPY_TO_CLIPBOARD.bat", prompt)
        self.assertNotIn("copy_to_clipboard.py", prompt)
        self.assertNotRegex(prompt, r"[A-Za-z]:\\Users\\")

    def test_bat_and_python_names_and_relative_path_rules_match(self):
        prompt = build_chatgpt_prompt()
        bat_match = re.search(r"```bat\n(.*?)\n```", prompt, flags=re.S)
        self.assertIsNotNone(bat_match)
        bat = bat_match.group(1)
        self.assertIn('cd /d "%~dp0"', bat)
        self.assertIn('py -3 "%~dp0copy_to_notion.py"', bat)
        self.assertIn('python "%~dp0copy_to_notion.py"', bat)
        self.assertIn("exit /b %EXIT_CODE%", bat)
        self.assertNotIn("copy_to_clipboard.py", bat)

        self.assertIn("BASE_DIR = Path(__file__).resolve().parent", prompt)
        self.assertIn('HTML_PATH = BASE_DIR / "notion_ready.html"', prompt)
        self.assertIn("CF_HTML", prompt)
        self.assertIn("finally", prompt)

    def test_python_template_requires_64_bit_safe_win32_signatures(self):
        prompt = build_chatgpt_prompt()
        self.assertIn('ctypes.WinDLL("kernel32", use_last_error=True)', prompt)
        self.assertIn("GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]", prompt)
        self.assertIn("GlobalAlloc.restype = wintypes.HANDLE", prompt)
        self.assertIn("GlobalLock.argtypes = [wintypes.HANDLE]", prompt)
        self.assertIn("GlobalLock.restype = ctypes.c_void_p", prompt)
        self.assertIn("SetClipboardData.restype = wintypes.HANDLE", prompt)
        self.assertIn("GlobalLock failed", prompt)
        self.assertIn("실패한 경우에만 `GlobalFree`", prompt)

    def test_custom_legacy_prompt_is_normalized_and_mandatory_rules_remain(self):
        prompt = build_chatgpt_prompt(
            prompt_template="Run COPY_TO_CLIPBOARD.bat and copy_to_clipboard.py"
        )
        self.assertNotIn("COPY_TO_CLIPBOARD.bat", prompt)
        self.assertNotIn("copy_to_clipboard.py", prompt)
        self.assertIn("COPY_TO_NOTION.bat", prompt)
        self.assertIn("copy_to_notion.py", prompt)
        for filename in EXPECTED_PACKAGE_FILES:
            self.assertIn(filename, prompt)


if __name__ == "__main__":
    unittest.main()
