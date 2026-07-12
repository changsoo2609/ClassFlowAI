import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.study_card_importer import import_study_cards
from modules.study_review_scheduler import (
    StudyReviewError,
    append_review_history,
    get_due_cards,
    load_review_state,
    save_review_state,
    schedule_review,
)


NOW = datetime(2026, 7, 12, 20, 0, tzinfo=timezone(timedelta(hours=9)))


def approved_card(card_id="card-001"):
    return {
        "card_id": card_id,
        "local_status": "approved",
        "excluded": False,
        "question": "테스트 질문입니다.",
    }


def valid_payload():
    return {
        "schema_version": 1,
        "subject": "테스트",
        "cards": [
            {
                "card_id": "card-001",
                "source_type": "concept",
                "card_type": "active_recall",
                "topic": "테스트",
                "question": "일정 계산을 설명하세요.",
                "choices": [],
                "answer": "결정적으로 계산합니다.",
                "key_points": ["결정성"],
                "explanation": "설명",
                "answer_status": "confirmed_from_source",
                "source_images": [],
                "tags": [],
                "difficulty": 2,
                "review_required": False,
            }
        ],
    }


class StudyReviewSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name) / "workspace"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_new_approved_card_is_due_immediately(self):
        self.assertEqual(get_due_cards([approved_card()], {}, NOW)[0]["card_id"], "card-001")

    def test_unapproved_and_excluded_cards_are_not_due(self):
        pending = {**approved_card("pending"), "local_status": "pending_review"}
        rejected = {**approved_card("rejected"), "local_status": "rejected"}
        excluded = {**approved_card("excluded"), "excluded": True}
        self.assertEqual(get_due_cards([pending, rejected, excluded], {}, NOW), [])

    def test_future_due_card_is_not_due(self):
        state = {"card-001": {"due_at": (NOW + timedelta(days=1)).isoformat()}}
        self.assertEqual(get_due_cards([approved_card()], state, NOW), [])

    def test_again_is_scheduled_ten_minutes_later_and_adds_lapse(self):
        state = {"card-001": {"interval_days": 30, "review_count": 2, "lapse_count": 1}}
        result = schedule_review("card-001", "again", state, NOW)
        self.assertEqual(datetime.fromisoformat(result["due_at"]), NOW + timedelta(minutes=10))
        self.assertEqual(result["lapse_count"], 2)
        self.assertLess(result["interval_days"], 30)

    def test_hard_interval_increases(self):
        state = {"card-001": {"interval_days": 10}}
        self.assertEqual(schedule_review("card-001", "hard", state, NOW)["interval_days"], 12)

    def test_good_interval_increases(self):
        state = {"card-001": {"interval_days": 10}}
        self.assertEqual(schedule_review("card-001", "good", state, NOW)["interval_days"], 23)

    def test_easy_interval_increases(self):
        state = {"card-001": {"interval_days": 10}}
        self.assertEqual(schedule_review("card-001", "easy", state, NOW)["interval_days"], 35)

    def test_interval_is_limited_to_365_days(self):
        for rating in ("hard", "good", "easy"):
            state = {"card-001": {"interval_days": 300}}
            self.assertLessEqual(schedule_review("card-001", rating, state, NOW)["interval_days"], 365)

    def test_new_card_initial_intervals(self):
        expected = {"hard": 1, "good": 3, "easy": 7}
        for rating, days in expected.items():
            state = {}
            self.assertEqual(schedule_review("card-001", rating, state, NOW)["interval_days"], days)

    def test_reimport_preserves_existing_review_state(self):
        source = Path(self.temp_dir.name) / "study_cards.json"
        source.write_text(json.dumps(valid_payload(), ensure_ascii=False), encoding="utf-8")
        state = {"card-001": schedule_review("card-001", "good", {}, NOW)}
        save_review_state(self.workspace, state)

        first = import_study_cards(source, self.workspace, confirm_warnings=True)
        second = import_study_cards(source, self.workspace, confirm_warnings=True)

        self.assertTrue(first["success"] and second["success"])
        self.assertEqual(load_review_state(self.workspace), state)

    def test_save_and_reload_preserves_state(self):
        state = {}
        schedule_review("card-001", "easy", state, NOW)
        save_review_state(self.workspace, state)
        self.assertEqual(load_review_state(self.workspace), state)

    def test_review_history_appends_json_lines(self):
        append_review_history(self.workspace, {"card_id": "card-001", "rating": "good"})
        append_review_history(self.workspace, {"card_id": "card-002", "rating": "hard"})
        history_path = self.workspace / "study" / "review_history.jsonl"
        events = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["card_id"] for event in events], ["card-001", "card-002"])

    def test_invalid_due_date_is_treated_as_due(self):
        state = {"card-001": {"due_at": "not-a-date"}}
        self.assertEqual(len(get_due_cards([approved_card()], state, NOW)), 1)

    def test_corrupt_state_file_reports_error_without_overwrite(self):
        path = self.workspace / "study" / "review_state.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(StudyReviewError):
            load_review_state(self.workspace)
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
