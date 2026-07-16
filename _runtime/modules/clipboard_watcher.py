import ctypes
import hashlib
import io
import sys
import time
from pathlib import Path
from typing import Optional

from PIL import Image, ImageGrab


CF_DIB = 8
GMEM_MOVEABLE = 0x0002
CLIPBOARD_OPEN_ATTEMPTS = 10
CLIPBOARD_RETRY_DELAY_SEC = 0.05


def get_clipboard_sequence_number() -> int | None:
    """Return the Windows clipboard change counter without reading its payload."""
    if not sys.platform.startswith("win"):
        return None
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetClipboardSequenceNumber.argtypes = []
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        return int(user32.GetClipboardSequenceNumber())
    except Exception:
        return None


def clipboard_sequence_changed(previous: int | None, current: int | None) -> bool:
    """Fallback to reading when Windows cannot provide a sequence number."""
    return current is None or previous is None or int(current) != int(previous)


def get_clipboard_image() -> Optional[Image.Image]:
    """
    // 1. 클립보드 데이터 읽기
    // 2. 이미지면 RGB 이미지로 반환하기
    // 3. 이미지가 아니면 None 반환하기
    """
    data = ImageGrab.grabclipboard()
    if isinstance(data, Image.Image):
        return data.convert("RGB")
    return None


def image_hash(image: Image.Image) -> str:
    """
    // 1. 이미지 bytes 생성하기
    // 2. 해시값 만들기
    // 3. 같은 캡처 중복 분석 방지하기
    """
    return hashlib.md5(image.tobytes()).hexdigest()


def save_image(image: Image.Image, path: Path) -> None:
    """
    // 1. 캡처 이미지 받기
    // 2. PNG 파일로 저장하기
    // 3. 저장 성공 여부 확인하기
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")
    if not path.exists():
        raise FileNotFoundError(f"캡처 저장 실패: {path}")


def image_to_dib_bytes(image: Image.Image) -> bytes:
    """Convert a Pillow image to CF_DIB bytes without the BMP file header."""
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="BMP")
    bmp = buffer.getvalue()
    if len(bmp) <= 14 or bmp[:2] != b"BM":
        raise ValueError("이미지를 Windows 클립보드 형식으로 변환하지 못했습니다.")
    return bmp[14:]


def open_clipboard_with_retry(
    open_clipboard,
    owner_hwnd: int | None = None,
    *,
    max_attempts: int = CLIPBOARD_OPEN_ATTEMPTS,
    retry_delay: float = CLIPBOARD_RETRY_DELAY_SEC,
    sleep=None,
) -> bool:
    """Try to open the Windows clipboard for a short, bounded interval."""
    sleep = sleep or time.sleep
    for attempt in range(max(1, int(max_attempts))):
        if open_clipboard(owner_hwnd):
            return True
        if attempt + 1 < max_attempts:
            sleep(max(0.0, float(retry_delay)))
    return False


def _set_windows_clipboard_dib(dib_data: bytes, owner_hwnd: int | None = None) -> None:
    if not sys.platform.startswith("win"):
        raise OSError("이미지 클립보드 복사는 Windows에서만 지원됩니다.")
    if not dib_data:
        raise ValueError("클립보드에 복사할 이미지 데이터가 없습니다.")

    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    memory_handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib_data))
    if not memory_handle:
        raise MemoryError("이미지 클립보드 메모리를 할당하지 못했습니다.")

    clipboard_open = False
    ownership_transferred = False
    try:
        memory_pointer = kernel32.GlobalLock(memory_handle)
        if not memory_pointer:
            raise OSError(ctypes.get_last_error(), "클립보드 메모리를 잠그지 못했습니다.")
        try:
            ctypes.memmove(memory_pointer, dib_data, len(dib_data))
        finally:
            kernel32.GlobalUnlock(memory_handle)

        clipboard_open = open_clipboard_with_retry(
            user32.OpenClipboard,
            owner_hwnd,
        )
        if not clipboard_open:
            raise OSError(ctypes.get_last_error(), "Windows 클립보드를 열 수 없습니다.")
        if not user32.EmptyClipboard():
            raise OSError(ctypes.get_last_error(), "Windows 클립보드를 비울 수 없습니다.")
        if not user32.SetClipboardData(CF_DIB, memory_handle):
            raise OSError(ctypes.get_last_error(), "이미지를 Windows 클립보드에 넣을 수 없습니다.")
        ownership_transferred = True
    finally:
        if clipboard_open:
            user32.CloseClipboard()
        if not ownership_transferred:
            kernel32.GlobalFree(memory_handle)


def _set_windows_clipboard_formats(
    formats: list[tuple[int, bytes]],
    owner_hwnd: int | None = None,
) -> None:
    """Atomically publish multiple byte payloads to the Windows clipboard."""
    if not sys.platform.startswith("win"):
        raise OSError("서식 클립보드 복사는 Windows에서만 지원됩니다.")
    if not formats or any(not data for _, data in formats):
        raise ValueError("클립보드에 복사할 데이터가 없습니다.")

    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    allocated: list[tuple[int, int]] = []
    transferred: set[int] = set()
    clipboard_open = False
    try:
        for clipboard_format, data in formats:
            memory_handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not memory_handle:
                raise MemoryError("클립보드 메모리를 할당하지 못했습니다.")
            allocated.append((clipboard_format, memory_handle))
            memory_pointer = kernel32.GlobalLock(memory_handle)
            if not memory_pointer:
                raise OSError(ctypes.get_last_error(), "클립보드 메모리를 잠그지 못했습니다.")
            try:
                ctypes.memmove(memory_pointer, data, len(data))
            finally:
                kernel32.GlobalUnlock(memory_handle)

        clipboard_open = open_clipboard_with_retry(user32.OpenClipboard, owner_hwnd)
        if not clipboard_open:
            raise OSError(ctypes.get_last_error(), "Windows 클립보드를 열 수 없습니다.")
        if not user32.EmptyClipboard():
            raise OSError(ctypes.get_last_error(), "Windows 클립보드를 비울 수 없습니다.")
        for clipboard_format, memory_handle in allocated:
            if not user32.SetClipboardData(clipboard_format, memory_handle):
                raise OSError(ctypes.get_last_error(), "클립보드 서식을 기록하지 못했습니다.")
            transferred.add(memory_handle)
    finally:
        if clipboard_open:
            user32.CloseClipboard()
        for _, memory_handle in allocated:
            if memory_handle not in transferred:
                kernel32.GlobalFree(memory_handle)


def copy_image_to_clipboard(image_path: Path, owner_hwnd: int | None = None) -> None:
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"이미지 파일을 찾을 수 없습니다: {image_path}")
    with Image.open(image_path) as image:
        dib_data = image_to_dib_bytes(image)
    _set_windows_clipboard_dib(dib_data, owner_hwnd=owner_hwnd)


def copy_pil_image_to_clipboard(image: Image.Image, owner_hwnd: int | None = None) -> None:
    """Copy one in-memory PNG/DIB payload in a single clipboard transaction."""
    if not isinstance(image, Image.Image):
        raise TypeError("클립보드에 복사할 이미지가 올바르지 않습니다.")
    if not sys.platform.startswith("win"):
        raise OSError("이미지 클립보드 복사는 Windows에서만 지원됩니다.")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
    user32.RegisterClipboardFormatW.restype = ctypes.c_uint
    png_format = int(user32.RegisterClipboardFormatW("PNG"))
    if not png_format:
        raise OSError(ctypes.get_last_error(), "PNG 클립보드 형식을 등록하지 못했습니다.")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    _set_windows_clipboard_formats(
        [
            (png_format, buffer.getvalue()),
            (CF_DIB, image_to_dib_bytes(image)),
        ],
        owner_hwnd=owner_hwnd,
    )
