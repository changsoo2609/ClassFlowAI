import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable


ALLOWED_SOURCE_TYPES = {
    "concept",
    "code",
    "error_resolution",
    "exam_question",
    "exam_explanation",
    "table_diagram",
    "mixed",
}

ALLOWED_CARD_TYPES = {
    "active_recall",
    "feynman",
    "exam_replay",
    "code_prediction",
    "debugging",
    "comparison",
}

ALLOWED_ANSWER_STATUSES = {
    "confirmed_from_source",
    "ai_suggested",
    "needs_verification",
}

ARRAY_FIELDS = ("source_images", "key_points", "tags", "choices")
MIN_QUESTION_LENGTH = 8
MAX_QUESTION_LENGTH = 300
MAX_CHOICES = 6
NEAR_DUPLICATE_RATIO = 0.90


def _empty_report() -> dict:
    return {
        "valid": True,
        "errors": [],
        "warnings": [],
        "duplicates": [],
        "stats": {
            "total_cards": 0,
            "confirmed_cards": 0,
            "review_required_cards": 0,
            "cards_with_missing_images": 0,
        },
    }


def _card_label(index: int, card_id: str = "") -> str:
    return f"카드 {index + 1} ({card_id})" if card_id else f"카드 {index + 1}"


def _normalize_question(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )


def _normalize_group_value(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).casefold()).strip()


def _normalize_image_reference(value) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if raw.casefold().startswith("images/"):
        raw = raw[7:]
    return raw.casefold()


def _prepare_available_images(available_images: Iterable | None) -> set[str] | None:
    if available_images is None:
        return None
    names: set[str] = set()
    for value in available_images:
        normalized = _normalize_image_reference(value)
        if not normalized:
            continue
        names.add(normalized)
        names.add(Path(normalized).name.casefold())
    return names


def _append_duplicate_id(report: dict, card_id: str) -> None:
    if card_id and card_id not in report["duplicates"]:
        report["duplicates"].append(card_id)


def _validate_question_duplicates(report: dict, questions: list[tuple[str, str]]) -> None:
    for left_index, (left_id, left_question) in enumerate(questions):
        if not left_id or not left_question:
            continue
        for right_id, right_question in questions[left_index + 1 :]:
            if not right_id or not right_question:
                continue

            exact_match = left_question == right_question
            if exact_match:
                similarity = 1.0
            elif min(len(left_question), len(right_question)) < MIN_QUESTION_LENGTH:
                continue
            else:
                similarity = SequenceMatcher(None, left_question, right_question).ratio()

            if similarity < NEAR_DUPLICATE_RATIO:
                continue

            _append_duplicate_id(report, left_id)
            _append_duplicate_id(report, right_id)
            report["warnings"].append(
                f"유사 질문 중복 가능성: {left_id} ↔ {right_id} "
                f"(유사도 {similarity:.2f})"
            )


def _validate_learning_unit_limits(report: dict, groups: dict[tuple, list[str]]) -> None:
    for group_key, card_ids in groups.items():
        if len(card_ids) <= 3:
            continue
        group_type, group_value = group_key
        description = "같은 주제" if group_type == "topic" else "같은 근거 이미지 묶음"
        report["warnings"].append(
            f"학습 단위당 카드가 3장을 초과했을 가능성: {description} "
            f"'{group_value}' → {', '.join(card_ids)}"
        )


def validate_study_cards_payload(payload, available_images=None) -> dict:
    report = _empty_report()
    available = _prepare_available_images(available_images)

    if not isinstance(payload, dict):
        report["errors"].append("최상위 값은 JSON 객체여야 합니다.")
        report["valid"] = False
        return report

    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != 1:
        report["errors"].append("schema_version은 정수 1이어야 합니다.")

    cards = payload.get("cards")
    if not isinstance(cards, list):
        report["errors"].append("cards는 배열이어야 합니다.")
        report["valid"] = False
        return report

    report["stats"]["total_cards"] = len(cards)
    seen_card_ids: dict[str, int] = {}
    questions: list[tuple[str, str]] = []
    learning_groups: dict[tuple, list[str]] = {}

    for index, card in enumerate(cards):
        if not isinstance(card, dict):
            report["errors"].append(f"카드 {index + 1}은 객체여야 합니다.")
            continue

        raw_card_id = card.get("card_id")
        card_id = raw_card_id.strip() if isinstance(raw_card_id, str) else ""
        label = _card_label(index, card_id)

        if not card_id:
            report["errors"].append(f"{label}: card_id가 비어 있습니다.")
        elif card_id in seen_card_ids:
            report["errors"].append(
                f"{label}: card_id가 중복됩니다. "
                f"(처음 등장: 카드 {seen_card_ids[card_id] + 1})"
            )
            _append_duplicate_id(report, card_id)
        else:
            seen_card_ids[card_id] = index

        source_type = card.get("source_type")
        if source_type not in ALLOWED_SOURCE_TYPES:
            report["errors"].append(
                f"{label}: 허용되지 않은 source_type입니다: {source_type!r}"
            )

        card_type = card.get("card_type")
        if card_type not in ALLOWED_CARD_TYPES:
            report["errors"].append(
                f"{label}: 허용되지 않은 card_type입니다: {card_type!r}"
            )

        answer_status = card.get("answer_status")
        if answer_status not in ALLOWED_ANSWER_STATUSES:
            report["errors"].append(
                f"{label}: 허용되지 않은 answer_status입니다: {answer_status!r}"
            )
        elif answer_status == "confirmed_from_source":
            report["stats"]["confirmed_cards"] += 1

        question = card.get("question")
        question_text = question.strip() if isinstance(question, str) else ""
        if not question_text:
            report["errors"].append(f"{label}: question이 비어 있습니다.")
        else:
            question_length = len(question_text)
            if question_length < MIN_QUESTION_LENGTH:
                report["warnings"].append(
                    f"{label}: 질문이 지나치게 짧습니다. ({question_length}자)"
                )
            elif question_length > MAX_QUESTION_LENGTH:
                report["warnings"].append(
                    f"{label}: 질문이 지나치게 깁니다. ({question_length}자)"
                )
            questions.append((card_id, _normalize_question(question_text)))

        arrays: dict[str, list] = {}
        for field_name in ARRAY_FIELDS:
            field_value = card.get(field_name)
            if not isinstance(field_value, list):
                report["errors"].append(f"{label}: {field_name}는 배열이어야 합니다.")
                arrays[field_name] = []
            else:
                arrays[field_name] = field_value

        difficulty = card.get("difficulty")
        if (
            isinstance(difficulty, bool)
            or not isinstance(difficulty, int)
            or not 1 <= difficulty <= 5
        ):
            report["errors"].append(f"{label}: difficulty는 1~5 범위의 정수여야 합니다.")

        answer = card.get("answer")
        answer_text = answer.strip() if isinstance(answer, str) else ""
        if answer_status == "confirmed_from_source" and not answer_text:
            report["errors"].append(
                f"{label}: confirmed_from_source 카드는 answer가 비어 있을 수 없습니다."
            )

        review_required = card.get("review_required")
        if not isinstance(review_required, bool):
            report["errors"].append(f"{label}: review_required는 boolean이어야 합니다.")
        if review_required is True:
            report["stats"]["review_required_cards"] += 1
        if answer_status in {"ai_suggested", "needs_verification"} and review_required is not True:
            report["errors"].append(
                f"{label}: {answer_status} 카드는 review_required가 true여야 합니다."
            )

        source_images = arrays["source_images"]
        if not source_images:
            report["warnings"].append(f"{label}: source_images가 비어 있습니다.")

        missing_images = []
        if available is not None:
            for image_reference in source_images:
                normalized = _normalize_image_reference(image_reference)
                if not normalized:
                    continue
                if normalized not in available and Path(normalized).name.casefold() not in available:
                    missing_images.append(str(image_reference))
            if missing_images:
                report["stats"]["cards_with_missing_images"] += 1
                report["warnings"].append(
                    f"{label}: 근거 이미지 파일을 찾을 수 없습니다: "
                    + ", ".join(missing_images)
                )

        key_points = arrays["key_points"]
        if not answer_text and not key_points:
            report["warnings"].append(
                f"{label}: answer와 key_points가 모두 비어 있습니다."
            )

        choices = arrays["choices"]
        if len(choices) > MAX_CHOICES:
            report["warnings"].append(
                f"{label}: 선택지가 너무 많습니다. ({len(choices)}개, 권장 최대 {MAX_CHOICES}개)"
            )

        topic = _normalize_group_value(card.get("topic"))
        if topic:
            group_key = ("topic", topic)
        else:
            image_group = tuple(
                sorted(
                    normalized
                    for normalized in (
                        _normalize_image_reference(value) for value in source_images
                    )
                    if normalized
                )
            )
            group_key = ("images", "|".join(image_group)) if image_group else None
        if group_key and card_id:
            learning_groups.setdefault(group_key, []).append(card_id)

    _validate_question_duplicates(report, questions)
    _validate_learning_unit_limits(report, learning_groups)
    report["valid"] = not report["errors"]
    return report


def validate_study_cards_file(json_path, images_dir=None) -> dict:
    json_path = Path(json_path)
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        report = _empty_report()
        report["errors"].append(f"파일 읽기 실패: {exc}")
        report["valid"] = False
        return report
    except (json.JSONDecodeError, UnicodeError) as exc:
        report = _empty_report()
        report["errors"].append(f"JSON 파싱 실패: {exc}")
        report["valid"] = False
        return report

    available_images = None
    images_dir_missing = False
    if images_dir is not None:
        images_path = Path(images_dir)
        if images_path.is_dir():
            available_images = [
                path.relative_to(images_path)
                for path in images_path.rglob("*")
                if path.is_file()
            ]
        else:
            available_images = []
            images_dir_missing = True

    report = validate_study_cards_payload(payload, available_images=available_images)
    if images_dir_missing:
        report["warnings"].insert(0, f"images 폴더를 찾을 수 없습니다: {Path(images_dir)}")
    return report
