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

from modules.clipboard_watcher import get_clipboard_image, image_hash, save_image
from modules.chatgpt_handoff_exporter import (
    DEFAULT_PROMPT_TEMPLATE,
    build_capture_timeline_markdown,
    export_chatgpt_handoff_zip,
    export_preview_html,
)
from modules.storage import (
    LESSONS_DIR_NAME,
    append_event,
    create_lesson_workspace,
    ensure_workspace,
    get_current_lesson,
    get_default_workspace,
    is_lesson_workspace,
    set_current_lesson,
    timestamp_file,
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
        "html_flow_subject": "",
        "html_flow_prompt_template": DEFAULT_PROMPT_TEMPLATE,
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
        "cap_reasoning_retry_count": 1,
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

        current_prompt = str(
            user_config.get("html_flow_prompt_template")
            or ""
        )
        if (
            not current_prompt.strip()
            or "LLM 보조 검토 결과" in current_prompt
            or "# 10. 최종 산출물" in current_prompt
        ):
            default_config["html_flow_prompt_template"] = (
                DEFAULT_PROMPT_TEMPLATE
            )
            migrated["html_flow_prompt_template"] = (
                DEFAULT_PROMPT_TEMPLATE
            )

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

    # 이전 설정명 자동 호환
    if not str(default_config.get("html_flow_prompt_template") or "").strip():
        old_prompt = default_config.get("chatgpt_handoff_prompt_template")
        default_config["html_flow_prompt_template"] = old_prompt if old_prompt else DEFAULT_PROMPT_TEMPLATE
    if not str(default_config.get("cap_reasoning_prompt") or "").strip():
        default_config["cap_reasoning_prompt"] = DEFAULT_CAP_PROMPT
    if not str(default_config.get("html_flow_subject") or "").strip():
        old_subject = default_config.get("chatgpt_handoff_subject")
        if old_subject:
            default_config["html_flow_subject"] = old_subject

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
        "html_flow_subject": str(config.get("html_flow_subject", "")),
        "html_flow_prompt_template": str(config.get("html_flow_prompt_template", DEFAULT_PROMPT_TEMPLATE)),
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
        "cap_reasoning_retry_count": int(config.get("cap_reasoning_retry_count", 1) or 1),
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
        self.paused = bool(self.config.get("pause_on_start", False))
        self.capture_mode = str(self.config.get("capture_mode", "capture") or "capture").lower()
        if self.capture_mode not in {"capture", "ocr"}:
            self.capture_mode = "capture"
        self.global_mouse_listener = None
        self.hotkey_capture_active = False
        self.last_mode_toggle_at = 0.0
        self.last_hash = None
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
        tk.Button(bottom, text="GPT ZIP파일 생성", command=self.export_chatgpt_handoff_zip_ui, width=20, height=2).pack(side="left", padx=(0, 6))
        tk.Button(bottom, text="HTML 흐름", command=self.open_html_flow_window, width=16, height=2).pack(side="left", padx=6)
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
        tk.Button(nav, text="전체 초기화", command=self.reset_today).pack(side="right")

        self.preview_label = tk.Label(left, text="아직 캡처가 없습니다.\nCtrl+Shift+S로 캡처하세요.\n휠클릭으로 OCR / CAP 모드를 전환할 수 있습니다.", bg="#f4f4f4")
        self.preview_label.pack(fill="both", expand=True)

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
        result_actions.pack(fill="x", pady=(8, 0))

        self.ocr_refine_button = tk.Button(
            result_actions,
            text="OCR 보정 후 복사",
            command=self.refine_current_ocr_and_copy,
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

            if process_mode == "ocr_interpret":
                key = "ocr_interpretation_elapsed_sec"
                target_record["last_process_type"] = "ocr_interpret"
            elif process_mode == "ocr_refine":
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
            if self.execution_mode == "ocr_interpret":
                mode_name = "OCR 해석"
            elif self.execution_mode == "ocr_refine":
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

        if last_process == "ocr_interpret" and record.get("ocr_interpretation_elapsed_sec") is not None:
            elapsed = record.get("ocr_interpretation_elapsed_sec")
            mode_name = "OCR 해석"
        elif last_process == "ocr_refine" and record.get("ocr_correction_elapsed_sec") is not None:
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
        refine_state = "disabled"
        refine_text = "OCR 보정 후 복사"
        second_state = "disabled"
        second_text = "CAP 해석 복사"
        second_command = self.copy_current_cap_result

        if record is not None:
            mode = str(
                record.get("mode")
                or "capture"
            ).lower()
            status = str(
                record.get("status")
                or ""
            )
            ocr_text = str(
                record.get("ocr_text")
                or ""
            ).strip()
            corrected_text = str(
                record.get("ocr_corrected_text")
                or ""
            ).strip()
            cap_text = str(
                record.get("cap_text")
                or ""
            ).strip()

            if status == "ocr_correction_running":
                refine_text = "OCR 보정 중…"
            elif (
                mode == "ocr"
                and ocr_text
                and not ocr_text.lstrip().startswith("## OCR 실패")
                and status != "ocr_interpretation_running"
            ):
                refine_state = "normal"

            if mode == "ocr":
                second_command = self.interpret_corrected_ocr

                if status == "ocr_interpretation_running":
                    second_text = "OCR 내용 해석 중…"
                elif corrected_text:
                    second_text = "OCR 내용 해석"
                    second_state = "normal"
                else:
                    second_text = "OCR 보정 후 해석"
            else:
                second_command = self.copy_current_cap_result

                if status == "cap_running":
                    second_text = "CAP 분석 중…"
                elif (
                    cap_text
                    and not cap_text.startswith("CAP 분석 실패")
                ):
                    second_text = "CAP 해석 복사"
                    second_state = "normal"

        try:
            self.ocr_refine_button.config(
                state=refine_state,
                text=refine_text,
            )
            self.cap_copy_button.config(
                state=second_state,
                text=second_text,
                command=second_command,
            )
        except Exception:
            pass



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



    def interpret_corrected_ocr(self):
        record = self.get_current_record()
        if record is None:
            self.set_status(
                "해석할 OCR 결과가 없습니다."
            )
            return

        if str(
            record.get("mode")
            or ""
        ).lower() != "ocr":
            self.set_status(
                "OCR 캡처를 선택하세요."
            )
            return

        corrected_text = str(
            record.get("ocr_corrected_text")
            or ""
        ).strip()

        if not corrected_text:
            self.set_status(
                "먼저 [OCR 보정 후 복사]를 실행하세요."
            )
            return

        image_path = Path(
            str(
                record.get("image_path")
                or ""
            )
        )
        if not image_path.exists():
            messagebox.showwarning(
                "OCR 내용 해석 불가",
                f"원본 이미지를 찾을 수 없습니다.\n\n{image_path}",
            )
            return

        if not self.has_nvidia_api_key():
            messagebox.showwarning(
                "OCR 내용 해석 불가",
                "NVIDIA API 키가 없습니다.\n\n설정에서 API 키를 입력하세요.",
            )
            return

        base_prompt = str(
            self.config.get("cap_reasoning_prompt")
            or DEFAULT_CAP_PROMPT
        ).strip()

        interpret_prompt = (
            base_prompt
            + "\n\n추가 지시:\n"
            + "- 아래 텍스트는 원본 이미지를 기준으로 보정된 OCR 결과입니다.\n"
            + "- 원본 이미지와 보정 OCR을 함께 참고하여 내용의 의미, 구조와 흐름을 설명하세요.\n"
            + "- OCR 전사본을 그대로 반복하지 말고, 학습자가 이해할 수 있게 핵심을 정리하세요.\n"
            + "- 이미지와 텍스트가 충돌하면 이미지를 우선하세요.\n"
            + "- 보이지 않는 내용은 추측하지 마세요.\n"
            + "- 최종 결과만 한국어 Markdown으로 반환하세요.\n\n"
            + "--- 보정 OCR 시작 ---\n"
            + corrected_text[:16000]
            + "\n--- 보정 OCR 끝 ---\n"
        )

        inference_config = dict(
            self.config
        )
        inference_config[
            "cap_reasoning_prompt"
        ] = interpret_prompt

        record["status"] = "ocr_interpretation_running"
        self.processing_state = "OCR 내용 해석 중"
        self.start_execution_timer(
            record,
            "ocr_interpret",
        )
        self.save_records()
        self.update_result_action_buttons()
        self.update_ocr_panel()
        self.update_mini_status()
        self.update_counter()
        self.set_status(
            "보정 OCR과 원본 이미지를 함께 해석 중입니다..."
        )

        def worker():
            try:
                result = analyze_capture_image(
                    image_path,
                    inference_config,
                )
            except Exception as exc:
                result = (
                    "OCR 내용 해석 실패\n\n"
                    + str(exc)
                )

            result = str(
                result
                or ""
            )
            if result.startswith("CAP 분석 실패"):
                result = result.replace(
                    "CAP 분석 실패",
                    "OCR 내용 해석 실패",
                    1,
                )

            self.root.after(
                0,
                lambda: self._after_ocr_interpretation(
                    record,
                    result,
                ),
            )

        threading.Thread(
            target=worker,
            daemon=True,
        ).start()


    def _after_ocr_interpretation(
        self,
        record: dict,
        result_text: str,
    ):
        result_text = str(
            result_text
            or ""
        ).strip()
        failed = result_text.startswith(
            "OCR 내용 해석 실패"
        )
        elapsed = self.stop_execution_timer(
            record,
            save_result=True,
        )

        if failed:
            record["status"] = "ocr_corrected"
            record[
                "ocr_interpretation_error"
            ] = result_text
            record["display_result_type"] = "ocr_corrected"
            self.processing_state = "OCR 내용 해석 실패"
        else:
            record[
                "ocr_interpretation_text"
            ] = result_text
            record[
                "ocr_interpretation_model"
            ] = str(
                self.config.get(
                    "cap_reasoning_model"
                )
                or "qwen/qwen3.5-397b-a17b"
            )
            record[
                "ocr_interpretation_at"
            ] = time.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            record["status"] = "ocr_interpretation_done"
            record["display_result_type"] = "ocr_interpretation"
            record.pop(
                "ocr_interpretation_error",
                None,
            )
            self.processing_state = "OCR 내용 해석 완료"

        self.save_records()
        self.rebuild_outputs_from_records()
        self.refresh_current_preview()
        self.update_mini_status()
        self.update_counter()
        self.update_result_action_buttons()

        append_event(
            self.paths["events"],
            {
                "type": "ocr_interpretation_done",
                "path": str(
                    record.get("image_path")
                    or ""
                ),
                "failed": failed,
                "model": str(
                    self.config.get(
                        "cap_reasoning_model"
                    )
                    or ""
                ),
                "elapsed_sec": round(
                    elapsed,
                    3,
                ),
            },
        )

        elapsed_text = self.format_execution_seconds(
            elapsed
        )

        if failed:
            self.set_status(
                f"OCR 내용 해석 실패 ({elapsed_text}). "
                "보정 OCR 결과는 유지했습니다."
            )
            messagebox.showerror(
                "OCR 내용 해석 실패",
                result_text,
            )
            return

        copied = self.copy_text_to_clipboard(
            result_text
        )
        self.set_status(
            f"OCR 내용 해석 완료 ({elapsed_text}) + 클립보드 복사 완료"
            if copied
            else f"OCR 내용 해석 완료 ({elapsed_text}), 복사 실패"
        )


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
            for key in [
                "ocr_interpretation_text",
                "ocr_interpretation_model",
                "ocr_interpretation_at",
                "ocr_interpretation_error",
                "ocr_interpretation_elapsed_sec",
            ]:
                record.pop(key, None)

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
        return f"현재 수업: {lesson_name} | 저장 위치: {self.workspace}"

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

        lessons_root = self.storage_root / LESSONS_DIR_NAME
        initial_dir = lessons_root if lessons_root.exists() else self.storage_root
        selected = filedialog.askdirectory(
            title="이전 수업 폴더 선택",
            initialdir=str(initial_dir),
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
            except Exception:
                self.capture_records = []
        active = self.active_record_indices()
        self.current_record_index = active[-1] if active else -1

    def save_records(self):
        records_path = self.paths.get("records")
        if records_path:
            records_path.parent.mkdir(parents=True, exist_ok=True)
            records_path.write_text(json.dumps(self.capture_records, ensure_ascii=False, indent=2), encoding="utf-8")

    def active_record_indices(self):
        return [i for i, r in enumerate(self.capture_records) if not r.get("deleted") and Path(str(r.get("image_path") or "")).exists()]

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
        self.update_mini_status()

    def add_capture_record(self, image_path: Path) -> dict:
        record = {
            "record_id": image_path.stem,
            "image_path": str(image_path),
            "status": "captured",
            "mode": self.capture_mode,
            "deleted": False,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.capture_records.append(record)
        self.current_record_index = len(self.capture_records) - 1
        self.save_records()
        return record

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

    def rebuild_outputs_from_records(self):
        self.save_records()
        try:
            active = [r for r in self.capture_records if not r.get("deleted") and Path(str(r.get("image_path") or "")).exists()]
            timeline_path = self.paths["outputs"] / "CAPTURE_TIMELINE.md"
            timeline_path.parent.mkdir(parents=True, exist_ok=True)
            timeline_path.write_text(build_capture_timeline_markdown(active, image_dir_name="../captures"), encoding="utf-8")
            export_preview_html(active, self.paths["notion_preview_html"])
        except Exception as e:
            append_event(self.paths["events"], {"type": "timeline_write_failed", "error": str(e)})

    def initialize_clipboard_baseline(self):
        if not bool(self.config.get("ignore_existing_clipboard_on_start", True)):
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
        if not messagebox.askyesno("현재 캡처 삭제", "현재 기록을 목록에서 제외할까요?\n원본 이미지 파일은 삭제하지 않습니다."):
            return
        record["deleted"] = True
        self.save_records()
        active = self.active_record_indices()
        self.current_record_index = active[-1] if active else -1
        self.rebuild_outputs_from_records()
        self.refresh_current_preview()
        self.set_status("현재 캡처 기록을 제외했습니다.")

    def reset_today(self):
        if not messagebox.askyesno("기록 초기화", "현재 목록을 초기화할까요?\n원본 캡처 이미지는 삭제하지 않습니다."):
            return
        self.capture_records = []
        self.current_record_index = -1
        self.save_records()
        self.rebuild_outputs_from_records()
        self.refresh_current_preview()
        self.set_status("목록을 초기화했습니다. 원본 이미지는 유지됩니다.")

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
                ocr_text = extract_text_from_image(image_path, self.config)
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

        if auto_copy and bool(self.config.get("copy_ocr_to_clipboard_on_done", True)):
            copied = self.copy_text_to_clipboard(cleaned_text)
            elapsed_text = self.format_execution_seconds(elapsed)
            self.set_status(
                f"OCR 완료 ({elapsed_text}) + 클립보드 복사 완료"
                if copied
                else f"OCR 완료 ({elapsed_text}), 복사 실패"
            )


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
                result_text = analyze_capture_image(image_path, self.config)
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

    def build_ocr_timeline_markdown(self, records: list[dict]) -> str:
        lines = [
            "# OCR_AND_CAP_TIMELINE",
            "",
            "이미지 원본이 최우선이며 아래 결과는 보조 자료입니다.",
            "",
            "---",
            "",
        ]
        has_any = False

        for idx, record in enumerate(records, 1):
            ocr_text = str(record.get("ocr_corrected_text") or record.get("ocr_text") or "").strip()
            cap_text = str(record.get("cap_text") or "").strip()
            if not ocr_text and not cap_text:
                continue

            has_any = True
            image_path = Path(str(record.get("image_path") or ""))
            mode = str(record.get("mode") or "capture").lower()

            lines += [
                f"## {idx}. {image_path.name}",
                "",
                f"- 모드: {'OCR' if mode == 'ocr' else 'CAP'}",
                f"- 이미지 파일: images/{image_path.name}",
                "",
            ]

            if ocr_text:
                lines += [
                    "### OCR 결과",
                    "",
                    "```text",
                    ocr_text,
                    "```",
                    "",
                ]

            if cap_text:
                lines += [
                    "### CAP 이미지 추론 결과",
                    "",
                    cap_text,
                    "",
                ]

            lines += ["---", ""]

        if not has_any:
            return "# OCR_AND_CAP_TIMELINE\n\n결과 없음\n"

        return "\n".join(lines)


    def write_ocr_timeline(self, records: list[dict]) -> None:
        try:
            path = self.paths["outputs"] / "OCR_TIMELINE.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.build_ocr_timeline_markdown(records), encoding="utf-8")
        except Exception as e:
            append_event(self.paths["events"], {"type": "ocr_timeline_write_failed", "error": str(e)})

    def run_nvidia_ocr_for_zip(self, active: list[dict]) -> None:
        """
        // 1. 설정의 NVIDIA API 키 확인하기
        // 2. OCR 결과가 없는 캡처만 API로 변환하기
        // 3. OCR_TIMELINE.md를 GPT ZIP에 들어갈 보조 자료로 준비하기
        """
        if not self.has_nvidia_api_key():
            return

        # 현재 OCR 모드이면 전체 캡처를 OCR 대상으로 보고, CAP 모드이면 OCR 모드에서 저장된 캡처만 처리합니다.
        if self.capture_mode == "ocr":
            ocr_candidates = active
        else:
            ocr_candidates = [r for r in active if str(r.get("mode") or "capture").lower() == "ocr"]

        if not ocr_candidates:
            return

        targets = [
            r for r in ocr_candidates
            if (not str(r.get("ocr_text") or "").strip())
            or str(r.get("ocr_text") or "").lstrip().startswith("## OCR 실패")
        ]
        if not targets:
            self.write_ocr_timeline(active)
            return

        total = len(targets)
        for idx, record in enumerate(targets, 1):
            image_path = Path(str(record.get("image_path") or ""))
            if not image_path.exists():
                continue
            self.processing_state = "OCR 처리 중"
            self.update_mini_status()
            self.set_status(f"OCR 처리 중: {idx}/{total} {image_path.name}")
            self.root.update_idletasks()
            ocr_text = extract_text_from_image(image_path, self.config)
            record["ocr_text"] = ocr_text
            record["ocr_provider"] = str(self.config.get("nvidia_ocr_model") or "nvidia/nemotron-ocr-v2")
            record["ocr_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            append_event(self.paths["events"], {"type": "nvidia_ocr_done", "path": str(image_path), "failed": ocr_text.lstrip().startswith("## OCR 실패")})

        self.save_records()
        self.write_ocr_timeline(active)
        self.processing_state = "OCR 완료"
        self.update_mini_status()

    def export_chatgpt_handoff_zip_ui(self):
        self._ensure_records_for_capture_files()
        self.rebuild_outputs_from_records()

        active = [r for r in self.capture_records if not r.get("deleted") and Path(str(r.get("image_path") or "")).exists()]
        if not active:
            messagebox.showwarning("GPT ZIP파일 생성 불가", "전달할 캡처 파일이 없습니다. 먼저 캡처를 추가해 주세요.")
            return

        try:
            self.run_nvidia_ocr_for_zip(active)
            self.rebuild_outputs_from_records()
            out_dir = self.paths["gpt_handoff"]
            zip_path, prompt_text = export_chatgpt_handoff_zip(
                active,
                out_dir,
                subject=str(self.config.get("html_flow_subject", "")),
                prompt_template=str(self.config.get("html_flow_prompt_template", "")),
            )
            self.copy_text_to_clipboard(prompt_text)
            self.processing_state = "대기"
            self.update_mini_status()
            self.set_status(f"GPT ZIP파일 생성 완료: {zip_path.name}")
            messagebox.showinfo(
                "GPT ZIP파일 생성 완료",
                "ZIP은 현재 캡처 파일 기준으로 생성되었습니다.\n"
                "OCR 모드와 NVIDIA API 키가 있으면 OCR_TIMELINE도 함께 포함됩니다.\n"
                "프롬프트는 클립보드에 복사되었습니다.\n\n"
                "1. 열린 폴더의 ZIP 파일을 ChatGPT에 업로드\n"
                "2. 입력창에 Ctrl+V\n\n"
                f"{zip_path}",
            )
            if sys.platform.startswith("win"):
                os.startfile(out_dir)
        except Exception as e:
            messagebox.showerror("GPT ZIP파일 생성 실패", f"ZIP 생성 중 오류가 발생했습니다.\n\n{e}")

    def open_html_flow_window(self):
        """
        // 1. 현재 캡처 목록을 기준으로 HTML 흐름 파일 생성하기
        // 2. 브라우저에서 HTML 양식/캡처 순서 확인하기
        // 3. GPT ZIP 생성 전 흐름을 눈으로 확인할 수 있게 하기
        """
        self._ensure_records_for_capture_files()
        self.rebuild_outputs_from_records()

        preview_path = self.paths.get("notion_preview_html")
        if preview_path is None:
            messagebox.showerror("HTML 흐름 오류", "HTML 흐름 파일 경로를 찾을 수 없습니다.")
            return

        try:
            active = [
                r for r in self.capture_records
                if not r.get("deleted") and Path(str(r.get("image_path") or "")).exists()
            ]
            export_preview_html(active, preview_path)
            self.set_status(f"HTML 흐름 열기: {preview_path.name}")
            if sys.platform.startswith("win"):
                os.startfile(preview_path)
            else:
                import webbrowser
                webbrowser.open(preview_path.as_uri())
        except Exception as e:
            messagebox.showerror("HTML 흐름 오류", f"HTML 흐름을 여는 중 오류가 발생했습니다.\n\n{e}")

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
        gpt_prompt_tab = tk.Frame(
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
        notebook.add(
            gpt_prompt_tab,
            text="GPT ZIP 프롬프트",
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

        # ------------------------------------------------------
        # GPT prompt only
        # ------------------------------------------------------
        section(
            gpt_prompt_tab,
            "GPT ZIP 정리 프롬프트",
        )
        gpt_prompt_editor = ScrolledText(
            gpt_prompt_tab,
            wrap="word",
            font=("맑은 고딕", 10),
            undo=True,
        )
        gpt_prompt_editor.pack(
            fill="both",
            expand=True,
            pady=(0, 8),
        )
        set_prompt_text(
            gpt_prompt_editor,
            str(
                self.config.get("html_flow_prompt_template")
                or DEFAULT_PROMPT_TEMPLATE
            ),
        )

        gpt_buttons = tk.Frame(
            gpt_prompt_tab
        )
        gpt_buttons.pack(fill="x")

        tk.Button(
            gpt_buttons,
            text="기본값 복원",
            command=lambda: set_prompt_text(
                gpt_prompt_editor,
                DEFAULT_PROMPT_TEMPLATE,
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
                gpt_prompt = gpt_prompt_editor.get(
                    "1.0",
                    "end-1c",
                ).strip()

                if not cap_prompt:
                    cap_prompt = DEFAULT_CAP_PROMPT
                if not gpt_prompt:
                    gpt_prompt = DEFAULT_PROMPT_TEMPLATE

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
                        "html_flow_prompt_template": gpt_prompt,

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
                        "cap_reasoning_retry_count": 1,
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

            for widget in [
                self.mini_status_window,
                self.mini_frame,
                self.mini_mode_label,
                self.mini_state_label,
            ]:
                widget.bind(
                    "<ButtonPress-1>",
                    self.start_mini_drag,
                )
                widget.bind(
                    "<B1-Motion>",
                    self.drag_mini_status,
                )
                widget.bind(
                    "<ButtonRelease-1>",
                    self.end_mini_drag,
                )
                widget.bind(
                    "<Double-Button-1>",
                    self.restore_main_window,
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

        self.mini_mode_var.set(
            "OCR"
            if self.capture_mode == "ocr"
            else "CAP"
        )
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
        self.running = False
        self.stop_execution_timer(save_result=False)
        try:
            if self.global_keyboard_listener:
                self.global_keyboard_listener.stop()
            if self.global_mouse_listener:
                self.global_mouse_listener.stop()
        except Exception:
            pass
        self.root.destroy()


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
