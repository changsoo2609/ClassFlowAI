import unittest
import queue
from unittest.mock import patch

from modules.study_review_window import StudyReviewWindow


class _Text:
    def __init__(self, value=""):
        self.value = value
        self.state = "normal"

    def get(self, _start, _end):
        return self.value

    def config(self, **kwargs):
        self.state = kwargs.get("state", self.state)

    def delete(self, _start, _end):
        self.value = ""

    def insert(self, _start, value):
        self.value = value


class _Button:
    def __init__(self):
        self.options = {}

    def config(self, **kwargs):
        self.options.update(kwargs)


class _Frame:
    def __init__(self):
        self.pack_calls = 0

    def pack(self, **_kwargs):
        self.pack_calls += 1


class _Window:
    def after(self, _delay, callback):
        callback()


class _PendingWindow:
    def __init__(self):
        self.callbacks = []

    def after(self, _delay, callback):
        self.callbacks.append(callback)


class _ImmediateThread:
    def __init__(self, target, daemon=True):
        self.target = target

    def start(self):
        self.target()


class _PendingThread:
    starts = 0

    def __init__(self, target, daemon=True):
        self.target = target

    def start(self):
        type(self).starts += 1


def _evaluation():
    return {
        "verdict": "partial",
        "coverage_score": 70,
        "matched_points": ["입력"],
        "missing_points": ["출력"],
        "incorrect_points": [],
        "feedback": "일부를 설명했습니다.",
        "retry_prompt": "출력은 무엇인가요?",
        "recommended_rating": "hard",
        "confidence": "medium",
        "safety_notice": "",
    }


def _review_window(answer="가상 사용자 답변", pending=False):
    window = object.__new__(StudyReviewWindow)
    window.window = _PendingWindow() if pending else _Window()
    window.config = {"nvidia_api_key": "mock"}
    window.answer_input = _Text(answer)
    window.ai_result_text = _Text()
    window.ai_button = _Button()
    window.ai_frame = _Frame()
    window.ratings_frame = object()
    window.evaluation_running = False
    window.evaluation_request_token = 0
    window.current_evaluation = None
    window.current_evaluation_at = None
    window.evaluation_results = queue.Queue()
    card = {
        "card_id": "virtual-feynman",
        "card_type": "feynman",
        "question": "가상 흐름을 설명하세요.",
        "answer": "입력 후 출력",
        "key_points": ["입력", "출력"],
        "answer_status": "confirmed_from_source",
    }
    window._current_card = lambda: card
    return window


class StudyReviewAiUiTests(unittest.TestCase):
    def test_empty_answer_does_not_start_background_request(self):
        window = _review_window(" ")
        with patch("modules.study_review_window.messagebox.showinfo") as showinfo, patch(
            "modules.study_review_window.threading.Thread"
        ) as thread:
            window.start_ai_evaluation()
        showinfo.assert_called_once()
        thread.assert_not_called()

    def test_duplicate_click_is_blocked_while_evaluating(self):
        window = _review_window(pending=True)
        _PendingThread.starts = 0
        with patch("modules.study_review_window.threading.Thread", _PendingThread):
            window.start_ai_evaluation()
            window.start_ai_evaluation()
        self.assertEqual(_PendingThread.starts, 1)
        self.assertTrue(window.evaluation_running)
        self.assertEqual(window.ai_button.options["state"], "disabled")

    def test_background_result_is_displayed_without_applying_rating(self):
        window = _review_window()
        with patch("modules.study_review_window.threading.Thread", _ImmediateThread), patch(
            "modules.study_review_window.evaluate_study_answer", return_value=_evaluation()
        ), patch("modules.study_review_window.schedule_review") as schedule:
            window.start_ai_evaluation()
        self.assertEqual(window.current_evaluation["recommended_rating"], "hard")
        self.assertIn("참고 추천: 어려움", window.ai_result_text.value)
        self.assertEqual(window.ai_frame.pack_calls, 1)
        self.assertFalse(window.evaluation_running)
        schedule.assert_not_called()

    def test_stale_background_result_does_not_block_current_result(self):
        window = _review_window()
        window.evaluation_request_token = 2
        window.evaluation_running = True
        window.evaluation_results.put((1, "old-card", "old", _evaluation(), None))
        window.evaluation_results.put((2, "virtual-feynman", "가상 사용자 답변", _evaluation(), None))
        window._poll_ai_evaluation(2)
        self.assertIsNotNone(window.current_evaluation)
        self.assertFalse(window.evaluation_running)


if __name__ == "__main__":
    unittest.main()
