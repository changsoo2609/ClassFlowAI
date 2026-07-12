import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from modules.study_card_importer import (
    import_study_cards,
    load_local_cards,
    save_local_cards,
)
from modules.study_review_window import open_today_review_window


STATUS_LABELS = {
    "pending_review": "검토 대기",
    "approved": "승인",
    "rejected": "제외",
}
ANSWER_STATUS_LABELS = {
    "confirmed_from_source": "근거에서 확인",
    "ai_suggested": "AI 제안",
    "needs_verification": "확인 필요",
}


def _as_lines(value) -> str:
    if not isinstance(value, list):
        return ""
    return "\n".join(str(item) for item in value)


def _parse_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


class StudyCardReviewWindow:
    def __init__(self, parent, workspace, config=None):
        self.workspace = Path(workspace)
        self.config = dict(config or {})
        self.cards = load_local_cards(self.workspace)
        self.visible_indices = []
        self.selected_index = None
        self.review_session = None

        self.window = tk.Toplevel(parent)
        self.window.title("ClassFlowAI 학습카드")
        self.window.geometry("1180x760")
        self.window.minsize(980, 620)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        top = tk.Frame(self.window, padx=10, pady=8)
        top.pack(fill="x")
        tk.Button(top, text="카드 가져오기", command=self.import_cards, width=15).pack(side="left")
        tk.Button(top, text="오늘의 복습", command=self.open_today_review, width=15).pack(side="left", padx=(6, 0))

        self.stats_var = tk.StringVar()
        tk.Label(top, textvariable=self.stats_var, anchor="w").pack(side="left", padx=16)

        filters = tk.LabelFrame(self.window, text="목록 필터", padx=8, pady=6)
        filters.pack(fill="x", padx=10, pady=(0, 8))
        self.status_filter = tk.StringVar(value="전체")
        self.subject_filter = tk.StringVar(value="전체")
        self.type_filter = tk.StringVar(value="전체")
        for label, variable in (
            ("상태", self.status_filter),
            ("과목", self.subject_filter),
            ("카드 유형", self.type_filter),
        ):
            tk.Label(filters, text=label).pack(side="left", padx=(4, 3))
            combo = ttk.Combobox(filters, textvariable=variable, state="readonly", width=18)
            combo.pack(side="left", padx=(0, 10))
            combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_list())
            if variable is self.status_filter:
                self.status_combo = combo
            elif variable is self.subject_filter:
                self.subject_combo = combo
            else:
                self.type_combo = combo

        body = tk.PanedWindow(self.window, orient="horizontal", sashwidth=6)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        list_frame = tk.Frame(body)
        detail = tk.Frame(body)
        body.add(list_frame, minsize=430)
        body.add(detail, minsize=480)

        columns = ("topic", "question", "type", "answer", "status", "images")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "topic": "주제",
            "question": "질문",
            "type": "카드 유형",
            "answer": "정답 신뢰도",
            "status": "검토 상태",
            "images": "이미지",
        }
        widths = {"topic": 100, "question": 260, "type": 100, "answer": 100, "status": 80, "images": 55}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=50, stretch=column == "question")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._select_card)

        self.entries = {}
        self._text_field(detail, "질문", "question", 4)
        self._text_field(detail, "선택지 (한 줄에 하나)", "choices", 3)
        self._text_field(detail, "답", "answer", 4)
        self._text_field(detail, "핵심 답변 요소 (한 줄에 하나)", "key_points", 4)
        self._text_field(detail, "설명", "explanation", 4)
        self._text_field(detail, "태그 (한 줄에 하나)", "tags", 3)

        image_frame = tk.LabelFrame(detail, text="근거 이미지", padx=6, pady=5)
        image_frame.pack(fill="x", pady=(4, 0))
        self.image_list = tk.Listbox(image_frame, height=3)
        self.image_list.pack(side="left", fill="x", expand=True)
        tk.Button(image_frame, text="이미지 열기", command=self.open_image).pack(side="right", padx=(6, 0))

        self.meta_var = tk.StringVar()
        self.reason_var = tk.StringVar()
        tk.Label(detail, textvariable=self.meta_var, anchor="w").pack(fill="x", pady=(7, 0))
        tk.Label(detail, textvariable=self.reason_var, anchor="w", fg="#8a4d00", wraplength=600, justify="left").pack(fill="x")

        actions = tk.Frame(detail, pady=8)
        actions.pack(fill="x")
        tk.Button(actions, text="수정 저장", command=self.save_edits).pack(side="left")
        tk.Button(actions, text="승인", command=lambda: self.set_status("approved")).pack(side="left", padx=5)
        tk.Button(actions, text="제외", command=lambda: self.set_status("rejected")).pack(side="left", padx=5)
        tk.Button(actions, text="검토 대기로 되돌리기", command=lambda: self.set_status("pending_review")).pack(side="left", padx=5)

    def _text_field(self, parent, label, key, height):
        frame = tk.LabelFrame(parent, text=label, padx=5, pady=4)
        frame.pack(fill="x", pady=(0, 4))
        widget = ScrolledText(frame, height=height, wrap="word")
        widget.pack(fill="x")
        self.entries[key] = widget

    def refresh(self):
        statuses = ["전체", "검토 대기", "승인", "제외"]
        subjects = sorted({str(card.get("subject") or "미분류") for card in self.cards})
        card_types = sorted({str(card.get("card_type") or "미분류") for card in self.cards})
        self.status_combo["values"] = statuses
        self.subject_combo["values"] = ["전체", *subjects]
        self.type_combo["values"] = ["전체", *card_types]
        if self.subject_filter.get() not in self.subject_combo["values"]:
            self.subject_filter.set("전체")
        if self.type_filter.get() not in self.type_combo["values"]:
            self.type_filter.set("전체")
        self.refresh_list()

    def refresh_list(self):
        selected_id = None
        if self.selected_index is not None and self.selected_index < len(self.cards):
            selected_id = self.cards[self.selected_index].get("card_id")
        self.tree.delete(*self.tree.get_children())
        self.visible_indices = []
        wanted_status = {value: key for key, value in STATUS_LABELS.items()}.get(self.status_filter.get())
        for index, card in enumerate(self.cards):
            status = card.get("local_status", "pending_review")
            subject = str(card.get("subject") or "미분류")
            card_type = str(card.get("card_type") or "미분류")
            if wanted_status and status != wanted_status:
                continue
            if self.subject_filter.get() != "전체" and subject != self.subject_filter.get():
                continue
            if self.type_filter.get() != "전체" and card_type != self.type_filter.get():
                continue
            self.visible_indices.append(index)
            item = self.tree.insert("", "end", values=(
                card.get("topic", ""),
                card.get("question", ""),
                card_type,
                ANSWER_STATUS_LABELS.get(card.get("answer_status"), card.get("answer_status", "")),
                STATUS_LABELS.get(status, status),
                len(card.get("source_images") or []),
            ))
            if selected_id and card.get("card_id") == selected_id:
                self.tree.selection_set(item)
        self._update_stats()

    def _update_stats(self):
        pending = sum(card.get("local_status") == "pending_review" for card in self.cards)
        approved = sum(card.get("local_status") == "approved" for card in self.cards)
        excluded = sum(card.get("local_status") == "rejected" or card.get("excluded") for card in self.cards)
        self.stats_var.set(f"전체 {len(self.cards)} · 검토 대기 {pending} · 승인 {approved} · 제외 {excluded}")

    def _select_card(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        row = self.tree.index(selection[0])
        if row >= len(self.visible_indices):
            return
        self.selected_index = self.visible_indices[row]
        card = self.cards[self.selected_index]
        values = {
            "question": card.get("question", ""),
            "choices": _as_lines(card.get("choices")),
            "answer": card.get("answer", ""),
            "key_points": _as_lines(card.get("key_points")),
            "explanation": card.get("explanation", ""),
            "tags": _as_lines(card.get("tags")),
        }
        for key, widget in self.entries.items():
            widget.delete("1.0", "end")
            widget.insert("1.0", values[key])
        self.image_list.delete(0, "end")
        for name in card.get("source_images") or []:
            self.image_list.insert("end", name)
        answer_label = ANSWER_STATUS_LABELS.get(card.get("answer_status"), card.get("answer_status", ""))
        self.meta_var.set(f"정답 신뢰도: {answer_label} · 검토 상태: {STATUS_LABELS.get(card.get('local_status'), '')}")
        reason = card.get("review_reason") or card.get("review_required_reason") or ""
        if not reason and card.get("review_required"):
            reason = "생성 결과에서 사용자 검토가 필요하다고 표시된 카드입니다."
        self.reason_var.set(f"검토 필요 이유: {reason}" if reason else "")

    def _current_card(self):
        if self.selected_index is None or self.selected_index >= len(self.cards):
            messagebox.showinfo("학습카드", "먼저 카드를 선택해 주세요.", parent=self.window)
            return None
        return self.cards[self.selected_index]

    def save_edits(self, quiet=False):
        card = self._current_card()
        if card is None:
            return False
        card["question"] = self.entries["question"].get("1.0", "end").strip()
        card["choices"] = _parse_lines(self.entries["choices"].get("1.0", "end"))
        card["answer"] = self.entries["answer"].get("1.0", "end").strip()
        card["key_points"] = _parse_lines(self.entries["key_points"].get("1.0", "end"))
        card["explanation"] = self.entries["explanation"].get("1.0", "end").strip()
        card["tags"] = _parse_lines(self.entries["tags"].get("1.0", "end"))
        card["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        save_local_cards(self.workspace, self.cards)
        self.refresh_list()
        if not quiet:
            messagebox.showinfo("학습카드", "수정 내용을 저장했습니다.", parent=self.window)
        return True

    def set_status(self, status):
        card = self._current_card()
        if card is None:
            return
        self.save_edits(quiet=True)
        card["local_status"] = status
        card["excluded"] = status == "rejected"
        save_local_cards(self.workspace, self.cards)
        self.refresh_list()
        self._select_card()

    def open_image(self):
        card = self._current_card()
        if card is None:
            return
        selection = self.image_list.curselection()
        if not selection:
            messagebox.showinfo("근거 이미지", "열 이미지를 선택해 주세요.", parent=self.window)
            return
        path = self.workspace / "study" / "images" / self.image_list.get(selection[0])
        if not path.is_file():
            messagebox.showwarning("근거 이미지", "이미지 파일을 찾을 수 없습니다.", parent=self.window)
            return
        webbrowser.open(path.resolve().as_uri())

    def import_cards(self):
        source = filedialog.askopenfilename(
            parent=self.window,
            title="학습카드 가져오기",
            filetypes=[("학습카드 파일", "*.json *.zip"), ("JSON", "*.json"), ("ZIP", "*.zip")],
        )
        if not source:
            return
        report = import_study_cards(source, self.workspace, confirm_warnings=False)
        if report.get("requires_confirmation"):
            warning_text = "\n".join(f"- {item}" for item in report.get("warnings", []))
            proceed = messagebox.askyesno(
                "검토가 필요한 가져오기",
                "다음 경고가 있습니다. 그래도 계속할까요?\n\n" + warning_text,
                parent=self.window,
            )
            if not proceed:
                return
            report = import_study_cards(source, self.workspace, confirm_warnings=True)
        if not report.get("success"):
            details = list(report.get("errors") or ["가져오기를 완료하지 못했습니다."])
            if report.get("warnings"):
                details.extend(["", "경고:", *[f"- {item}" for item in report["warnings"]]])
            messagebox.showerror("카드 가져오기 실패", "\n".join(details), parent=self.window)
            return
        self.cards = load_local_cards(self.workspace)
        self.selected_index = None
        self.refresh()
        summary = [
            f"읽은 카드: {report.get('read_count', 0)}",
            f"새로 추가: {report.get('added_count', 0)}",
            f"병합: {report.get('merged_count', 0)}",
            f"중복으로 건너뜀: {report.get('skipped_count', 0)}",
            f"검토 필요: {report.get('review_required_count', 0)}",
        ]
        if report.get("conflict_count"):
            summary.append(f"충돌: {report['conflict_count']}")
        if report.get("warnings"):
            summary.extend(["", "경고:", *[f"- {item}" for item in report["warnings"]]])
        messagebox.showinfo("카드 가져오기 완료", "\n".join(summary), parent=self.window)

    def open_today_review(self):
        try:
            review_window = getattr(self.review_session, "window", None)
            if review_window is not None and review_window.winfo_exists():
                review_window.deiconify()
                review_window.lift()
                return
            self.review_session = open_today_review_window(self.window, self.workspace, self.config)
        except Exception as exc:
            messagebox.showerror("오늘의 복습 열기 실패", str(exc), parent=self.window)


def open_study_card_review_window(parent, workspace, config=None):
    return StudyCardReviewWindow(parent, workspace, config)
