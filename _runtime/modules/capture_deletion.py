from pathlib import Path


CAPTURE_FILE_FIELDS = (
    "image_path",
    "thumbnail_path",
    "thumb_path",
    "preview_path",
    "ocr_result_path",
    "ocr_path",
    "temp_path",
    "temporary_path",
)
CAPTURE_FILE_LIST_FIELDS = (
    "thumbnail_files",
    "ocr_result_files",
    "temp_files",
    "temporary_files",
)


class CaptureDeletionError(RuntimeError):
    def __init__(self, message, path=None):
        super().__init__(message)
        self.path = Path(path) if path else None


def _record_paths(record: dict, workspace: Path | None = None) -> list[Path]:
    values = []
    for field in CAPTURE_FILE_FIELDS:
        value = str(record.get(field) or "").strip()
        if value:
            values.append(Path(value))
    for field in CAPTURE_FILE_LIST_FIELDS:
        field_values = record.get(field)
        if isinstance(field_values, list):
            values.extend(Path(str(value)) for value in field_values if str(value or "").strip())
    unique = []
    seen = set()
    for value in values:
        if workspace is not None and not value.is_absolute():
            value = Path(workspace) / value
        resolved = value.resolve()
        key = str(resolved).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


def _inside_workspace(path: Path, workspace: Path) -> bool:
    path = path.resolve()
    workspace = workspace.resolve()
    return path != workspace and workspace in path.parents


def delete_capture_files(workspace: Path, record: dict, all_records: list[dict]) -> dict:
    workspace = Path(workspace).resolve()
    paths = _record_paths(record, workspace)
    original_value = str(record.get("image_path") or "").strip()
    if not original_value:
        raise CaptureDeletionError("캡처 원본 파일 경로가 없습니다.")
    original_path = Path(original_value)
    if not original_path.is_absolute():
        original_path = workspace / original_path
    original = original_path.resolve()
    if original not in paths:
        paths.insert(0, original)

    for path in paths:
        if not _inside_workspace(path, workspace):
            raise CaptureDeletionError("수업 폴더 외부 파일은 삭제할 수 없습니다.", path)

    other_references = set()
    for other in all_records:
        if other is record:
            continue
        other_references.update(str(path).casefold() for path in _record_paths(other, workspace))

    deleted = []
    retained_shared = []
    failed_related = []
    for path in paths:
        if str(path).casefold() in other_references:
            retained_shared.append(path)
            continue
        existed = path.exists()
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            if path == original:
                raise CaptureDeletionError(f"캡처 원본 파일을 삭제하지 못했습니다: {exc}", path) from exc
            failed_related.append({"path": path, "error": str(exc)})
            continue
        if existed:
            deleted.append(path)
    return {
        "original": original,
        "deleted": deleted,
        "retained_shared": retained_shared,
        "failed_related": failed_related,
    }
