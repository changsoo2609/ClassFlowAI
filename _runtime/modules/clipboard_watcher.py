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

        for _ in range(10):
            if user32.OpenClipboard(owner_hwnd):
                clipboard_open = True
                break
            time.sleep(0.05)
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


def copy_image_to_clipboard(image_path: Path, owner_hwnd: int | None = None) -> None:
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"이미지 파일을 찾을 수 없습니다: {image_path}")
    with Image.open(image_path) as image:
        dib_data = image_to_dib_bytes(image)
    _set_windows_clipboard_dib(dib_data, owner_hwnd=owner_hwnd)
