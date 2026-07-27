import base64
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from modules.model_retry import post_with_transient_retry


DEFAULT_CAP_MODEL = "google/diffusiongemma-26b-a4b-it"
RETIRED_QWEN_CAP_MODEL = "qwen/qwen3.5-397b-a17b"
DEFAULT_CAP_API_BASE = "https://integrate.api.nvidia.com/v1/chat/completions"
MODEL_DIAGNOSTIC_LOG_PATH = (
    Path(os.environ.get("LOCALAPPDATA") or Path.home())
    / "ClassFlowAI"
    / "model_request_diagnostics.jsonl"
)


def _diagnostic_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_diagnostic_event(event: dict) -> None:
    try:
        MODEL_DIAGNOSTIC_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with MODEL_DIAGNOSTIC_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        # Diagnostic logging must never make model requests fail.
        pass


def _data_url_metadata(data_url: str) -> tuple[str, int]:
    header, _, encoded = str(data_url or "").partition(",")
    mime_type = ""
    if header.startswith("data:"):
        mime_type = header[5:].split(";", 1)[0]
    padding = len(encoded) - len(encoded.rstrip("="))
    byte_size = max(0, (len(encoded) * 3) // 4 - padding)
    return mime_type, byte_size


def _request_diagnostic(
    api_base: str,
    model: str,
    payload: dict,
    data_url: str,
    timeout: tuple[int, int],
) -> dict:
    roles = []
    content_types = []
    has_image_url = False
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        roles.append(str(message.get("role") or ""))
        types = []
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "")
                types.append(item_type)
                has_image_url = has_image_url or item_type == "image_url"
        content_types.append(types)

    mime_type, image_bytes = _data_url_metadata(data_url)
    return {
        "event": "request",
        "request_at": _diagnostic_timestamp(),
        "endpoint": api_base,
        "model_repr": repr(model),
        "model_length": len(model),
        "message_roles": roles,
        "message_content_types": content_types,
        "has_image_url": has_image_url,
        "data_url_mime_type": mime_type,
        "image_bytes": image_bytes,
        "payload_keys": sorted(payload.keys()),
        "timeout": list(timeout),
    }


def _response_header(response, name: str) -> str:
    headers = getattr(response, "headers", {}) or {}
    target = name.lower()
    for key, value in getattr(headers, "items", lambda: ())():
        if str(key).lower() == target:
            return str(value or "")[:500]
    return ""


def _http_error_diagnostic(response) -> dict:
    error_code = ""
    error_message = ""
    try:
        payload = response.json()
    except Exception:
        payload = None

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            error_code = str(error.get("code") or error.get("status") or "")
            error_message = str(
                error.get("message") or error.get("detail") or error.get("title") or ""
            )
        elif error:
            error_message = str(error)

        error_code = error_code or str(payload.get("code") or payload.get("status") or "")
        error_message = error_message or str(
            payload.get("message") or payload.get("detail") or payload.get("title") or ""
        )

    request_id = ""
    for header_name in ("x-request-id", "nvcf-reqid", "request-id", "trace-id"):
        request_id = _response_header(response, header_name)
        if request_id:
            break

    return {
        "event": "http_error",
        "response_at": _diagnostic_timestamp(),
        "status_code": int(getattr(response, "status_code", 0) or 0),
        "content_type": _response_header(response, "content-type"),
        "error_code": error_code[:500],
        "error_message": error_message[:1000],
        "request_id": request_id,
    }


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

    fenced = re.fullmatch(r"```(?:markdown|md|text|json)?\s*\n?(.*?)\n?```", text, flags=re.S | re.I)
    if fenced:
        text = fenced.group(1).strip()

    return text


def _failure(title: str, detail: str) -> str:
    return f"CAP 분석 실패\n\n{title}\n\n{detail}".strip()


LEGACY_CAP_REPORT_PROMPT = '당신은 수업·개발·문서 캡처 화면을 해석하는 이미지 분석 도우미입니다.\n\n이 모드의 목적은 화면의 모든 글자를 OCR처럼 그대로 옮기는 것이 아닙니다.\n이미지 전체를 직접 보고, 화면이 무엇을 보여주는지와 핵심 의미를 이해한 뒤\n사용자가 바로 복사해 학습 기록이나 메모에 붙여넣을 수 있는 간결한 Markdown으로 정리하세요.\n\n핵심 원칙:\n- 이미지 전체의 배치, 시각적 관계, 코드·표·그래프·화면 상태를 함께 해석하세요.\n- 화면의 목적과 핵심 내용을 우선 설명하세요.\n- 보이는 글자를 모두 전사하지 말고, 이해에 필요한 핵심 문구만 짧게 인용하세요.\n- 이미지에 없는 내용을 사실처럼 만들지 마세요.\n- 합리적인 해석은 가능하지만, 근거가 부족하면 "확인 필요"라고 표시하세요.\n- 분석 과정이나 장황한 사족은 출력하지 마세요.\n- 최종 결과는 바로 복사 가능한 한국어 Markdown만 반환하세요.\n\n화면 유형별 기준:\n\n1. 코드 화면\n- 코드가 무엇을 구현하는지 설명하세요.\n- 입력 → 처리 → 출력의 실행 흐름을 정리하세요.\n- 주요 클래스·함수·변수의 역할과 연결 관계를 설명하세요.\n- 화면에 오류가 있으면 오류 위치, 직접 원인, 수정 방향을 구분하세요.\n- 핵심 줄만 짧게 인용하고 전체 코드를 그대로 전사하지 마세요.\n- 보이지 않는 코드는 추측하지 마세요.\n\n2. 표·캘린더·인포그래픽\n- 표의 행·열, 단계, 순서, 범주 사이의 관계를 해석하세요.\n- 이 자료가 전달하려는 핵심 메시지를 설명하세요.\n- 모든 셀을 단순 나열하지 말고 중요한 패턴과 구조를 정리하세요.\n- 정확히 읽히지 않는 세부 글자는 "확인 필요"로 남기세요.\n\n3. 슬라이드·일반 문서\n- 주제, 핵심 주장, 설명 흐름을 요약하세요.\n- 제목 → 핵심 내용 → 중요한 근거 순서로 정리하세요.\n- 본문 전체를 그대로 베끼지 마세요.\n\n4. 실행 결과·콘솔·오류 화면\n- 어떤 작업의 결과인지 설명하세요.\n- 정상 결과인지 오류인지 구분하세요.\n- 오류이면 메시지, 원인 후보, 수정 방향을 나누세요.\n- 원인 후보는 확정 사실처럼 단정하지 마세요.\n\n5. 웹·앱 화면\n- 사용자가 보고 있는 기능과 현재 상태를 설명하세요.\n- 주요 버튼, 입력, 결과 영역이 어떻게 연결되는지 정리하세요.\n- 단순한 UI 요소 나열보다 실제 사용 흐름을 설명하세요.\n\n6. 영상·사진·장면\n- 장면에서 보이는 상황과 핵심 맥락을 간단히 설명하세요.\n- 자막은 의미 이해에 필요한 부분만 짧게 반영하세요.\n- 등장인물의 신원이나 보이지 않는 사건을 추측하지 마세요.\n\n권장 출력 형식:\n\n## 화면 해석\n- 화면 유형:\n- 핵심 내용:\n- 화면에서 확인되는 근거:\n\n## 구조 또는 흐름\n- 이미지의 구성과 요소 사이의 관계를 설명\n\n## 학습·활용 포인트\n- 이 화면에서 이해하거나 기억할 내용\n\n## 확인 필요\n- 불명확하거나 이미지에서 확정할 수 없는 부분이 있을 때만 작성\n\n내용이 단순하면 위 형식을 억지로 모두 채우지 말고 더 짧게 작성하세요.\n최종 응답에는 해석 결과만 출력하세요.'

LEGACY_SHARED_CAP_PROMPT = """
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

DEFAULT_MANUAL_CAP_PROMPT = """
너는 강의, 개발 실습, 문서 화면 캡처 한 장을 초보자가 복습하기 쉬운 학습 메모로 정리하는 도우미다.

목표:
- 화면에 보이는 글자를 모두 옮기는 것이 아니라, 이 화면이 무엇을 설명하거나 수행하는지 이해시킨다.
- 원본 이미지에서 실제로 확인되는 내용을 가장 우선한다.
- 화면 밖 코드, 설정값, 버전, 실행 결과를 추측하여 만들어내지 않는다.
- 사용자가 이미지와 함께 복사해 학습 기록에 바로 사용할 수 있는 한국어 Markdown을 작성한다.

해석 순서:
1. 현재 화면의 구체적인 주제를 파악한다.
2. 핵심 개념, 기능 또는 문제 상황을 쉬운 말로 설명한다.
3. 처리 과정이 있으면 `입력 → 처리 → 결과` 순서로 연결한다.
4. 주요 코드, 함수, 설정 또는 화면 요소가 서로 어떻게 연결되는지 설명한다.
5. 초보자가 잘못 이해하기 쉬운 부분이나 실제 사용 시 주의할 점을 정리한다.
6. 화면의 핵심을 이해하는 데 필요하지 않은 UI 요소와 문구는 나열하지 않는다.

화면별 규칙:
- 코드 화면: 코드가 최종적으로 수행하는 일을 먼저 설명하고 실행 순서와 주요 클래스·함수·변수의 역할을 연결한다. 명확히 보이는 핵심 코드만 짧게 제시하며 잘린 코드는 완성하지 않는다.
- 오류 화면: `문제 → 직접 확인되는 원인 → 수정 위치 → 해결 방법 → 핵심` 순서로 설명한다. 오류 메시지 원문은 코드 블록에 보존하고 확인된 원인과 후보를 구분한다.
- 실행 결과 화면: 어떤 동작의 결과인지, 정상인지 오류인지, 앞선 코드나 설정과 어떻게 연결되는지 설명한다.
- 슬라이드·표·다이어그램: 문구를 전부 나열하지 않고 요소 사이의 관계와 핵심 흐름, 실제 의미의 장점과 한계를 설명한다.
- 웹·앱 화면: 버튼과 입력창을 나열하지 않고 사용자의 작업과 입력·결과의 연결을 설명한다.

출력 규칙:
- 첫 줄은 화면 내용을 대표하는 구체적인 `## 제목`으로 작성한다.
- 일반적으로 3~7개의 문장으로 정리한다.
- 복잡할 때만 `### 핵심 개념`, `### 실행 흐름`, `### 문제와 해결`, `### 주의할 점`, `### 핵심` 중 필요한 제목을 사용한다.
- `화면 해석`, `이미지 분석`, `관찰 내용`, `분석 결과`, `화면에서 확인되는 근거` 같은 보고서형 제목은 사용하지 않는다.
- 같은 내용을 제목, 본문, 핵심 문장에서 반복하지 않는다.
- 불확실한 정보가 실제로 있을 때만 마지막에 `### 확인 필요`를 추가한다.
- 분석 과정, 자기평가, JSON, 프롬프트 설명, 전체를 감싼 코드펜스는 출력하지 않는다.
- 최종 답변에는 사용자가 복사할 학습 메모만 출력한다.
""".strip()


DEFAULT_FLOW_INTERPRETATION_PROMPT = """
너는 현재 강의 캡처를 전체 수업 노트 안에 배치할 수 있는 하나의 학습 블록으로 정리한다.

입력에는 현재 캡처 순번·레코드 ID·원본 이미지·보조 OCR과, 직전 캡처의 존재 여부·제목·학습 요약·레코드 ID·그룹 ID가 제공된다.

목표:
1. 현재 캡처가 직전 내용의 연속인지 새로운 주제인지 판단한다.
2. 연속된 내용이면 앞에서 설명한 정의와 배경을 반복하지 않고 새롭게 추가되거나 변경된 내용만 설명한다.
3. 새로운 주제이면 이해에 필요한 최소한의 정의부터 설명한다.
4. 실행 결과나 오류 화면이면 어떤 앞선 작업에서 나온 결과인지 연결한다.
5. 원본 이미지에서 확인할 수 없는 코드, 설정, 수치 또는 실행 결과는 추측하지 않는다.

연속성 판단:
- 같은 클래스, 함수, 개념 또는 작업을 계속 설명하거나 앞 코드에 줄을 추가하고 실행 결과를 확인하면 `continues_previous`는 true다.
- 같은 오류를 수정하거나 수정 결과를 확인하는 화면도 연속일 수 있다.
- 주제가 전환되거나 별개의 개념을 시작하면 false다.
- 화면 배치나 프로그램이 같다는 이유만으로 연속으로 판단하지 않는다.
- 직전 정보가 없거나 의미가 불명확하면 false다.

본문 규칙:
- 화면 문구를 나열하지 않고 현재 화면에서 새로 배울 내용을 중심으로 보통 3~7문장으로 작성한다.
- 처리 흐름은 `입력 → 처리 → 결과`로 표현한다.
- 코드 화면은 역할과 실행 순서를, 오류 화면은 오류 원문과 문제·원인·수정 위치·해결 방법을 구분한다.
- `body_markdown` 안에는 첫 줄 제목이나 별도의 `## 제목`을 포함하지 않는다.
- 불확실한 내용은 본문에 섞지 말고 `review_required`에 넣는다.

반드시 다음 JSON 객체만 반환한다.
{
  "title": "현재 캡처를 대표하는 구체적인 제목",
  "continues_previous": true,
  "body_markdown": "이미지와 함께 표시할 학습 설명",
  "review_required": []
}

출력 제약:
- `title`에 `화면 해석`, `코드 설명`, `학습 내용`, `분석 결과` 같은 일반 제목을 사용하지 않는다.
- `review_required`는 문자열 배열이며 확인할 내용이 없으면 빈 배열이다.
- JSON 밖에 안내 문구, Markdown 코드펜스, 분석 과정, 자기평가를 출력하지 않는다.
""".strip()


# 기존 외부 import는 수동 CAP 기본값을 계속 가리킨다.
DEFAULT_CAP_PROMPT = DEFAULT_MANUAL_CAP_PROMPT


def _context_text(value, limit: int) -> str:
    return str(value or "").strip()[: max(0, int(limit))]


def build_flow_interpretation_prompt(
    ocr_text: str,
    *,
    capture_index: int | None = None,
    record_id: str = "",
    previous: dict | None = None,
) -> str:
    """Build the flow-only prompt from context available when the worker runs."""
    previous = previous if isinstance(previous, dict) else None
    previous_exists = bool(previous)
    return (
        DEFAULT_FLOW_INTERPRETATION_PROMPT
        + "\n\n--- 현재 캡처 정보 ---\n"
        + f"capture_index: {capture_index if capture_index is not None else ''}\n"
        + f"record_id: {_context_text(record_id, 200)}\n"
        + "보조 OCR:\n"
        + _context_text(ocr_text, 12000)
        + "\n\n--- 직전 캡처 정보 ---\n"
        + f"exists: {'true' if previous_exists else 'false'}\n"
        + f"record_id: {_context_text(previous.get('record_id') if previous else '', 200)}\n"
        + f"title: {_context_text(previous.get('title') if previous else '', 500)}\n"
        + f"group_id: {_context_text(previous.get('group_id') if previous else '', 200)}\n"
        + "summary:\n"
        + _context_text(previous.get("summary") if previous else "", 6000)
    )


_MARKDOWN_TITLE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_DUPLICATE_BODY_TITLE = re.compile(r"^\s{0,3}##(?!#)\s+(.+?)\s*$")


def _split_leading_markdown_title(text: str) -> tuple[str, str]:
    lines = str(text or "").strip().splitlines()
    if not lines:
        return "", ""
    match = _MARKDOWN_TITLE.match(lines[0])
    if not match:
        return "", "\n".join(lines).strip()
    return match.group(1).strip(), "\n".join(lines[1:]).strip()


def _strip_duplicate_body_title(text: str) -> str:
    lines = str(text or "").strip().splitlines()
    if lines and _DUPLICATE_BODY_TITLE.match(lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()


def parse_flow_interpretation_result(text: str) -> dict:
    """Validate flow JSON, with a conservative legacy-Markdown fallback."""
    cleaned = _clean_model_output(text)
    if not cleaned or cleaned.startswith(("CAP 분석 실패", "수업 흐름 해석 실패")):
        raise ValueError("수업 흐름 해석 응답이 비어 있거나 실패했습니다.")

    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        if cleaned.lstrip().startswith(("{", "[")):
            raise ValueError("수업 흐름 JSON을 해석하지 못했습니다.") from exc
        title, body = _split_leading_markdown_title(cleaned)
        body = body.strip()
        if not title or not body:
            raise ValueError("Markdown fallback에는 제목과 본문이 모두 필요합니다.")
        return {
            "title": title[:80],
            "continues_previous": False,
            "body_markdown": body,
            "review_required": [],
            "parse_fallback": True,
        }

    if not isinstance(value, dict):
        raise ValueError("수업 흐름 응답은 JSON 객체여야 합니다.")
    title = value.get("title")
    body = value.get("body_markdown")
    continues = value.get("continues_previous")
    review = value.get("review_required")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("수업 흐름 제목이 비어 있습니다.")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("수업 흐름 본문이 비어 있습니다.")
    if not isinstance(continues, bool):
        raise ValueError("continues_previous는 bool이어야 합니다.")
    if not isinstance(review, list) or any(not isinstance(item, str) for item in review):
        raise ValueError("review_required는 문자열 배열이어야 합니다.")
    normalized_title = title.strip().splitlines()[0].strip()
    title_heading = _MARKDOWN_TITLE.fullmatch(normalized_title)
    if title_heading:
        normalized_title = title_heading.group(1).strip()
    body_without_title = _strip_duplicate_body_title(body)
    if not body_without_title:
        raise ValueError("수업 흐름 본문이 비어 있습니다.")
    return {
        "title": normalized_title[:80],
        "continues_previous": continues,
        "body_markdown": body_without_title,
        "review_required": [item.strip() for item in review if item.strip()],
        "parse_fallback": False,
    }


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
    request_timeout = (connect_timeout, read_timeout)
    _write_diagnostic_event(
        _request_diagnostic(
            api_base,
            model,
            payload,
            data_url,
            request_timeout,
        )
    )
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
            timeout=request_timeout,
            on_retry=on_retry,
        )
    except requests.exceptions.Timeout:
        return _failure("CAP 이미지 추론 시간이 초과되었습니다.", "두 번째 요청도 제한 시간을 초과했습니다.")
    except requests.exceptions.ConnectionError:
        return _failure("CAP 이미지 추론 서버에 연결하지 못했습니다.", "두 번째 요청도 연결에 실패했습니다.")
    except requests.exceptions.RequestException as exc:
        return _failure("CAP 이미지 추론 요청에 실패했습니다.", type(exc).__name__)

    if response.status_code >= 400:
        _write_diagnostic_event(_http_error_diagnostic(response))

    if response.status_code in {401, 403}:
        return _failure(
            "NVIDIA API 인증에 실패했습니다.",
            f"HTTP {response.status_code}",
        )

    if response.status_code == 410:
        return _failure(
            "선택한 CAP 모델을 현재 NVIDIA API 키로 사용할 수 없습니다.",
            (
                "설정에서 현재 제공되는 모델로 변경하거나 NVIDIA 계정 권한을 확인하세요. "
                f"현재 추론 모델: {model}. HTTP 410"
            ),
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
