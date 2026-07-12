import json
import tempfile
import unittest
from pathlib import Path

from modules.study_card_validator import (
    validate_study_cards_file,
    validate_study_cards_payload,
)


def valid_card(card_id="card-001", question="파이썬 리스트 컴프리헨션의 장점은 무엇인가요?"):
    return {
        "card_id": card_id,
        "source_type": "concept",
        "card_type": "active_recall",
        "topic": "파이썬 리스트 컴프리헨션",
        "question": question,
        "choices": [],
        "answer": "반복문을 간결한 표현식으로 작성할 수 있습니다.",
        "key_points": ["간결성", "새 리스트 생성"],
        "explanation": "반복과 조건을 한 표현식으로 결합합니다.",
        "answer_status": "confirmed_from_source",
        "source_images": ["capture_001.png"],
        "tags": ["python"],
        "difficulty": 2,
        "review_required": False,
    }


def payload_with(*cards):
    return {
        "schema_version": 1,
        "subject": "테스트",
        "generated_at": "2026-07-12T20:00:00+09:00",
        "cards": list(cards),
    }


class StudyCardValidatorTests(unittest.TestCase):
    def test_valid_card_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            images_dir = root / "images"
            images_dir.mkdir()
            (images_dir / "capture_001.png").write_bytes(b"image")
            json_path = root / "study_cards.json"
            json_path.write_text(
                json.dumps(payload_with(valid_card()), ensure_ascii=False),
                encoding="utf-8",
            )

            report = validate_study_cards_file(json_path, images_dir=images_dir)

        self.assertTrue(report["valid"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["stats"]["total_cards"], 1)
        self.assertEqual(report["stats"]["confirmed_cards"], 1)
        self.assertEqual(report["stats"]["cards_with_missing_images"], 0)

    def test_duplicate_card_id(self):
        first = valid_card("card-001")
        second = valid_card("card-001", "리스트 컴프리헨션은 언제 사용하나요?")
        report = validate_study_cards_payload(payload_with(first, second))
        self.assertFalse(report["valid"])
        self.assertTrue(any("card_id가 중복" in error for error in report["errors"]))
        self.assertIn("card-001", report["duplicates"])

    def test_invalid_card_type(self):
        card = valid_card()
        card["card_type"] = "unknown_type"
        report = validate_study_cards_payload(payload_with(card))
        self.assertFalse(report["valid"])
        self.assertTrue(any("허용되지 않은 card_type" in error for error in report["errors"]))

    def test_confirmed_answer_must_not_be_empty(self):
        card = valid_card()
        card["answer"] = ""
        report = validate_study_cards_payload(payload_with(card))
        self.assertFalse(report["valid"])
        self.assertTrue(any("answer가 비어" in error for error in report["errors"]))

    def test_review_required_status_must_be_true(self):
        card = valid_card()
        card["answer_status"] = "needs_verification"
        card["answer"] = ""
        card["review_required"] = False
        report = validate_study_cards_payload(payload_with(card))
        self.assertFalse(report["valid"])
        self.assertTrue(any("review_required가 true" in error for error in report["errors"]))

    def test_missing_source_image_is_warning(self):
        report = validate_study_cards_payload(
            payload_with(valid_card()),
            available_images=[],
        )
        self.assertTrue(report["valid"])
        self.assertTrue(any("근거 이미지 파일" in warning for warning in report["warnings"]))
        self.assertEqual(report["stats"]["cards_with_missing_images"], 1)

    def test_similar_questions_are_reported_as_duplicates(self):
        first = valid_card("card-001", "HTTP 상태 코드 404의 의미는 무엇인가요?")
        second = valid_card("card-002", "HTTP 상태코드 404 의미는 무엇인가요?")
        report = validate_study_cards_payload(payload_with(first, second))
        self.assertTrue(report["valid"])
        self.assertEqual(report["duplicates"], ["card-001", "card-002"])
        self.assertTrue(any("유사 질문 중복" in warning for warning in report["warnings"]))

    def test_invalid_json_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            json_path = Path(temp_dir) / "study_cards.json"
            json_path.write_text('{"schema_version": 1, "cards": [}', encoding="utf-8")
            report = validate_study_cards_file(json_path)

        self.assertFalse(report["valid"])
        self.assertTrue(any("JSON 파싱 실패" in error for error in report["errors"]))

    def test_required_structure_errors(self):
        card = valid_card()
        card.update(
            {
                "source_type": "unknown_source",
                "answer_status": "unknown_status",
                "question": "",
                "source_images": "capture_001.png",
                "key_points": "point",
                "tags": "python",
                "choices": "A",
                "difficulty": 6,
            }
        )
        report = validate_study_cards_payload(payload_with(card))
        joined = "\n".join(report["errors"])
        self.assertFalse(report["valid"])
        self.assertIn("허용되지 않은 source_type", joined)
        self.assertIn("허용되지 않은 answer_status", joined)
        self.assertIn("question이 비어", joined)
        self.assertIn("source_images는 배열", joined)
        self.assertIn("key_points는 배열", joined)
        self.assertIn("tags는 배열", joined)
        self.assertIn("choices는 배열", joined)
        self.assertIn("difficulty는 1~5", joined)

    def test_quality_and_learning_unit_warnings(self):
        cards = []
        for index in range(4):
            card = valid_card(
                f"card-{index + 1:03d}",
                f"리스트 컴프리헨션 활용 방법 {index + 1}을 설명하세요.",
            )
            card["topic"] = "같은 학습 단위"
            cards.append(card)
        cards[0]["choices"] = list("ABCDEFG")
        cards[0]["source_images"] = []
        cards[0]["answer"] = ""
        cards[0]["key_points"] = []
        cards[0]["answer_status"] = "needs_verification"
        cards[0]["review_required"] = True

        report = validate_study_cards_payload(payload_with(*cards))
        joined = "\n".join(report["warnings"])
        self.assertTrue(report["valid"])
        self.assertIn("source_images가 비어", joined)
        self.assertIn("answer와 key_points가 모두 비어", joined)
        self.assertIn("선택지가 너무 많", joined)
        self.assertIn("학습 단위당 카드가 3장을 초과", joined)


if __name__ == "__main__":
    unittest.main()
