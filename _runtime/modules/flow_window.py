import html
import json
import re
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from PIL import Image, ImageTk

def _plain(value: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", value or "")
    text = re.sub(r"(?i)</(?:p|div|li|h[1-6])>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _section_copy_context(section: dict) -> dict:
    image_paths = []
    text_parts = []
    for item in section.get("items", []):
        item_type = item.get("type")
        if item_type == "capture" and len(image_paths) < 2:
            image_paths.append(Path(str(item.get("imageSrc") or "")))
        elif item_type in {"explanation", "note"}:
            value = _plain(str(item.get("html") or ""))
            if value:
                text_parts.append(value)
        elif item_type == "code":
            code = str(item.get("code") or "").strip()
            if code:
                language = str(item.get("language") or "").strip()
                text_parts.append(f"```{language}\n{code}\n```")
    return {
        "section_id": str(section.get("id") or ""),
        "image_paths": image_paths,
        "text": "\n\n".join(text_parts).strip(),
    }


def _consume_wheel_delta(delta: int, remainder: int = 0) -> tuple[int, int]:
    """Convert Windows wheel/touchpad deltas into stable scroll notches."""
    total = int(delta or 0) + int(remainder or 0)
    steps = int(total / 120)
    return steps, total - (steps * 120)


def _preview_size(width: int, height: int, max_width: int = 760, max_height: int = 460) -> tuple[int, int]:
    width = max(1, int(width or 1))
    height = max(1, int(height or 1))
    scale = min(max_width / width, max_height / height, 1.0)
    return max(1, round(width * scale)), max(1, round(height * scale))


class FlowResultWindow:
    """Read-only lesson flow whose individual image/text blocks are copyable."""

    def __init__(
        self,
        parent,
        workspace,
        document,
        flow_path,
        status_callback=None,
        error_callback=None,
        internal_image_copy_callback=None,
    ):
        self.parent = parent
        self.workspace = Path(workspace)
        self.document = document
        self.flow_path = Path(flow_path)
        self.status_callback = status_callback
        self.error_callback = error_callback
        self.internal_image_copy_callback = internal_image_copy_callback
        self.photos = []
        self._image_previews = []
        self._preview_refresh_job = None
        self._layout_job = None
        self._scrollregion_job = None
        self._wheel_remainder = 0
        self._last_canvas_width = None

        self.window = tk.Toplevel(parent)
        self.window.title("ClassFlowAI 수업 흐름")
        self.window.geometry("1020x760")
        self.window.minsize(820, 620)

        header = tk.Frame(self.window, padx=12, pady=9)
        header.pack(fill="x")
        tk.Label(
            header,
            text="블록을 우클릭하면 원본 이미지 또는 해설 텍스트를 복사할 수 있습니다.",
            fg="#4b5563",
        ).pack(side="left")
        self.copy_status = tk.StringVar(value="")
        tk.Label(header, textvariable=self.copy_status, fg="#2563eb").pack(side="right")

        self.flow_frame = tk.Frame(self.window)
        self.flow_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._build_flow()
        self._flow_mtime_ns = self._current_flow_mtime()
        self.window.after(2500, self._poll_document_updates)

    def _notify(self, message: str) -> None:
        self.copy_status.set(message)
        if self.status_callback:
            self.status_callback(message)

    def _log_error(self, event_type: str, exc: Exception, **values) -> None:
        if not self.error_callback:
            return
        try:
            self.error_callback({"type": event_type, "error": str(exc), **values})
        except Exception:
            pass

    def _copy_text_value(self, value: str) -> bool:
        value = str(value or "")
        if not value.strip():
            return False
        self.window.clipboard_clear()
        self.window.clipboard_append(value)
        self.window.update_idletasks()
        return True

    def copy_text_block(self, value: str) -> None:
        try:
            copied = self._copy_text_value(value)
        except Exception as exc:
            self._log_error("flow_text_copy_failed", exc)
            messagebox.showerror("텍스트 복사 실패", str(exc), parent=self.window)
            return
        self._notify("텍스트를 클립보드에 복사했습니다." if copied else "복사할 텍스트가 없습니다.")

    def _copy_image_path(self, image_path: Path) -> None:
        if not self.internal_image_copy_callback:
            raise RuntimeError("내부 이미지 복사 callback이 연결되지 않았습니다.")
        with Image.open(Path(image_path)) as source:
            image = source.convert("RGB").copy()
        self.internal_image_copy_callback(image)

    def copy_image_block(self, image_path: Path) -> None:
        try:
            self._copy_image_path(image_path)
        except Exception as exc:
            self._log_error("flow_image_copy_failed", exc, path=str(image_path))
            messagebox.showerror(
                "이미지 복사 실패",
                "원본 이미지를 복사하지 못했습니다.\n"
                "파일이 존재하는지 또는 클립보드가 다른 프로그램에서 사용 중인지 확인해 주세요.",
                parent=self.window,
            )
            return
        self._notify("원본 이미지를 클립보드에 복사했습니다.")

    def copy_bundle_text(self, context: dict) -> None:
        try:
            copied = self._copy_text_value(str(context.get("text") or ""))
        except Exception as exc:
            self._log_error(
                "flow_bundle_text_copy_failed",
                exc,
                section_id=str(context.get("section_id") or ""),
            )
            messagebox.showerror("텍스트 복사 실패", str(exc), parent=self.window)
            return
        self._notify("해설 텍스트를 클립보드에 복사했습니다." if copied else "복사할 해설 텍스트가 없습니다.")

    def _bind_context_menu(self, widget, label: str, callback) -> None:
        def show(event):
            menu = tk.Menu(self.window, tearoff=False)
            menu.add_command(label=label, command=callback)
            menu.tk_popup(event.x_root, event.y_root)
            menu.grab_release()
            return "break"

        widget.bind("<Button-3>", show)

    def _bind_bundle_context_menu(self, widget, context: dict, image_path: Path | None = None) -> None:
        def show(event):
            menu = tk.Menu(self.window, tearoff=False)
            selected_image = image_path
            if selected_image is None:
                image_paths = list(context.get("image_paths") or [])
                selected_image = image_paths[0] if image_paths else None
            menu.add_command(
                label="원본 이미지만 복사",
                command=(lambda path=selected_image: self.copy_image_block(path)) if selected_image else None,
                state="normal" if selected_image else "disabled",
            )
            menu.add_command(
                label="텍스트만 복사",
                command=lambda: self.copy_bundle_text(context),
                state="normal" if str(context.get("text") or "").strip() else "disabled",
            )
            menu.tk_popup(event.x_root, event.y_root)
            menu.grab_release()
            return "break"

        widget.bind("<Button-3>", show)

    def _current_flow_mtime(self) -> int:
        try:
            return self.flow_path.stat().st_mtime_ns
        except OSError:
            return 0

    def _poll_document_updates(self) -> None:
        if not self.window.winfo_exists():
            return
        current_mtime = self._current_flow_mtime()
        if current_mtime and current_mtime != self._flow_mtime_ns:
            try:
                scroll_position = 0.0
                try:
                    scroll_position = float(self.flow_canvas.yview()[0])
                except Exception:
                    pass
                document = json.loads(self.flow_path.read_text(encoding="utf-8"))
                self.document = document
                self.photos.clear()
                self._image_previews.clear()
                for child in self.flow_frame.winfo_children():
                    child.destroy()
                self._build_flow()
                self.window.after(60, lambda position=scroll_position: self._restore_scroll(position))
                self._flow_mtime_ns = current_mtime
                self._notify("백그라운드 해석 결과를 수업 흐름에 반영했습니다.")
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                self._log_error("flow_document_refresh_failed", exc, path=str(self.flow_path))
        self.window.after(2500, self._poll_document_updates)

    def _restore_scroll(self, position: float) -> None:
        try:
            self.flow_canvas.yview_moveto(max(0.0, min(float(position), 1.0)))
            self._schedule_preview_refresh(self.flow_canvas, delay=1)
        except tk.TclError:
            pass

    def _schedule_canvas_width(self, canvas, canvas_window, width: int) -> None:
        """Coalesce the many configure events emitted while resizing/moving a window."""
        width = int(width)
        if self._last_canvas_width == width:
            return
        self._last_canvas_width = width
        if self._layout_job is not None:
            try:
                self.window.after_cancel(self._layout_job)
            except Exception:
                pass
        self._layout_job = self.window.after(
            70,
            lambda: self._apply_canvas_width(canvas, canvas_window, width),
        )

    def _apply_canvas_width(self, canvas, canvas_window, width: int) -> None:
        self._layout_job = None
        try:
            current = int(float(canvas.itemcget(canvas_window, "width") or 0))
            if abs(current - int(width)) > 2:
                canvas.itemconfigure(canvas_window, width=int(width))
        except tk.TclError:
            return

    def _schedule_scrollregion(self, canvas) -> None:
        if self._scrollregion_job is not None:
            try:
                self.window.after_cancel(self._scrollregion_job)
            except Exception:
                pass
        self._scrollregion_job = self.window.after_idle(lambda: self._apply_scrollregion(canvas))

    def _apply_scrollregion(self, canvas) -> None:
        self._scrollregion_job = None
        try:
            bounds = canvas.bbox("all")
            if bounds:
                canvas.configure(scrollregion=bounds)
        except tk.TclError:
            return

    def _on_mousewheel(self, event, canvas):
        steps, self._wheel_remainder = _consume_wheel_delta(
            getattr(event, "delta", 0),
            self._wheel_remainder,
        )
        if steps:
            canvas.yview_scroll(-steps * 2, "units")
            self._schedule_preview_refresh(canvas)
        return "break"

    def _bind_mousewheel_tree(self, widget, canvas) -> None:
        widget.bind("<MouseWheel>", lambda event: self._on_mousewheel(event, canvas))
        for child in widget.winfo_children():
            self._bind_mousewheel_tree(child, canvas)

    def _scroll_canvas(self, canvas, *args) -> None:
        canvas.yview(*args)
        self._schedule_preview_refresh(canvas)

    def _schedule_preview_refresh(self, canvas, delay: int = 25) -> None:
        if getattr(self, "_preview_refresh_job", None) is not None:
            try:
                self.window.after_cancel(self._preview_refresh_job)
            except Exception:
                pass
        self._preview_refresh_job = self.window.after(
            delay,
            lambda: self._refresh_visible_previews(canvas),
        )

    @staticmethod
    def _set_preview_placeholder(entry) -> None:
        preview_canvas = entry["canvas"]
        preview_canvas.delete("preview")
        if not preview_canvas.find_withtag("placeholder"):
            preview_canvas.create_text(
                int(entry["width"]) // 2,
                int(entry["height"]) // 2,
                text="스크롤하면 미리보기를 표시합니다.",
                fill="#6b7280",
                tags="placeholder",
            )

    def _load_preview(self, entry) -> None:
        if entry.get("photo") is not None:
            return
        try:
            with Image.open(entry["path"]) as source:
                preview = source.copy()
            preview.thumbnail((760, 460), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(preview)
            preview_canvas = entry["canvas"]
            preview_canvas.delete("placeholder")
            preview_canvas.create_image(
                int(entry["width"]) // 2,
                int(entry["height"]) // 2,
                image=photo,
                anchor="center",
                tags="preview",
            )
            entry["photo"] = photo
        except Exception as exc:
            entry["canvas"].delete("placeholder")
            entry["canvas"].create_text(
                int(entry["width"]) // 2,
                int(entry["height"]) // 2,
                text=f"미리보기를 열 수 없습니다: {exc}",
                fill="#b42318",
                width=max(100, int(entry["width"]) - 20),
                tags="placeholder",
            )

    def _refresh_visible_previews(self, canvas) -> None:
        self._preview_refresh_job = None
        try:
            canvas_top = canvas.winfo_rooty()
            canvas_bottom = canvas_top + canvas.winfo_height()
        except tk.TclError:
            return
        preload_margin = 520
        for entry in self._image_previews:
            widget = entry["canvas"]
            try:
                top = widget.winfo_rooty()
                bottom = top + widget.winfo_height()
            except tk.TclError:
                continue
            nearby = bottom >= canvas_top - preload_margin and top <= canvas_bottom + preload_margin
            if nearby:
                self._load_preview(entry)
            elif entry.get("photo") is not None:
                self._set_preview_placeholder(entry)
                entry["photo"] = None

    def _build_flow(self) -> None:
        canvas = tk.Canvas(self.flow_frame, highlightthickness=0, yscrollincrement=24)
        self.flow_canvas = canvas
        self._last_canvas_width = None
        scrollbar = ttk.Scrollbar(
            self.flow_frame,
            orient="vertical",
            command=lambda *args: self._scroll_canvas(canvas, *args),
        )
        content = tk.Frame(canvas, padx=16, pady=12)
        content.bind("<Configure>", lambda _event: self._schedule_scrollregion(canvas))
        canvas_window = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda event: self._schedule_canvas_width(canvas, canvas_window, event.width),
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        title = str(self.document.get("title") or "수업 흐름")
        title_label = tk.Label(content, text=title, font=("맑은 고딕", 18, "bold"))
        title_label.pack(anchor="w", pady=(0, 12))
        self._bind_context_menu(title_label, "제목 복사", lambda value=title: self.copy_text_block(value))

        sections = self.document.get("sections", [])
        if not sections:
            tk.Label(content, text="아직 수업 흐름에 표시할 내용이 없습니다.", fg="#6b7280").pack(anchor="w")
            return

        for section in sections:
            section_frame = tk.LabelFrame(content, text=section.get("title") or "관련 내용", padx=12, pady=10)
            section_frame.pack(fill="x", pady=7)
            copy_context = _section_copy_context(section)
            self._bind_bundle_context_menu(section_frame, copy_context)
            for item in section.get("items", []):
                item_type = item.get("type")
                if item_type == "capture":
                    self._add_image_block(
                        section_frame,
                        Path(str(item.get("imageSrc") or "")),
                        copy_context,
                    )
                elif item_type in {"explanation", "note"}:
                    self._add_text_block(
                        section_frame,
                        _plain(str(item.get("html") or "")),
                        copy_context,
                    )
                elif item_type == "code":
                    self._add_code_block(
                        section_frame,
                        str(item.get("code") or ""),
                        copy_context,
                    )
        self._bind_mousewheel_tree(content, canvas)
        canvas.bind("<MouseWheel>", lambda event: self._on_mousewheel(event, canvas))
        self._schedule_preview_refresh(canvas, delay=1)

    def _add_image_block(self, parent, path: Path, copy_context: dict | None = None) -> None:
        copy_context = copy_context or {"section_id": "", "image_paths": [path], "text": ""}
        block = tk.LabelFrame(parent, text="그림 · 우클릭하여 복사", padx=8, pady=8, fg="#374151")
        block.pack(fill="x", pady=6)
        self._bind_bundle_context_menu(
            block,
            copy_context,
            path,
        )
        if not path.is_file():
            tk.Label(block, text=f"이미지 파일 없음: {path}", fg="#b42318").pack(anchor="w")
            return
        try:
            with Image.open(path) as source:
                width, height = _preview_size(*source.size)
            preview_canvas = tk.Canvas(
                block,
                width=width,
                height=height,
                highlightthickness=0,
                background="#f3f4f6",
                cursor="hand2",
            )
            preview_canvas.pack(anchor="w")
            entry = {
                "canvas": preview_canvas,
                "path": path,
                "width": width,
                "height": height,
                "photo": None,
            }
            self._image_previews.append(entry)
            self._set_preview_placeholder(entry)
            self._bind_bundle_context_menu(
                preview_canvas,
                copy_context,
                path,
            )
        except Exception as exc:
            tk.Label(block, text=f"이미지를 열 수 없습니다: {exc}", fg="#b42318").pack(anchor="w")

    def _add_text_block(self, parent, value: str, copy_context: dict | None = None) -> None:
        copy_context = copy_context or {"section_id": "", "image_paths": [], "text": value}
        block = tk.LabelFrame(parent, text="텍스트 · 우클릭하여 복사", padx=8, pady=8, fg="#374151")
        block.pack(fill="x", pady=6)
        self._bind_bundle_context_menu(block, copy_context)
        label = tk.Label(block, text=value, justify="left", anchor="w", wraplength=800, cursor="hand2")
        label.pack(fill="x", anchor="w")
        self._bind_bundle_context_menu(label, copy_context)

    def _add_code_block(self, parent, value: str, copy_context: dict | None = None) -> None:
        copy_context = copy_context or {"section_id": "", "image_paths": [], "text": value}
        block = tk.LabelFrame(parent, text="텍스트 · 우클릭하여 복사", padx=8, pady=8, fg="#374151")
        block.pack(fill="x", pady=6)
        self._bind_bundle_context_menu(block, copy_context)
        code = ScrolledText(block, height=min(14, max(3, value.count("\n") + 2)), wrap="none", cursor="hand2")
        code.insert("1.0", value)
        code.configure(state="disabled", background="#f5f7fa")
        code.pack(fill="x")
        self._bind_bundle_context_menu(code, copy_context)


def open_flow_result_window(
    parent,
    workspace,
    document,
    flow_path,
    status_callback=None,
    error_callback=None,
    internal_image_copy_callback=None,
):
    return FlowResultWindow(
        parent,
        workspace,
        document,
        flow_path,
        status_callback,
        error_callback,
        internal_image_copy_callback,
    )
