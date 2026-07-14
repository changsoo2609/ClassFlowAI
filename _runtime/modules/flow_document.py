import hashlib
import html
import json
import re
from datetime import datetime
from pathlib import Path

from modules.capture_order import active_ordered_records


CODE_FENCE = re.compile(r"```(?P<language>[\w.+#-]*)\s*\n(?P<code>.*?)```", re.DOTALL)


def _text(value) -> str:
    return str(value or "").strip()


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _record_id(record: dict, index: int) -> str:
    return _text(record.get("record_id")) or f"capture-{index:03d}"


def _analysis_text(record: dict) -> str:
    if _text(record.get("mode")).lower() == "ocr":
        return _text(record.get("ocr_interpretation_text"))
    return _text(record.get("cap_text"))


def _is_failed_analysis(text: str) -> bool:
    lowered = text.casefold()
    return any(marker in lowered for marker in ("분석 실패", "ocr 실패", "보정 실패", "해석 실패"))


def _section_title(record: dict, index: int, analysis: str) -> str:
    explicit = _text(record.get("title") or record.get("memo") or record.get("note"))
    if explicit:
        return explicit.splitlines()[0][:80]
    first_line = next((line.strip("# -*\t") for line in analysis.splitlines() if line.strip()), "")
    return first_line[:80] or f"캡처 {index}"


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
                "html": "<p>" + html.escape(before).replace("\n", "<br>") + "</p>",
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
            "html": "<p>" + html.escape(remainder).replace("\n", "<br>") + "</p>",
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
        analysis = _analysis_text(record)
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
            section["items"].extend(_ocr_analysis_items(section["id"], record, analysis))
        else:
            section["items"].extend(_analysis_items(section["id"], analysis))
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
