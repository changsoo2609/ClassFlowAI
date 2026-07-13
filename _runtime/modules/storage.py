import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


LESSONS_DIR_NAME = "lessons"
CURRENT_LESSON_FILE_NAME = ".classflow_current_lesson.json"
LESSON_METADATA_FILE_NAME = "lesson.json"


def get_desktop() -> Path:
    candidates = [
        Path.home() / "Desktop",
        Path.home() / "바탕 화면",
        Path.home() / "바탕화면",
    ]
    for path in candidates:
        if path.exists():
            return path
    return Path.home()


def get_default_workspace(use_daily_folder: bool = True) -> Path:
    root = get_desktop() / "학습용" / "ClassFlowAI" / "실시간저장"
    if use_daily_folder:
        return root / datetime.now().strftime("%Y-%m-%d")
    return root


def _cleanup_known_garbage(outputs: Path) -> None:
    # 이전 버전에서 만들던 불필요한 산출물만 안전하게 제거한다.
    for name in [
        "today_notes.md",
        "today_summary.md",
        "quiz.md",
        "notion_paste.html",
        "OCR_TIMELINE.md",
        "notion_paste_preview.html",
    ]:
        p = outputs / name
        try:
            if p.exists() and p.is_file():
                p.unlink()
        except Exception:
            pass


def ensure_workspace(workspace: Path) -> dict[str, Path]:
    captures = workspace / "captures"
    logs = workspace / "logs"
    outputs = workspace / "outputs"
    state = workspace / "state"
    gpt_handoff = outputs / "gpt_handoff"

    for folder in [workspace, captures, logs, outputs, state, gpt_handoff]:
        folder.mkdir(parents=True, exist_ok=True)

    _cleanup_known_garbage(outputs)

    return {
        "workspace": workspace,
        "captures": captures,
        "logs": logs,
        "outputs": outputs,
        "gpt_handoff": gpt_handoff,
        "events": logs / "events.jsonl",
        "records": state / "capture_records.json",
        "notion_preview_html": outputs / "html_flow_preview.html",
        "capture_timeline": outputs / "CAPTURE_TIMELINE.md",
    }


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def lesson_metadata_path(workspace: Path) -> Path:
    return Path(workspace) / "state" / LESSON_METADATA_FILE_NAME


def is_lesson_workspace(workspace: Path) -> bool:
    """Return whether a folder can be opened as a ClassFlowAI lesson."""
    workspace = Path(workspace)
    if not workspace.is_dir():
        return False
    return any(
        path.exists()
        for path in [
            lesson_metadata_path(workspace),
            workspace / "state" / "capture_records.json",
            workspace / "captures",
        ]
    )


def set_current_lesson(storage_root: Path, lesson_workspace: Path) -> None:
    storage_root = Path(storage_root).resolve()
    lesson_workspace = Path(lesson_workspace).resolve()
    write_json_atomic(
        storage_root / CURRENT_LESSON_FILE_NAME,
        {
            "workspace": str(lesson_workspace),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


def get_current_lesson(storage_root: Path) -> Path:
    """Resolve the last lesson selected for a storage root.

    Existing installations have captures directly in ``storage_root`` and no
    pointer file. In that case the storage root itself remains the active lesson.
    """
    storage_root = Path(storage_root).resolve()
    pointer_path = storage_root / CURRENT_LESSON_FILE_NAME
    try:
        value = json.loads(pointer_path.read_text(encoding="utf-8"))
        candidate = Path(str(value.get("workspace") or "")).expanduser()
        if is_lesson_workspace(candidate):
            return candidate.resolve()
    except Exception:
        pass
    return storage_root


def create_lesson_workspace(storage_root: Path) -> Path:
    storage_root = Path(storage_root).resolve()
    lessons_root = storage_root / LESSONS_DIR_NAME
    lessons_root.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    stem = now.strftime("lesson_%Y-%m-%d_%H-%M-%S")
    lesson_workspace = lessons_root / stem
    suffix = 2
    while lesson_workspace.exists():
        lesson_workspace = lessons_root / f"{stem}_{suffix}"
        suffix += 1

    paths = ensure_workspace(lesson_workspace)
    write_json_atomic(
        lesson_metadata_path(lesson_workspace),
        {
            "lesson_id": lesson_workspace.name,
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "workspace": str(lesson_workspace),
        },
    )
    paths["records"].write_text("[]\n", encoding="utf-8")
    return lesson_workspace


def timestamp_file() -> str:
    now = datetime.now()
    return now.strftime("%Y-%m-%d_%H-%M-%S") + f"_{int(now.microsecond / 1000):03d}"


def timestamp_log() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def display_time() -> str:
    return datetime.now().strftime("%H:%M:%S")


def append_event(events_path: Path, event: dict[str, Any]) -> None:
    event.setdefault("time", timestamp_log())
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
