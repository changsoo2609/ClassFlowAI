import hashlib
from pathlib import Path
from typing import Optional

from PIL import Image, ImageGrab


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