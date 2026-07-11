import json
from datetime import datetime
from pathlib import Path
from typing import Any


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
