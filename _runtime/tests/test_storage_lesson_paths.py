import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from modules.storage import (
    create_lesson_workspace,
    is_lesson_workspace,
    short_workspace_display,
)


class LessonPathTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "실시간저장"
        self.now = datetime(2026, 7, 14, 12, 3, 19)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_new_lesson_uses_date_and_time_without_legacy_names(self):
        workspace = create_lesson_workspace(self.root, now=self.now)
        self.assertEqual(workspace, self.root / "2026-07-14" / "12-03-19")
        self.assertNotIn("lessons", workspace.parts)
        self.assertFalse(workspace.name.startswith("lesson_"))
        self.assertTrue(is_lesson_workspace(workspace))

    def test_existing_daily_root_does_not_duplicate_date(self):
        daily_root = self.root / "2026-07-14"
        workspace = create_lesson_workspace(daily_root, now=self.now)
        self.assertEqual(workspace, daily_root / "12-03-19")

    def test_same_second_uses_safe_suffix(self):
        first = create_lesson_workspace(self.root, now=self.now)
        second = create_lesson_workspace(self.root, now=self.now)
        self.assertEqual(first.name, "12-03-19")
        self.assertEqual(second.name, "12-03-19_2")

    def test_legacy_lesson_remains_openable(self):
        legacy = self.root / "2026-07-14" / "lessons" / "lesson_2026-07-14_12-03-19"
        (legacy / "captures").mkdir(parents=True)
        self.assertTrue(is_lesson_workspace(legacy))

    def test_short_display_starts_at_realtime_storage(self):
        workspace = self.root / "2026-07-14" / "12-03-19"
        self.assertEqual(
            short_workspace_display(workspace),
            str(Path("실시간저장") / "2026-07-14" / "12-03-19"),
        )


if __name__ == "__main__":
    unittest.main()
