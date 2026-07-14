import base64
import mimetypes
import re
import io
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from modules.model_retry import post_with_transient_retry

NVIDIA_MODEL_NAME = "nvidia/nemotron-ocr-v2"
NVIDIA_MODEL_URL = "https://build.nvidia.com/nvidia/nemotron-ocr-v2"
NVIDIA_INVOKE_URL = "https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-ocr-v2"


def _get_api_key(config: dict) -> str:
    """
    설정/API 키 입력값을 정규화한다.

    사용자가 아래처럼 복사해 넣는 경우를 방어한다.
    - Bearer nvapi-...
    - "nvapi-..."
    - 'nvapi-...'
    - 앞뒤 공백/줄바꿈 포함
    """
    raw = str(
        config.get("nvidia_api_key")
        or os.environ.get("NVIDIA_API_KEY")
        or os.environ.get("OCR_API_KEY")
        or ""
    ).strip()

    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()

    raw = raw.strip().strip('"').strip("'").strip()
    return raw


def _normalize_urls(raw_url: str) -> list[str]:
    """
    NVIDIA hosted endpoint는 nemotron-ocr-v2만 사용한다.
    v1 fallback은 사용하지 않는다.
    """
    url = str(raw_url or NVIDIA_INVOKE_URL).strip().rstrip("/")
    if not url:
        url = NVIDIA_INVOKE_URL

    candidates: list[str] = []

    def add(u: str):
        u = str(u or "").strip().rstrip("/")
        if u and u not in candidates:
            candidates.append(u)

    if "ai.api.nvidia.com" in url:
        add(NVIDIA_INVOKE_URL)
        return candidates

    # 로컬 NIM 계열 주소를 사용한 경우만 /v1/infer 형태 후보를 구성한다.
    if url.endswith("/v1/infer"):
        add(url)
    elif url.endswith("/v1"):
        add(url + "/infer")
    elif "/v1/" in url:
        add(url)
    else:
        add(url + "/v1/infer")

    return candidates

def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _as_int(value, default: int) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _preprocess_for_ocr(img: Image.Image, mode: str) -> Image.Image:
    """
    OCR 전송용 이미지 전처리.
    원본 파일은 수정하지 않고 API 전송용 복사본에만 적용한다.
    """
    mode = str(mode or "sharp_gray").strip().lower()

    if mode in {"none", "raw", "original"}:
        return img.convert("RGB")

    if mode in {"gray", "sharp_gray", "contrast", "high_contrast", "bw"}:
        gray = ImageOps.grayscale(img)
        gray = ImageOps.autocontrast(gray)

        if mode in {"sharp_gray", "contrast", "high_contrast", "bw"}:
            gray = ImageEnhance.Contrast(gray).enhance(1.45)

        if mode in {"sharp_gray", "high_contrast", "bw"}:
            gray = gray.filter(ImageFilter.UnsharpMask(radius=1.1, percent=180, threshold=3))

        if mode == "high_contrast":
            gray = ImageEnhance.Contrast(gray).enhance(1.25)

        if mode == "bw":
            # 완전 흑백은 코드/파일명에는 유리할 수 있지만,
            # 회색 UI나 얇은 글자는 손실될 수 있어 기본값으로 쓰지 않는다.
            gray = gray.point(lambda p: 255 if p > 175 else 0)

        return gray.convert("RGB")

    return img.convert("RGB")


def _ocr_resize_scale(
    long_side: int,
    upscale_enabled: bool,
    upscale_factor: float,
    target_long_side: int,
    max_long_side: int,
) -> float:
    """Resize small inputs toward the target without enlarging already-large captures."""
    long_side = max(1, int(long_side))
    scale = 1.0
    if upscale_enabled and target_long_side > 0 and long_side < target_long_side:
        scale = min(max(1.0, float(upscale_factor)), target_long_side / long_side)
    if long_side * scale > max_long_side:
        scale = max_long_side / long_side
    return scale



def _image_to_data_url(image_path: Path, config: dict) -> str:
    """
    OCR 전송용 이미지만 확대한다.
    원본 캡처 파일은 그대로 유지하고, API로 보내는 복사본만 크게 만든다.
    """
    upscale_enabled = _as_bool(config.get("ocr_upscale_enabled"), True)
    upscale_factor = max(1.0, min(_as_float(config.get("ocr_upscale_factor"), 2.5), 3.5))
    target_long_side = max(0, _as_int(config.get("ocr_target_long_side"), 2800))
    max_long_side = max(800, _as_int(config.get("ocr_max_long_side"), 3600))
    image_format = str(config.get("ocr_image_format") or "png").lower().strip()
    preprocess_mode = str(config.get("ocr_preprocess_mode") or "sharp_gray").strip().lower()

    if image_format not in {"png", "jpg", "jpeg"}:
        image_format = "png"

    with Image.open(image_path) as img:
        img = img.convert("RGB")
        long_side = max(img.width, img.height)
        scale = _ocr_resize_scale(
            long_side,
            upscale_enabled,
            upscale_factor,
            target_long_side,
            max_long_side,
        )

        if scale > 1.01:
            new_size = (
                max(1, int(img.width * scale)),
                max(1, int(img.height * scale)),
            )
            img = img.resize(new_size, Image.Resampling.LANCZOS)
        elif long_side > max_long_side:
            scale = max_long_side / max(long_side, 1)
            new_size = (
                max(1, int(img.width * scale)),
                max(1, int(img.height * scale)),
            )
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        img = _preprocess_for_ocr(img, preprocess_mode)

        buffer = io.BytesIO()
        if image_format in {"jpg", "jpeg"}:
            img.save(buffer, format="JPEG", quality=92, optimize=True)
            mime = "image/jpeg"
        else:
            img.save(buffer, format="PNG", optimize=True)
            mime = "image/png"

    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _clean_text(text: str) -> str:
    lines = []
    for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def _extract_text(payload: Any) -> str:
    texts: list[str] = []

    def add(value: Any):
        value = _clean_text(str(value or ""))
        if value:
            texts.append(value)

    if isinstance(payload, dict):
        for page in payload.get("data", []) or []:
            if not isinstance(page, dict):
                continue
            for item in page.get("text_detections", []) or []:
                if not isinstance(item, dict):
                    continue
                pred = item.get("text_prediction") or {}
                if isinstance(pred, dict):
                    add(pred.get("text"))
                else:
                    add(item.get("text"))

        # 게이트웨이/프록시가 다른 형태로 감싸는 경우 대비
        for key in ("text", "output", "result", "markdown", "content"):
            if payload.get(key):
                add(payload.get(key))

    cleaned = []
    seen = set()
    for t in texts:
        if t not in seen:
            cleaned.append(t)
            seen.add(t)
    return "\n".join(cleaned).strip()


def _post_clean_ocr_text(text: str, config: dict) -> str:
    """
    OCR 결과 후처리.

    안전 원칙:
    - OCR 원문은 앱에서 별도로 보존한다.
    - 후처리는 파일 목록 화면처럼 확실한 경우에만 강하게 적용한다.
    - 코드 화면으로 보이면 원문을 거의 건드리지 않는다.
    """
    if not _as_bool(config.get("ocr_post_cleanup_enabled"), True):
        return text

    raw_lines = [line.strip() for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]
    if not raw_lines:
        return ""

    file_ext_pattern = re.compile(
        r"\.(ipynb|ipnb|iynb|pynb|ppnb|py|java|html|css|js|sql|md|txt|csv|xlsx|png|jpg|jpeg|webp|json|xml|yml|yaml)\b",
        re.IGNORECASE,
    )
    ex_prefix_pattern = re.compile(r"^(?:[0-9]{1,3}\s+)?ex\d{1,3}[\s_\-]", re.IGNORECASE)

    code_markers = [
        "public class", "private ", "protected ", "import ", "from ", "def ",
        "return ", "if ", "else", "for ", "while ", "try:", "catch", "System.out",
        "@Controller", "@GetMapping", "@PostMapping", "<html", "</", "{", "}", ";"
    ]

    lower_text = "\n".join(raw_lines).lower()
    code_score = sum(1 for marker in code_markers if marker.lower() in lower_text)
    file_like_count = sum(1 for line in raw_lines if file_ext_pattern.search(line) or ex_prefix_pattern.search(line))
    file_list_context = file_like_count >= 2 and file_like_count >= max(2, int(len(raw_lines) * 0.45))

    # 코드 화면이면 파일명 전용 보정을 적용하지 않는다.
    # 단, 파일 목록으로 매우 강하게 보이면 파일 목록으로 처리한다.
    if code_score >= 2 and not file_list_context:
        return "\n".join(raw_lines).strip()

    # 파일 목록으로 보기 어렵다면 원문 보존을 우선한다.
    if not file_list_context:
        return "\n".join(raw_lines).strip()

    cleaned: list[str] = []

    def fix_common_extension_typos(line: str) -> str:
        line = re.sub(r"\.{1,3}(?:p{1,2}nb|ipnb|iynb|pynb|jpy?nb|ipyn[b8])\b", ".ipynb", line, flags=re.IGNORECASE)
        line = re.sub(r"\.ipynb[|｜¦.]+$", ".ipynb", line, flags=re.IGNORECASE)
        return line

    for raw in raw_lines:
        line = raw.strip()
        if not line:
            continue

        line = re.sub(r"[|｜¦]+$", "", line).strip()
        line = fix_common_extension_typos(line)

        looks_like_file_item = bool(file_ext_pattern.search(line) or ex_prefix_pattern.search(line))

        if looks_like_file_item:
            line = re.sub(
                r"^(?:[0-9]{1,3}|[A-Za-z]{1,3}|[Oo0Il|｜¦]{1,3})\s+(?=(?:ex\d{1,3}|[A-Za-z0-9가-힣_\-.()]))",
                "",
                line,
                flags=re.IGNORECASE,
            ).strip()

        # 파일 목록 아이콘이 Latin/Cyrillic 혼합 CO/co/сO/Со처럼 읽히는 경우
        # 파일 목록 문맥에서만 제거한다.
        compact = line.replace(" ", "")
        normalized_noise = compact.translate(str.maketrans({
            "с": "c", "С": "C",
            "о": "o", "О": "O",
            "Ι": "I", "І": "I", "ⅼ": "l",
        }))
        if re.fullmatch(r"[A-Za-z0-9OoIl|｜¦]{1,3}", normalized_noise):
            continue

        if looks_like_file_item:
            line = re.sub(r"(?<!\.)\.+$", "", line).strip()

        cleaned.append(line)

    return "\n".join(cleaned).strip()



def _failure(title: str, detail: str, extra: str = "") -> str:
    body = f"## OCR 실패\n\n{title}\n\n### 원인\n\n{detail}"
    if extra:
        body += f"\n\n### 응답 일부\n\n```text\n{extra}\n```"
    return body.strip()


def extract_text_from_image(image_path: Path, config: dict, on_retry=None) -> str:
    """
    // 1. NVIDIA API 키 확인하기
    // 2. 캡처 이미지를 base64 data URL로 변환하기
    // 3. NVIDIA OCR API에 요청하기
    // 4. 추출 텍스트 반환하기
    """
    api_key = _get_api_key(config)
    if not api_key:
        return ""

    image_path = Path(image_path)
    if not image_path.exists():
        return _failure("이미지 파일을 찾을 수 없습니다.", f"`{image_path}`")

    try:
        import requests
    except Exception as e:
        return _failure("requests 패키지가 없습니다.", f"`{e}`\n\nINSTALL_FIRST.bat을 다시 실행하세요.")

    try:
        data_url = _image_to_data_url(image_path, config)
    except Exception as e:
        return _failure("이미지를 읽을 수 없습니다.", f"`{e}`")

    payload = {
        "input": [{"type": "image_url", "url": data_url}],
        "merge_levels": ["paragraph"],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    timeout = int(config.get("nvidia_ocr_timeout_sec") or 60)
    api_base = str(config.get("nvidia_api_base") or NVIDIA_INVOKE_URL)
    candidates = _normalize_urls(api_base)

    last_status = None
    last_body = ""
    last_url = ""
    for url in candidates:
        last_url = url
        try:
            response = post_with_transient_retry(
                requests,
                url,
                headers=headers,
                json=payload,
                timeout=timeout,
                on_retry=on_retry,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_body = type(e).__name__
            continue
        except requests.exceptions.RequestException as e:
            last_body = type(e).__name__
            continue

        last_status = response.status_code
        if response.status_code == 404:
            last_body = f"HTTP {response.status_code}"
            continue
        if response.status_code in {401, 403}:
            return _failure(
                "NVIDIA API 인증에 실패했습니다.",
                (
                    f"- HTTP 상태: `{response.status_code}`\n"
                    f"- 모델: `{config.get('nvidia_ocr_model') or NVIDIA_MODEL_NAME}`\n\n"
                    "설정에서 API 키와 OCR 모델을 확인하세요."
                ),
            )

        if response.status_code >= 400:
            return _failure(
                "OCR API가 오류를 반환했습니다.",
                f"- HTTP 상태: `{response.status_code}`\n- 모델: `{config.get('nvidia_ocr_model') or NVIDIA_MODEL_NAME}`\n- 요청 주소: `{url}`",
            )

        try:
            result = response.json()
        except Exception as e:
            return _failure("OCR API 응답을 JSON으로 해석하지 못했습니다.", f"`{type(e).__name__}`")

        raw_text = _extract_text(result)
        cleaned_text = _post_clean_ocr_text(raw_text, config) or "[OCR 결과 없음]"
        raw_text = raw_text or ""
        if raw_text.strip() and cleaned_text.strip() != raw_text.strip():
            return cleaned_text.strip() + "\n\n--- OCR_RAW_TEXT ---\n" + raw_text.strip()
        return cleaned_text

    return _failure(
        "OCR API가 404를 반환했습니다.",
        f"- HTTP 상태: `{last_status}`\n- 모델: `{config.get('nvidia_ocr_model') or NVIDIA_MODEL_NAME}`\n- 마지막 요청 주소: `{last_url}`\n\nAPI 키 또는 NVIDIA hosted endpoint 접근 권한을 확인하세요.",
        last_body[:1200],
    )
