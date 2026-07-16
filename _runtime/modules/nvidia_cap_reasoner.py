import base64
import io
import json
import os
import re
from pathlib import Path
from typing import Any

from PIL import Image

from modules.model_retry import post_with_transient_retry


DEFAULT_CAP_MODEL = "qwen/qwen3.5-397b-a17b"
DEFAULT_CAP_API_BASE = "https://integrate.api.nvidia.com/v1/chat/completions"


def _get_api_key(config: dict) -> str:
    raw = str(
        config.get("nvidia_api_key")
        or os.environ.get("NVIDIA_API_KEY")
        or os.environ.get("OCR_API_KEY")
        or ""
    ).strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return raw.strip().strip('"').strip("'").strip()


def _image_to_data_url(image_path: Path, max_long_side: int = 3200) -> str:
    """
    CAP 모드는 구조와 글자 관계를 직접 보도록 원본에 가까운 RGB PNG를 보낸다.
    지나치게 큰 이미지만 긴 변 기준으로 축소한다.
    """
    with Image.open(image_path) as image:
        image = image.convert("RGB")
        long_side = max(image.width, image.height)
        if long_side > max_long_side:
            scale = max_long_side / long_side
            image = image.resize(
                (
                    max(1, int(image.width * scale)),
                    max(1, int(image.height * scale)),
                ),
                Image.Resampling.LANCZOS,
            )

        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)

    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _extract_message_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return str(payload or "").strip()

    choices = payload.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        if isinstance(message, dict):
            content = message.get("content")
            if content:
                return str(content).strip()

        text = choices[0].get("text")
        if text:
            return str(text).strip()

    for key in ("content", "text", "output", "result"):
        value = payload.get(key)
        if value:
            return str(value).strip()

    return ""


def _clean_model_output(text: str) -> str:
    """
    사용자에게 복사할 최종 결과만 남긴다.
    모델이 전체 결과를 markdown 코드펜스로 감싼 경우 바깥 펜스만 제거한다.
    """
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()

    # 일부 reasoning 모델이 노출하는 think 블록 제거
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).strip()

    fenced = re.fullmatch(r"```(?:markdown|md|text)?\s*\n?(.*?)\n?```", text, flags=re.S | re.I)
    if fenced:
        text = fenced.group(1).strip()

    return text


def _failure(title: str, detail: str) -> str:
    return f"CAP 분석 실패\n\n{title}\n\n{detail}".strip()


DEFAULT_CAP_PROMPT = '당신은 수업·개발·문서 캡처 화면을 해석하는 이미지 분석 도우미입니다.\n\n이 모드의 목적은 화면의 모든 글자를 OCR처럼 그대로 옮기는 것이 아닙니다.\n이미지 전체를 직접 보고, 화면이 무엇을 보여주는지와 핵심 의미를 이해한 뒤\n사용자가 바로 복사해 학습 기록이나 메모에 붙여넣을 수 있는 간결한 Markdown으로 정리하세요.\n\n핵심 원칙:\n- 이미지 전체의 배치, 시각적 관계, 코드·표·그래프·화면 상태를 함께 해석하세요.\n- 화면의 목적과 핵심 내용을 우선 설명하세요.\n- 보이는 글자를 모두 전사하지 말고, 이해에 필요한 핵심 문구만 짧게 인용하세요.\n- 이미지에 없는 내용을 사실처럼 만들지 마세요.\n- 합리적인 해석은 가능하지만, 근거가 부족하면 "확인 필요"라고 표시하세요.\n- 분석 과정이나 장황한 사족은 출력하지 마세요.\n- 최종 결과는 바로 복사 가능한 한국어 Markdown만 반환하세요.\n\n화면 유형별 기준:\n\n1. 코드 화면\n- 코드가 무엇을 구현하는지 설명하세요.\n- 입력 → 처리 → 출력의 실행 흐름을 정리하세요.\n- 주요 클래스·함수·변수의 역할과 연결 관계를 설명하세요.\n- 화면에 오류가 있으면 오류 위치, 직접 원인, 수정 방향을 구분하세요.\n- 핵심 줄만 짧게 인용하고 전체 코드를 그대로 전사하지 마세요.\n- 보이지 않는 코드는 추측하지 마세요.\n\n2. 표·캘린더·인포그래픽\n- 표의 행·열, 단계, 순서, 범주 사이의 관계를 해석하세요.\n- 이 자료가 전달하려는 핵심 메시지를 설명하세요.\n- 모든 셀을 단순 나열하지 말고 중요한 패턴과 구조를 정리하세요.\n- 정확히 읽히지 않는 세부 글자는 "확인 필요"로 남기세요.\n\n3. 슬라이드·일반 문서\n- 주제, 핵심 주장, 설명 흐름을 요약하세요.\n- 제목 → 핵심 내용 → 중요한 근거 순서로 정리하세요.\n- 본문 전체를 그대로 베끼지 마세요.\n\n4. 실행 결과·콘솔·오류 화면\n- 어떤 작업의 결과인지 설명하세요.\n- 정상 결과인지 오류인지 구분하세요.\n- 오류이면 메시지, 원인 후보, 수정 방향을 나누세요.\n- 원인 후보는 확정 사실처럼 단정하지 마세요.\n\n5. 웹·앱 화면\n- 사용자가 보고 있는 기능과 현재 상태를 설명하세요.\n- 주요 버튼, 입력, 결과 영역이 어떻게 연결되는지 정리하세요.\n- 단순한 UI 요소 나열보다 실제 사용 흐름을 설명하세요.\n\n6. 영상·사진·장면\n- 장면에서 보이는 상황과 핵심 맥락을 간단히 설명하세요.\n- 자막은 의미 이해에 필요한 부분만 짧게 반영하세요.\n- 등장인물의 신원이나 보이지 않는 사건을 추측하지 마세요.\n\n권장 출력 형식:\n\n## 화면 해석\n- 화면 유형:\n- 핵심 내용:\n- 화면에서 확인되는 근거:\n\n## 구조 또는 흐름\n- 이미지의 구성과 요소 사이의 관계를 설명\n\n## 학습·활용 포인트\n- 이 화면에서 이해하거나 기억할 내용\n\n## 확인 필요\n- 불명확하거나 이미지에서 확정할 수 없는 부분이 있을 때만 작성\n\n내용이 단순하면 위 형식을 억지로 모두 채우지 말고 더 짧게 작성하세요.\n최종 응답에는 해석 결과만 출력하세요.'

# 구버전 사용자 설정에 저장된 보고서형 기본 프롬프트를 식별하기 위해 보존한다.
LEGACY_CAP_REPORT_PROMPT = DEFAULT_CAP_PROMPT


DEFAULT_FLOW_INTERPRETATION_PROMPT = """
너는 강의 캡처 화면을 학생이 복습하기 좋은 수업 노트로 정리하는 역할이다.

입력으로 원본 캡처 이미지와 보조 OCR 텍스트가 제공된다.
최종 결과는 이미지 분석 보고서가 아니라 학생이 바로 읽고 이해할 수 있는 한국어 Markdown 수업 정리여야 한다.

작성 원칙:
1. 이미지에서 실제로 확인되는 내용을 우선한다.
2. OCR은 작은 글자와 표현을 확인하기 위한 보조 자료로만 사용한다.
3. 이미지와 OCR이 충돌하면 이미지를 우선한다.
4. 화면의 문장을 그대로 나열하지 말고, 배우는 개념과 내용이 등장한 이유를 문맥으로 연결해 설명한다.
5. 정의, 역할, 처리 흐름, 장점, 한계, 사용 시점이 자연스럽게 이어지도록 작성한다.
6. 같은 내용을 여러 제목이나 문단에서 표현만 바꾸어 반복하지 않는다.
7. 화면 유형, 화면에서 확인되는 근거, 관찰 내용, 분석 과정 같은 내부 판단 정보는 출력하지 않는다.
8. 첫 줄은 화면 내용을 대표하는 구체적인 `## 제목`으로 작성한다.
9. 캡처 내용에 필요한 섹션만 선택하고 고정 양식을 억지로 채우지 않는다. 내용이 짧으면 2~3개 섹션만 사용한다.
10. 이미지 범위 안에서 기술적으로 오해하기 쉬운 표현을 정확히 풀어 쓰되, 보이지 않는 버전·함수 동작·수치·설정값은 추측하지 않는다.
11. 최종 답변에는 분석 과정, JSON, 전체를 감싼 Markdown 코드펜스, 프롬프트 설명이나 안내 문구를 포함하지 않는다.

내용별 작성 규칙:
- 개념 화면: 개념이 무엇인지 → 어떤 역할을 하는지 → 언제 사용하는지 → 주의할 점의 흐름으로 설명한다. 단순 정의에서 끝내지 않는다.
- 처리 흐름 화면: 가능하면 `입력 → 처리 → 결과`처럼 한 줄 흐름을 먼저 제시하고 각 단계가 왜 필요한지 설명한다.
- 장단점 화면: 슬라이드 문구를 반복하지 말고 장점과 한계가 실제 사용에서 무엇을 의미하는지 설명한다.
- 코드 화면: 코드가 하는 일, 실행 순서, 핵심 함수나 문법, 입력과 출력, 초보자가 헷갈리기 쉬운 부분을 설명한다. 코드가 명확히 보일 때만 코드 블록을 사용하고 잘리거나 불명확한 코드를 임의로 완성하지 않는다.
- 오류 화면: 문제 → 원인 → 수정 위치 → 해결 방법 → 핵심의 흐름으로 설명한다. 화면에 보이는 오류 메시지 원문은 보존하며, 원인 후보를 확정 사실처럼 단정하지 않는다.
- 표·다이어그램 화면: 모든 셀이나 문구를 옮기지 말고 요소 사이의 관계와 자료가 전달하는 핵심 흐름을 설명한다.

Markdown 규칙:
- 첫 줄은 반드시 캡처 내용을 대표하는 구체적인 `## 제목`이다.
- `화면 해석`, `화면 유형`, `화면에서 확인되는 근거`, `구조 또는 흐름`, `학습·활용 포인트`, `이미지 분석`, `분석 결과`, `관찰 내용`을 제목이나 메타 항목으로 출력하지 않는다.
- 본문은 짧은 문단 중심으로 쓰고, 목록은 실제 단계·장점·주의점에만 사용한다.
- 핵심 흐름은 필요할 때 `→`로 표현한다.
- 장점, 한계, 사용 시점, 핵심 중 캡처에 필요한 섹션만 사용한다.
- 복습할 핵심 문장은 필요할 때 마지막 `### 핵심` 아래 인용문 한 줄로 정리한다.
- 불필요한 표를 만들지 않고 같은 내용을 반복하지 않는다.

불확실성 처리:
- 코드 일부가 잘렸거나, 작은 글자가 식별되지 않거나, 앞뒤 화면이 없어 의미를 확정하기 어렵거나, 화면 밖 정보가 꼭 필요한 경우에만 마지막에 `### 확인 필요`를 추가한다.
- 확인할 내용이 없으면 `확인 필요` 섹션을 출력하지 않는다.
- `확인 필요: 없음`, `## 확인 필요\n- 없음` 같은 문구는 절대 출력하지 않는다.

권장 흐름은 구체적인 제목 → 핵심 개념 → 필요한 처리 흐름 → 실제 장점·한계 또는 주의점 → 사용 시점이나 예시 → 핵심 한 줄이다. 캡처 내용에 맞지 않는 단계는 생략한다.
""".strip()

# 수동 CAP 이미지 해석과 백그라운드 수업 흐름이 같은 복습용 문서 규칙을 사용한다.
DEFAULT_CAP_PROMPT = DEFAULT_FLOW_INTERPRETATION_PROMPT


def build_flow_interpretation_prompt(ocr_text: str) -> str:
    """Build the lesson-flow-only prompt without changing the manual CAP prompt."""
    return (
        DEFAULT_FLOW_INTERPRETATION_PROMPT
        + "\n\n아래 OCR 텍스트는 보조 자료이며 원본 이미지보다 우선하지 않는다.\n"
        + "--- 보조 OCR 시작 ---\n"
        + str(ocr_text or "").strip()[:16000]
        + "\n--- 보조 OCR 끝 ---"
    )


def build_cap_prompt(config: dict | None = None) -> str:
    config = config or {}
    custom_prompt = str(config.get("cap_reasoning_prompt") or "").strip()
    return custom_prompt or DEFAULT_CAP_PROMPT


def _apply_model_request_options(payload: dict, model: str) -> None:
    # Qwen 3.5는 thinking 모드가 기본값이라 짧은 이미지 분석도 첫 응답이
    # 오래 지연될 수 있다. ClassFlowAI는 최종 결과만 사용하므로 공식 API의
    # thinking 비활성 옵션을 이 모델 계열에만 적용한다.
    if str(model or "").strip().lower().startswith("qwen/qwen3.5-"):
        payload["chat_template_kwargs"] = {"enable_thinking": False}


DEFAULT_OCR_CORRECTION_PROMPT = """
원본 이미지와 아래 OCR 결과를 직접 비교하여 OCR 오류만 수정하세요.

목적:
- 사용자가 바로 복사할 수 있는 정확한 텍스트를 반환
- 설명, 요약, 평가, 수정 내역은 출력하지 않음
- 최종 수정 텍스트만 출력

수정 허용:
- 잘못 인식된 글자
- 명백한 띄어쓰기 오류
- 줄바꿈과 읽기 순서
- 표·목록에서 이미지 배치로 확실히 확인되는 순서
- 파일명과 확장자의 명백한 OCR 오류

금지:
- 이미지에 없는 내용을 문맥으로 추가
- 보이지 않는 문장 복원
- 내용을 요약하거나 다시 작성
- 코드 식별자, 숫자, 연산자, 괄호, 따옴표, 세미콜론을 추측으로 변경
- 확신이 없는 부분을 그럴듯하게 채우기

코드 화면:
- 들여쓰기와 줄 구조를 가능한 한 유지
- 판독이 불가능한 부분은 [확인 필요]로 표시
- 코드 설명은 추가하지 않음

출력에는 보정된 본문만 포함하세요.
""".strip()


def correct_ocr_with_image(image_path: Path, ocr_text: str, config: dict, on_retry=None) -> str:
    api_key = _get_api_key(config)
    if not api_key:
        return "OCR 보정 실패\n\nNVIDIA API 키가 없습니다."

    image_path = Path(image_path)
    if not image_path.exists():
        return f"OCR 보정 실패\n\n이미지 파일을 찾을 수 없습니다.\n{image_path}"

    current_ocr = str(ocr_text or "").strip()
    if not current_ocr:
        return "OCR 보정 실패\n\n보정할 OCR 결과가 없습니다."

    try:
        import requests
    except Exception as exc:
        return f"OCR 보정 실패\n\nrequests 패키지가 없습니다.\n{exc}"

    try:
        data_url = _image_to_data_url(
            image_path,
            max_long_side=int(config.get("cap_reasoning_max_long_side") or 3200),
        )
    except Exception as exc:
        return f"OCR 보정 실패\n\n이미지를 준비하지 못했습니다.\n{exc}"

    model = str(config.get("cap_reasoning_model") or DEFAULT_CAP_MODEL).strip()
    api_base = str(config.get("cap_reasoning_api_base") or DEFAULT_CAP_API_BASE).strip()
    connect_timeout = int(config.get("cap_reasoning_connect_timeout_sec") or 15)
    read_timeout = int(config.get("cap_reasoning_timeout_sec") or 150)
    max_tokens = min(int(config.get("cap_reasoning_max_tokens") or 4096), 3000)

    prompt = (
        DEFAULT_OCR_CORRECTION_PROMPT
        + "\n\n--- 현재 OCR 결과 ---\n"
        + current_ocr
        + "\n--- OCR 결과 끝 ---"
    )

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "temperature": 0,
        "top_p": 0.8,
        "max_tokens": max_tokens,
        "stream": False,
    }
    _apply_model_request_options(payload, model)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        response = post_with_transient_retry(
            requests,
            api_base,
            headers=headers,
            json=payload,
            timeout=(connect_timeout, read_timeout),
            on_retry=on_retry,
        )
    except requests.exceptions.Timeout:
        return "OCR 보정 실패\n\n응답 제한 시간이 초과되었습니다."
    except requests.exceptions.ConnectionError:
        return "OCR 보정 실패\n\n서버에 연결하지 못했습니다."
    except requests.exceptions.RequestException as exc:
        return f"OCR 보정 실패\n\n요청에 실패했습니다.\n{type(exc).__name__}"

    if response.status_code in {401, 403}:
        return (
            "OCR 보정 실패\n\n"
            f"NVIDIA API 인증에 실패했습니다. HTTP {response.status_code}\n"
        )

    if response.status_code >= 400:
        return (
            "OCR 보정 실패\n\n"
            f"API가 오류를 반환했습니다. HTTP {response.status_code}\n"
        )

    try:
        result = response.json()
        corrected = _clean_model_output(_extract_message_text(result))
    except Exception as exc:
        return (
            "OCR 보정 실패\n\n"
            f"응답을 해석하지 못했습니다.\n{type(exc).__name__}"
        )

    return corrected or "OCR 보정 실패\n\n모델이 보정 텍스트를 반환하지 않았습니다."



def analyze_capture_image(image_path: Path, config: dict, on_retry=None) -> str:
    api_key = _get_api_key(config)
    if not api_key:
        return _failure("NVIDIA API 키가 없습니다.", "설정에서 NVIDIA API 키를 입력하세요.")

    image_path = Path(image_path)
    if not image_path.exists():
        return _failure("이미지 파일을 찾을 수 없습니다.", str(image_path))

    try:
        import requests
    except Exception as exc:
        return _failure("requests 패키지가 없습니다.", str(exc))

    try:
        data_url = _image_to_data_url(
            image_path,
            max_long_side=int(config.get("cap_reasoning_max_long_side") or 3200),
        )
    except Exception as exc:
        return _failure("이미지를 전송용으로 준비하지 못했습니다.", str(exc))

    model = str(config.get("cap_reasoning_model") or DEFAULT_CAP_MODEL).strip()
    api_base = str(config.get("cap_reasoning_api_base") or DEFAULT_CAP_API_BASE).strip()
    connect_timeout = int(config.get("cap_reasoning_connect_timeout_sec") or 15)
    read_timeout = int(config.get("cap_reasoning_timeout_sec") or 150)
    max_tokens = int(config.get("cap_reasoning_max_tokens") or 4096)

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_cap_prompt(config)},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            }
        ],
        "temperature": 0.1,
        "top_p": 0.9,
        "max_tokens": max_tokens,
        "stream": False,
    }
    _apply_model_request_options(payload, model)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        response = post_with_transient_retry(
            requests,
            api_base,
            headers=headers,
            json=payload,
            timeout=(connect_timeout, read_timeout),
            on_retry=on_retry,
        )
    except requests.exceptions.Timeout:
        return _failure("CAP 이미지 추론 시간이 초과되었습니다.", "두 번째 요청도 제한 시간을 초과했습니다.")
    except requests.exceptions.ConnectionError:
        return _failure("CAP 이미지 추론 서버에 연결하지 못했습니다.", "두 번째 요청도 연결에 실패했습니다.")
    except requests.exceptions.RequestException as exc:
        return _failure("CAP 이미지 추론 요청에 실패했습니다.", type(exc).__name__)

    if response.status_code in {401, 403}:
        return _failure(
            "NVIDIA API 인증에 실패했습니다.",
            f"HTTP {response.status_code}",
        )

    if response.status_code >= 400:
        return _failure(
            "CAP 이미지 추론 API가 오류를 반환했습니다.",
            f"HTTP {response.status_code}",
        )

    try:
        result = response.json()
        text = _clean_model_output(_extract_message_text(result))
    except Exception as exc:
        return _failure(
            "CAP 이미지 추론 응답을 해석하지 못했습니다.",
            type(exc).__name__,
        )

    return text or _failure("CAP 결과가 비어 있습니다.", "모델이 텍스트를 반환하지 않았습니다.")
