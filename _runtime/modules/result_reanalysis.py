from datetime import datetime
from pathlib import Path


def _text(value) -> str:
    return str(value or "").strip()


def _timestamp(value: str | None = None) -> str:
    return value or datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def result_type(record: dict) -> str:
    return "ocr" if _text(record.get("mode")).lower() == "ocr" else "cap"


def current_result(record: dict) -> str:
    if result_type(record) == "ocr":
        return _text(record.get("ocr_corrected_text") or record.get("ocr_text"))
    return _text(record.get("cap_text_edited") or record.get("cap_text"))


def previous_result(record: dict) -> str:
    if result_type(record) == "ocr":
        return _text(record.get("ocr_previous_corrected_text"))
    return _text(record.get("cap_text_previous"))


def has_previous_result(record: dict) -> bool:
    return bool(previous_result(record))


def _invalidate_ocr_flow(record: dict) -> None:
    for key in (
        "flow_title",
        "flow_title_edited",
        "flow_interpretation_text",
        "flow_interpretation_text_edited",
        "ocr_interpretation_text",
        "flow_continues_previous",
        "flow_review_required",
        "flow_interpretation_parse_fallback",
        "ocr_interpretation_error",
        "flow_interpretation_error",
        "flow_interpretation_status",
        "group_id",
    ):
        record.pop(key, None)
    record["flow_interpretation_needs_refresh"] = True


def begin_reanalysis(
    record: dict,
    workspace: Path,
    job_id: str,
    started_at: str | None = None,
) -> dict:
    if _text(record.get("result_reanalysis_status")).lower() == "running":
        raise ValueError("이미 결과 재분석이 진행 중입니다.")

    capture_id = _text(record.get("record_id"))
    current = current_result(record)
    if not capture_id:
        raise ValueError("캡처 ID가 없습니다.")
    if not current:
        raise ValueError("다시 분석할 현재 결과가 없습니다.")

    try:
        version = max(0, int(record.get("result_reanalysis_request_version") or 0)) + 1
    except (TypeError, ValueError):
        version = 1
    workspace_value = str(Path(workspace).resolve())
    started_value = _timestamp(started_at)
    kind = result_type(record)
    job = {
        "job_id": _text(job_id),
        "capture_id": capture_id,
        "workspace": workspace_value,
        "result_type": kind,
        "request_version": version,
        "started_at": started_value,
        "current_result": current,
    }
    if not job["job_id"]:
        raise ValueError("재분석 작업 ID가 없습니다.")

    record["result_reanalysis_job_id"] = job["job_id"]
    record["result_reanalysis_capture_id"] = capture_id
    record["result_reanalysis_workspace"] = workspace_value
    record["result_reanalysis_result_type"] = kind
    record["result_reanalysis_request_version"] = version
    record["result_reanalysis_started_at"] = started_value
    record["result_reanalysis_status"] = "running"
    record.pop("result_reanalysis_error", None)
    return job


def is_current_reanalysis(record: dict, job: dict) -> bool:
    if not isinstance(record, dict) or record.get("deleted"):
        return False
    try:
        record_version = int(record.get("result_reanalysis_request_version") or 0)
        job_version = int(job.get("request_version") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        _text(record.get("record_id")) == _text(job.get("capture_id"))
        and _text(record.get("result_reanalysis_job_id")) == _text(job.get("job_id"))
        and _text(record.get("result_reanalysis_workspace")) == _text(job.get("workspace"))
        and _text(record.get("result_reanalysis_result_type")) == _text(job.get("result_type"))
        and record_version == job_version
    )


def apply_reanalysis_success(
    record: dict,
    job: dict,
    result: str,
    completed_at: str | None = None,
) -> bool:
    if not is_current_reanalysis(record, job):
        return False
    value = _text(result)
    if not value:
        return False

    previous = _text(job.get("current_result"))
    if _text(job.get("result_type")) == "ocr":
        flow_was_pending = _text(record.get("flow_interpretation_status")).lower() in {
            "queued",
            "running",
        }
        record["ocr_previous_corrected_text"] = previous
        record["ocr_corrected_text"] = value
        record["ocr_correction_at"] = _timestamp(completed_at)
        record["status"] = "ocr_corrected"
        record["display_result_type"] = "ocr_corrected"
        record.pop("ocr_correction_error", None)
        _invalidate_ocr_flow(record)
        if flow_was_pending:
            record["flow_interpretation_requeue"] = True
    else:
        record["cap_text_previous"] = previous
        record["cap_text_edited"] = value
        record["analysis_edited_at"] = _timestamp(completed_at)
        record["status"] = "cap_done"
        record["display_result_type"] = "cap"

    record["result_reanalysis_status"] = "done"
    record["result_reanalysis_completed_at"] = _timestamp(completed_at)
    record.pop("result_reanalysis_error", None)
    return True


def restore_previous_result(record: dict, restored_at: str | None = None) -> bool:
    if _text(record.get("result_reanalysis_status")).lower() == "running":
        return False
    previous = previous_result(record)
    current = current_result(record)
    if not previous or not current:
        return False

    if result_type(record) == "ocr":
        record["ocr_previous_corrected_text"] = current
        record["ocr_corrected_text"] = previous
        record["ocr_correction_at"] = _timestamp(restored_at)
        record["status"] = "ocr_corrected"
        record["display_result_type"] = "ocr_corrected"
        _invalidate_ocr_flow(record)
    else:
        record["cap_text_previous"] = current
        record["cap_text_edited"] = previous
        record["analysis_edited_at"] = _timestamp(restored_at)
        record["status"] = "cap_done"
        record["display_result_type"] = "cap"

    record["result_reanalysis_status"] = "done"
    record["result_reanalysis_completed_at"] = _timestamp(restored_at)
    record.pop("result_reanalysis_error", None)
    return True


def apply_reanalysis_failure(
    record: dict,
    job: dict,
    error: str,
    completed_at: str | None = None,
) -> bool:
    if not is_current_reanalysis(record, job):
        return False
    record["result_reanalysis_status"] = "failed"
    record["result_reanalysis_completed_at"] = _timestamp(completed_at)
    record["result_reanalysis_error"] = _text(error)[:1000] or "알 수 없는 오류"
    return True
