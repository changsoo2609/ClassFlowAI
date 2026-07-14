import unittest
from pathlib import Path


RUNTIME = Path(__file__).resolve().parents[1]


class RemovedFeatureTests(unittest.TestCase):
    def test_learning_card_runtime_modules_are_removed(self):
        removed = [
            "study_answer_evaluator.py",
            "study_card_importer.py",
            "study_card_review.py",
            "study_card_validator.py",
            "study_review_scheduler.py",
            "study_review_window.py",
        ]
        for name in removed:
            self.assertFalse((RUNTIME / "modules" / name).exists(), name)
        self.assertFalse((RUNTIME / "validate_study_cards.py").exists())

    def test_app_does_not_import_learning_card_runtime(self):
        source = (RUNTIME / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("modules.study_", source)
        self.assertNotIn("remove_capture_references", source)

    def test_whole_document_copy_is_not_present(self):
        source = (RUNTIME / "modules" / "flow_window.py").read_text(encoding="utf-8")
        self.assertNotIn("copy_all", source)
        self.assertNotIn("flow_clipboard", source)
        self.assertNotIn("전체 복사하기", source)


if __name__ == "__main__":
    unittest.main()
