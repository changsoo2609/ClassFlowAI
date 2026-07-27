import hashlib
import html
import json
import re
from datetime import datetime
from pathlib import Path

from modules.capture_order import active_ordered_records


CODE_FENCE = re.compile(r"```(?P<language>[\w.+#-]*)\s*\n(?P<code>.*?)```", re.DOTALL)
MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
SECTION_TITLE_HEADING = re.compile(r"^\s{0,3}##(?!#)\s+(.+?)\s*$")


def _text(value) -> str:
    return str(value or "").strip()


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _record_id(record: dict, index: int) -> str:
    return _text(record.get("record_id")) or f"capture-{index:03d}"


def _is_ocr_record(record: dict) -> bool:
    return _text(record.get("mode")).lower() == "ocr"


def original_analysis_text(record: dict) -> str:
    if _is_ocr_record(record):
        return _text(
            record.get("flow_interpretation_text")
            or record.get("ocr_interpretation_text")
        )
    return _text(record.get("cap_text"))


def effective_analysis_text(record: dict) -> str:
    if _is_ocr_record(record):
        return _text(
            record.get("flow_interpretation_text_edited")
            or record.get("flow_interpretation_text")
            or record.get("ocr_interpretation_text")
        )
    return _text(record.get("cap_text_edited") or record.get("cap_text"))


def _is_failed_analysis(text: str) -> bool:
    lowered = text.casefold()
    return any(marker in lowered for marker in ("분석 실패", "ocr 실패", "보정 실패", "해석 실패"))


def effective_section_title(record: dict) -> str:
    explicit = _text(
        record.get("flow_title_edited")
        or record.get("flow_title")
        or record.get("title")
        or record.get("memo")
        or record.get("note")
    )
    if explicit:
        return explicit.splitlines()[0][:80]
    legacy_title, _body = _split_leading_heading(effective_analysis_text(record))
    return legacy_title[:80]


def _section_title(record: dict, index: int, analysis: str) -> str:
    explicit = effective_section_title(record)
    if explicit:
        return explicit
    legacy_title, _body = _split_leading_heading(analysis)
    return legacy_title[:80] or f"캡처 {index}"


def _split_leading_heading(value: str) -> tuple[str, str]:
    lines = str(value or "").strip().splitlines()
    if not lines:
        return "", ""
    match = SECTION_TITLE_HEADING.match(lines[0])
    if not match:
        return "", "\n".join(lines).strip()
    return match.group(1).strip(), "\n".join(lines[1:]).strip()


def editable_analysis_text(record: dict) -> str:
    analysis = effective_analysis_text(record)
    if not _is_ocr_record(record):
        return analysis
    legacy_title, body = _split_leading_heading(analysis)
    title = effective_section_title(record) or legacy_title
    body = body if legacy_title else analysis
    return f"## {title}\n{body}".strip() if title else body


def is_analysis_edited(record: dict) -> bool:
    if _is_ocr_record(record):
        return bool(
            _text(record.get("flow_title_edited"))
            or _text(record.get("flow_interpretation_text_edited"))
        )
    return bool(_text(record.get("cap_text_edited")))


def apply_analysis_edit(record: dict, value: str, edited_at: str | None = None) -> None:
    text = _text(value)
    if not text:
        raise ValueError("수정한 해석 내용을 입력하세요.")
    if not original_analysis_text(record):
        raise ValueError("수정할 원본 해석 결과가 없습니다.")

    if _is_ocr_record(record):
        title, body = _split_leading_heading(text)
        if title and not body:
            raise ValueError("제목 아래에 해석 내용을 입력하세요.")
        if not title:
            title = effective_section_title(record)
            body = text
        if title:
            record["flow_title_edited"] = title[:80]
        else:
            record.pop("flow_title_edited", None)
        record["flow_interpretation_text_edited"] = body
    else:
        record["cap_text_edited"] = text

    record["analysis_edited_at"] = edited_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def restore_analysis_original(record: dict) -> bool:
    keys = (
        ("flow_title_edited", "flow_interpretation_text_edited")
        if _is_ocr_record(record)
        else ("cap_text_edited",)
    )
    changed = any(key in record for key in keys)
    for key in (*keys, "analysis_edited_at"):
        record.pop(key, None)
    return changed


def has_editable_analysis(record: dict) -> bool:
    analysis = original_analysis_text(record)
    if not analysis or _is_failed_analysis(analysis):
        return False
    if _is_ocr_record(record):
        return _text(record.get("flow_interpretation_status")).lower() == "done"
    return True


def _inline_markdown(value: str) -> str:
    escaped = html.escape(str(value or ""))
    return re.sub(
        r"(?<!\w)\*\*(?=\S)(.*?\S)\*\*(?!\w)",
        r"<strong>\1</strong>",
        escaped,
    )


def _markdown_html(value: str) -> str:
    blocks = []
    current = []
    for raw_line in str(value or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            if current:
                blocks.append(current)
                current = []
            continue
        heading = MARKDOWN_HEADING.match(line)
        if heading:
            current.append("<strong>" + _inline_markdown(heading.group(1)) + "</strong>")
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.+)$", line)
        if bullet:
            current.append("• " + _inline_markdown(bullet.group(1)))
            continue
        current.append(_inline_markdown(line))
    if current:
        blocks.append(current)
    return "".join("<p>" + "<br>".join(block) + "</p>" for block in blocks)


def _analysis_items(section_id: str, analysis: str) -> list[dict]:
    if not analysis or _is_failed_analysis(analysis):
        return [{
            "id": _stable_id("note", section_id + ":missing"),
            "type": "note",
            "html": "<p>해설을 준비하고 있습니다.</p>",
        }]

    items = []
    cursor = 0
    code_index = 0
    for match in CODE_FENCE.finditer(analysis):
        before = analysis[cursor:match.start()].strip()
        if before:
            items.append({
                "id": _stable_id("explanation", section_id + f":{cursor}"),
                "type": "explanation",
                "html": _markdown_html(before),
            })
        code_index += 1
        items.append({
            "id": _stable_id("code", section_id + f":{code_index}"),
            "type": "code",
            "language": _text(match.group("language")),
            "code": match.group("code").strip(),
            "explanation": "",
        })
        cursor = match.end()
    remainder = analysis[cursor:].strip()
    if remainder:
        items.append({
            "id": _stable_id("explanation", section_id + f":{cursor}"),
            "type": "explanation",
            "html": _markdown_html(remainder),
        })
    return items


def _ocr_analysis_items(section_id: str, record: dict, analysis: str) -> list[dict]:
    status = _text(record.get("flow_interpretation_status")).lower()
    if status == "done" and analysis:
        return _analysis_items(section_id, analysis)
    if status == "waiting_for_api_key":
        message = (
            "수업 흐름 해석을 위한 API 키가 필요합니다.\n"
            "빠른 OCR 결과는 현재 결과 화면에서 확인할 수 있습니다."
        )
    elif status == "failed":
        message = (
            "수업 흐름 해석에 실패했습니다.\n"
            "빠른 OCR 결과는 현재 결과 화면에 유지되어 있습니다."
        )
    elif status in {"queued", "running"}:
        message = "수업 흐름 해설을 백그라운드에서 준비하고 있습니다."
    else:
        message = "수업 흐름 해설을 준비하고 있습니다."
    return [{
        "id": _stable_id("note", section_id + ":ocr-status:" + (status or "pending")),
        "type": "note",
        "html": "<p>" + html.escape(message).replace("\n", "<br>") + "</p>",
    }]


def build_flow_document(records: list[dict], title: str = "수업 흐름") -> dict:
    sections = []
    modes = set()
    for index, record in enumerate(active_ordered_records(records), 1):
        capture_id = _record_id(record, index)
        mode = "ocr" if _text(record.get("mode")).lower() == "ocr" else "cap"
        modes.add(mode)
        analysis = effective_analysis_text(record)
        _legacy_title, analysis_body = _split_leading_heading(analysis)
        group_id = _text(
            record.get("group_id")
            or record.get("capture_group_id")
            or record.get("related_group_id")
            or record.get("bundle_id")
            or record.get("section_id")
        )
        if group_id and sections and sections[-1].get("_groupKey") == group_id:
            section = sections[-1]
        else:
            section_id = _stable_id("section", group_id or capture_id)
            section = {
                "id": section_id,
                "title": _section_title(record, index, analysis),
                "summary": "",
                "items": [],
                "_groupKey": group_id,
            }
            sections.append(section)
        section["items"].append({
            "id": _stable_id("capture", capture_id),
            "type": "capture",
            "captureId": capture_id,
            "imageSrc": _text(record.get("image_path")),
            "alt": f"수업 캡처 {index}",
        })
        if mode == "ocr":
            section["items"].extend(_ocr_analysis_items(section["id"], record, analysis_body))
        else:
            section["items"].extend(_analysis_items(section["id"], analysis_body))
        raw_review_required = record.get("flow_review_required")
        review_required = [
            _text(value)
            for value in (raw_review_required if isinstance(raw_review_required, list) else [])
            if _text(value)
        ]
        if review_required:
            review_html = "<p><strong>확인 필요</strong><br>" + "<br>".join(
                "• " + html.escape(value) for value in review_required
            ) + "</p>"
            section["items"].append({
                "id": _stable_id("note", capture_id + ":review-required"),
                "type": "note",
                "html": review_html,
            })
        memo = _text(record.get("memo") or record.get("note"))
        if memo and memo != section["title"]:
            section["items"].append({
                "id": _stable_id("note", capture_id + ":memo"),
                "type": "note",
                "html": "<p><strong>메모:</strong> " + html.escape(memo).replace("\n", "<br>") + "</p>",
            })

    for section in sections:
        section.pop("_groupKey", None)

    source_mode = next(iter(modes)) if len(modes) == 1 else "mixed"
    return validate_flow_document({
        "id": _stable_id("flow", title + ":" + str(len(sections))),
        "title": title or "수업 흐름",
        "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sourceMode": source_mode,
        "sections": sections,
        "schemaVersion": 1,
    })


def validate_flow_document(value) -> dict:
    document = dict(value) if isinstance(value, dict) else {}
    document["id"] = _text(document.get("id")) or _stable_id("flow", "fallback")
    document["title"] = _text(document.get("title")) or "수업 흐름"
    document["createdAt"] = _text(document.get("createdAt")) or datetime.now().astimezone().isoformat(timespec="seconds")
    if document.get("sourceMode") not in {"ocr", "cap", "mixed"}:
        document["sourceMode"] = "mixed"
    sections = []
    for section_index, value in enumerate(document.get("sections", []), 1):
        if not isinstance(value, dict):
            continue
        section = dict(value)
        section["id"] = _text(section.get("id")) or _stable_id("section", str(section_index))
        section["title"] = _text(section.get("title")) or f"관련 내용 {section_index}"
        section["summary"] = _text(section.get("summary"))
        items = []
        for item_index, item_value in enumerate(section.get("items", []), 1):
            if not isinstance(item_value, dict) or item_value.get("type") not in {"capture", "explanation", "code", "note"}:
                continue
            item = dict(item_value)
            item["id"] = _text(item.get("id")) or _stable_id("item", section["id"] + f":{item_index}")
            if item["type"] == "capture":
                item["captureId"] = _text(item.get("captureId")) or f"capture-{section_index}-{item_index}"
                item["imageSrc"] = _text(item.get("imageSrc"))
                item["alt"] = _text(item.get("alt")) or "수업 캡처"
            elif item["type"] in {"explanation", "note"}:
                item["html"] = _text(item.get("html")) or "<p>확인 필요</p>"
            else:
                item["language"] = _text(item.get("language"))
                item["code"] = _text(item.get("code"))
                item["explanation"] = _text(item.get("explanation"))
            items.append(item)
        section["items"] = items
        sections.append(section)
    document["sections"] = sections
    return document


def save_flow_document(path: Path, document: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(validate_flow_document(document), ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
