import copy
import queue
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from tkinter.scrolledtext import ScrolledText

from modules.study_card_importer import load_local_cards
from modules.study_answer_evaluator import (
    StudyAnswerEvaluationError,
    append_answer_evaluation,
    evaluate_study_answer,
    evaluation_history_event,
)
from modules.study_review_scheduler import (
    append_review_history,
    get_due_cards,
    load_review_state,
    save_review_state,
    schedule_review,
)


RATING_LABELS = {
    "again": "모름",
    "hard": "어려움",
    "good": "보통",
    "easy": "쉬움",
}
ANSWER_STATUS_LABELS = {
    "confirmed_from_source": "근거에서 확인",
    "ai_suggested": "AI 제안",
    "needs_verification": "확인 필요",
}


def _lines(value) -> str:
    return "\n".join(str(item) for item in value) if isinstance(value, list) else ""


class StudyReviewWindow:
    def __init__(self, parent, workspace, config=None):
        self.parent = parent
        self.workspace = Path(workspace)
        self.config = dict(config or {})
        self.cards = load_local_cards(self.workspace)
        self.review_state = load_review_state(self.workspace)
        self.due_cards = get_due_cards(self.cards, self.review_state)
        self.current_index = 0
        self.completed = 0
        self.rating_counts = {rating: 0 for rating in RATING_LABELS}
        self.answer_revealed = False
        self.evaluation_running = False
        self.evaluation_request_token = 0
        self.current_evaluation = None
        self.current_evaluation_at = None
        self.evaluation_results = queue.Queue()

        if not self.due_cards:
            messagebox.showinfo("오늘의 복습", "오늘 예정된 복습을 모두 완료했습니다.", parent=parent)
            self.window = None
            return

        self.window = tk.Toplevel(parent)
        self.window.title("ClassFlowAI 오늘의 복습")
        self.window.geometry("820x760")
        self.window.minsize(700, 620)
        self._build_ui()
        self._show_card()

    def _build_ui(self):
        top = tk.Frame(self.window, padx=12, pady=10)
        top.pack(fill="x")
        self.progress_var = tk.StringVar()
        tk.Label(top, textvariable=self.progress_var, font=("맑은 고딕", 11, "bold")).pack(anchor="w")

        front = tk.LabelFrame(self.window, text="카드 앞면", padx=12, pady=10)
        front.pack(fill="x", padx=12, pady=(0, 8))
        self.meta_var = tk.StringVar()
        self.question_var = tk.StringVar()
        self.choices_var = tk.StringVar()
        tk.Label(front, textvariable=self.meta_var, fg="#555555", anchor="w").pack(fill="x")
        tk.Label(
            front,
            textvariable=self.question_var,
            font=("맑은 고딕", 14, "bold"),
            wraplength=760,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(8, 5))
        tk.Label(front, textvariable=self.choices_var, wraplength=760, justify="left", anchor="w").pack(fill="x")

        evidence = tk.Frame(front)
        evidence.pack(fill="x", pady=(8, 0))
        tk.Label(evidence, text="근거 이미지:").pack(side="left")
        self.image_list = tk.Listbox(evidence, height=2, exportselection=False)
        self.image_list.pack(side="left", fill="x", expand=True, padx=6)
        tk.Button(evidence, text="이미지 열기", command=self.open_image).pack(side="right")

        answer_box = tk.LabelFrame(self.window, text="내 답변", padx=12, pady=8)
        answer_box.pack(fill="x", padx=12, pady=(0, 8))
        self.answer_hint_var = tk.StringVar()
        tk.Label(answer_box, textvariable=self.answer_hint_var, fg="#555555", anchor="w").pack(fill="x")
        self.answer_input = ScrolledText(answer_box, height=6, wrap="word")
        self.answer_input.pack(fill="x", pady=(5, 7))
        answer_actions = tk.Frame(answer_box)
        answer_actions.pack(fill="x")
        self.ai_button = tk.Button(
            answer_actions,
            text="AI로 설명 점검 (실험)",
            command=self.start_ai_evaluation,
            width=21,
        )
        self.ai_button.pack(side="left")
        self.check_button = tk.Button(answer_actions, text="답 확인", command=self.reveal_answer, width=14)
        self.check_button.pack(side="right")

        self.result_frame = tk.LabelFrame(self.window, text="답 비교", padx=12, pady=8)
        self.user_answer_var = tk.StringVar()
        self.correct_answer_var = tk.StringVar()
        self.key_points_var = tk.StringVar()
        self.explanation_var = tk.StringVar()
        self.answer_status_var = tk.StringVar()
        for variable, color in (
            (self.user_answer_var, "#333333"),
            (self.correct_answer_var, "#0f5132"),
            (self.key_points_var, "#7a4b00"),
            (self.explanation_var, "#333333"),
            (self.answer_status_var, "#555555"),
        ):
            tk.Label(
                self.result_frame,
                textvariable=variable,
                fg=color,
                wraplength=760,
                justify="left",
                anchor="w",
            ).pack(fill="x", pady=2)
        self.rewrite_button = tk.Button(
            self.result_frame,
            text="내 설명 다시 작성",
            command=self.rewrite_feynman_answer,
        )

        self.ai_frame = tk.LabelFrame(self.window, text="AI 설명 점검 · 실험 기능", padx=12, pady=8)
        self.ai_result_text = ScrolledText(self.ai_frame, height=9, wrap="word", state="disabled")
        self.ai_result_text.pack(fill="x")

        self.ratings_frame = tk.LabelFrame(self.window, text="직접 평가", padx=12, pady=9)
        self.ratings_frame.pack(fill="x", padx=12, pady=(0, 12))
        self.rating_buttons = {}
        for rating, label in RATING_LABELS.items():
            button = tk.Button(
                self.ratings_frame,
                text=label,
                command=lambda value=rating: self.rate_card(value),
                state="disabled",
                width=12,
            )
            button.pack(side="left", padx=5)
            self.rating_buttons[rating] = button

    def _current_card(self):
        if self.current_index >= len(self.due_cards):
            return None
        return self.due_cards[self.current_index]

    def _show_card(self):
        card = self._current_card()
        if card is None:
            self._finish_session()
            return
        remaining = len(self.due_cards) - self.current_index
        self.progress_var.set(
            f"오늘 남은 카드 {remaining} · 현재 {self.current_index + 1}/{len(self.due_cards)} · 이번 세션 완료 {self.completed}"
        )
        subject = str(card.get("subject") or "미분류")
        topic = str(card.get("topic") or "미분류")
        card_type = str(card.get("card_type") or "")
        self.meta_var.set(f"{subject} · {topic} · {card_type}")
        self.question_var.set(str(card.get("question") or ""))
        choices = card.get("choices") or []
        self.choices_var.set(
            "\n".join(f"{index + 1}. {choice}" for index, choice in enumerate(choices))
            if isinstance(choices, list)
            else ""
        )
        self.image_list.delete(0, "end")
        for name in card.get("source_images") or []:
            self.image_list.insert("end", str(name))
        if self.image_list.size():
            self.image_list.selection_set(0)

        hint = (
            "이 내용을 처음 배우는 사람에게 설명하듯 작성해 보세요."
            if card_type == "feynman"
            else "기억나는 답을 작성한 뒤 확인하세요."
        )
        self.answer_hint_var.set(hint)
        self.answer_input.config(state="normal")
        self.answer_input.delete("1.0", "end")
        self.check_button.config(state="normal")
        self.result_frame.pack_forget()
        self.ai_frame.pack_forget()
        self.rewrite_button.pack_forget()
        for button in self.rating_buttons.values():
            button.config(state="disabled")
        self.answer_revealed = False
        self.evaluation_request_token += 1
        self.evaluation_running = False
        self.current_evaluation = None
        self.current_evaluation_at = None
        self.ai_button.config(state="normal", text="AI로 설명 점검 (실험)")
        self.answer_input.focus_set()

    def reveal_answer(self):
        card = self._current_card()
        if card is None:
            return
        user_answer = self.answer_input.get("1.0", "end").strip()
        self.answer_input.config(state="disabled")
        self.check_button.config(state="disabled")
        self.user_answer_var.set(f"내 답변: {user_answer or '(입력 없음)'}")
        self.correct_answer_var.set(f"확인된 답: {card.get('answer') or '(확인된 답 없음)'}")
        self.key_points_var.set(f"핵심 답변 요소:\n{_lines(card.get('key_points')) or '(없음)'}")
        self.explanation_var.set(f"설명: {card.get('explanation') or '(없음)'}")
        answer_status = ANSWER_STATUS_LABELS.get(card.get("answer_status"), card.get("answer_status", ""))
        images = ", ".join(str(name) for name in card.get("source_images") or []) or "없음"
        self.answer_status_var.set(f"근거 이미지: {images} · 정답 신뢰도: {answer_status}")
        self.result_frame.pack(fill="x", padx=12, pady=(0, 8), before=self.window.pack_slaves()[-1])
        if card.get("card_type") == "feynman":
            self.rewrite_button.pack(anchor="e", pady=(5, 0))
        for button in self.rating_buttons.values():
            button.config(state="normal")
        self.answer_revealed = True

    def rewrite_feynman_answer(self):
        if not self.answer_revealed:
            return
        self.result_frame.pack_forget()
        for button in self.rating_buttons.values():
            button.config(state="disabled")
        self.answer_input.config(state="normal")
        self.check_button.config(state="normal")
        self.ai_frame.pack_forget()
        self.evaluation_request_token += 1
        self.evaluation_running = False
        self.ai_button.config(state="normal", text="AI로 설명 점검 (실험)")
        self.current_evaluation = None
        self.current_evaluation_at = None
        self.answer_revealed = False
        self.answer_input.focus_set()

    def start_ai_evaluation(self):
        if self.evaluation_running:
            return
        card = self._current_card()
        if card is None:
            return
        user_answer = self.answer_input.get("1.0", "end").strip()
        if not user_answer:
            messagebox.showinfo("AI 설명 점검", "먼저 답변을 작성해 주세요.", parent=self.window)
            return
        self.evaluation_running = True
        self.evaluation_request_token += 1
        token = self.evaluation_request_token
        card_id = str(card.get("card_id") or "")
        self.ai_button.config(state="disabled", text="AI 평가 중...")

        def worker():
            try:
                result = evaluate_study_answer(card, user_answer, self.config)
                error = None
            except StudyAnswerEvaluationError as exc:
                result = None
                error = exc.user_message
            except Exception:
                result = None
                error = "AI 평가 중 오류가 발생했습니다. 직접 평가를 계속할 수 있습니다."
            self.evaluation_results.put((token, card_id, user_answer, result, error))

        threading.Thread(target=worker, daemon=True).start()
        self.window.after(50, lambda: self._poll_ai_evaluation(token))

    def _poll_ai_evaluation(self, token):
        if token != self.evaluation_request_token:
            return
        while True:
            try:
                result = self.evaluation_results.get_nowait()
            except queue.Empty:
                if self.evaluation_running:
                    self.window.after(100, lambda: self._poll_ai_evaluation(token))
                return
            if result[0] == token:
                self._finish_ai_evaluation(*result)
                return

    def _finish_ai_evaluation(self, token, card_id, user_answer, result, error):
        if token != self.evaluation_request_token:
            return
        self.evaluation_running = False
        self.ai_button.config(state="normal", text="AI로 설명 점검 (실험)")
        card = self._current_card()
        if card is None or str(card.get("card_id") or "") != card_id:
            return
        current_answer = self.answer_input.get("1.0", "end").strip()
        if current_answer != user_answer:
            self.current_evaluation = None
            messagebox.showinfo(
                "AI 설명 점검",
                "답변이 변경되어 이전 AI 평가 결과를 표시하지 않았습니다.",
                parent=self.window,
            )
            return
        if error:
            self.current_evaluation = None
            messagebox.showwarning("AI 설명 점검 실패", error, parent=self.window)
            return
        self.current_evaluation = result
        self.current_evaluation_at = datetime.now().astimezone().isoformat(timespec="seconds")
        rating_label = RATING_LABELS.get(result.get("recommended_rating"), result.get("recommended_rating", ""))
        display = "\n\n".join(
            [
                result.get("safety_notice") or "AI 평가는 참고 자료이며 최종 평가는 직접 선택하세요.",
                f"잘 설명한 부분:\n{_lines(result.get('matched_points')) or '(없음)'}",
                f"빠진 부분:\n{_lines(result.get('missing_points')) or '(없음)'}",
                f"잘못 이해한 부분:\n{_lines(result.get('incorrect_points')) or '(없음)'}",
                f"참고 피드백: {result.get('feedback') or '(없음)'}",
                f"다시 설명할 때 생각할 질문: {result.get('retry_prompt') or '(없음)'}",
                f"참고 추천: {rating_label} · 신뢰도: {result.get('confidence', '')} · 자동 반영되지 않습니다.",
            ]
        )
        self.ai_result_text.config(state="normal")
        self.ai_result_text.delete("1.0", "end")
        self.ai_result_text.insert("1.0", display)
        self.ai_result_text.config(state="disabled")
        self.ai_frame.pack(fill="x", padx=12, pady=(0, 8), before=self.ratings_frame)

    def rate_card(self, rating):
        card = self._current_card()
        if card is None or not self.answer_revealed:
            return
        previous_state = copy.deepcopy(self.review_state)
        reviewed_at = datetime.now().astimezone()
        try:
            updated = schedule_review(card.get("card_id"), rating, self.review_state, reviewed_at)
            save_review_state(self.workspace, self.review_state)
            event = {
                "card_id": card.get("card_id"),
                "reviewed_at": updated["last_reviewed_at"],
                "rating": rating,
                "user_answer": self.answer_input.get("1.0", "end").strip(),
                "next_due_at": updated["due_at"],
                "interval_days": updated["interval_days"],
            }
            append_review_history(self.workspace, event)
        except Exception as exc:
            self.review_state = previous_state
            try:
                save_review_state(self.workspace, previous_state)
            except Exception:
                pass
            messagebox.showerror("복습 저장 실패", str(exc), parent=self.window)
            return
        evaluation_save_error = None
        if self.current_evaluation is not None:
            try:
                evaluation_event = evaluation_history_event(
                    card.get("card_id"),
                    self.current_evaluation,
                    rating,
                    self.current_evaluation_at,
                )
                append_answer_evaluation(self.workspace, evaluation_event)
            except Exception:
                evaluation_save_error = "AI 평가 기록을 저장하지 못했지만 복습 평가는 정상 저장되었습니다."
        if evaluation_save_error:
            messagebox.showwarning("AI 평가 기록", evaluation_save_error, parent=self.window)
        self.evaluation_request_token += 1
        self.evaluation_running = False
        self.completed += 1
        self.rating_counts[rating] += 1
        self.current_index += 1
        self._show_card()

    def open_image(self):
        selection = self.image_list.curselection()
        if not selection:
            messagebox.showinfo("근거 이미지", "열 이미지를 선택해 주세요.", parent=self.window)
            return
        path = self.workspace / "study" / "images" / self.image_list.get(selection[0])
        if not path.is_file():
            messagebox.showwarning("근거 이미지", "이미지 파일을 찾을 수 없습니다.", parent=self.window)
            return
        webbrowser.open(path.resolve().as_uri())

    def _next_due_text(self) -> str:
        card_ids = {
            str(card.get("card_id"))
            for card in self.cards
            if card.get("local_status") == "approved" and not card.get("excluded")
        }
        due_values = []
        for card_id in card_ids:
            value = self.review_state.get(card_id, {}).get("due_at")
            try:
                due_values.append(datetime.fromisoformat(value).astimezone())
            except (TypeError, ValueError):
                continue
        if not due_values:
            return "예정 없음"
        return min(due_values).astimezone().strftime("%Y-%m-%d %H:%M")

    def _finish_session(self):
        summary = [
            f"완료 카드: {self.completed}",
            f"모름: {self.rating_counts['again']}",
            f"어려움: {self.rating_counts['hard']}",
            f"보통: {self.rating_counts['good']}",
            f"쉬움: {self.rating_counts['easy']}",
            f"다음 예정 복습: {self._next_due_text()}",
        ]
        messagebox.showinfo("오늘의 복습 완료", "\n".join(summary), parent=self.window)
        self.window.destroy()


def open_today_review_window(parent, workspace, config=None):
    return StudyReviewWindow(parent, workspace, config)
