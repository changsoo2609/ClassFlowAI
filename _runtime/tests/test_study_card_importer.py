import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from modules.study_card_importer import (
    import_study_cards,
    load_local_cards,
    save_local_cards,
)


def valid_card(
    card_id="card-001",
    question="파이썬 리스트 컴프리헨션의 장점은 무엇인가요?",
    topic="파이썬 리스트 컴프리헨션",
    image_name="evidence.png",
):
    return {
        "card_id": card_id,
        "source_type": "concept",
        "card_type": "active_recall",
        "topic": topic,
        "question": question,
        "choices": [],
        "answer": "반복문을 간결한 표현식으로 작성할 수 있습니다.",
        "key_points": ["간결성", "새 리스트 생성"],
        "explanation": "반복과 조건을 한 표현식으로 결합합니다.",
        "answer_status": "confirmed_from_source",
        "source_images": [image_name],
        "tags": ["python"],
        "difficulty": 2,
        "review_required": False,
    }


def payload_with(*cards, subject="프로그래밍"):
    return {
        "schema_version": 1,
        "subject": subject,
        "generated_at": "2026-07-12T20:00:00+09:00",
        "cards": list(cards),
    }


class StudyCardImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_json_source(
        self,
        folder_name="source",
        payload=None,
        image_name="evidence.png",
        image_data=b"image-v1",
    ):
        source_dir = self.root / folder_name
        images_dir = source_dir / "images"
        images_dir.mkdir(parents=True)
        (images_dir / image_name).write_bytes(image_data)
        json_path = source_dir / "study_cards.json"
        json_path.write_text(
            json.dumps(payload or payload_with(valid_card()), ensure_ascii=False),
            encoding="utf-8",
        )
        return json_path

    def _write_zip(self, payload=None, extra_entries=None):
        zip_path = self.root / "notion_paste_package.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "study_cards.json",
                json.dumps(payload or payload_with(valid_card()), ensure_ascii=False),
            )
            archive.writestr("images/evidence.png", b"zip-image")
            for name, data in extra_entries or []:
                archive.writestr(name, data)
        return zip_path

    def test_import_valid_json(self):
        source = self._write_json_source()
        report = import_study_cards(source, self.workspace)

        self.assertTrue(report["success"])
        self.assertEqual(report["read_count"], 1)
        self.assertEqual(report["added_count"], 1)
        cards = load_local_cards(self.workspace)
        self.assertEqual(cards[0]["local_status"], "pending_review")
        self.assertEqual(cards[0]["subject"], "프로그래밍")
        self.assertTrue((self.workspace / "study" / "images" / "evidence.png").exists())
        self.assertEqual(len(list((self.workspace / "study" / "imports").glob("*.json"))), 1)

    def test_import_valid_zip(self):
        source = self._write_zip()
        report = import_study_cards(source, self.workspace)

        self.assertTrue(report["success"])
        self.assertEqual(report["added_count"], 1)
        self.assertEqual(report["images"]["copied"], 1)
        self.assertTrue((self.workspace / "study" / "images" / "evidence.png").exists())

    def test_validation_error_blocks_import(self):
        card = valid_card()
        card["card_type"] = "invalid_type"
        source = self._write_json_source(payload=payload_with(card))
        report = import_study_cards(source, self.workspace)

        self.assertFalse(report["success"])
        self.assertTrue(report["errors"])
        self.assertFalse((self.workspace / "study" / "cards.json").exists())

    def test_reimport_same_card_does_not_duplicate(self):
        source = self._write_json_source()
        first = import_study_cards(source, self.workspace)
        second = import_study_cards(source, self.workspace)

        self.assertTrue(first["success"] and second["success"])
        self.assertEqual(second["added_count"], 0)
        self.assertEqual(second["skipped_count"], 1)
        self.assertEqual(len(load_local_cards(self.workspace)), 1)

    def test_existing_user_edits_and_status_are_preserved(self):
        source = self._write_json_source()
        import_study_cards(source, self.workspace)
        cards = load_local_cards(self.workspace)
        cards[0]["question"] = "사용자가 수정한 질문"
        cards[0]["answer"] = "사용자가 수정한 답"
        cards[0]["local_status"] = "approved"
        cards[0]["excluded"] = False
        save_local_cards(self.workspace, cards)

        report = import_study_cards(source, self.workspace)
        saved = load_local_cards(self.workspace)[0]

        self.assertTrue(report["success"])
        self.assertEqual(report["conflict_count"], 1)
        self.assertEqual(saved["question"], "사용자가 수정한 질문")
        self.assertEqual(saved["answer"], "사용자가 수정한 답")
        self.assertEqual(saved["local_status"], "approved")

    def test_reimport_identical_image_reuses_local_file(self):
        source = self._write_json_source()
        import_study_cards(source, self.workspace)
        report = import_study_cards(source, self.workspace)

        self.assertTrue(report["success"])
        self.assertEqual(report["images"]["reused"], 1)
        self.assertEqual(len(list((self.workspace / "study" / "images").iterdir())), 1)

    def test_image_name_collision_uses_content_hash(self):
        first = self._write_json_source(folder_name="source-a", image_data=b"first-image")
        second_card = valid_card(
            "card-002",
            "데이터베이스 인덱스의 장점은 무엇인가요?",
            "데이터베이스 인덱스",
        )
        second = self._write_json_source(
            folder_name="source-b",
            payload=payload_with(second_card, subject="데이터베이스"),
            image_data=b"second-image",
        )
        import_study_cards(first, self.workspace)
        report = import_study_cards(second, self.workspace)

        self.assertTrue(report["success"])
        self.assertEqual(report["images"]["renamed"], 1)
        cards = load_local_cards(self.workspace)
        second_image = next(card for card in cards if card["card_id"] == "card-002")["source_images"][0]
        self.assertNotEqual(second_image, "evidence.png")
        self.assertTrue((self.workspace / "study" / "images" / second_image).exists())

    def test_zip_parent_path_is_rejected(self):
        source = self._write_zip(extra_entries=[("../outside.txt", b"attack")])
        report = import_study_cards(source, self.workspace)

        self.assertFalse(report["success"])
        self.assertTrue(any("경로" in error for error in report["errors"]))
        self.assertFalse((self.workspace / "study" / "cards.json").exists())

    def test_unreferenced_zip_image_is_not_extracted(self):
        source = self._write_zip(extra_entries=[("images/unused.png", b"unused")])
        report = import_study_cards(source, self.workspace)

        self.assertTrue(report["success"])
        image_names = {path.name for path in (self.workspace / "study" / "images").iterdir()}
        self.assertEqual(image_names, {"evidence.png"})

    def test_ai_suggested_card_is_pending_review(self):
        card = valid_card()
        card["answer_status"] = "ai_suggested"
        card["review_required"] = True
        source = self._write_json_source(payload=payload_with(card))
        report = import_study_cards(source, self.workspace)

        self.assertTrue(report["success"])
        self.assertEqual(report["review_required_count"], 1)
        self.assertEqual(load_local_cards(self.workspace)[0]["local_status"], "pending_review")

    def test_save_then_load_preserves_data(self):
        cards = [
            {
                **valid_card(),
                "local_status": "rejected",
                "excluded": True,
                "imported_at": "2026-07-12T20:00:00+09:00",
                "updated_at": "2026-07-12T20:10:00+09:00",
                "source_package": "package.zip",
            }
        ]
        save_local_cards(self.workspace, cards)
        loaded = load_local_cards(self.workspace)
        self.assertEqual(loaded, cards)


if __name__ == "__main__":
    unittest.main()
