import copy
import hashlib
import json
import os
import re
import stat
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from modules.study_card_validator import (
    validate_study_cards_file,
    validate_study_cards_payload,
)


LOCAL_STATUSES = {"pending_review", "approved", "rejected"}
LOCAL_FIELDS = {
    "local_status",
    "imported_at",
    "updated_at",
    "source_package",
    "excluded",
}
UNION_FIELDS = {"source_images", "tags"}
MAX_JSON_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 250 * 1024 * 1024

_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class StudyCardImportError(ValueError):
    pass


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _study_paths(workspace) -> dict[str, Path]:
    root = Path(workspace) / "study"
    return {
        "root": root,
        "cards": root / "cards.json",
        "imports": root / "imports",
        "images": root / "images",
    }


def _ensure_study_paths(workspace) -> dict[str, Path]:
    paths = _study_paths(workspace)
    for key in ("root", "imports", "images"):
        paths[key].mkdir(parents=True, exist_ok=True)
    return paths


def load_local_cards(workspace) -> list:
    cards_path = _study_paths(workspace)["cards"]
    if not cards_path.exists():
        return []
    try:
        value = json.loads(cards_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise StudyCardImportError(f"로컬 학습카드를 읽을 수 없습니다: {exc}") from exc
    if not isinstance(value, list):
        raise StudyCardImportError("로컬 study/cards.json의 최상위 값은 배열이어야 합니다.")
    return value


def save_local_cards(workspace, cards) -> None:
    if not isinstance(cards, list):
        raise StudyCardImportError("저장할 학습카드는 배열이어야 합니다.")
    paths = _ensure_study_paths(workspace)
    temp_path = paths["cards"].with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(cards, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, paths["cards"])


def _normalize_text(value) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )


def _normalize_image_reference(value) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if raw.casefold().startswith("images/"):
        raw = raw[7:]
    return raw.casefold()


def _source_image_signature(card: dict) -> tuple[str, ...]:
    values = card.get("source_images")
    if not isinstance(values, list):
        return ()
    return tuple(sorted({_normalize_image_reference(value) for value in values if str(value or "").strip()}))


def _find_matching_card(cards: list[dict], incoming: dict) -> tuple[int | None, str]:
    incoming_id = str(incoming.get("card_id") or "").strip()
    if incoming_id:
        for index, existing in enumerate(cards):
            if str(existing.get("card_id") or "").strip() == incoming_id:
                return index, "card_id"

    incoming_question = _normalize_text(incoming.get("question"))
    incoming_topic = _normalize_text(incoming.get("topic"))
    if incoming_question and incoming_topic:
        for index, existing in enumerate(cards):
            if (
                _normalize_text(existing.get("question")) == incoming_question
                and _normalize_text(existing.get("topic")) == incoming_topic
            ):
                return index, "question_topic"

    incoming_images = _source_image_signature(incoming)
    incoming_type = str(incoming.get("card_type") or "").strip()
    if incoming_images and incoming_type:
        for index, existing in enumerate(cards):
            if (
                _source_image_signature(existing) == incoming_images
                and str(existing.get("card_type") or "").strip() == incoming_type
            ):
                return index, "images_card_type"

    return None, ""


def _union_values(existing, incoming, field_name: str) -> list:
    existing_list = existing if isinstance(existing, list) else []
    incoming_list = incoming if isinstance(incoming, list) else []
    result = copy.deepcopy(existing_list)
    seen = set()
    for value in result:
        normalized = (
            _normalize_image_reference(value)
            if field_name == "source_images"
            else _normalize_text(value)
        )
        seen.add(normalized)
    for value in incoming_list:
        normalized = (
            _normalize_image_reference(value)
            if field_name == "source_images"
            else _normalize_text(value)
        )
        if normalized in seen:
            continue
        result.append(copy.deepcopy(value))
        seen.add(normalized)
    return result


def merge_study_cards(existing_cards, incoming_cards) -> dict:
    if not isinstance(existing_cards, list) or not isinstance(incoming_cards, list):
        raise StudyCardImportError("기존 카드와 가져올 카드는 배열이어야 합니다.")

    merged_cards = copy.deepcopy(existing_cards)
    added_count = 0
    skipped_count = 0
    merged_count = 0
    conflict_items = []

    for incoming_value in incoming_cards:
        if not isinstance(incoming_value, dict):
            raise StudyCardImportError("가져올 카드 항목은 객체여야 합니다.")
        incoming = copy.deepcopy(incoming_value)
        match_index, matched_by = _find_matching_card(merged_cards, incoming)
        if match_index is None:
            merged_cards.append(incoming)
            added_count += 1
            continue

        existing = merged_cards[match_index]
        combined = copy.deepcopy(existing)
        changed = False
        for field_name in UNION_FIELDS:
            union = _union_values(existing.get(field_name), incoming.get(field_name), field_name)
            if union != existing.get(field_name, []):
                combined[field_name] = union
                changed = True

        conflict_fields = []
        for field_name, incoming_value in incoming.items():
            if field_name in LOCAL_FIELDS or field_name in UNION_FIELDS or field_name == "card_id":
                continue
            if field_name not in existing:
                combined[field_name] = copy.deepcopy(incoming_value)
                changed = True
            elif existing.get(field_name) != incoming_value:
                conflict_fields.append(field_name)

        if changed:
            combined["updated_at"] = _now_iso()
            merged_cards[match_index] = combined

        if conflict_fields:
            conflict_items.append(
                {
                    "existing_card_id": str(existing.get("card_id") or ""),
                    "incoming_card_id": str(incoming.get("card_id") or ""),
                    "matched_by": matched_by,
                    "fields": sorted(conflict_fields),
                }
            )
        elif changed:
            merged_count += 1
        else:
            skipped_count += 1

    return {
        "cards": merged_cards,
        "added_count": added_count,
        "skipped_count": skipped_count,
        "merged_count": merged_count,
        "conflict_count": len(conflict_items),
        "conflicts": conflict_items,
    }


def _validate_path_component(component: str) -> None:
    if not component or component in {".", ".."}:
        raise StudyCardImportError(f"비정상 경로 구성요소를 거부했습니다: {component!r}")
    if component.endswith((" ", ".")) or ":" in component:
        raise StudyCardImportError(f"비정상 경로 구성요소를 거부했습니다: {component!r}")
    if any(ord(character) < 32 for character in component):
        raise StudyCardImportError("제어 문자가 포함된 경로를 거부했습니다.")
    stem = component.split(".", 1)[0].casefold()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise StudyCardImportError(f"예약된 Windows 경로를 거부했습니다: {component!r}")


def _safe_relative_path(value, *, strip_images_prefix: bool = False) -> PurePosixPath:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", raw):
        raise StudyCardImportError(f"절대경로나 빈 경로를 거부했습니다: {value!r}")
    path = PurePosixPath(raw)
    parts = list(path.parts)
    if strip_images_prefix and parts and parts[0].casefold() == "images":
        parts = parts[1:]
    if not parts:
        raise StudyCardImportError(f"비정상 이미지 경로를 거부했습니다: {value!r}")
    for component in parts:
        _validate_path_component(component)
    return PurePosixPath(*parts)


def _validate_zip_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members = {}
    for info in archive.infolist():
        safe_path = _safe_relative_path(info.filename)
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise StudyCardImportError(f"ZIP 심볼릭 링크를 거부했습니다: {info.filename}")
        normalized = safe_path.as_posix().casefold()
        if normalized in members:
            raise StudyCardImportError(f"ZIP에 중복 경로가 있습니다: {info.filename}")
        members[normalized] = info
    return members


def _read_json_bytes(data: bytes, source_name: str) -> dict:
    if len(data) > MAX_JSON_BYTES:
        raise StudyCardImportError(f"study_cards.json이 너무 큽니다: {source_name}")
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StudyCardImportError(f"study_cards.json 파싱 실패: {exc}") from exc
    return payload


def _card_image_references(payload: dict) -> list[PurePosixPath]:
    references = []
    seen = set()
    cards = payload.get("cards") if isinstance(payload, dict) else None
    if not isinstance(cards, list):
        return references
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get("source_images"), list):
            continue
        for value in card["source_images"]:
            reference = _safe_relative_path(value, strip_images_prefix=True)
            normalized = reference.as_posix().casefold()
            if normalized not in seen:
                references.append(reference)
                seen.add(normalized)
    return references


def _prepare_json_source(source_path: Path) -> dict:
    images_dir = source_path.parent / "images"
    validation = validate_study_cards_file(source_path, images_dir=images_dir)
    if validation["errors"]:
        return {"validation": validation, "payload": None, "images": {}, "errors": []}

    try:
        payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StudyCardImportError(f"study_cards.json을 읽을 수 없습니다: {exc}") from exc

    image_sources = {}
    for reference in _card_image_references(payload):
        candidate = images_dir.joinpath(*reference.parts)
        try:
            resolved_root = images_dir.resolve()
            resolved_candidate = candidate.resolve()
        except OSError as exc:
            raise StudyCardImportError(f"근거 이미지 경로를 확인할 수 없습니다: {exc}") from exc
        if resolved_candidate != resolved_root and resolved_root not in resolved_candidate.parents:
            raise StudyCardImportError(f"이미지 경로가 images 폴더를 벗어납니다: {reference}")
        if candidate.is_symlink():
            raise StudyCardImportError(f"이미지 심볼릭 링크를 거부했습니다: {reference}")
        if candidate.is_file():
            size = candidate.stat().st_size
            if size > MAX_IMAGE_BYTES:
                raise StudyCardImportError(f"근거 이미지가 너무 큽니다: {reference}")
            image_sources[reference.as_posix().casefold()] = {
                "name": reference.name,
                "data": candidate.read_bytes(),
            }
    return {
        "validation": validation,
        "payload": payload,
        "images": image_sources,
        "errors": [],
    }


def _prepare_zip_source(source_path: Path) -> dict:
    try:
        archive = zipfile.ZipFile(source_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise StudyCardImportError(f"ZIP 파일을 열 수 없습니다: {exc}") from exc

    with archive:
        members = _validate_zip_members(archive)
        json_candidates = [
            (name, info)
            for name, info in members.items()
            if not info.is_dir() and PurePosixPath(name).name == "study_cards.json"
        ]
        if not json_candidates:
            raise StudyCardImportError("ZIP에서 study_cards.json을 찾을 수 없습니다.")
        if len(json_candidates) > 1:
            raise StudyCardImportError("ZIP에 study_cards.json이 여러 개 있어 가져올 수 없습니다.")

        json_name, json_info = json_candidates[0]
        if json_info.file_size > MAX_JSON_BYTES:
            raise StudyCardImportError("ZIP의 study_cards.json이 너무 큽니다.")
        payload = _read_json_bytes(archive.read(json_info), json_info.filename)

        json_parent = PurePosixPath(json_name).parent
        images_prefix = (
            PurePosixPath("images")
            if str(json_parent) == "."
            else json_parent / "images"
        )
        available_images = []
        image_members = {}
        prefix_parts = images_prefix.parts
        for member_name, info in members.items():
            member_path = PurePosixPath(member_name)
            if info.is_dir() or member_path.parts[: len(prefix_parts)] != prefix_parts:
                continue
            remaining = member_path.parts[len(prefix_parts) :]
            if not remaining:
                continue
            relative = PurePosixPath(*remaining).as_posix()
            available_images.append(relative)
            image_members[relative.casefold()] = info

        validation = validate_study_cards_payload(payload, available_images=available_images)
        if validation["errors"]:
            return {"validation": validation, "payload": None, "images": {}, "errors": []}

        image_sources = {}
        total_size = 0
        for reference in _card_image_references(payload):
            info = image_members.get(reference.as_posix().casefold())
            if info is None:
                continue
            if info.file_size > MAX_IMAGE_BYTES:
                raise StudyCardImportError(f"ZIP 근거 이미지가 너무 큽니다: {reference}")
            total_size += info.file_size
            if total_size > MAX_TOTAL_IMAGE_BYTES:
                raise StudyCardImportError("ZIP 근거 이미지의 전체 크기가 제한을 초과합니다.")
            image_sources[reference.as_posix().casefold()] = {
                "name": reference.name,
                "data": archive.read(info),
            }

    return {
        "validation": validation,
        "payload": payload,
        "images": image_sources,
        "errors": [],
    }


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _existing_file_by_name(images_dir: Path, name: str) -> Path | None:
    wanted = name.casefold()
    for path in images_dir.iterdir():
        if path.is_file() and path.name.casefold() == wanted:
            return path
    return None


def _write_image(images_dir: Path, preferred_name: str, data: bytes) -> tuple[str, str]:
    source_hash = _hash_bytes(data)
    existing = _existing_file_by_name(images_dir, preferred_name)
    if existing is not None and _hash_bytes(existing.read_bytes()) == source_hash:
        return existing.name, "reused"

    renamed = existing is not None
    if existing is None:
        target = images_dir / preferred_name
    else:
        source_path = Path(preferred_name)
        suffix = source_path.suffix
        stem = source_path.stem or "image"
        target = images_dir / f"{stem}_{source_hash[:8]}{suffix}"
        counter = 2
        while True:
            collision = _existing_file_by_name(images_dir, target.name)
            if collision is None:
                break
            if _hash_bytes(collision.read_bytes()) == source_hash:
                return collision.name, "reused"
            target = images_dir / f"{stem}_{source_hash[:8]}_{counter}{suffix}"
            counter += 1

    temp_path = images_dir / f".{target.name}.{source_hash[:12]}.tmp"
    temp_path.write_bytes(data)
    os.replace(temp_path, target)
    return target.name, "renamed" if renamed else "copied"


def _store_referenced_images(payload: dict, image_sources: dict, images_dir: Path) -> dict:
    stats = {"copied": 0, "reused": 0, "renamed": 0, "missing": []}
    stored_names = {}
    for reference_key, source in image_sources.items():
        stored_name, action = _write_image(images_dir, source["name"], source["data"])
        stored_names[reference_key] = stored_name
        stats[action] += 1

    cards = payload.get("cards") if isinstance(payload, dict) else []
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get("source_images"), list):
            continue
        updated_references = []
        seen = set()
        for raw_reference in card["source_images"]:
            safe_reference = _safe_relative_path(raw_reference, strip_images_prefix=True)
            reference_key = safe_reference.as_posix().casefold()
            stored_name = stored_names.get(reference_key)
            local_reference = stored_name or safe_reference.as_posix()
            normalized = local_reference.casefold()
            if normalized not in seen:
                updated_references.append(local_reference)
                seen.add(normalized)
            if stored_name is None and local_reference not in stats["missing"]:
                stats["missing"].append(local_reference)
        card["source_images"] = updated_references
    return stats


def _decorate_incoming_cards(payload: dict, source_package: str) -> list[dict]:
    imported_at = _now_iso()
    subject = str(payload.get("subject") or "").strip()
    decorated = []
    for value in payload.get("cards") or []:
        card = copy.deepcopy(value)
        if subject and not str(card.get("subject") or "").strip():
            card["subject"] = subject
        card["local_status"] = "pending_review"
        card["imported_at"] = imported_at
        card["updated_at"] = imported_at
        card["source_package"] = source_package
        card["excluded"] = False
        decorated.append(card)
    return decorated


def _base_report(source_path: Path) -> dict:
    return {
        "success": False,
        "requires_confirmation": False,
        "source_package": source_path.name,
        "read_count": 0,
        "added_count": 0,
        "merged_count": 0,
        "skipped_count": 0,
        "conflict_count": 0,
        "conflicts": [],
        "review_required_count": 0,
        "errors": [],
        "warnings": [],
        "validation": None,
        "images": {"copied": 0, "reused": 0, "renamed": 0, "missing": []},
    }


def _write_import_history(paths: dict[str, Path], source_path: Path, report: dict) -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_stem = re.sub(r"[^A-Za-z0-9가-힣._-]+", "_", source_path.stem)[:60] or "import"
    history_path = paths["imports"] / f"{timestamp}_{safe_stem}.json"
    history = {
        "source_package": source_path.name,
        "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "imported_at": _now_iso(),
        "read_count": report["read_count"],
        "added_count": report["added_count"],
        "merged_count": report["merged_count"],
        "skipped_count": report["skipped_count"],
        "conflict_count": report["conflict_count"],
        "review_required_count": report["review_required_count"],
        "validation": {
            "valid": report["validation"]["valid"],
            "errors": report["validation"]["errors"],
            "warnings": report["validation"]["warnings"],
            "duplicates": report["validation"]["duplicates"],
            "stats": report["validation"]["stats"],
        },
        "images": report["images"],
    }
    history_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def import_study_cards(source_path, workspace, confirm_warnings=False) -> dict:
    source_path = Path(source_path)
    report = _base_report(source_path)
    if not source_path.is_file():
        report["errors"].append(f"가져올 파일을 찾을 수 없습니다: {source_path.name}")
        return report

    try:
        suffix = source_path.suffix.casefold()
        if suffix == ".json":
            prepared = _prepare_json_source(source_path)
        elif suffix == ".zip":
            prepared = _prepare_zip_source(source_path)
        else:
            raise StudyCardImportError("study_cards.json 또는 notion_paste_package.zip만 가져올 수 있습니다.")

        validation = prepared["validation"]
        report["validation"] = validation
        report["errors"].extend(validation["errors"])
        report["warnings"].extend(validation["warnings"])
        if validation["errors"] or prepared["payload"] is None:
            return report

        payload = prepared["payload"]
        report["read_count"] = len(payload.get("cards") or [])
        if validation["warnings"] and not confirm_warnings:
            report["requires_confirmation"] = True
            return report

        paths = _ensure_study_paths(workspace)
        existing_cards = load_local_cards(workspace)
        image_stats = _store_referenced_images(payload, prepared["images"], paths["images"])
        report["images"] = image_stats
        if image_stats["missing"]:
            report["warnings"].append(
                "가져오지 못한 근거 이미지: " + ", ".join(image_stats["missing"])
            )

        incoming_cards = _decorate_incoming_cards(payload, source_path.name)
        merge_report = merge_study_cards(existing_cards, incoming_cards)
        save_local_cards(workspace, merge_report["cards"])

        report.update(
            {
                "success": True,
                "added_count": merge_report["added_count"],
                "merged_count": merge_report["merged_count"],
                "skipped_count": merge_report["skipped_count"],
                "conflict_count": merge_report["conflict_count"],
                "conflicts": merge_report["conflicts"],
                "review_required_count": sum(
                    1
                    for card in incoming_cards
                    if card.get("local_status") == "pending_review"
                ),
            }
        )
        try:
            _write_import_history(paths, source_path, report)
        except OSError as exc:
            report["warnings"].append(f"가져오기 이력을 저장하지 못했습니다: {exc}")
        return report
    except (OSError, StudyCardImportError, zipfile.BadZipFile) as exc:
        report["errors"].append(str(exc))
        return report
