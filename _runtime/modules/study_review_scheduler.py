import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path


RATINGS = {"again", "hard", "good", "easy"}
MAX_INTERVAL_DAYS = 365
TEN_MINUTES_IN_DAYS = 10 / (24 * 60)


class StudyReviewError(ValueError):
    pass


def _study_path(workspace, filename: str) -> Path:
    return Path(workspace) / "study" / filename


def _local_datetime(value=None) -> datetime:
    if value is None:
        return datetime.now().astimezone()
    if not isinstance(value, datetime):
        raise StudyReviewError("now는 datetime 값이어야 합니다.")
    if value.tzinfo is None:
        return value.astimezone()
    return value


def _parse_datetime(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def load_review_state(workspace) -> dict:
    path = _study_path(workspace, "review_state.json")
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StudyReviewError(f"복습 상태를 읽을 수 없습니다: {exc}") from exc
    if not isinstance(state, dict):
        raise StudyReviewError("review_state.json의 최상위 값은 객체여야 합니다.")
    for card_id, value in state.items():
        if not isinstance(card_id, str) or not card_id.strip() or not isinstance(value, dict):
            raise StudyReviewError("review_state.json에 잘못된 카드 상태가 있습니다.")
    return state


def save_review_state(workspace, state) -> None:
    if not isinstance(state, dict):
        raise StudyReviewError("저장할 복습 상태는 객체여야 합니다.")
    study_dir = Path(workspace) / "study"
    study_dir.mkdir(parents=True, exist_ok=True)
    path = study_dir / "review_state.json"
    temp_path = study_dir / ".review_state.json.tmp"
    try:
        temp_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise StudyReviewError(f"복습 상태를 저장할 수 없습니다: {exc}") from exc


def get_due_cards(cards, review_state, now=None) -> list:
    if not isinstance(cards, list) or not isinstance(review_state, dict):
        raise StudyReviewError("카드 배열과 복습 상태 객체가 필요합니다.")
    current = _local_datetime(now)
    due = []
    for position, card in enumerate(cards):
        if not isinstance(card, dict):
            continue
        card_id = str(card.get("card_id") or "").strip()
        if not card_id:
            continue
        if card.get("local_status") != "approved" or bool(card.get("excluded")):
            continue
        state_item = review_state.get(card_id)
        due_at = _parse_datetime(state_item.get("due_at")) if isinstance(state_item, dict) else None
        if due_at is None or due_at <= current:
            due.append((due_at or datetime.min.replace(tzinfo=current.tzinfo), position, card))
    due.sort(key=lambda item: (item[0], item[1]))
    return [card for _due_at, _position, card in due]


def _next_interval(rating: str, previous_interval: float | None, is_new: bool) -> float:
    if is_new:
        return {
            "again": TEN_MINUTES_IN_DAYS,
            "hard": 1.0,
            "good": 3.0,
            "easy": 7.0,
        }[rating]

    previous = max(TEN_MINUTES_IN_DAYS, float(previous_interval or TEN_MINUTES_IN_DAYS))
    if rating == "again":
        return TEN_MINUTES_IN_DAYS
    if rating == "hard":
        return float(max(1, math.ceil(previous * 1.2)))
    if rating == "good":
        return float(max(round(previous * 2.3), math.floor(previous) + 1))
    return float(max(round(previous * 3.5), math.floor(previous) + 2))


def schedule_review(card_id, rating, review_state, now=None) -> dict:
    card_id = str(card_id or "").strip()
    if not card_id:
        raise StudyReviewError("card_id가 필요합니다.")
    if rating not in RATINGS:
        raise StudyReviewError(f"허용되지 않은 복습 평가입니다: {rating}")
    if not isinstance(review_state, dict):
        raise StudyReviewError("복습 상태는 객체여야 합니다.")

    current = _local_datetime(now)
    previous = review_state.get(card_id)
    previous = previous if isinstance(previous, dict) else {}
    is_new = not bool(previous)
    interval = min(
        float(MAX_INTERVAL_DAYS),
        _next_interval(rating, previous.get("interval_days"), is_new),
    )
    due_at = current + (timedelta(minutes=10) if rating == "again" else timedelta(days=interval))
    updated = {
        "card_id": card_id,
        "due_at": due_at.isoformat(timespec="seconds"),
        "interval_days": interval,
        "review_count": int(previous.get("review_count") or 0) + 1,
        "lapse_count": int(previous.get("lapse_count") or 0) + (1 if rating == "again" else 0),
        "last_rating": rating,
        "last_reviewed_at": current.isoformat(timespec="seconds"),
    }
    review_state[card_id] = updated
    return updated


def append_review_history(workspace, event) -> None:
    if not isinstance(event, dict):
        raise StudyReviewError("복습 이력은 객체여야 합니다.")
    study_dir = Path(workspace) / "study"
    study_dir.mkdir(parents=True, exist_ok=True)
    path = study_dir / "review_history.jsonl"
    temp_path = study_dir / ".review_history.jsonl.tmp"
    try:
        existing = path.read_bytes() if path.exists() else b""
        line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        separator = b"" if not existing or existing.endswith((b"\n", b"\r")) else b"\n"
        temp_path.write_bytes(existing + separator + line)
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise StudyReviewError(f"복습 이력을 저장할 수 없습니다: {exc}") from exc
