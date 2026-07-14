import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from tkinter import messagebox, filedialog, ttk
from tkinter.scrolledtext import ScrolledText
import tkinter as tk

from PIL import Image, ImageTk

try:
    from pynput import keyboard as pynput_keyboard
    from pynput import mouse as pynput_mouse
except Exception:
    pynput_keyboard = None
    pynput_mouse = None

from modules.clipboard_watcher import (
    clipboard_sequence_changed,
    copy_image_to_clipboard,
    get_clipboard_image,
    get_clipboard_sequence_number,
    image_hash,
    save_image,
)
from modules.capture_order import (
    active_ordered_records,
    move_record,
    next_display_order,
    normalize_display_orders,
    restore_capture_order_if_confirmed,
)
from modules.capture_deletion import delete_capture_files
from modules.flow_document import (
    build_flow_document,
    save_flow_document,
)
from modules.flow_window import open_flow_result_window
from modules.storage import (
    append_event,
    create_lesson_workspace,
    ensure_workspace,
    get_current_lesson,
    get_default_workspace,
    is_lesson_workspace,
    set_current_lesson,
    short_workspace_display,
    timestamp_file,
    write_json_atomic,
)
from modules.ocr_engine import extract_text_from_image
from modules.nvidia_cap_reasoner import (
    analyze_capture_image,
    correct_ocr_with_image,
    DEFAULT_CAP_PROMPT,
)


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"  # 배포 기본값: 읽기 전용으로 취급
LEGACY_CONFIG_LOCAL_PATH = BASE_DIR / "config.local.json"

_USER_CONFIG_ROOT = os.environ.get("LOCALAPPDATA")
if _USER_CONFIG_ROOT:
    USER_CONFIG_DIR = Path(_USER_CONFIG_ROOT) / "ClassFlowAI"
else:
    USER_CONFIG_DIR = Path.home() / ".classflowai"

USER_CONFIG_PATH = USER_CONFIG_DIR / "settings.json"
USER_SECRET_PATH = USER_CONFIG_DIR / "secrets.json"

EXIT_CONFIRMATION_MESSAGE = (
    "ClassFlowAI를 종료할까요?\n"
    "현재 저장된 수업 기록과 이미지는 유지됩니다."
)


def confirm_application_exit(confirm_callback, close_callback) -> bool:
    if not confirm_callback("프로그램 종료", EXIT_CONFIRMATION_MESSAGE):
        return False
    close_callback()
    return True


def bind_mini_widget_events(widgets, start_drag, drag, end_drag, open_main, open_menu) -> None:
    for widget in widgets:
        widget.bind("<ButtonPress-1>", start_drag)
        widget.bind("<B1-Motion>", drag)
        widget.bind("<ButtonRelease-1>", end_drag)
        widget.bind("<Double-Button-1>", open_main)
        widget.bind("<Button-3>", open_menu)


def listbox_index_at_y(listbox, y: int) -> int | None:
    """Return an item only when the pointer is inside its rendered row."""
    try:
        if int(listbox.size()) <= 0:
            return None
        index = int(listbox.nearest(y))
        bbox = listbox.bbox(index)
        if not bbox:
            return None
        _, row_y, _, row_height = bbox
        if not (row_y <= y < row_y + row_height):
            return None
        return index
    except Exception:
        return None


def get_hidden_subprocess_kwargs() -> dict:
    if not sys.platform.startswith("win"):
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def popen_hidden_command(cmd, cwd=None):
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **get_hidden_subprocess_kwargs(),
    )


def _read_json_dict(path: Path) -> dict:
    try:
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
    except Exception:
        pass
    return {}


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def load_config() -> dict:
    default_config = {
        "settings_schema_version": 5,
        "workspace_dir": "",
        "use_daily_folder": True,
        "poll_interval_sec": 1.0,
        "ignore_existing_clipboard_on_start": True,
        "hide_app_during_screenshot": True,
        "pause_on_start": False,
        "screenshot_hotkey": "ctrl+shift+s",
        "mode_toggle_hotkey": "middle",
        "pause_toggle_hotkey": "ctrl+middle",
        "show_window_hotkey": "shift+middle",
        "capture_mode": "capture",
        "mini_status_enabled": True,
        "mini_status_topmost": True,
        "mini_status_x": 20,
        "mini_status_y": 20,
        "mini_status_width": 56,
        "mini_status_height": 56,
        "ocr_provider": "nvidia_nim",
        "nvidia_api_key": "",
        "nvidia_ocr_model": "nvidia/nemotron-ocr-v2",
        "nvidia_model_url": "https://build.nvidia.com/nvidia/nemotron-ocr-v2",
        "nvidia_api_base": "https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-ocr-v2",
        "nvidia_ocr_timeout_sec": 60,
        "ocr_upscale_enabled": True,
        "ocr_upscale_factor": 2.5,
        "ocr_target_long_side": 2800,
        "ocr_max_long_side": 3600,
        "ocr_image_format": "png",
        "ocr_preprocess_mode": "sharp_gray",
        "ocr_post_cleanup_enabled": True,
        "copy_ocr_to_clipboard_on_done": True,
        "cap_reasoning_model": "qwen/qwen3.5-397b-a17b",
        "cap_reasoning_model_url": "https://build.nvidia.com/qwen/qwen3.5-397b-a17b",
        "cap_reasoning_prompt": DEFAULT_CAP_PROMPT,
        "cap_reasoning_api_base": "https://integrate.api.nvidia.com/v1/chat/completions",
        "cap_reasoning_connect_timeout_sec": 15,
        "cap_reasoning_timeout_sec": 150,
        "cap_reasoning_max_tokens": 4096,
        "cap_reasoning_max_long_side": 3200,
        "copy_cap_to_clipboard_on_done": False,
    }

    # 1. 배포 폴더의 config.json은 기능 기본값만 읽습니다.
    # API 키가 실수로 들어가 있어도 절대 사용하지 않습니다.
    packaged_config = _read_json_dict(CONFIG_PATH)
    packaged_config.pop("nvidia_api_key", None)
    default_config.update(packaged_config)

    # 2. API 키와 사용자 설정은 Windows 사용자별 LOCALAPPDATA에서만 읽습니다.
    # 프로그램 폴더의 config.local.json은 배포 보안을 위해 읽지 않습니다.
    user_config = _read_json_dict(USER_CONFIG_PATH)
    default_config.update(user_config)
    # 재시도는 modules.model_retry의 공통 정책으로만 관리한다.
    default_config.pop("cap_reasoning_retry_count", None)
    default_config.update(_read_json_dict(USER_SECRET_PATH))

    # 이전 설정을 현재 배포 기본값으로 1회 정리합니다.
    try:
        schema_version = int(
            user_config.get("settings_schema_version", 0)
            or 0
        )
    except Exception:
        schema_version = 0

    migrated = dict(user_config)
    migration_needed = False
    removed_export_settings = (
        "html_flow_subject",
        "html_flow_prompt_template",
        "chatgpt_handoff_subject",
        "chatgpt_handoff_prompt_template",
    )
    for key in removed_export_settings:
        default_config.pop(key, None)
        if key in migrated:
            migrated.pop(key, None)
            migration_needed = True

    if schema_version < 2:
        old_mode = str(
            user_config.get(
                "mode_toggle_hotkey",
                "ctrl+middle",
            )
            or ""
        ).strip().lower()
        old_pause = str(
            user_config.get(
                "pause_toggle_hotkey",
                "middle",
            )
            or ""
        ).strip().lower()

        if (
            old_mode == "ctrl+middle"
            and old_pause == "middle"
        ):
            default_config["mode_toggle_hotkey"] = "middle"
            default_config["pause_toggle_hotkey"] = "ctrl+middle"

        migrated["mode_toggle_hotkey"] = default_config.get(
            "mode_toggle_hotkey",
            "middle",
        )
        migrated["pause_toggle_hotkey"] = default_config.get(
            "pause_toggle_hotkey",
            "ctrl+middle",
        )
        migration_needed = True

    if schema_version < 4:
        # 이전 테스트 키는 이번 배포에서 1회 삭제합니다.
        try:
            if USER_SECRET_PATH.exists():
                USER_SECRET_PATH.unlink()
        except Exception:
            pass

        default_config["nvidia_api_key"] = ""

        # 모델은 배포 기본값으로 1회 초기화합니다.
        default_ocr_model = "nvidia/nemotron-ocr-v2"
        default_cap_model = "qwen/qwen3.5-397b-a17b"

        default_config["nvidia_ocr_model"] = default_ocr_model
        default_config["nvidia_model_url"] = (
            f"https://build.nvidia.com/{default_ocr_model}"
        )
        default_config["nvidia_api_base"] = (
            f"https://ai.api.nvidia.com/v1/cv/{default_ocr_model}"
        )
        default_config["cap_reasoning_model"] = default_cap_model
        default_config["cap_reasoning_model_url"] = (
            f"https://build.nvidia.com/{default_cap_model}"
        )

        default_config["mini_status_width"] = 56
        default_config["mini_status_height"] = 56

        migrated.update(
            {
                "settings_schema_version": 4,
                "nvidia_ocr_model": default_ocr_model,
                "nvidia_model_url": (
                    f"https://build.nvidia.com/{default_ocr_model}"
                ),
                "nvidia_api_base": (
                    f"https://ai.api.nvidia.com/v1/cv/{default_ocr_model}"
                ),
                "cap_reasoning_model": default_cap_model,
                "cap_reasoning_model_url": (
                    f"https://build.nvidia.com/{default_cap_model}"
                ),
                "mini_status_width": 56,
                "mini_status_height": 56,
            }
        )
        migration_needed = True

    if schema_version < 5:
        default_config["copy_cap_to_clipboard_on_done"] = False
        migrated["copy_cap_to_clipboard_on_done"] = False

        migrated["settings_schema_version"] = 5
        migration_needed = True

    if migration_needed:
        try:
            _write_json_atomic(
                USER_CONFIG_PATH,
                migrated,
            )
        except Exception:
            pass

    if not str(default_config.get("cap_reasoning_prompt") or "").strip():
        default_config["cap_reasoning_prompt"] = DEFAULT_CAP_PROMPT

    return default_config


def save_config(config: dict) -> None:
    visible = {
        "settings_schema_version": 5,
        "workspace_dir": config.get("workspace_dir", ""),
        "use_daily_folder": bool(config.get("use_daily_folder", True)),
        "poll_interval_sec": float(config.get("poll_interval_sec", 1.0)),
        "ignore_existing_clipboard_on_start": bool(config.get("ignore_existing_clipboard_on_start", True)),
        "hide_app_during_screenshot": bool(config.get("hide_app_during_screenshot", True)),
        "pause_on_start": bool(config.get("pause_on_start", False)),
        "screenshot_hotkey": str(config.get("screenshot_hotkey", "ctrl+shift+s")),
        "mode_toggle_hotkey": str(config.get("mode_toggle_hotkey", "middle")),
        "pause_toggle_hotkey": str(config.get("pause_toggle_hotkey", "ctrl+middle")),
        "show_window_hotkey": str(config.get("show_window_hotkey", "shift+middle")),
        "capture_mode": str(config.get("capture_mode", "capture")),
        "mini_status_enabled": bool(config.get("mini_status_enabled", True)),
        "mini_status_topmost": bool(config.get("mini_status_topmost", True)),
        "mini_status_x": int(config.get("mini_status_x", 20)),
        "mini_status_y": int(config.get("mini_status_y", 20)),
        "mini_status_width": int(config.get("mini_status_width", 56)),
        "mini_status_height": int(config.get("mini_status_height", 56)),
        "copy_ocr_to_clipboard_on_done": bool(config.get("copy_ocr_to_clipboard_on_done", True)),
        "ocr_provider": "nvidia_nim",
        "nvidia_ocr_model": str(config.get("nvidia_ocr_model", "nvidia/nemotron-ocr-v2")),
        "nvidia_model_url": str(config.get("nvidia_model_url", "https://build.nvidia.com/nvidia/nemotron-ocr-v2")),
        "nvidia_api_base": str(config.get("nvidia_api_base", "https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-ocr-v2")),
        "nvidia_ocr_timeout_sec": int(config.get("nvidia_ocr_timeout_sec", 60) or 60),
        "ocr_upscale_enabled": bool(config.get("ocr_upscale_enabled", True)),
        "ocr_upscale_factor": float(config.get("ocr_upscale_factor", 2.0) or 2.0),
        "ocr_target_long_side": int(config.get("ocr_target_long_side", 2200) or 2200),
        "ocr_max_long_side": int(config.get("ocr_max_long_side", 3000) or 3000),
        "ocr_image_format": str(config.get("ocr_image_format", "png")),
        "ocr_preprocess_mode": str(config.get("ocr_preprocess_mode", "sharp_gray")),
        "ocr_post_cleanup_enabled": bool(config.get("ocr_post_cleanup_enabled", True)),
        "cap_reasoning_model": str(config.get("cap_reasoning_model", "qwen/qwen3.5-397b-a17b")),
        "cap_reasoning_model_url": str(config.get("cap_reasoning_model_url", "https://build.nvidia.com/qwen/qwen3.5-397b-a17b")),
        "cap_reasoning_prompt": str(config.get("cap_reasoning_prompt", DEFAULT_CAP_PROMPT)),
        "cap_reasoning_api_base": str(config.get("cap_reasoning_api_base", "https://integrate.api.nvidia.com/v1/chat/completions")),
        "cap_reasoning_connect_timeout_sec": int(config.get("cap_reasoning_connect_timeout_sec", 15) or 15),
        "cap_reasoning_timeout_sec": int(config.get("cap_reasoning_timeout_sec", 150) or 150),
        "cap_reasoning_max_tokens": int(config.get("cap_reasoning_max_tokens", 4096) or 4096),
        "cap_reasoning_max_long_side": int(config.get("cap_reasoning_max_long_side", 3200) or 3200),
        "copy_cap_to_clipboard_on_done": False,
    }
    # 배포 폴더(_runtime)는 수정하지 않는다.
    # OneDrive/압축 해제 권한과 무관하게 사용자 전용 폴더에 저장한다.
    _write_json_atomic(USER_CONFIG_PATH, visible)

    key_value = str(config.get("nvidia_api_key", "")).strip()
    if key_value.lower().startswith("bearer "):
        key_value = key_value[7:].strip()
    key_value = key_value.strip().strip('"').strip("'").strip()

    if key_value:
        _write_json_atomic(USER_SECRET_PATH, {"nvidia_api_key": key_value})
    elif USER_SECRET_PATH.exists():
        try:
            USER_SECRET_PATH.unlink()
        except Exception:
            pass


class ClassFlowAIApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.config = load_config()

        workspace_dir = self.config.get("workspace_dir")
        self.storage_root = Path(workspace_dir) if workspace_dir else get_default_workspace(self.config.get("use_daily_folder", True))
        self.workspace = get_current_lesson(self.storage_root)
        self.paths = ensure_workspace(self.workspace)

        self.running = True
        self.closing = False
        self.paused = bool(self.config.get("pause_on_start", False))
        self.capture_mode = str(self.config.get("capture_mode", "capture") or "capture").lower()
        if self.capture_mode not in {"capture", "ocr"}:
            self.capture_mode = "capture"
        self.global_mouse_listener = None
        self.hotkey_capture_active = False
        self.last_mode_toggle_at = 0.0
        self.last_hash = None
        self.last_clipboard_sequence = None
        self.current_preview = None
        self.capture_records: list[dict] = []
        self.current_record_index = -1
        self.global_pressed_keys = set()
        self.global_keyboard_listener = None
        self.last_screenshot_hotkey_at = 0.0
        self.mini_status_window = None
        self.mini_drag_offset = (0, 0)
        self.processing_state = "일시정지" if self.paused else "대기"

        # OCR/CAP 실행 시간 표시용 상태
        self.execution_started_at = None
        self.execution_record = None
        self.execution_mode = ""
        self.execution_timer_job = None
        self.pending_capture_updates = 0
        self.lesson_switch_lock = threading.RLock()
        self.flow_interpretation_queue = queue.Queue()
        self.flow_interpretation_pending = set()
        self.flow_interpretation_lock = threading.RLock()
        self.load_records()

        self.root.title("ClassFlowAI")

        # 첫 실행부터 작업표시줄 안쪽에 들어오도록 화면 크기에 맞춰 조정합니다.
        screen_width = max(900, int(self.root.winfo_screenwidth()))
        screen_height = max(620, int(self.root.winfo_screenheight()))
        window_width = min(1120, max(900, screen_width - 80))
        window_height = min(760, max(600, screen_height - 120))
        self.root.geometry(f"{window_width}x{window_height}")
        self.root.minsize(860, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.build_ui()
        self.update_mode_badge()
        self.create_mini_status_window()
        self.start_global_hotkey_listener()
        self.initialize_clipboard_baseline()
        self.refresh_current_preview()
        self.set_status("실행됨. CAP/OCR 모드를 확인한 뒤 스크린샷을 모으세요.")

        self.watch_thread = threading.Thread(target=self.clipboard_watch_loop, daemon=True)
        self.watch_thread.start()
        self.flow_interpretation_worker = threading.Thread(
            target=self._flow_interpretation_worker_loop,
            daemon=True,
        )
        self.flow_interpretation_worker.start()

    def build_ui(self):
        top = tk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=8)
        tk.Label(top, text="ClassFlowAI", font=("맑은 고딕", 17, "bold")).pack(side="left")

        self.counter_var = tk.StringVar()
        tk.Label(top, textvariable=self.counter_var, font=("맑은 고딕", 10)).pack(side="right")

        lesson_row = tk.Frame(self.root)
        lesson_row.pack(fill="x", padx=10)
        self.workspace_var = tk.StringVar(value=self.lesson_location_text())
        tk.Label(lesson_row, textvariable=self.workspace_var, anchor="w").pack(side="left", fill="x", expand=True)
        tk.Button(
            lesson_row,
            text="새 수업 시작",
            command=self.start_new_lesson,
            width=13,
        ).pack(side="right", padx=(6, 0))
        tk.Button(
            lesson_row,
            text="이전 수업 열기",
            command=self.open_previous_lesson,
            width=13,
        ).pack(side="right", padx=(6, 0))

        self.status_var = tk.StringVar()
        status_row = tk.Frame(self.root)
        status_row.pack(fill="x", padx=10, pady=5)
        tk.Label(status_row, textvariable=self.status_var, anchor="w", fg="#333333").pack(side="left", fill="x", expand=True)
        mode_box = tk.Frame(status_row)
        mode_box.pack(side="right")

        self.mode_var = tk.StringVar()
        self.mode_label = tk.Label(
            mode_box,
            textvariable=self.mode_var,
            font=("맑은 고딕", 9, "bold"),
            padx=10,
            pady=3,
            relief="solid",
            bd=1,
        )
        self.mode_label.pack(anchor="e")

        self.execution_time_var = tk.StringVar(value="실행 시간 · -")
        self.execution_time_label = tk.Label(
            mode_box,
            textvariable=self.execution_time_var,
            font=("맑은 고딕", 9),
            fg="#555555",
            anchor="e",
        )
        self.execution_time_label.pack(anchor="e", pady=(3, 0))

        bottom = tk.Frame(self.root)
        bottom.pack(side="bottom", fill="x", padx=10, pady=(4, 8))
        tk.Button(bottom, text="수업 흐름", command=self.open_flow_window, width=16, height=2).pack(side="left")
        tk.Button(bottom, text="설정", command=self.open_settings_window, width=12, height=2).pack(side="right")
        tk.Button(bottom, text="캡처 폴더 열기", command=self.open_capture_folder, width=16, height=2).pack(side="right", padx=(0, 6))

        body = tk.Frame(self.root)
        body.pack(side="top", fill="both", expand=True, padx=10, pady=(6, 2))

        left = tk.LabelFrame(body, text="캡처 미리보기", padx=8, pady=8)
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))

        nav = tk.Frame(left)
        nav.pack(fill="x", pady=(0, 6))
        tk.Button(nav, text="◀ 이전", command=self.show_prev_record).pack(side="left")
        self.record_pos_var = tk.StringVar(value="0 / 0")
        tk.Label(nav, textvariable=self.record_pos_var, font=("맑은 고딕", 10)).pack(side="left", padx=12)
        tk.Button(nav, text="다음 ▶", command=self.show_next_record).pack(side="left")
        tk.Button(nav, text="현재 삭제", command=self.delete_current_record).pack(side="right", padx=(6, 0))
        tk.Button(nav, text="수업 초기화", command=self.reset_today).pack(side="right")

        order_box = tk.LabelFrame(left, text="현재 수업 캡처 목록", padx=6, pady=6)
        order_box.pack(fill="x", pady=(0, 6))
        list_row = tk.Frame(order_box)
        list_row.pack(fill="x")
        self.capture_listbox = tk.Listbox(
            list_row,
            height=5,
            exportselection=False,
            activestyle="dotbox",
        )
        self.capture_listbox.pack(side="left", fill="x", expand=True)
        capture_scroll = tk.Scrollbar(
            list_row,
            orient="vertical",
            command=self.capture_listbox.yview,
        )
        capture_scroll.pack(side="right", fill="y")
        self.capture_listbox.config(yscrollcommand=capture_scroll.set)
        self.capture_listbox.bind("<<ListboxSelect>>", self.select_capture_from_list)
        self.capture_listbox.bind("<Button-3>", self.show_capture_list_context_menu)
        order_actions = tk.Frame(order_box)
        order_actions.pack(fill="x", pady=(6, 0))
        tk.Button(order_actions, text="위로 이동", command=lambda: self.move_current_capture(-1)).pack(side="left")
        tk.Button(order_actions, text="아래로 이동", command=lambda: self.move_current_capture(1)).pack(side="left", padx=(6, 0))
        tk.Button(order_actions, text="원래 촬영 순서로 복원", command=self.confirm_restore_capture_order).pack(side="right")

        self.preview_label = tk.Label(left, text="아직 캡처가 없습니다.\nCtrl+Shift+S로 캡처하세요.\n휠클릭으로 OCR / CAP 모드를 전환할 수 있습니다.", bg="#f4f4f4")
        self.preview_label.pack(fill="both", expand=True)
        self.preview_label.bind("<Button-3>", self.show_capture_preview_context_menu)

        right = tk.LabelFrame(body, text="현재 결과", padx=8, pady=8)
        right.pack(side="right", fill="both", expand=True, padx=(6, 0))
        self.flow_text = tk.Text(
            right,
            wrap="word",
            font=("맑은 고딕", 10),
            undo=False,
            takefocus=True,
        )
        self.flow_text.pack(fill="both", expand=True)
        self.flow_text.insert("1.0", self.get_ocr_panel_text())
        self.flow_text.bind("<Control-a>", self.select_all_result_text)
        self.flow_text.bind("<Control-A>", self.select_all_result_text)
        self.flow_text.bind("<Key>", self.guard_result_text_edit)

        result_actions = tk.Frame(right)
        self.result_actions = result_actions

        self.ocr_refine_button = tk.Button(
            result_actions,
            text="CAP 원본 이미지 복사",
            command=self.copy_current_cap_image,
            width=18,
            height=2,
            state="disabled",
        )
        self.ocr_refine_button.pack(side="left")

        self.cap_copy_button = tk.Button(
            result_actions,
            text="CAP 해석 복사",
            command=self.copy_current_cap_result,
            width=18,
            height=2,
            state="disabled",
        )
        self.cap_copy_button.pack(side="left", padx=(8, 0))


    def format_execution_seconds(self, seconds) -> str:
        try:
            seconds = max(0.0, float(seconds))
        except Exception:
            return "-"

        if seconds < 60:
            return f"{seconds:.1f}초"

        total_seconds = int(round(seconds))
        minutes, remain = divmod(total_seconds, 60)
        if minutes < 60:
            return f"{minutes}분 {remain:02d}초"

        hours, minutes = divmod(minutes, 60)
        return f"{hours}시간 {minutes:02d}분 {remain:02d}초"


    def start_execution_timer(self, record: dict, mode: str):
        self.stop_execution_timer(save_result=False)
        self.execution_started_at = time.perf_counter()
        self.execution_record = record
        self.execution_mode = str(mode or "").lower()
        record["processing_started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.update_execution_time_label()
        self.schedule_execution_timer()


    def schedule_execution_timer(self):
        try:
            if self.execution_timer_job is not None:
                self.root.after_cancel(self.execution_timer_job)
        except Exception:
            pass

        if self.execution_started_at is None:
            self.execution_timer_job = None
            return

        self.execution_timer_job = self.root.after(250, self.execution_timer_tick)


    def execution_timer_tick(self):
        self.execution_timer_job = None
        if self.execution_started_at is None:
            self.update_execution_time_label()
            return

        self.update_execution_time_label()
        self.schedule_execution_timer()


    def stop_execution_timer(self, record: dict | None = None, save_result: bool = True) -> float:
        target_record = record or self.execution_record
        elapsed = 0.0

        if self.execution_started_at is not None:
            elapsed = max(0.0, time.perf_counter() - self.execution_started_at)

        if save_result and target_record is not None and elapsed > 0:
            process_mode = str(self.execution_mode or "").lower()
            record_mode = str(target_record.get("mode") or "capture").lower()

            if process_mode == "ocr_refine":
                key = "ocr_correction_elapsed_sec"
                target_record["last_process_type"] = "ocr_refine"
            elif process_mode == "ocr" or record_mode == "ocr":
                key = "ocr_elapsed_sec"
                target_record["last_process_type"] = "ocr"
            else:
                key = "cap_elapsed_sec"
                target_record["last_process_type"] = "cap"

            target_record[key] = round(elapsed, 3)
            target_record["processing_finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        try:
            if self.execution_timer_job is not None:
                self.root.after_cancel(self.execution_timer_job)
        except Exception:
            pass

        self.execution_timer_job = None
        self.execution_started_at = None
        self.execution_record = None
        self.execution_mode = ""
        self.update_execution_time_label()
        return elapsed


    def update_execution_time_label(self):
        if not hasattr(self, "execution_time_var"):
            return

        record = self.get_current_record()

        if (
            self.execution_started_at is not None
            and self.execution_record is not None
            and record is self.execution_record
        ):
            elapsed = time.perf_counter() - self.execution_started_at
            if self.execution_mode == "ocr_refine":
                mode_name = "OCR 보정"
            elif self.execution_mode == "ocr":
                mode_name = "OCR"
            else:
                mode_name = "CAP"

            self.execution_time_var.set(
                f"{mode_name} 실행 중 · {self.format_execution_seconds(elapsed)}"
            )
            self.execution_time_label.config(fg="#9a3412")
            return

        if record is None:
            self.execution_time_var.set("실행 시간 · -")
            self.execution_time_label.config(fg="#555555")
            return

        mode = str(record.get("mode") or "capture").lower()
        last_process = str(record.get("last_process_type") or "").lower()

        if last_process == "ocr_refine" and record.get("ocr_correction_elapsed_sec") is not None:
            elapsed = record.get("ocr_correction_elapsed_sec")
            mode_name = "OCR 보정"
        elif mode == "ocr":
            elapsed = record.get("ocr_elapsed_sec")
            mode_name = "OCR"
        else:
            elapsed = record.get("cap_elapsed_sec")
            mode_name = "CAP"

        if elapsed is None:
            self.execution_time_var.set("실행 시간 · -")
        else:
            self.execution_time_var.set(
                f"{mode_name} 실행 시간 · {self.format_execution_seconds(elapsed)}"
            )

        self.execution_time_label.config(fg="#555555")


    def get_flow_overview_text(self) -> str:
        return (
            "OCR 모드: 글자를 빠르게 추출하고 자동 복사합니다.\n"
            "CAP 모드: 원본 이미지를 유지한 채 화면의 의미와 구조를 해석합니다.\n\n"
            "CAP 해석 텍스트가 필요할 때만 [CAP 해석 복사]를 사용하세요."
        )


    def get_current_result_text(self, record: dict | None = None) -> str:
        record = record or self.get_current_record()
        if record is None:
            return ""

        display_type = str(
            record.get("display_result_type")
            or ""
        ).lower()

        if display_type == "ocr_interpretation":
            interpretation = str(
                record.get("ocr_interpretation_text")
                or ""
            ).strip()
            if interpretation:
                return interpretation

        mode = str(record.get("mode") or "capture").lower()
        if mode == "ocr":
            return str(
                record.get("ocr_corrected_text")
                or record.get("ocr_text")
                or ""
            ).strip()

        return str(record.get("cap_text") or "").strip()


    def get_ocr_panel_text(self) -> str:
        record = self.get_current_record()
        if record is None:
            return (
                "아직 캡처가 없습니다.\n\n"
                "OCR 모드: 빠른 글자 추출 → 보정 후 복사 → 내용 추론 가능\n"
                "CAP 모드: 이미지 원본 유지 → 내용 추론 → 필요할 때 해석 복사"
            )

        result_text = self.get_current_result_text(record)
        status = str(record.get("status") or "")

        if result_text:
            return result_text

        if status in {"ocr_running", "cap_running"}:
            return "OCR 처리 중..." if status == "ocr_running" else "CAP 이미지 분석 중..."

        mode = str(record.get("mode") or "capture").lower()
        return (
            "아직 결과가 없습니다.\n"
            + ("OCR 처리를 기다리는 중입니다." if mode == "ocr" else "CAP 이미지 분석을 기다리는 중입니다.")
        )


    def update_result_action_buttons(self):
        if not hasattr(self, "ocr_refine_button"):
            return

        record = self.get_current_record()
        mode = str(record.get("mode") or "capture").lower() if record is not None else ""

        if record is None:
            try:
                self.result_actions.pack_forget()
            except Exception:
                pass
            return

        try:
            if not self.result_actions.winfo_manager():
                self.result_actions.pack(fill="x", pady=(8, 0))
        except Exception:
            pass

        if mode == "ocr":
            status = str(record.get("status") or "")
            ocr_text = str(record.get("ocr_text") or "").strip()
            refine_state = "disabled"
            refine_text = "OCR 보정 후 복사"
            if status == "ocr_correction_running":
                refine_text = "OCR 보정 중…"
            elif ocr_text and not ocr_text.lstrip().startswith("## OCR 실패"):
                refine_state = "normal"
            try:
                if not self.ocr_refine_button.winfo_manager():
                    self.ocr_refine_button.pack(side="right")
                else:
                    self.ocr_refine_button.pack_configure(side="right")
                self.ocr_refine_button.config(
                    state=refine_state,
                    text=refine_text,
                    command=self.refine_current_ocr_and_copy,
                )
                # 수업 흐름 해석은 OCR 완료 후 백그라운드에서 자동 실행된다.
                # 별도의 수동 실행 버튼은 노출하지 않는다.
                self.cap_copy_button.pack_forget()
            except Exception:
                pass
            return

        try:
            if not self.ocr_refine_button.winfo_manager():
                self.ocr_refine_button.pack(side="left")
            if not self.cap_copy_button.winfo_manager():
                self.cap_copy_button.pack(side="left", padx=(8, 0))
        except Exception:
            pass

        refine_state = "disabled"
        refine_text = "CAP 원본 이미지 복사"
        refine_command = self.copy_current_cap_image
        second_state = "disabled"
        second_text = "CAP 해석 복사"
        second_command = self.copy_current_cap_result

        status = str(record.get("status") or "")
        cap_text = str(record.get("cap_text") or "").strip()
        image_path = Path(str(record.get("image_path") or ""))
        if image_path.exists():
            refine_state = "normal"
        if status == "cap_running":
            second_text = "CAP 분석 중…"
        elif status == "cap_failed":
            second_text = "다시 시도"
            second_command = self.retry_current_model_request
            second_state = "normal"
        elif cap_text and not cap_text.startswith("CAP 분석 실패"):
            second_state = "normal"

        try:
            self.ocr_refine_button.config(
                state=refine_state,
                text=refine_text,
                command=refine_command,
            )
            self.cap_copy_button.config(
                state=second_state,
                text=second_text,
                command=second_command,
            )
        except Exception:
            pass

    def retry_current_model_request(self):
        """Retry the selected record without creating another capture or record."""
        record = self.get_current_record()
        if record is None:
            self.set_status("다시 시도할 캡처가 없습니다.")
            return
        if str(record.get("mode") or "capture").lower() == "ocr":
            self.run_ocr_for_record_async(record, auto_copy=True, force=True)
        else:
            self.run_cap_reasoning_for_record_async(record, auto_copy=False, force=True)

    def _show_transient_retry_status(self, message: str):
        self.root.after(0, lambda: self.set_status(message))



    def select_all_result_text(self, _event=None):
        try:
            self.flow_text.tag_add("sel", "1.0", "end-1c")
            self.flow_text.mark_set("insert", "1.0")
            self.flow_text.see("1.0")
        except Exception:
            pass
        return "break"


    def guard_result_text_edit(self, event):
        """
        결과창은 읽기 전용이지만 마우스 선택, Ctrl+A, Ctrl+C,
        방향키와 스크롤은 사용할 수 있게 유지한다.
        """
        ctrl_pressed = bool(event.state & 0x0004)
        key = str(event.keysym or "").lower()

        if ctrl_pressed and key == "a":
            return self.select_all_result_text(event)

        if ctrl_pressed and key in {"c", "insert"}:
            return None

        navigation_keys = {
            "left", "right", "up", "down",
            "home", "end", "prior", "next",
        }
        if key in navigation_keys:
            return None

        return "break"

    def copy_current_cap_result(self):
        record = self.get_current_record()
        if record is None:
            self.set_status(
                "복사할 CAP 결과가 없습니다."
            )
            return

        if str(
            record.get("mode")
            or ""
        ).lower() == "ocr":
            self.set_status(
                "CAP 캡처를 선택하세요."
            )
            return

        cap_text = str(
            record.get("cap_text")
            or ""
        ).strip()

        if (
            not cap_text
            or cap_text.startswith("CAP 분석 실패")
        ):
            self.set_status(
                "복사할 정상 CAP 해석 결과가 없습니다."
            )
            return

        copied = self.copy_text_to_clipboard(
            cap_text
        )
        self.set_status(
            "CAP 해석 내용을 클립보드에 복사했습니다."
            if copied
            else "CAP 해석 내용 복사에 실패했습니다."
        )


    def copy_current_cap_image(self):
        record = self.get_current_record()
        if record is None:
            self.set_status("복사할 CAP 원본 이미지가 없습니다.")
            return
        if str(record.get("mode") or "").lower() == "ocr":
            self.set_status("CAP 모드로 생성된 캡처에서만 원본 이미지를 복사할 수 있습니다.")
            return

        self.copy_record_original_image(record)

    def copy_record_original_image(self, record: dict) -> bool:
        """Copy an OCR or CAP record's original file without changing the record."""
        if not isinstance(record, dict) or record.get("deleted"):
            self.set_status("복사할 원본 이미지가 없습니다.")
            messagebox.showwarning("원본 이미지 복사 불가", "선택한 캡처를 사용할 수 없습니다.")
            return False

        image_path = Path(str(record.get("image_path") or ""))
        if not image_path.is_file():
            messagebox.showwarning(
                "원본 이미지 복사 불가",
                "선택한 캡처의 원본 이미지 파일을 찾을 수 없습니다.",
            )
            self.set_status("원본 이미지 파일을 찾을 수 없습니다.")
            return False

        previous_hash = self.last_hash
        try:
            with Image.open(image_path) as image:
                image.verify()
            with Image.open(image_path) as image:
                self.last_hash = image_hash(image.convert("RGB"))
            copy_image_to_clipboard(image_path, owner_hwnd=self.root.winfo_id())
        except Exception:
            self.last_hash = previous_hash
            self.set_status("원본 이미지 복사에 실패했습니다.")
            messagebox.showerror(
                "원본 이미지 복사 실패",
                "원본 이미지를 클립보드에 복사할 수 없습니다. 잠시 후 다시 시도해 주세요.",
            )
            return False

        try:
            append_event(
                self.paths["events"],
                {
                    "type": (
                        "cap_image_copied"
                        if str(record.get("mode") or "").lower() != "ocr"
                        else "capture_image_copied"
                    ),
                    "path": str(image_path),
                },
            )
        except Exception:
            pass
        self.set_status("원본 이미지가 클립보드에 복사되었습니다.")
        return True


    def refine_current_ocr_and_copy(self):
        record = self.get_current_record()
        if record is None:
            self.set_status("OCR 보정 불가: 선택된 캡처가 없습니다.")
            return

        if str(record.get("mode") or "").lower() != "ocr":
            self.set_status("OCR 모드로 생성된 캡처에서만 보정할 수 있습니다.")
            return

        image_path = Path(str(record.get("image_path") or ""))
        if not image_path.exists():
            messagebox.showwarning(
                "OCR 보정 불가",
                f"원본 이미지를 찾을 수 없습니다.\n\n{image_path}",
            )
            return

        ocr_text = str(record.get("ocr_text") or "").strip()
        if not ocr_text or ocr_text.lstrip().startswith("## OCR 실패"):
            messagebox.showwarning(
                "OCR 보정 불가",
                "현재 캡처에 정상 OCR 결과가 없습니다.",
            )
            return

        if not self.has_nvidia_api_key():
            messagebox.showwarning(
                "OCR 보정 불가",
                "NVIDIA API 키가 없습니다.\n\n설정에서 API 키를 입력하세요.",
            )
            return

        record["status"] = "ocr_correction_running"
        self.processing_state = "OCR 보정 중"
        self.start_execution_timer(record, "ocr_refine")
        self.save_records()
        self.update_result_action_buttons()
        self.update_ocr_panel()
        self.update_mini_status()
        self.update_counter()
        self.set_status("원본 이미지와 OCR 결과를 비교해 보정 중입니다...")

        def worker():
            try:
                corrected = correct_ocr_with_image(
                    image_path=image_path,
                    ocr_text=ocr_text,
                    config=self.config,
                    on_retry=self._show_transient_retry_status,
                )
            except Exception as exc:
                corrected = f"OCR 보정 실패\n\n{exc}"

            self.root.after(
                0,
                lambda: self._after_ocr_correction(record, corrected),
            )

        threading.Thread(target=worker, daemon=True).start()


    def _after_ocr_correction(self, record: dict, corrected_text: str):
        corrected_text = str(corrected_text or "").strip()
        failed = corrected_text.startswith("OCR 보정 실패")
        elapsed = self.stop_execution_timer(record, save_result=True)

        if failed:
            record["status"] = "ocr_done"
            record["ocr_correction_error"] = corrected_text
            self.processing_state = "OCR 보정 실패"
        else:
            flow_was_pending = record.get("flow_interpretation_status") in {"queued", "running"}
            for key in [
                "ocr_interpretation_text",
                "ocr_interpretation_error",
                "flow_interpretation_error",
            ]:
                record.pop(key, None)

            if flow_was_pending:
                record["flow_interpretation_requeue"] = True
            else:
                record.pop("flow_interpretation_status", None)

            record["ocr_corrected_text"] = corrected_text
            record["ocr_correction_model"] = str(
                self.config.get("cap_reasoning_model")
                or "qwen/qwen3.5-397b-a17b"
            )
            record["ocr_correction_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            record["status"] = "ocr_corrected"
            record["display_result_type"] = "ocr_corrected"
            record.pop("ocr_correction_error", None)
            self.processing_state = "OCR 보정 완료"

        self.save_records()
        self.rebuild_outputs_from_records()
        self.refresh_current_preview()
        self.update_mini_status()
        self.update_counter()
        self.update_result_action_buttons()

        append_event(
            self.paths["events"],
            {
                "type": "ocr_correction_done",
                "path": str(record.get("image_path") or ""),
                "failed": failed,
                "model": str(self.config.get("cap_reasoning_model") or ""),
                "elapsed_sec": round(elapsed, 3),
            },
        )

        elapsed_text = self.format_execution_seconds(elapsed)

        if failed:
            self.set_status(
                f"OCR 보정 실패 ({elapsed_text}). 기존 OCR 결과는 유지했습니다."
            )
            messagebox.showerror(
                "OCR 보정 실패",
                corrected_text,
            )
            return

        copied = self.copy_text_to_clipboard(corrected_text)
        self.set_status(
            f"OCR 보정 완료 ({elapsed_text}) + 클립보드 복사 완료"
            if copied
            else f"OCR 보정 완료 ({elapsed_text}), 복사 실패"
        )
        if not record.get("flow_interpretation_requeue"):
            self.start_flow_interpretation_background(record, force=True)



    def update_ocr_panel(self):
        try:
            self.flow_text.delete("1.0", tk.END)
            self.flow_text.insert("1.0", self.get_ocr_panel_text())
        except Exception:
            pass
        self.update_result_action_buttons()


    def set_status(self, text: str):
        self.status_var.set(f"상태: {text}")

    def lesson_location_text(self) -> str:
        try:
            is_legacy_workspace = self.workspace.resolve() == self.storage_root.resolve()
        except Exception:
            is_legacy_workspace = self.workspace == self.storage_root
        lesson_name = self.workspace.name or str(self.workspace)
        if is_legacy_workspace:
            lesson_name = f"{lesson_name} (기존 수업)"
        return f"현재 수업: {lesson_name} | 저장 위치: {short_workspace_display(self.workspace)}"

    def lesson_switch_blocked(self) -> bool:
        with self.lesson_switch_lock:
            if self.pending_capture_updates > 0 or self.execution_started_at is not None:
                messagebox.showwarning(
                    "수업 전환 대기",
                    "현재 캡처 또는 OCR/CAP 처리가 끝난 뒤 수업을 전환하세요.",
                )
                return True
        return False

    def activate_lesson(self, lesson_workspace: Path) -> bool:
        lesson_workspace = Path(lesson_workspace).resolve()
        with self.lesson_switch_lock:
            if self.pending_capture_updates > 0 or self.execution_started_at is not None:
                messagebox.showwarning(
                    "수업 전환 대기",
                    "현재 캡처 또는 OCR/CAP 처리가 끝난 뒤 수업을 전환하세요.",
                )
                return False
            new_paths = ensure_workspace(lesson_workspace)
            set_current_lesson(self.storage_root, lesson_workspace)
            self.workspace = lesson_workspace
            self.paths = new_paths
            self.capture_records = []
            self.current_record_index = -1
            self.current_preview = None
            self.processing_state = "일시정지" if self.paused else "대기"
            self.load_records()

        self.workspace_var.set(self.lesson_location_text())
        self.refresh_current_preview()
        self.update_mode_badge()
        return True

    def start_new_lesson(self):
        if self.lesson_switch_blocked():
            return
        if not messagebox.askyesno(
            "새 수업 시작",
            "현재 수업의 기록과 원본 이미지는 그대로 보존됩니다.\n새 수업을 시작할까요?",
        ):
            return

        previous_workspace = self.workspace
        try:
            with self.lesson_switch_lock:
                if self.pending_capture_updates > 0 or self.execution_started_at is not None:
                    messagebox.showwarning(
                        "수업 전환 대기",
                        "현재 캡처 또는 OCR/CAP 처리가 끝난 뒤 수업을 전환하세요.",
                    )
                    return
                lesson_workspace = create_lesson_workspace(self.storage_root)
                if not self.activate_lesson(lesson_workspace):
                    return
            append_event(
                self.paths["events"],
                {
                    "type": "lesson_created",
                    "workspace": str(lesson_workspace),
                    "previous_workspace": str(previous_workspace),
                },
            )
            self.set_status(f"새 수업을 시작했습니다: {lesson_workspace.name}")
        except Exception as exc:
            messagebox.showerror(
                "새 수업 시작 실패",
                f"새 수업 폴더를 만들 수 없습니다.\n\n{exc}",
            )

    def open_previous_lesson(self):
        if self.lesson_switch_blocked():
            return

        selected = filedialog.askdirectory(
            title="이전 수업 폴더 선택",
            initialdir=str(self.storage_root),
            mustexist=True,
        )
        if not selected:
            return

        lesson_workspace = Path(selected)
        if lesson_workspace.name.lower() in {"captures", "logs", "outputs", "state"}:
            lesson_workspace = lesson_workspace.parent
        lesson_workspace = lesson_workspace.resolve()

        if not is_lesson_workspace(lesson_workspace):
            messagebox.showwarning(
                "수업 폴더 확인",
                "선택한 폴더에서 ClassFlowAI 수업 기록을 찾을 수 없습니다.\n"
                "captures 또는 state 폴더가 있는 수업 폴더를 선택하세요.",
            )
            return

        try:
            if lesson_workspace == self.workspace.resolve():
                self.set_status("이미 열려 있는 수업입니다.")
                return
        except Exception:
            pass

        previous_workspace = self.workspace
        try:
            if not self.activate_lesson(lesson_workspace):
                return
            append_event(
                self.paths["events"],
                {
                    "type": "lesson_opened",
                    "workspace": str(lesson_workspace),
                    "previous_workspace": str(previous_workspace),
                },
            )
            self.set_status(f"이전 수업을 열었습니다: {lesson_workspace.name}")
        except Exception as exc:
            messagebox.showerror(
                "이전 수업 열기 실패",
                f"선택한 수업을 열 수 없습니다.\n\n{exc}",
            )

    def load_records(self):
        records_path = self.paths.get("records")
        if records_path and records_path.exists():
            try:
                data = json.loads(records_path.read_text(encoding="utf-8"))
                self.capture_records = data if isinstance(data, list) else []
                self.capture_records = [record for record in self.capture_records if isinstance(record, dict)]
            except Exception:
                self.capture_records = []
        normalize_display_orders(self.capture_records)
        active = self.active_record_indices()
        self.current_record_index = active[-1] if active else -1

    def save_records(self):
        records_path = self.paths.get("records")
        if records_path:
            write_json_atomic(records_path, self.capture_records)

    def active_record_indices(self):
        indices_by_identity = {id(record): index for index, record in enumerate(self.capture_records)}
        return [indices_by_identity[id(record)] for record in active_ordered_records(self.capture_records)]

    def get_current_record(self):
        if 0 <= self.current_record_index < len(self.capture_records):
            record = self.capture_records[self.current_record_index]
            if not record.get("deleted"):
                return record
        return None

    def active_position_text(self):
        active = self.active_record_indices()
        if not active or self.current_record_index not in active:
            return "0 / 0"
        return f"{active.index(self.current_record_index) + 1} / {len(active)}"

    def update_counter(self):
        active_count = len(self.active_record_indices())
        pause_text = "일시정지" if self.paused else "감지 중"
        self.record_pos_var.set(self.active_position_text())
        self.counter_var.set(f"{self.active_position_text()} | 캡처 {active_count}개 | {pause_text}")
        self.refresh_capture_list()
        self.update_mini_status()

    def add_capture_record(self, image_path: Path) -> dict:
        captured_at = time.strftime("%Y-%m-%d %H:%M:%S")
        record = {
            "record_id": image_path.stem,
            "image_path": str(image_path),
            "status": "captured",
            "mode": self.capture_mode,
            "deleted": False,
            "created_at": captured_at,
            "captured_at": captured_at,
            "display_order": next_display_order(self.capture_records),
        }
        self.capture_records.append(record)
        self.current_record_index = len(self.capture_records) - 1
        self.save_records()
        return record

    def refresh_capture_list(self):
        if not hasattr(self, "capture_listbox"):
            return
        selected_record = self.get_current_record()
        try:
            first_visible = self.capture_listbox.nearest(0)
        except Exception:
            first_visible = 0
        active = self.active_record_indices()
        self.capture_list_record_indices = active
        self.capture_listbox.delete(0, tk.END)
        selected_position = None
        for position, record_index in enumerate(active):
            record = self.capture_records[record_index]
            image_name = Path(str(record.get("image_path") or "")).name
            captured_at = str(record.get("captured_at") or record.get("created_at") or "시간 확인 필요")
            self.capture_listbox.insert(tk.END, f"{position + 1}. {captured_at} · {image_name}")
            if record is selected_record:
                selected_position = position
        if selected_position is not None:
            self.capture_listbox.selection_set(selected_position)
            self.capture_listbox.activate(selected_position)
        if active:
            self.capture_listbox.yview_moveto(min(first_visible, len(active) - 1) / max(len(active), 1))

    def select_capture_from_list(self, _event=None):
        if not hasattr(self, "capture_listbox"):
            return
        selection = self.capture_listbox.curselection()
        if not selection:
            return
        position = int(selection[0])
        indices = getattr(self, "capture_list_record_indices", [])
        if 0 <= position < len(indices):
            self.current_record_index = indices[position]
            self.refresh_current_preview()

    def show_capture_list_context_menu(self, event):
        position = listbox_index_at_y(self.capture_listbox, int(event.y))
        indices = getattr(self, "capture_list_record_indices", [])
        if position is None or not (0 <= position < len(indices)):
            return "break"
        record_index = indices[position]
        if not (0 <= record_index < len(self.capture_records)):
            return "break"
        record = self.capture_records[record_index]
        if record.get("deleted"):
            return "break"
        self.current_record_index = record_index
        self.capture_listbox.selection_clear(0, tk.END)
        self.capture_listbox.selection_set(position)
        self.capture_listbox.activate(position)
        self.refresh_current_preview()
        return self.show_capture_context_menu(event, record)

    def show_capture_preview_context_menu(self, event):
        record = self.get_current_record()
        if record is None:
            return "break"
        return self.show_capture_context_menu(event, record)

    def show_capture_context_menu(self, event, record: dict):
        menu = tk.Menu(self.root, tearoff=0)
        self.capture_context_menu = menu
        menu.add_command(
            label="원본 이미지 복사",
            command=lambda selected=record: self.copy_record_original_image(selected),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass
        return "break"

    def move_current_capture(self, direction: int):
        record = self.get_current_record()
        if record is None:
            self.set_status("순서를 변경할 캡처를 선택하세요.")
            return
        with self.lesson_switch_lock:
            previous_orders = [item.get("display_order") for item in self.capture_records]
            if not move_record(self.capture_records, record, direction):
                self.set_status("이미 첫 항목입니다." if direction < 0 else "이미 마지막 항목입니다.")
                return
            try:
                self.save_records()
            except Exception as exc:
                for index, item in enumerate(self.capture_records):
                    previous = previous_orders[index]
                    if previous is None:
                        item.pop("display_order", None)
                    else:
                        item["display_order"] = previous
                self.set_status("캡처 순서를 저장하지 못했습니다. 기존 순서를 유지합니다.")
                messagebox.showerror("캡처 순서 저장 실패", f"정렬 정보를 저장할 수 없습니다.\n\n{exc}")
                return
        self.rebuild_outputs_from_records(save_records=False)
        self.refresh_current_preview()
        self.set_status("캡처 학습 흐름 순서를 변경했습니다.")

    def confirm_restore_capture_order(self):
        with self.lesson_switch_lock:
            previous_orders = [item.get("display_order") for item in self.capture_records]
            confirmed = False

            def confirm():
                nonlocal confirmed
                confirmed = messagebox.askyesno(
                    "원래 촬영 순서로 복원",
                    "이미지는 삭제하지 않고 정렬 정보만 원래 촬영 순서로 되돌립니다.\n계속할까요?",
                )
                return confirmed

            if not restore_capture_order_if_confirmed(self.capture_records, confirm):
                if not confirmed:
                    return
                self.set_status("이미 원래 촬영 순서입니다.")
                return
            try:
                self.save_records()
            except Exception as exc:
                for index, item in enumerate(self.capture_records):
                    previous = previous_orders[index]
                    if previous is None:
                        item.pop("display_order", None)
                    else:
                        item["display_order"] = previous
                self.set_status("촬영 순서 복원을 저장하지 못했습니다. 기존 순서를 유지합니다.")
                messagebox.showerror("촬영 순서 복원 실패", f"정렬 정보를 저장할 수 없습니다.\n\n{exc}")
                return
        self.rebuild_outputs_from_records(save_records=False)
        self.refresh_current_preview()
        self.set_status("원래 촬영 순서로 복원했습니다. 이미지와 기록은 변경하지 않았습니다.")

    def _capture_image_files(self) -> list[Path]:
        exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        captures_dir = self.paths.get("captures")
        if not captures_dir or not captures_dir.exists():
            return []
        return sorted([p for p in captures_dir.iterdir() if p.is_file() and p.suffix.lower() in exts])

    def _ensure_records_for_capture_files(self) -> None:
        known = set()
        for record in self.capture_records:
            try:
                known.add(str(Path(record.get("image_path", "")).resolve()))
            except Exception:
                continue

        added = False
        for image_path in self._capture_image_files():
            resolved = str(image_path.resolve())
            if resolved in known:
                continue
            self.add_capture_record(image_path)
            known.add(resolved)
            added = True

        if added:
            self.save_records()

    def rebuild_outputs_from_records(self, save_records: bool = True):
        if save_records:
            self.save_records()
        try:
            document = self.build_current_flow_document()
            save_flow_document(self.paths["flow_document"], document)
        except Exception as e:
            append_event(self.paths["events"], {"type": "flow_document_write_failed", "error": str(e)})

    def build_current_flow_document(self) -> dict:
        return build_flow_document(self.capture_records, title=self.workspace.name or "수업 흐름")

    def initialize_clipboard_baseline(self):
        ignore_existing = bool(self.config.get("ignore_existing_clipboard_on_start", True))
        self.last_clipboard_sequence = get_clipboard_sequence_number() if ignore_existing else None
        if not ignore_existing:
            return
        # Windows에서는 변경 번호만 기억하면 고해상도 클립보드 이미지를 시작 시
        # 읽고 변환할 필요가 없다. API를 사용할 수 없을 때만 기존 방식으로 대체한다.
        if self.last_clipboard_sequence is not None:
            return
        try:
            image = get_clipboard_image()
            if image is not None:
                self.last_hash = image_hash(image)
        except Exception:
            self.last_hash = None

    def clipboard_watch_loop(self):
        while self.running:
            try:
                if not self.paused:
                    sequence = get_clipboard_sequence_number()
                    if not clipboard_sequence_changed(self.last_clipboard_sequence, sequence):
                        time.sleep(float(self.config.get("poll_interval_sec", 1.0)))
                        continue
                    if sequence is not None:
                        self.last_clipboard_sequence = sequence
                    image = get_clipboard_image()
                    if image is not None:
                        h = image_hash(image)
                        if h != self.last_hash:
                            self.last_hash = h
                            self.handle_new_clipboard_image(image)
                time.sleep(float(self.config.get("poll_interval_sec", 1.0)))
            except Exception as e:
                append_event(self.paths["events"], {"type": "clipboard_loop_error", "error": str(e)})
                time.sleep(1.0)

    def handle_new_clipboard_image(self, image: Image.Image):
        with self.lesson_switch_lock:
            image_path = self.paths["captures"] / f"capture_{timestamp_file()}.png"
            save_image(image, image_path)
            record = self.add_capture_record(image_path)
            self.processing_state = "캡처 완료"
            append_event(self.paths["events"], {"type": "capture_saved", "path": str(image_path), "mode": self.capture_mode})
            self.pending_capture_updates += 1

        def update_ui():
            try:
                with self.lesson_switch_lock:
                    self.current_record_index = self.capture_records.index(record)
                    self.rebuild_outputs_from_records()
                    self.refresh_current_preview()

                    if str(record.get("mode") or "capture").lower() == "ocr":
                        self.set_status(f"OCR 시작: {image_path.name}")
                        self.run_ocr_for_record_async(record, auto_copy=True, force=True)
                    else:
                        self.set_status(f"CAP 이미지 분석 시작: {image_path.name}")
                        self.run_cap_reasoning_for_record_async(record, auto_copy=True, force=True)
            finally:
                with self.lesson_switch_lock:
                    self.pending_capture_updates = max(0, self.pending_capture_updates - 1)

        self.root.after(0, update_ui)

    def show_preview(self, image: Image.Image):
        preview = image.copy()
        preview.thumbnail((470, 470))
        self.current_preview = ImageTk.PhotoImage(preview)
        self.preview_label.config(image=self.current_preview, text="")

    def refresh_current_preview(self):
        record = self.get_current_record()
        if record is None:
            self.preview_label.config(image="", text="아직 캡처가 없습니다.\nCtrl+Shift+S로 캡처하세요.")
        else:
            image_path = Path(record.get("image_path", ""))
            if image_path.exists():
                try:
                    self.show_preview(Image.open(image_path))
                except Exception:
                    self.preview_label.config(image="", text=f"이미지를 열 수 없습니다.\n{image_path}")
            else:
                self.preview_label.config(image="", text=f"이미지 파일을 찾을 수 없습니다.\n{image_path}")
        self.update_ocr_panel()
        self.update_execution_time_label()
        self.update_counter()

    def show_prev_record(self):
        active = self.active_record_indices()
        if not active:
            return
        if self.current_record_index not in active:
            self.current_record_index = active[-1]
        else:
            pos = max(active.index(self.current_record_index) - 1, 0)
            self.current_record_index = active[pos]
        self.refresh_current_preview()

    def show_next_record(self):
        active = self.active_record_indices()
        if not active:
            return
        if self.current_record_index not in active:
            self.current_record_index = active[-1]
        else:
            pos = min(active.index(self.current_record_index) + 1, len(active) - 1)
            self.current_record_index = active[pos]
        self.refresh_current_preview()

    def delete_current_record(self):
        record = self.get_current_record()
        if record is None:
            return
        capture_id = str(record.get("record_id") or "").strip()
        image_path = Path(str(record.get("image_path") or ""))
        if not messagebox.askyesno(
            "현재 캡처 삭제",
            "현재 캡처의 원본 이미지와 연결된 결과를 삭제할까요?\n"
            "삭제한 파일은 복구할 수 없습니다.",
        ):
            return

        try:
            with self.lesson_switch_lock:
                if self.execution_record is record or self.pending_capture_updates > 0:
                    messagebox.showwarning(
                        "캡처 삭제 대기",
                        "현재 캡처 처리가 끝난 뒤 삭제해 주세요.",
                    )
                    return
                deletion = delete_capture_files(self.workspace, record, self.capture_records)
                self.capture_records.remove(record)
                normalize_display_orders(self.capture_records)
                self.save_records()
        except Exception as exc:
            failed_path = getattr(exc, "path", None) or image_path
            try:
                append_event(
                    self.paths["events"],
                    {
                        "type": "capture_delete_failed",
                        "captureId": capture_id,
                        "path": str(failed_path),
                        "exists": Path(failed_path).exists(),
                        "error": str(exc),
                        "stack": traceback.format_exc(),
                    },
                )
            except Exception:
                pass
            messagebox.showerror(
                "캡처 삭제 실패",
                "캡처 원본 파일을 삭제하지 못했습니다.\n"
                "파일이 다른 프로그램에서 사용 중인지 확인해 주세요.\n\n"
                f"{exc}",
            )
            return

        for failure in deletion.get("failed_related", []):
            try:
                append_event(
                    self.paths["events"],
                    {
                        "type": "capture_related_file_delete_failed",
                        "captureId": capture_id,
                        "path": str(failure["path"]),
                        "error": failure["error"],
                    },
                )
            except Exception:
                pass
        active = self.active_record_indices()
        self.current_record_index = active[-1] if active else -1
        self.rebuild_outputs_from_records(save_records=False)
        self.refresh_current_preview()
        self.set_status(
            f"캡처를 삭제했습니다. 관련 파일 {len(deletion['deleted'])}개를 정리했습니다."
        )

    def reset_today(self):
        with self.lesson_switch_lock:
            if self.pending_capture_updates > 0 or self.execution_started_at is not None:
                messagebox.showwarning(
                    "초기화 대기",
                    "현재 캡처 또는 OCR/CAP 처리가 끝난 뒤 수업을 초기화하세요.",
                )
                return

        delete_images = messagebox.askyesnocancel(
            "현재 수업 초기화",
            "현재 수업의 캡처 기록을 초기화합니다.\n\n"
            "예: 기록과 원본 이미지를 함께 삭제 (복구할 수 없음)\n"
            "아니요: 기록만 초기화하고 원본 이미지는 유지\n"
            "취소: 초기화하지 않음",
            icon="warning",
        )
        if delete_images is None:
            return

        try:
            with self.lesson_switch_lock:
                if self.pending_capture_updates > 0 or self.execution_started_at is not None:
                    messagebox.showwarning(
                        "초기화 대기",
                        "현재 캡처 또는 OCR/CAP 처리가 끝난 뒤 수업을 초기화하세요.",
                    )
                    return
                result = self._apply_current_lesson_reset(bool(delete_images))
        except Exception as exc:
            messagebox.showerror(
                "현재 수업 초기화 실패",
                f"현재 수업을 초기화할 수 없습니다.\n\n{exc}",
            )
            return

        failed_paths = result["failed_paths"]
        if failed_paths:
            preview = "\n".join(str(path) for path in failed_paths[:5])
            if len(failed_paths) > 5:
                preview += f"\n외 {len(failed_paths) - 5}개"
            messagebox.showwarning(
                "일부 이미지 삭제 실패",
                "현재 수업 기록은 초기화했지만 일부 원본 이미지를 삭제하지 못했습니다.\n"
                "삭제되지 않은 이미지는 목록에서 제외된 상태로 유지됩니다.\n\n"
                + preview,
            )
            self.set_status(
                f"수업 기록 초기화 완료, 이미지 {result['deleted_count']}개 삭제, "
                f"{len(failed_paths)}개 삭제 실패"
            )
        elif delete_images:
            self.set_status(
                f"현재 수업 기록과 원본 이미지 {result['deleted_count']}개를 삭제했습니다."
            )
        else:
            self.set_status(
                f"현재 수업 기록을 초기화했습니다. 원본 이미지 {result['retained_count']}개는 유지됩니다."
            )

    def _apply_current_lesson_reset(self, delete_images: bool) -> dict:
        image_paths = self._capture_image_files()
        deleted_count = 0
        failed_paths: list[Path] = []

        if delete_images:
            for image_path in image_paths:
                try:
                    image_path.unlink(missing_ok=True)
                    deleted_count += 1
                except Exception:
                    failed_paths.append(image_path)
            retained_paths = failed_paths
        else:
            retained_paths = image_paths

        reset_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.capture_records = [
            {
                "record_id": image_path.stem,
                "image_path": str(image_path),
                "status": "reset",
                "deleted": True,
                "reset_at": reset_at,
            }
            for image_path in retained_paths
        ]
        self.current_record_index = -1
        self.current_preview = None
        self.rebuild_outputs_from_records()
        self.refresh_current_preview()
        try:
            append_event(
                self.paths["events"],
                {
                    "type": "lesson_reset",
                    "delete_images": bool(delete_images),
                    "image_count": len(image_paths),
                    "deleted_count": deleted_count,
                    "retained_count": len(retained_paths),
                    "delete_failed_count": len(failed_paths),
                },
            )
        except Exception:
            pass
        return {
            "image_count": len(image_paths),
            "deleted_count": deleted_count,
            "retained_count": len(retained_paths),
            "failed_paths": failed_paths,
        }

    def copy_text_to_clipboard(self, text: str) -> bool:
        try:
            if not text:
                return False
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            return True
        except Exception as e:
            append_event(self.paths["events"], {"type": "clipboard_copy_failed", "error": str(e)})
            return False

    def copy_current_ocr_to_clipboard(self):
        record = self.get_current_record()
        if record is None:
            messagebox.showwarning("OCR 복사 불가", "현재 선택된 캡처가 없습니다.")
            return

        text = str(record.get("ocr_text") or "").strip()
        if not text:
            messagebox.showwarning(
                "OCR 복사 불가",
                "현재 캡처에 저장된 OCR 결과가 없습니다.\n\n[현재 OCR 실행]을 먼저 누르거나 OCR 모드로 캡처하세요."
            )
            return

        if text.lstrip().startswith("## OCR 실패"):
            messagebox.showwarning("OCR 복사 불가", "현재 OCR 결과가 실패 로그입니다. 설정/API 키를 확인하세요.")
            return

        if self.copy_text_to_clipboard(text):
            self.set_status("현재 OCR 텍스트를 클립보드에 복사했습니다.")
            messagebox.showinfo("OCR 복사 완료", "OCR 텍스트를 클립보드에 복사했습니다.")
        else:
            messagebox.showerror("OCR 복사 실패", "클립보드 복사에 실패했습니다.")

    def run_current_ocr_ui(self):
        record = self.get_current_record()
        if record is None:
            messagebox.showwarning("OCR 실행 불가", "현재 선택된 캡처가 없습니다.")
            return
        self.run_ocr_for_record_async(record, auto_copy=True, force=True)

    def run_ocr_for_record_async(self, record: dict, auto_copy: bool = True, force: bool = False):
        image_path = Path(str(record.get("image_path") or ""))
        if not image_path.exists():
            messagebox.showwarning("OCR 실행 불가", f"이미지 파일을 찾을 수 없습니다.\n\n{image_path}")
            return

        if not self.has_nvidia_api_key():
            self.set_status("OCR 실행 불가: NVIDIA API 키가 없습니다. 설정에서 API 키를 입력하세요.")
            messagebox.showwarning(
                "OCR 실행 불가",
                "NVIDIA API 키가 없습니다.\n\n설정 > OCR > API 키를 입력한 뒤 다시 시도하세요."
            )
            return

        existing = str(record.get("ocr_text") or "").strip()
        if existing and not force and not existing.lstrip().startswith("## OCR 실패"):
            if auto_copy:
                self.copy_text_to_clipboard(existing)
                self.set_status("이미 저장된 OCR 텍스트를 클립보드에 복사했습니다.")
            return

        def worker():
            self.root.after(0, lambda: self._before_ocr_record(record))
            try:
                ocr_text = extract_text_from_image(
                    image_path,
                    self.config,
                    on_retry=self._show_transient_retry_status,
                )
            except Exception as e:
                ocr_text = f"## OCR 실패\n\nOCR 처리 중 예외가 발생했습니다.\n\n### 원인\n\n`{e}`"
            self.root.after(0, lambda: self._after_ocr_record(record, ocr_text, auto_copy=auto_copy))

        threading.Thread(target=worker, daemon=True).start()

    def _before_ocr_record(self, record: dict):
        image_path = Path(str(record.get("image_path") or ""))
        self.start_execution_timer(record, "ocr")
        self.processing_state = "OCR 처리 중"
        record["status"] = "ocr_running"
        self.update_mini_status()
        self.update_ocr_panel()
        self.update_counter()
        self.set_status(f"OCR 처리 중: {image_path.name}")

    def split_ocr_text(self, ocr_text: str) -> tuple[str, str]:
        """
        ocr_engine은 실패 메시지까지 포함해 문자열을 반환한다.
        성공 결과에서는 정리본과 원문을 구분할 수 있도록 아래 마커를 지원한다.
        구버전 결과에는 같은 텍스트를 원문/정리본으로 함께 사용한다.
        """
        text = str(ocr_text or "")
        marker = "\n\n--- OCR_RAW_TEXT ---\n"
        if marker in text:
            cleaned, raw = text.split(marker, 1)
            return cleaned.strip(), raw.strip()
        return text.strip(), text.strip()


    def _after_ocr_record(self, record: dict, ocr_text: str, auto_copy: bool = True):
        cleaned_text, raw_text = self.split_ocr_text(ocr_text)
        failed = str(cleaned_text or "").lstrip().startswith("## OCR 실패")
        record["ocr_text"] = cleaned_text or ""
        record["ocr_raw_text"] = raw_text or cleaned_text or ""

        # OCR을 다시 실행하면 이전 보정 결과는 더 이상 현재 결과가 아닙니다.
        for key in [
            "ocr_corrected_text",
            "ocr_correction_model",
            "ocr_correction_at",
            "ocr_correction_error",
            "ocr_correction_elapsed_sec",
            "ocr_interpretation_text",
            "ocr_interpretation_error",
            "flow_interpretation_status",
            "flow_interpretation_error",
        ]:
            record.pop(key, None)
        record["ocr_cleaned_diff"] = bool((raw_text or "").strip() and (raw_text or "").strip() != (cleaned_text or "").strip())
        record["ocr_provider"] = str(self.config.get("nvidia_ocr_model") or "nvidia/nemotron-ocr-v2")
        record["ocr_preprocess"] = str(self.config.get("ocr_preprocess_mode") or "sharp_gray")
        record["ocr_upscale"] = f"{self.config.get('ocr_upscale_factor', 2.5)}x / {self.config.get('ocr_target_long_side', 2800)}px"
        record["ocr_cleanup"] = bool(self.config.get("ocr_post_cleanup_enabled", True))
        record["ocr_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        record["status"] = "ocr_failed" if failed else "ocr_done"
        record["display_result_type"] = "ocr"
        record["last_process_type"] = "ocr"
        self.processing_state = "OCR 실패" if failed else "OCR 완료"
        elapsed = self.stop_execution_timer(record, save_result=True)

        self.save_records()
        self.rebuild_outputs_from_records()
        self.refresh_current_preview()
        self.update_mini_status()
        self.update_counter()
        append_event(
            self.paths["events"],
            {
                "type": "ocr_done",
                "path": str(record.get("image_path") or ""),
                "failed": failed,
                "auto_copy": bool(auto_copy),
                "elapsed_sec": round(elapsed, 3),
            },
        )

        if failed:
            self.set_status(f"OCR 실패 ({self.format_execution_seconds(elapsed)}): 설정/API 키 또는 OCR 응답을 확인하세요.")
            return

        # 빠른 OCR 결과는 즉시 사용할 수 있게 두고, 수업 흐름용 의미 해석은
        # 별도 작업에서 진행한다. 이 작업은 현재 결과 패널이나 클립보드를 바꾸지 않는다.
        interpretation_started = self.start_flow_interpretation_background(record)
        background_suffix = " · 수업 흐름 해석은 백그라운드 진행" if interpretation_started else ""

        if auto_copy and bool(self.config.get("copy_ocr_to_clipboard_on_done", True)):
            copied = self.copy_text_to_clipboard(cleaned_text)
            elapsed_text = self.format_execution_seconds(elapsed)
            self.set_status(
                f"OCR 완료 ({elapsed_text}) + 클립보드 복사 완료{background_suffix}"
                if copied
                else f"OCR 완료 ({elapsed_text}), 복사 실패{background_suffix}"
            )
        elif interpretation_started:
            self.set_status(
                f"OCR 완료 ({self.format_execution_seconds(elapsed)}) · 수업 흐름 해석은 백그라운드 진행"
            )


    def start_flow_interpretation_background(self, record: dict, force: bool = False) -> bool:
        """Queue an OCR capture for the single lesson-flow interpretation worker."""
        if not isinstance(record, dict) or str(record.get("mode") or "").lower() != "ocr":
            return False
        if record.get("flow_interpretation_status") in {"queued", "running"}:
            return False
        if record.get("ocr_interpretation_text") and not force:
            return False

        ocr_text = str(record.get("ocr_corrected_text") or record.get("ocr_text") or "").strip()
        image_path = Path(str(record.get("image_path") or ""))
        if not ocr_text or ocr_text.lstrip().startswith("## OCR 실패") or not image_path.is_file():
            return False
        if not self.has_nvidia_api_key():
            record["flow_interpretation_status"] = "waiting_for_api_key"
            self.save_records()
            self.rebuild_outputs_from_records(save_records=False)
            return False

        workspace_at_start = Path(self.workspace).resolve()
        record_id = str(record.get("record_id") or "").strip()
        if not record_id:
            return False
        pending_key = (str(workspace_at_start), record_id)
        with self.flow_interpretation_lock:
            if pending_key in self.flow_interpretation_pending:
                return False
            self.flow_interpretation_pending.add(pending_key)

        base_prompt = str(self.config.get("cap_reasoning_prompt") or DEFAULT_CAP_PROMPT).strip()
        prompt = (
            base_prompt
            + "\n\n수업 흐름 전용 추가 지시:\n"
            + "- 아래 OCR과 원본 이미지를 함께 보고 학습자가 이해하기 쉬운 해설을 작성하세요.\n"
            + "- OCR 문장을 그대로 반복하지 말고 개념, 절차, 코드 또는 오류의 의미를 설명하세요.\n"
            + "- 이미지와 OCR이 충돌하면 이미지를 우선하고 보이지 않는 내용은 추측하지 마세요.\n"
            + "- 최종 결과만 한국어 Markdown으로 반환하세요.\n\n"
            + "--- OCR 시작 ---\n"
            + ocr_text[:16000]
            + "\n--- OCR 끝 ---\n"
        )
        inference_config = dict(self.config)
        inference_config["cap_reasoning_prompt"] = prompt
        record["flow_interpretation_status"] = "queued"
        record.pop("flow_interpretation_error", None)
        self.save_records()
        self.rebuild_outputs_from_records(save_records=False)
        self.update_result_action_buttons()
        self.flow_interpretation_queue.put({
            "record": record,
            "record_id": record_id,
            "pending_key": pending_key,
            "workspace": workspace_at_start,
            "image_path": image_path,
            "config": inference_config,
        })
        return True


    def _flow_interpretation_worker_loop(self) -> None:
        while self.running:
            try:
                job = self.flow_interpretation_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if job is None:
                self.flow_interpretation_queue.task_done()
                return
            record = job["record"]
            workspace_at_start = job["workspace"]
            pending_key = job["pending_key"]
            try:
                if (
                    Path(self.workspace).resolve() != Path(workspace_at_start).resolve()
                    or not any(value is record for value in self.capture_records)
                ):
                    with self.flow_interpretation_lock:
                        self.flow_interpretation_pending.discard(pending_key)
                    continue
                record["flow_interpretation_status"] = "running"
                self.root.after(0, lambda current=record: self._mark_flow_interpretation_running(current))
                try:
                    result = analyze_capture_image(
                        job["image_path"],
                        job["config"],
                        on_retry=self._show_transient_retry_status,
                    )
                except Exception as exc:
                    result = "수업 흐름 해석 실패\n\n" + str(exc)
                result = str(result or "").strip()
                if result.startswith("CAP 분석 실패"):
                    result = result.replace("CAP 분석 실패", "수업 흐름 해석 실패", 1)
                self.root.after(
                    0,
                    lambda current=record, value=result, workspace=workspace_at_start, key=pending_key: (
                        self._after_flow_interpretation(current, value, workspace, key)
                    ),
                )
            finally:
                self.flow_interpretation_queue.task_done()


    def _mark_flow_interpretation_running(self, record: dict) -> None:
        if any(value is record for value in self.capture_records):
            self.save_records()
            self.rebuild_outputs_from_records(save_records=False)


    def _after_flow_interpretation(
        self,
        record: dict,
        result_text: str,
        workspace_at_start: Path,
        pending_key=None,
    ) -> None:
        key = pending_key or (
            str(Path(workspace_at_start).resolve()),
            str(record.get("record_id") or "").strip(),
        )
        with self.flow_interpretation_lock:
            self.flow_interpretation_pending.discard(key)
        # 수업을 바꾸거나 캡처를 삭제한 동안 끝난 오래된 작업은 현재 수업에 섞지 않는다.
        if Path(self.workspace).resolve() != Path(workspace_at_start).resolve():
            return
        if not any(value is record for value in self.capture_records):
            return

        if record.pop("flow_interpretation_requeue", False):
            record.pop("flow_interpretation_status", None)
            self.save_records()
            self.rebuild_outputs_from_records(save_records=False)
            self.start_flow_interpretation_background(record, force=True)
            return

        result_text = str(result_text or "").strip()
        failed = not result_text or result_text.startswith("수업 흐름 해석 실패")
        if failed:
            record["flow_interpretation_status"] = "failed"
            record["flow_interpretation_error"] = result_text or "빈 응답"
        else:
            record["flow_interpretation_status"] = "done"
            record["ocr_interpretation_text"] = result_text
            record.pop("flow_interpretation_error", None)

        self.save_records()
        self.rebuild_outputs_from_records(save_records=False)
        self.update_result_action_buttons()
        append_event(
            self.paths["events"],
            {
                "type": "flow_interpretation_done",
                "path": str(record.get("image_path") or ""),
                "failed": failed,
                "background": True,
            },
        )
        if failed:
            self.set_status("OCR 결과는 유지했습니다. 수업 흐름 백그라운드 해석은 실패했습니다.")
        else:
            self.set_status("수업 흐름 백그라운드 해석이 완료되었습니다.")


    def run_cap_reasoning_for_record_async(self, record: dict, auto_copy: bool = False, force: bool = False):
        image_path = Path(str(record.get("image_path") or ""))
        if not image_path.exists():
            messagebox.showwarning("CAP 실행 불가", f"이미지 파일을 찾을 수 없습니다.\n\n{image_path}")
            return

        if not self.has_nvidia_api_key():
            self.set_status("CAP 실행 불가: NVIDIA API 키가 없습니다.")
            messagebox.showwarning(
                "CAP 실행 불가",
                "NVIDIA API 키가 없습니다.\n\n설정에서 NVIDIA API 키를 입력하세요.",
            )
            return

        existing = str(record.get("cap_text") or "").strip()
        if existing and not force:
            self.set_status(
                "저장된 CAP 해석 결과를 표시했습니다. "
                "원본 이미지는 클립보드에 유지됩니다."
            )
            return

        def before():
            self.start_execution_timer(record, "capture")
            self.processing_state = "CAP 분석 중"
            record["status"] = "cap_running"
            self.save_records()
            self.update_mini_status()
            self.update_ocr_panel()
            self.update_counter()
            self.set_status("CAP 이미지 분석 중...")

        def worker():
            self.root.after(0, before)
            try:
                result_text = analyze_capture_image(
                    image_path,
                    self.config,
                    on_retry=self._show_transient_retry_status,
                )
            except Exception as exc:
                result_text = f"CAP 분석 실패\n\n{exc}"

            self.root.after(
                0,
                lambda: self._after_cap_reasoning_record(
                    record,
                    result_text,
                    auto_copy=auto_copy,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()


    def _after_cap_reasoning_record(self, record: dict, result_text: str, auto_copy: bool = False):
        result_text = str(result_text or "").strip()
        failed = result_text.startswith("CAP 분석 실패")

        record["cap_text"] = result_text
        record["cap_model"] = str(
            self.config.get("cap_reasoning_model")
            or "qwen/qwen3.5-397b-a17b"
        )
        record["cap_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        record["status"] = "cap_failed" if failed else "cap_done"
        record["display_result_type"] = "cap"
        record["last_process_type"] = "cap"
        self.processing_state = "CAP 실패" if failed else "CAP 완료"
        elapsed = self.stop_execution_timer(record, save_result=True)

        self.save_records()
        self.rebuild_outputs_from_records()
        self.refresh_current_preview()
        self.update_mini_status()
        self.update_counter()

        append_event(
            self.paths["events"],
            {
                "type": "cap_reasoning_done",
                "path": str(record.get("image_path") or ""),
                "failed": failed,
                "model": record.get("cap_model"),
                "elapsed_sec": round(elapsed, 3),
            },
        )

        if failed:
            self.set_status(f"CAP 이미지 분석 실패 ({self.format_execution_seconds(elapsed)}). 현재 결과창에서 오류를 확인하세요.")
            return

        self.set_status(
            "CAP 이미지 분석 완료 "
            f"({self.format_execution_seconds(elapsed)}). "
            "원본 이미지는 클립보드에 유지됩니다. "
            "텍스트가 필요하면 [CAP 해석 복사]를 누르세요."
        )



    def has_nvidia_api_key(self) -> bool:
        return bool(str(self.config.get("nvidia_api_key") or os.environ.get("NVIDIA_API_KEY") or os.environ.get("OCR_API_KEY") or "").strip())

    def open_flow_window(self):
        self._ensure_records_for_capture_files()
        self.rebuild_outputs_from_records()
        try:
            document = self.build_current_flow_document()
            save_flow_document(self.paths["flow_document"], document)
            open_flow_result_window(
                self.root,
                self.workspace,
                document,
                self.paths["flow_document"],
                self.set_status,
                lambda event: append_event(self.paths["events"], event),
            )
            self.set_status("현재 수업의 수업 흐름 창을 열었습니다.")
        except Exception as e:
            messagebox.showerror("수업 흐름 열기 실패", str(e))

    def open_capture_folder(self):
        capture_dir = self.paths.get("captures")
        if capture_dir is None:
            messagebox.showerror("캡처 폴더 열기 실패", "캡처 폴더 경로를 찾을 수 없습니다.")
            return

        capture_dir = Path(capture_dir)
        capture_dir.mkdir(parents=True, exist_ok=True)

        try:
            if sys.platform.startswith("win"):
                os.startfile(capture_dir)
            else:
                import webbrowser
                webbrowser.open(capture_dir.as_uri())
            self.set_status(f"캡처 폴더 열기: {capture_dir}")
        except Exception as e:
            messagebox.showerror("캡처 폴더 열기 실패", f"캡처 폴더를 여는 중 오류가 발생했습니다.\n\n{e}")


    def open_settings_window(self):
        win = tk.Toplevel(self.root)
        win.title("ClassFlowAI 설정")
        win.geometry("900x720")
        win.minsize(820, 660)
        win.transient(self.root)
        win.grab_set()

        footer = tk.Frame(
            win,
            bd=1,
            relief="raised",
            bg="#f4f4f4",
        )
        footer.pack(
            fill="x",
            side="bottom",
        )

        status_var = tk.StringVar(
            value="필요한 항목만 수정한 뒤 저장하세요."
        )
        tk.Label(
            footer,
            textvariable=status_var,
            anchor="w",
            bg="#f4f4f4",
        ).pack(
            side="left",
            padx=12,
            pady=10,
        )

        notebook = ttk.Notebook(win)
        notebook.pack(
            fill="both",
            expand=True,
            padx=12,
            pady=12,
        )

        basic_tab = tk.Frame(
            notebook,
            padx=16,
            pady=14,
        )
        cap_prompt_tab = tk.Frame(
            notebook,
            padx=16,
            pady=14,
        )

        notebook.add(
            basic_tab,
            text="기본 / 모델",
        )
        notebook.add(
            cap_prompt_tab,
            text="CAP 프롬프트",
        )

        # 기본 / 모델 탭은 한 화면에 전부 표시합니다.
        basic_form = basic_tab

        def section(parent, title):
            tk.Label(
                parent,
                text=title,
                font=("맑은 고딕", 12, "bold"),
                anchor="w",
            ).pack(
                fill="x",
                pady=(7, 3),
            )

        def labeled_entry(
            parent,
            label,
            initial="",
            show=None,
            width_label=16,
        ):
            row = tk.Frame(parent)
            row.pack(
                fill="x",
                pady=3,
            )
            tk.Label(
                row,
                text=label,
                width=width_label,
                anchor="w",
            ).pack(side="left")

            var = tk.StringVar(
                value=str(initial or "")
            )
            entry = tk.Entry(
                row,
                textvariable=var,
                show=show,
            )
            entry.pack(
                side="left",
                fill="x",
                expand=True,
            )
            return row, var, entry

        def hotkey_row(parent, key, label):
            row, var, _entry = labeled_entry(
                parent,
                label,
                self.config.get(key, ""),
            )
            tk.Button(
                row,
                text="눌러서 변경",
                width=12,
                command=lambda v=var, name=label: self.open_hotkey_capture_dialog(
                    v,
                    f"{name} 변경",
                ),
            ).pack(
                side="left",
                padx=(6, 0),
            )
            return var

        def set_prompt_text(widget, value):
            widget.delete("1.0", tk.END)
            widget.insert("1.0", value)
            widget.mark_set("insert", "1.0")
            widget.see("1.0")

        # ------------------------------------------------------
        # 기본 / 모델
        # ------------------------------------------------------
        section(
            basic_form,
            "저장 위치",
        )
        workspace_row, workspace_var, _ = labeled_entry(
            basic_form,
            "캡처 저장 위치",
            self.config.get("workspace_dir", ""),
        )

        def browse_workspace():
            selected = filedialog.askdirectory(
                title="캡처 저장 위치 선택"
            )
            if selected:
                workspace_var.set(selected)

        tk.Button(
            workspace_row,
            text="찾기",
            width=8,
            command=browse_workspace,
        ).pack(
            side="left",
            padx=(6, 0),
        )

        section(
            basic_form,
            "단축키",
        )
        screenshot_hotkey_var = hotkey_row(
            basic_form,
            "screenshot_hotkey",
            "화면 캡처",
        )
        mode_hotkey_var = hotkey_row(
            basic_form,
            "mode_toggle_hotkey",
            "OCR / CAP 전환",
        )
        pause_hotkey_var = hotkey_row(
            basic_form,
            "pause_toggle_hotkey",
            "감지 일시정지",
        )
        show_window_hotkey_var = hotkey_row(
            basic_form,
            "show_window_hotkey",
            "창 표시 / 최소화",
        )

        section(
            basic_form,
            "NVIDIA API 키",
        )
        key_row, new_api_key_var, key_entry = labeled_entry(
            basic_form,
            "API 키",
            "",
            show="*",
        )
        key_visible = {"value": False}
        clear_saved_key = {"value": False}

        def toggle_key_visibility():
            key_visible["value"] = not key_visible["value"]
            key_entry.config(
                show=(
                    ""
                    if key_visible["value"]
                    else "*"
                )
            )
            key_toggle_button.config(
                text=(
                    "숨기기"
                    if key_visible["value"]
                    else "표시"
                )
            )

        def mark_key_for_deletion():
            clear_saved_key["value"] = True
            new_api_key_var.set("")
            status_var.set(
                "저장하면 API 키를 삭제합니다."
            )

        key_toggle_button = tk.Button(
            key_row,
            text="표시",
            width=8,
            command=toggle_key_visibility,
        )
        key_toggle_button.pack(
            side="left",
            padx=(6, 0),
        )

        tk.Button(
            key_row,
            text="삭제",
            width=8,
            command=mark_key_for_deletion,
        ).pack(
            side="left",
            padx=(6, 0),
        )

        def model_row(
            parent,
            title,
            current_model,
            default_model,
        ):
            section(
                parent,
                title,
            )

            id_row = tk.Frame(parent)
            id_row.pack(
                fill="x",
                pady=(4, 2),
            )
            tk.Label(
                id_row,
                text="모델 ID",
                width=16,
                anchor="w",
            ).pack(side="left")

            values = []
            for model_id in [
                current_model,
                default_model,
            ]:
                if (
                    model_id
                    and model_id not in values
                ):
                    values.append(model_id)

            var = tk.StringVar(
                value=current_model,
            )
            combo = ttk.Combobox(
                id_row,
                textvariable=var,
                values=values,
                state="normal",
            )
            combo.pack(
                side="left",
                fill="x",
                expand=True,
            )

            def normalized():
                return str(
                    var.get()
                    or ""
                ).strip().strip("/")

            def page_url():
                model_id = normalized()
                return (
                    f"https://build.nvidia.com/{model_id}"
                    if model_id
                    else ""
                )

            url_var = tk.StringVar(
                value=page_url(),
            )

            def refresh_url(_event=None):
                url_var.set(
                    page_url()
                )

            def open_page():
                url = page_url()
                if url:
                    webbrowser.open(url)

            tk.Button(
                id_row,
                text="모델 페이지",
                width=12,
                command=open_page,
            ).pack(
                side="left",
                padx=(6, 0),
            )

            url_row = tk.Frame(parent)
            url_row.pack(
                fill="x",
                pady=(0, 4),
            )
            tk.Label(
                url_row,
                text="모델 페이지 URL",
                width=16,
                anchor="w",
            ).pack(side="left")

            url_entry = tk.Entry(
                url_row,
                textvariable=url_var,
                state="readonly",
                readonlybackground="#f4f4f4",
            )
            url_entry.pack(
                side="left",
                fill="x",
                expand=True,
            )

            combo.bind(
                "<<ComboboxSelected>>",
                refresh_url,
            )
            combo.bind(
                "<KeyRelease>",
                refresh_url,
            )

            return var, normalized

        ocr_model_var, normalized_ocr_model = model_row(
            basic_form,
            "OCR 모델",
            str(
                self.config.get("nvidia_ocr_model")
                or "nvidia/nemotron-ocr-v2"
            ),
            "nvidia/nemotron-ocr-v2",
        )

        cap_model_var, normalized_cap_model = model_row(
            basic_form,
            "CAP 추론 모델",
            str(
                self.config.get("cap_reasoning_model")
                or "qwen/qwen3.5-397b-a17b"
            ),
            "qwen/qwen3.5-397b-a17b",
        )

        # ------------------------------------------------------
        # CAP prompt only
        # ------------------------------------------------------
        section(
            cap_prompt_tab,
            "이미지 해석 프롬프트",
        )
        cap_prompt_editor = ScrolledText(
            cap_prompt_tab,
            wrap="word",
            font=("맑은 고딕", 10),
            undo=True,
        )
        cap_prompt_editor.pack(
            fill="both",
            expand=True,
            pady=(0, 8),
        )
        set_prompt_text(
            cap_prompt_editor,
            str(
                self.config.get("cap_reasoning_prompt")
                or DEFAULT_CAP_PROMPT
            ),
        )

        cap_buttons = tk.Frame(
            cap_prompt_tab
        )
        cap_buttons.pack(fill="x")

        tk.Button(
            cap_buttons,
            text="기본값 복원",
            command=lambda: set_prompt_text(
                cap_prompt_editor,
                DEFAULT_CAP_PROMPT,
            ),
        ).pack(side="left")

        def save_settings():
            try:
                ocr_model = normalized_ocr_model()
                cap_model = normalized_cap_model()

                if not ocr_model:
                    notebook.select(basic_tab)
                    messagebox.showwarning(
                        "모델 ID 필요",
                        "OCR 모델 ID를 입력하세요.",
                    )
                    return

                if not cap_model:
                    notebook.select(basic_tab)
                    messagebox.showwarning(
                        "모델 ID 필요",
                        "CAP 모델 ID를 입력하세요.",
                    )
                    return

                cap_prompt = cap_prompt_editor.get(
                    "1.0",
                    "end-1c",
                ).strip()
                if not cap_prompt:
                    cap_prompt = DEFAULT_CAP_PROMPT

                new_config = dict(self.config)
                new_config.update(
                    {
                        "settings_schema_version": 5,
                        "workspace_dir": workspace_var.get().strip(),
                        "screenshot_hotkey": screenshot_hotkey_var.get().strip(),
                        "mode_toggle_hotkey": mode_hotkey_var.get().strip(),
                        "pause_toggle_hotkey": pause_hotkey_var.get().strip(),
                        "show_window_hotkey": show_window_hotkey_var.get().strip(),

                        "nvidia_ocr_model": ocr_model,
                        "nvidia_model_url": (
                            f"https://build.nvidia.com/{ocr_model}"
                        ),
                        "nvidia_api_base": (
                            f"https://ai.api.nvidia.com/v1/cv/{ocr_model}"
                        ),

                        "cap_reasoning_model": cap_model,
                        "cap_reasoning_model_url": (
                            f"https://build.nvidia.com/{cap_model}"
                        ),
                        "cap_reasoning_prompt": cap_prompt,

                        "hide_app_during_screenshot": True,
                        "mini_status_enabled": True,
                        "mini_status_topmost": True,
                        "mini_status_width": 56,
                        "mini_status_height": 56,
                        "ocr_upscale_enabled": True,
                        "ocr_post_cleanup_enabled": True,
                        "copy_ocr_to_clipboard_on_done": True,
                        "copy_cap_to_clipboard_on_done": False,

                        "cap_reasoning_api_base": "https://integrate.api.nvidia.com/v1/chat/completions",
                        "cap_reasoning_connect_timeout_sec": 15,
                        "cap_reasoning_timeout_sec": 150,
                        "cap_reasoning_max_tokens": 4096,
                        "cap_reasoning_max_long_side": 3200,
                    }
                )

                replacement_key = str(
                    new_api_key_var.get()
                    or ""
                ).strip()

                if replacement_key.lower().startswith(
                    "bearer "
                ):
                    replacement_key = replacement_key[7:].strip()

                replacement_key = (
                    replacement_key
                    .strip()
                    .strip('"')
                    .strip("'")
                )

                if clear_saved_key["value"]:
                    new_config["nvidia_api_key"] = ""
                elif replacement_key:
                    new_config["nvidia_api_key"] = replacement_key
                else:
                    new_config["nvidia_api_key"] = str(
                        self.config.get("nvidia_api_key")
                        or ""
                    )

                requested_workspace_value = str(
                    new_config.get("workspace_dir")
                    or ""
                ).strip()
                requested_storage_root = (
                    Path(requested_workspace_value)
                    if requested_workspace_value
                    else get_default_workspace(
                        bool(new_config.get("use_daily_folder", True))
                    )
                )
                try:
                    storage_root_changed = (
                        requested_storage_root.resolve()
                        != self.storage_root.resolve()
                    )
                except Exception:
                    storage_root_changed = requested_storage_root != self.storage_root
                if storage_root_changed and self.lesson_switch_blocked():
                    return

                save_config(new_config)
                self.config = load_config()

                workspace_value = str(
                    self.config.get("workspace_dir")
                    or ""
                ).strip()

                updated_storage_root = (
                    Path(workspace_value)
                    if workspace_value
                    else get_default_workspace(
                        bool(
                            self.config.get(
                                "use_daily_folder",
                                True,
                            )
                        )
                    )
                )
                if storage_root_changed:
                    self.storage_root = updated_storage_root
                    self.workspace = get_current_lesson(
                        self.storage_root
                    )
                    self.paths = ensure_workspace(
                        self.workspace
                    )
                    self.load_records()
                else:
                    self.storage_root = updated_storage_root
                self.workspace_var.set(
                    self.lesson_location_text()
                )

                try:
                    if self.global_keyboard_listener:
                        self.global_keyboard_listener.stop()
                    if self.global_mouse_listener:
                        self.global_mouse_listener.stop()
                except Exception:
                    pass

                self.global_pressed_keys = set()
                self.start_global_hotkey_listener()
                self.update_mode_badge()

                try:
                    if (
                        self.mini_status_window is not None
                        and self.mini_status_window.winfo_exists()
                    ):
                        self.mini_status_window.destroy()
                except Exception:
                    pass

                self.mini_status_window = None
                self.create_mini_status_window()
                self.refresh_current_preview()

                self.set_status(
                    "설정을 저장했습니다."
                )
                messagebox.showinfo(
                    "설정 저장",
                    "설정을 저장했습니다.",
                )
                win.destroy()
            except Exception as exc:
                messagebox.showerror(
                    "설정 저장 실패",
                    "설정 저장 중 오류가 발생했습니다.\n\n"
                    + str(exc),
                )

        tk.Button(
            footer,
            text="저장",
            command=save_settings,
            width=12,
            height=2,
        ).pack(
            side="right",
            padx=(8, 12),
            pady=8,
        )

        tk.Button(
            footer,
            text="닫기",
            command=win.destroy,
            width=10,
            height=2,
        ).pack(
            side="right",
            padx=8,
            pady=8,
        )



    def format_hotkey_for_display(self, hotkey: str) -> str:
        tokens = str(hotkey or "").strip().lower().replace(" ", "").split("+")
        labels = {
            "ctrl": "Ctrl", "control": "Ctrl", "shift": "Shift", "alt": "Alt",
            "win": "Win", "cmd": "Win", "command": "Win",
            "middle": "휠클릭", "wheel": "휠클릭", "left": "좌클릭", "right": "우클릭",
            "space": "Space", "enter": "Enter", "esc": "Esc",
        }
        result = []
        for token in tokens:
            if token:
                result.append(labels.get(token, token.upper() if len(token) == 1 else token))
        return "+".join(result) if result else "미설정"

    def update_mode_badge(self):
        if not hasattr(self, "mode_var"):
            return
        hotkey_text = self.format_hotkey_for_display(self.config.get("mode_toggle_hotkey", "middle"))
        if self.capture_mode == "ocr":
            self.mode_var.set(f"OCR 모드 | {hotkey_text}")
            self.mode_label.config(bg="#1d4ed8", fg="#ffffff")
        else:
            self.mode_var.set(f"CAP 이미지 해석 | {hotkey_text}")
            self.mode_label.config(bg="#ffedd5", fg="#9a3412")
        self.update_mini_status()

    def save_runtime_mode(self):
        try:
            new_config = dict(self.config)
            new_config["capture_mode"] = self.capture_mode
            save_config(new_config)
            self.config = load_config()
        except Exception as e:
            append_event(self.paths["events"], {"type": "mode_save_failed", "error": str(e)})

    def toggle_capture_mode_global(self):
        return self.toggle_capture_mode()

    def toggle_capture_mode(self, event=None):
        now = time.perf_counter()
        if now - self.last_mode_toggle_at < 0.45:
            return "break"
        self.last_mode_toggle_at = now
        self.capture_mode = "capture" if self.capture_mode == "ocr" else "ocr"
        self.processing_state = "OCR 모드" if self.capture_mode == "ocr" else "CAP 이미지 해석 모드"
        self.update_mode_badge()
        self.save_runtime_mode()
        self.update_counter()
        self.set_status(f"{self.mode_var.get()}로 전환했습니다.")
        return "break"

    def open_hotkey_capture_dialog(self, target_var, title="단축키 변경"):
        if pynput_keyboard is None or pynput_mouse is None:
            messagebox.showerror("단축키 변경 불가", "pynput이 설치되어 있지 않아 단축키 기록을 사용할 수 없습니다.")
            return

        self.hotkey_capture_active = True
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("430x170")
        dialog.transient(self.root)
        dialog.grab_set()

        info_var = tk.StringVar(value="원하는 키 조합을 누르세요.\n예: Ctrl+Shift+S / Ctrl+휠클릭 / 휠클릭")
        tk.Label(dialog, textvariable=info_var, justify="left", font=("맑은 고딕", 10)).pack(fill="x", padx=16, pady=(16, 8))

        pressed = set()
        listeners = {"keyboard": None, "mouse": None}
        finished = {"value": False}

        def normalize_output(tokens):
            order = ["ctrl", "shift", "alt", "win"]
            rest = sorted([t for t in tokens if t not in order])
            final = [t for t in order if t in tokens] + rest
            return "+".join(final)

        def cleanup():
            try:
                if listeners["keyboard"]:
                    listeners["keyboard"].stop()
            except Exception:
                pass
            try:
                if listeners["mouse"]:
                    listeners["mouse"].stop()
            except Exception:
                pass
            self.hotkey_capture_active = False

        def finish(tokens):
            if finished["value"]:
                return
            finished["value"] = True
            value = normalize_output(tokens)
            cleanup()
            if value:
                self.root.after(0, lambda: target_var.set(value))
            self.root.after(0, dialog.destroy)

        def cancel():
            if finished["value"]:
                return
            finished["value"] = True
            cleanup()
            dialog.destroy()

        def on_press(key):
            token = self.key_to_token(key)
            if not token:
                return
            pressed.add(token)
            info_var.set("입력 중: " + self.format_hotkey_for_display(normalize_output(pressed)))
            if token not in {"ctrl", "shift", "alt", "win"}:
                finish(set(pressed))

        def on_release(key):
            token = self.key_to_token(key)
            if token in {"ctrl", "shift", "alt", "win"}:
                pressed.discard(token)

        def on_click(x, y, button, is_pressed):
            if not is_pressed:
                return
            token = self.mouse_button_to_token(button)
            if token:
                finish(set(pressed) | {token})

        tk.Button(dialog, text="취소", command=cancel, width=10).pack(pady=(4, 12))
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        listeners["keyboard"] = pynput_keyboard.Listener(on_press=on_press, on_release=on_release)
        listeners["mouse"] = pynput_mouse.Listener(on_click=on_click)
        listeners["keyboard"].daemon = True
        listeners["mouse"].daemon = True
        listeners["keyboard"].start()
        listeners["mouse"].start()

    def refresh_flow_overview(self):
        self.flow_text.configure(state="normal")
        self.flow_text.delete("1.0", tk.END)
        self.flow_text.insert("1.0", self.get_ocr_panel_text())
        self.flow_text.configure(state="disabled")

    def normalize_hotkey_tokens(self, hotkey: str) -> set[str]:
        aliases = {"control": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl", "shift_l": "shift", "shift_r": "shift", "alt_l": "alt", "alt_r": "alt", "cmd": "win", "command": "win", "spacebar": "space", "wheel": "middle", "휠클릭": "middle"}
        tokens = set()
        for raw in str(hotkey or "").lower().replace(" ", "").split("+"):
            if raw:
                tokens.add(aliases.get(raw, raw))
        return tokens

    def key_to_token(self, key):
        if pynput_keyboard is None:
            return ""
        special = {
            pynput_keyboard.Key.ctrl: "ctrl",
            pynput_keyboard.Key.ctrl_l: "ctrl",
            pynput_keyboard.Key.ctrl_r: "ctrl",
            pynput_keyboard.Key.shift: "shift",
            pynput_keyboard.Key.shift_l: "shift",
            pynput_keyboard.Key.shift_r: "shift",
            pynput_keyboard.Key.alt: "alt",
            pynput_keyboard.Key.alt_l: "alt",
            pynput_keyboard.Key.alt_r: "alt",
            pynput_keyboard.Key.cmd: "win",
            pynput_keyboard.Key.cmd_l: "win",
            pynput_keyboard.Key.cmd_r: "win",
            pynput_keyboard.Key.space: "space",
            pynput_keyboard.Key.enter: "enter",
            pynput_keyboard.Key.esc: "esc",
        }
        if key in special:
            return special[key]
        ch = getattr(key, "char", None)
        if ch:
            return str(ch).lower()
        return str(getattr(key, "name", "")).lower()

    def mouse_button_to_token(self, button):
        if pynput_mouse is None:
            return ""
        if button == pynput_mouse.Button.middle:
            return "middle"
        if button == pynput_mouse.Button.left:
            return "left"
        if button == pynput_mouse.Button.right:
            return "right"
        return str(button).lower().replace("button.", "")

    def get_active_modifier_tokens(self) -> set[str]:
        """
        마우스 클릭 순간의 실제 Ctrl/Shift/Alt/Win 상태를 읽는다.

        global_pressed_keys만 사용하면 키 릴리스 이벤트 누락으로
        Ctrl이 계속 눌린 것으로 남거나, 반대로 조합이 인식되지 않을 수 있다.
        Windows에서는 GetAsyncKeyState를 우선 사용한다.
        """
        modifier_tokens = {"ctrl", "shift", "alt", "win"}

        if sys.platform.startswith("win"):
            try:
                user32 = ctypes.windll.user32
                active = set()

                # VK_CONTROL, VK_SHIFT, VK_MENU(Alt), VK_LWIN, VK_RWIN
                if user32.GetAsyncKeyState(0x11) & 0x8000:
                    active.add("ctrl")
                if user32.GetAsyncKeyState(0x10) & 0x8000:
                    active.add("shift")
                if user32.GetAsyncKeyState(0x12) & 0x8000:
                    active.add("alt")
                if (
                    user32.GetAsyncKeyState(0x5B) & 0x8000
                    or user32.GetAsyncKeyState(0x5C) & 0x8000
                ):
                    active.add("win")

                return active
            except Exception:
                pass

        return {
            token
            for token in self.global_pressed_keys
            if token in modifier_tokens
        }


    def mouse_hotkey_matches(self, hotkey: str, button_token: str) -> bool:
        """
        마우스 버튼과 보조키 조합을 정확히 비교한다.

        예:
        - middle       -> 보조키 없이 휠클릭
        - ctrl+middle  -> Ctrl을 누른 상태의 휠클릭

        일반 문자키나 과거에 남은 키 상태는 비교에서 제외한다.
        """
        tokens = self.normalize_hotkey_tokens(hotkey)
        mouse_tokens = {"middle", "left", "right"}
        modifier_tokens = {"ctrl", "shift", "alt", "win"}

        configured_buttons = tokens.intersection(mouse_tokens)
        if configured_buttons != {button_token}:
            return False

        configured_modifiers = tokens.intersection(modifier_tokens)
        active_modifiers = self.get_active_modifier_tokens()
        return configured_modifiers == active_modifiers


    def hotkey_matches(self, hotkey: str, extra_token: str = "", exact: bool = False) -> bool:
        tokens = self.normalize_hotkey_tokens(hotkey)
        if not tokens:
            return False

        pressed = set(self.global_pressed_keys)
        if extra_token:
            pressed.add(extra_token)

        return tokens == pressed if exact else tokens.issubset(pressed)

    def start_global_hotkey_listener(self):
        if pynput_keyboard is None or pynput_mouse is None:
            self.set_status("전역 단축키는 pynput 설치 후 사용 가능합니다.")
            return
        try:
            mouse_tokens = {"middle", "left", "right"}

            def maybe_handle_keyboard_hotkeys():
                if self.hotkey_capture_active:
                    return
                screenshot_hotkey = str(self.config.get("screenshot_hotkey", "ctrl+shift+s"))
                mode_hotkey = str(self.config.get("mode_toggle_hotkey", "middle"))
                pause_hotkey = str(self.config.get("pause_toggle_hotkey", "ctrl+middle"))
                show_window_hotkey = str(self.config.get("show_window_hotkey", "shift+middle"))

                if self.normalize_hotkey_tokens(screenshot_hotkey) and not self.normalize_hotkey_tokens(screenshot_hotkey).intersection(mouse_tokens):
                    if self.hotkey_matches(screenshot_hotkey):
                        self.root.after(0, self.launch_screenshot_tool)
                if self.normalize_hotkey_tokens(mode_hotkey) and not self.normalize_hotkey_tokens(mode_hotkey).intersection(mouse_tokens):
                    if self.hotkey_matches(mode_hotkey):
                        self.root.after(0, self.toggle_capture_mode_global)
                if self.normalize_hotkey_tokens(pause_hotkey) and not self.normalize_hotkey_tokens(pause_hotkey).intersection(mouse_tokens):
                    if self.hotkey_matches(pause_hotkey):
                        self.root.after(0, self.toggle_pause)
                if self.normalize_hotkey_tokens(show_window_hotkey) and not self.normalize_hotkey_tokens(show_window_hotkey).intersection(mouse_tokens):
                    if self.hotkey_matches(show_window_hotkey):
                        self.root.after(0, self.toggle_main_window)

            def on_press(key):
                token = self.key_to_token(key)
                if not token:
                    return
                self.global_pressed_keys.add(token)
                maybe_handle_keyboard_hotkeys()

            def on_release(key):
                token = self.key_to_token(key)
                if token:
                    self.global_pressed_keys.discard(token)

            def on_click(x, y, button, pressed):
                if not pressed or self.hotkey_capture_active:
                    return
                button_token = self.mouse_button_to_token(button)
                if not button_token:
                    return
                # 보조키가 포함된 더 구체적인 조합을 먼저 검사한다.
                if self.mouse_hotkey_matches(
                    str(self.config.get("pause_toggle_hotkey", "ctrl+middle")),
                    button_token,
                ):
                    self.root.after(0, self.toggle_pause)
                    return

                if self.mouse_hotkey_matches(
                    str(self.config.get("show_window_hotkey", "shift+middle")),
                    button_token,
                ):
                    self.root.after(0, self.toggle_main_window)
                    return

                if self.mouse_hotkey_matches(
                    str(self.config.get("mode_toggle_hotkey", "middle")),
                    button_token,
                ):
                    self.root.after(0, self.toggle_capture_mode_global)
                    return

                if self.mouse_hotkey_matches(
                    str(self.config.get("screenshot_hotkey", "ctrl+shift+s")),
                    button_token,
                ):
                    self.root.after(0, self.launch_screenshot_tool)
                    return

                if button_token == "middle":
                    append_event(
                        self.paths["events"],
                        {
                            "type": "middle_click_unmatched",
                            "active_modifiers": sorted(self.get_active_modifier_tokens()),
                            "mode_hotkey": str(self.config.get("mode_toggle_hotkey", "middle")),
                            "pause_hotkey": str(self.config.get("pause_toggle_hotkey", "ctrl+middle")),
                            "show_window_hotkey": str(self.config.get("show_window_hotkey", "shift+middle")),
                        },
                    )

            self.global_keyboard_listener = pynput_keyboard.Listener(on_press=on_press, on_release=on_release)
            self.global_mouse_listener = pynput_mouse.Listener(on_click=on_click)
            self.global_keyboard_listener.daemon = True
            self.global_mouse_listener.daemon = True
            self.global_keyboard_listener.start()
            self.global_mouse_listener.start()
        except Exception as e:
            append_event(self.paths["events"], {"type": "global_hotkey_failed", "error": str(e)})

    def launch_screenshot_tool(self):
        now = time.perf_counter()
        if now - self.last_screenshot_hotkey_at < 0.8:
            return
        self.last_screenshot_hotkey_at = now
        try:
            self.set_status("Windows 캡처 도구를 실행합니다. 영역을 선택하면 자동 저장됩니다.")
            if bool(self.config.get("hide_app_during_screenshot", True)):
                try:
                    self.root.iconify()
                except Exception:
                    pass
                try:
                    if self.mini_status_window is not None and self.mini_status_window.winfo_exists():
                        self.mini_status_window.withdraw()
                except Exception:
                    pass
                self.root.after(5000, self.restore_mini_after_screenshot)

            def start_screenclip():
                try:
                    if sys.platform.startswith("win"):
                        try:
                            os.startfile("ms-screenclip:")
                        except Exception:
                            popen_hidden_command(["explorer.exe", "ms-screenclip:"])
                except Exception as e:
                    self.set_status(f"캡처 도구 실행 실패: {e}")

            self.root.after(250, start_screenclip)
        except Exception as e:
            self.set_status(f"캡처 도구 실행 실패: {e}")

    def toggle_pause(self):
        self.paused = not self.paused
        self.processing_state = "일시정지" if self.paused else "대기"
        self.set_status("감지를 일시정지했습니다." if self.paused else "감지를 다시 시작했습니다.")
        self.update_counter()

    def create_mini_status_window(self):
        if not bool(self.config.get("mini_status_enabled", True)):
            return
        try:
            self.mini_status_window = tk.Toplevel(self.root)
            self.mini_status_window.title("ClassFlowAI Mini")
            self.mini_status_window.overrideredirect(True)
            if bool(self.config.get("mini_status_topmost", True)):
                self.mini_status_window.attributes("-topmost", True)
            try:
                self.mini_status_window.attributes("-toolwindow", True)
            except Exception:
                pass
            x = int(self.config.get("mini_status_x", 20))
            y = int(self.config.get("mini_status_y", 20))
            w = int(self.config.get("mini_status_width", 56))
            h = int(self.config.get("mini_status_height", 56))
            self.mini_status_window.geometry(f"{w}x{h}+{x}+{y}")

            self.mini_frame = tk.Frame(
                self.mini_status_window,
                bd=1,
                relief="solid",
                bg="#7c2d12",
                cursor="fleur",
            )
            self.mini_frame.pack(fill="both", expand=True)

            self.mini_mode_var = tk.StringVar(value="CAP")
            self.mini_state_var = tk.StringVar(value="WAIT")

            self.mini_mode_label = tk.Label(
                self.mini_frame,
                textvariable=self.mini_mode_var,
                font=("맑은 고딕", 10, "bold"),
                bg="#7c2d12",
                fg="#ffffff",
                cursor="fleur",
            )
            self.mini_mode_label.pack(
                fill="x",
                padx=2,
                pady=(7, 0),
            )

            self.mini_state_label = tk.Label(
                self.mini_frame,
                textvariable=self.mini_state_var,
                font=("맑은 고딕", 7, "bold"),
                bg="#7c2d12",
                fg="#ffffff",
                cursor="fleur",
            )
            self.mini_state_label.pack(
                fill="x",
                padx=2,
                pady=(1, 5),
            )

            bind_mini_widget_events(
                [
                    self.mini_status_window,
                    self.mini_frame,
                    self.mini_mode_label,
                    self.mini_state_label,
                ],
                self.start_mini_drag,
                self.drag_mini_status,
                self.end_mini_drag,
                self.restore_main_window,
                self.show_mini_context_menu,
            )
            self.update_mini_status()
        except Exception as e:
            append_event(self.paths["events"], {"type": "mini_create_failed", "error": str(e)})

    def update_mini_status(self):
        if not hasattr(self, "mini_state_var"):
            return

        state_text = str(
            self.processing_state
            or ""
        )

        if self.paused:
            state = "멈춤"
            bg = "#6b7280"
        elif "실패" in state_text:
            state = "실패"
            bg = "#b91c1c"
        elif "중" in state_text:
            state = "진행"
            bg = "#d97706"
        elif (
            "완료" in state_text
            or state_text == "캡처 완료"
        ):
            state = "완료"
            bg = "#15803d"
        else:
            state = "대기"
            bg = (
                "#1d4ed8"
                if self.capture_mode == "ocr"
                else "#9a3412"
            )

        self.mini_mode_var.set("OCR" if self.capture_mode == "ocr" else "CAP")
        self.mini_state_var.set(state)

        try:
            for widget in [
                self.mini_frame,
                self.mini_mode_label,
                self.mini_state_label,
            ]:
                widget.config(bg=bg)
        except Exception:
            pass

    def show_mini_context_menu(self, event):
        try:
            menu = tk.Menu(self.mini_status_window, tearoff=0)
            self.mini_context_menu = menu
            menu.add_command(label="메인 창 열기", command=self.restore_main_window)
            menu.add_command(
                label="감지 다시 시작" if self.paused else "감지 일시정지",
                command=self.toggle_pause,
            )
            menu.add_separator()
            menu.add_command(label="프로그램 종료", command=self.request_app_exit)
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass
        return "break"

    def request_app_exit(self) -> bool:
        return confirm_application_exit(
            lambda title, message: messagebox.askyesno(title, message, parent=self.mini_status_window),
            self.on_close,
        )


    def start_mini_drag(self, event):
        try:
            self.mini_status_window.update_idletasks()
            self.mini_drag_start_pointer = (
                self.mini_status_window.winfo_pointerx(),
                self.mini_status_window.winfo_pointery(),
            )
            self.mini_drag_start_window = (
                self.mini_status_window.winfo_x(),
                self.mini_status_window.winfo_y(),
            )
            self.mini_status_window.grab_set()
        except Exception:
            self.mini_drag_start_pointer = (
                event.x_root,
                event.y_root,
            )
            self.mini_drag_start_window = (
                self.mini_status_window.winfo_x(),
                self.mini_status_window.winfo_y(),
            )


    def drag_mini_status(self, event):
        try:
            pointer_x = self.mini_status_window.winfo_pointerx()
            pointer_y = self.mini_status_window.winfo_pointery()

            start_pointer_x, start_pointer_y = (
                self.mini_drag_start_pointer
            )
            start_window_x, start_window_y = (
                self.mini_drag_start_window
            )

            x = start_window_x + (
                pointer_x - start_pointer_x
            )
            y = start_window_y + (
                pointer_y - start_pointer_y
            )

            width = int(
                self.config.get(
                    "mini_status_width",
                    56,
                )
            )
            height = int(
                self.config.get(
                    "mini_status_height",
                    56,
                )
            )

            screen_width = (
                self.mini_status_window.winfo_screenwidth()
            )
            screen_height = (
                self.mini_status_window.winfo_screenheight()
            )

            x = max(
                0,
                min(
                    x,
                    max(
                        0,
                        screen_width - width,
                    ),
                ),
            )
            y = max(
                0,
                min(
                    y,
                    max(
                        0,
                        screen_height - height,
                    ),
                ),
            )

            self.mini_status_window.geometry(
                f"{width}x{height}+{x}+{y}"
            )
        except Exception:
            pass


    def end_mini_drag(self, event=None):
        try:
            self.mini_status_window.grab_release()
        except Exception:
            pass

        try:
            new_config = dict(self.config)
            new_config["mini_status_x"] = int(
                self.mini_status_window.winfo_x()
            )
            new_config["mini_status_y"] = int(
                self.mini_status_window.winfo_y()
            )
            save_config(new_config)
            self.config = load_config()
        except Exception:
            pass


    def is_main_window_foreground(self) -> bool:
        try:
            if self.root.state() != "normal":
                return False
        except Exception:
            return False

        if sys.platform.startswith("win"):
            try:
                foreground = int(
                    ctypes.windll.user32.GetForegroundWindow()
                )
                root_handle = int(
                    self.root.winfo_id()
                )
                return (
                    foreground != 0
                    and foreground == root_handle
                )
            except Exception:
                pass

        try:
            return self.root.focus_displayof() is not None
        except Exception:
            return False


    def toggle_main_window(self, event=None):
        """
        전역 단축키로 메인 창을 표시하거나 최소화합니다.

        - 메인 창이 이미 앞에 있으면 최소화
        - 최소화되었거나 다른 창 뒤에 있으면 앞으로 표시
        """
        try:
            self.root.update_idletasks()

            if self.is_main_window_foreground():
                self.root.iconify()
                self.set_status(
                    "ClassFlowAI 창을 최소화했습니다."
                )
            else:
                self.restore_main_window()

            self.update_mini_status()
        except Exception:
            self.restore_main_window()

        return "break"


    def restore_main_window(self, event=None):
        """
        최소화되었거나 다른 창 뒤에 있는 메인 창을 앞으로 가져옵니다.
        미니 상태창 더블클릭과 창 토글의 표시 동작에서 사용합니다.
        """
        try:
            self.root.deiconify()
            try:
                self.root.state("normal")
            except Exception:
                pass

            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.focus_force()

            def release_topmost():
                try:
                    self.root.attributes("-topmost", False)
                    self.root.lift()
                except Exception:
                    pass

            self.root.after(350, release_topmost)

            if (
                self.mini_status_window is not None
                and self.mini_status_window.winfo_exists()
            ):
                self.mini_status_window.deiconify()

            self.set_status("ClassFlowAI 창을 앞으로 표시했습니다.")
        except Exception:
            pass
        return "break"

    def restore_mini_after_screenshot(self):
        try:
            if self.mini_status_window is not None and self.mini_status_window.winfo_exists():
                self.mini_status_window.deiconify()
                if bool(self.config.get("mini_status_topmost", True)):
                    self.mini_status_window.attributes("-topmost", True)
        except Exception:
            pass

    def on_close(self):
        if self.closing:
            return
        self.closing = True
        self.running = False
        try:
            self.flow_interpretation_queue.put_nowait(None)
        except Exception:
            pass
        self.stop_execution_timer(save_result=False)
        try:
            if self.global_keyboard_listener:
                self.global_keyboard_listener.stop()
            if self.global_mouse_listener:
                self.global_mouse_listener.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


def _write_startup_ready_flag() -> Path:
    ready_path = Path(__file__).resolve().parent / "APP_STARTED.flag"
    try:
        ready_path.write_text(
            time.strftime("%Y-%m-%d %H:%M:%S"),
            encoding="utf-8",
        )
    except Exception:
        pass
    return ready_path


def _write_startup_error(detail: str) -> Path:
    error_path = Path(__file__).resolve().parent / "STARTUP_ERROR.log"
    try:
        error_path.write_text(detail, encoding="utf-8")
    except Exception:
        pass
    return error_path


def main():
    root = None
    try:
        root = tk.Tk()
        ClassFlowAIApp(root)
        _write_startup_ready_flag()
        root.mainloop()
    except Exception:
        detail = traceback.format_exc()
        error_path = _write_startup_error(detail)

        try:
            if root is None:
                root = tk.Tk()
                root.withdraw()
            messagebox.showerror(
                "ClassFlowAI 실행 오류",
                "프로그램 시작 중 오류가 발생했습니다.\n\n"
                f"오류 기록: {error_path}\n\n"
                f"{detail[-1600:]}",
            )
        except Exception:
            pass

        print(detail, file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
