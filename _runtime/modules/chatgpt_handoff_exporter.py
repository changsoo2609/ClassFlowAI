import base64
import html
import json
import mimetypes
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path


DEFAULT_PROMPT_TEMPLATE = '''아래 ZIP은 ClassFlowAI로 시간순 수집한 수업 캡처 패키지입니다.

목표는 캡처를 한 장씩 설명하는 것이 아니라, 수업의 실제 학습 흐름을 복원하여 Notion에 붙여넣을 수 있는 회고록과 복사 도구를 만드는 것입니다.

## 1. 입력 자료와 우선순위

가능한 입력 자료:
- `images/`: 캡처 이미지 원본
- `CAPTURE_TIMELINE.md`: 시간순 캡처 기록
- `OCR_TIMELINE.md`: OCR·CAP 보조 결과
- 기타 안내 문서

판단 우선순위:
1. 이미지 원본
2. 시간순 기록
3. OCR·CAP 등 텍스트 보조 자료
4. 파일명과 메타데이터

필수 원칙:
- 이미지와 OCR이 충돌하면 이미지를 우선합니다.
- OCR과 CAP 결과를 정답으로 간주하지 않습니다.
- 이미지에 보이지 않는 코드, 설명, 결과를 추측하지 않습니다.
- 판독할 수 없는 부분은 `이미지에서 정확한 확인 필요`로 표시합니다.
- 존재하지 않는 입력 파일을 있다고 가정하지 않습니다.

## 2. 수업 흐름 복원

전체 캡처를 시간순으로 검토하여 다음 흐름을 찾으세요.

- 개념 설명 → 코드 작성 → 실행 → 결과 확인
- 오류 발생 → 원인 확인 → 수정 → 재실행
- 이전 코드에서 추가되거나 변경된 부분
- 같은 내용을 연속해서 보여주는 캡처
- 학습 내용이 거의 없는 화면 이동이나 중복 캡처

같은 내용을 보여주는 연속 캡처는 하나의 학습 단계로 통합하세요.

각 단계에는 근거 이미지 파일명을 표시하되, 이미지 파일명만 나열하지 말고 그 이미지가 어떤 학습 과정을 보여주는지 설명하세요.

## 3. 화면 해석 기준

### 개념 화면
- 무엇을 배우는 내용인지
- 왜 필요한지
- 다른 개념과 어떤 관계인지
- 초보자가 헷갈리기 쉬운 부분

### 코드 화면
- 이미지에서 실제로 확인되는 코드만 설명
- 코드 블록의 역할
- 입력 → 처리 → 출력 흐름
- 주요 함수·변수·클래스의 관계
- 이전 캡처와 비교해 변경된 부분

전체 코드를 길게 전사하지 말고 학습에 필요한 핵심 코드만 짧게 인용하세요.

### 실행 결과와 오류 화면
- 어떤 코드의 실행 결과인지
- 정상 실행인지 오류인지
- 화면에서 확인되는 오류 메시지와 위치
- 이미지로 확인 가능한 직접 원인
- 가능한 원인 후보와 확인이 필요한 부분
- 수정 이후 결과

원인 후보는 확정 사실처럼 표현하지 않습니다.

### 파일 목록과 단순 화면
- 수업 흐름에 필요한 파일만 언급
- 단순 이동이나 중복 화면은 독립된 학습 단계로 만들지 않음
- 확장자나 파일명이 불명확하면 이미지 확인 필요로 표시

## 4. 최종 문서

다음 구조로 `notion_ready.md`와 동일한 내용의 `notion_ready.html`을 작성하세요.

# ClassFlowAI 수업 정리

## 오늘 수업 한눈에 보기
- 주요 주제
- 전체 진행 흐름
- 최종적으로 구현하거나 확인한 결과
- 남은 확인 사항

## 수업 흐름

### 1. 학습 단계 제목
- 관련 이미지:
- 화면 유형:
- 배운 내용:
- 코드 또는 화면의 역할:
- 이전·다음 단계와의 연결:

필요한 경우에만 추가:
- 실행 결과:
- 오류와 수정:
- 헷갈리기 쉬운 부분:
- 확인 필요:

## 핵심 코드와 개념

### 개념 또는 코드 이름
- 역할:
- 사용된 위치:
- 동작 흐름:
- 기억할 점:

## 오류 및 문제 해결
실제 오류와 수정 과정이 있을 때만 작성합니다.

### 문제 제목
- 관련 이미지:
- 문제:
- 원인:
- 해결:
- 결과:
- 핵심:

## 오늘 복습할 것
- 우선 복습 항목 3~5개
- 직접 다시 작성해볼 코드
- 추가 확인할 개념

## 한 줄 정리
- 오늘 수업의 핵심을 한 문장으로 정리

빈 항목을 형식적으로 채우지 말고, 실제 캡처에서 확인되는 내용만 작성하세요.

## 5. Notion용 HTML

`notion_ready.html`은 웹페이지 디자인이 아니라 Notion 붙여넣기용 단순 HTML이어야 합니다.

주요 태그:
- `h1`, `h2`, `h3`
- `p`
- `ul`, `ol`, `li`
- `strong`, `em`
- `blockquote`
- `pre`, `code`
- `hr`, `br`

규칙:
- JavaScript, 외부 CSS, 외부 폰트 사용 금지
- 복잡한 `div`, `flex`, `grid`, `position` 사용 금지
- 코드에는 `pre`와 `code` 사용
- 제목 단계는 `h1 → h2 → h3` 유지
- 각 학습 단계에 관련 이미지 파일명 표시
- 이미지가 없어도 문서 내용을 이해할 수 있게 작성

## 6. 클립보드 복사 도구

다음 파일을 만드세요.
- `COPY_TO_CLIPBOARD.bat`
- `copy_to_clipboard.py`

`copy_to_clipboard.py` 요구사항:
- Windows `CF_HTML` 형식으로 `notion_ready.html` 복사
- `StartHTML`, `EndHTML`, `StartFragment`, `EndFragment`를 UTF-8 바이트 기준으로 정확히 계산
- HTML에 `<!--StartFragment-->`, `<!--EndFragment-->` 포함
- HTML 복사 실패 시 `notion_ready.md`를 Unicode 텍스트로 복사
- 성공 또는 실패 이유를 콘솔에 출력

`COPY_TO_CLIPBOARD.bat` 실행 후 사용자가 Notion에서 `Ctrl+V`만 하면 되게 구성하세요.

## 7. 최종 산출물

최종 결과는 다음 구조의 `notion_paste_package.zip`으로 만드세요.

notion_paste_package.zip
├─ COPY_TO_CLIPBOARD.bat
├─ copy_to_clipboard.py
├─ notion_ready.html
├─ notion_ready.md
└─ images/

최종 점검:
- 이미지 기준으로 수업 흐름을 복원했는가
- 중복 캡처를 반복 설명하지 않았는가
- 추측한 코드나 결과가 없는가
- Markdown과 HTML 내용이 일치하는가
- 이미지 없이도 문서를 이해할 수 있는가
- CF_HTML 위치값이 UTF-8 바이트 기준으로 계산되는가'''

CAPTURE_FIRST_GUIDE = '''# CAPTURE_FIRST_GUIDE

이 ZIP은 수업 캡처 원본과 시간순 기록을 전달하기 위한 패키지입니다.

## 판단 순서
1. `images/`의 이미지 원본
2. `CAPTURE_TIMELINE.md`
3. `OCR_TIMELINE.md`
4. 파일명과 캡처 시간

## 원칙
- 이미지와 텍스트가 충돌하면 이미지를 우선합니다.
- OCR·CAP 결과는 보조 자료입니다.
- 보이지 않는 코드나 결과는 추측하지 않습니다.
- 연속된 중복 캡처는 하나의 학습 단계로 묶습니다.
- 오류는 메시지, 원인 후보, 수정, 결과를 구분합니다.
- 최종 결과는 Notion용 HTML과 Markdown으로 작성합니다.'''

def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", str(name or "").strip())
    name = re.sub(r"\s+", "_", name)
    return name[:80] or "ClassFlowAI"


def _active_records(records: list[dict]) -> list[dict]:
    return [r for r in records if not r.get("deleted") and Path(str(r.get("image_path") or "")).exists()]


def _clean_text(text: str) -> str:
    lines = []
    for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def build_capture_timeline_markdown(records: list[dict], image_dir_name: str = "images") -> str:
    active = _active_records(records)
    lines = [
        "# ClassFlowAI 스크린샷 타임라인",
        "",
        "아래 순서대로 수업 스크린샷을 확인하세요.",
        "",
        "---",
        "",
    ]

    if not active:
        return "# ClassFlowAI 스크린샷 타임라인\n\n기록 없음\n"

    for idx, record in enumerate(active, 1):
        created_at = str(record.get("created_at") or "시간 확인 필요")
        image_path = Path(str(record.get("image_path") or ""))
        image_name = image_path.name if image_path.name else f"capture_{idx:03d}.png"
        lines += [
            f"## {idx}. {created_at}",
            "",
            f"- 이미지 파일: {image_dir_name}/{image_name}",
            "",
            f"![capture_{idx}]({image_dir_name}/{image_name})" if image_path.name else "[이미지 없음]",
            "",
            "---",
            "",
        ]

    return "\n".join(lines)


def build_ocr_timeline_markdown(records: list[dict]) -> str:
    active = _active_records(records)
    lines = [
        "# OCR_AND_CAP_TIMELINE",
        "",
        "이미지 원본이 최우선이며 아래 텍스트는 보조 자료입니다.",
        "",
        "---",
        "",
    ]

    has_any = False
    for idx, record in enumerate(active, 1):
        ocr_text = _clean_text(record.get("ocr_corrected_text") or record.get("ocr_text") or "")
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



def _image_data_uri(path: Path) -> str:
    if not path.exists():
        return ""
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def build_preview_html(records: list[dict]) -> str:
    active = _active_records(records)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    parts = [f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>ClassFlowAI HTML 흐름</title>
<style>
body {{ font-family: "Malgun Gothic", Arial, sans-serif; max-width: 1040px; margin: 32px auto; line-height: 1.65; color: #222; background: #fafafa; }}
.wrap {{ background: #fff; border: 1px solid #ddd; border-radius: 14px; padding: 24px; }}
h1 {{ font-size: 28px; margin-top: 0; border-bottom: 2px solid #222; padding-bottom: 10px; }}
h2 {{ font-size: 20px; margin-top: 28px; border-left: 5px solid #222; padding-left: 10px; }}
.box {{ border: 1px solid #ddd; border-radius: 12px; padding: 16px; margin: 14px 0; background: #fff; }}
.flow {{ background: #f4f6f8; }}
pre {{ white-space: pre-wrap; background: #f7f7f7; border: 1px solid #ddd; border-radius: 10px; padding: 14px; }}
.capture {{ border: 1px solid #ddd; border-radius: 12px; padding: 16px; margin: 22px 0; background: #fff; }}
img {{ max-width: 100%; border: 1px solid #ddd; border-radius: 8px; }}
.meta {{ color: #666; font-size: 13px; }}
ul {{ margin-top: 8px; }}
</style>
</head>
<body>
<div class="wrap">
<h1>ClassFlowAI HTML 흐름</h1>
<p class="meta">생성 시각: {html.escape(now)}</p>

<div class="box flow">
<h2>1. 작업 흐름</h2>
<ul>
<li>캡처 파일 저장</li>
<li>GPT ZIP파일 생성</li>
<li>ZIP을 ChatGPT에 업로드</li>
<li>자동 복사된 프롬프트 붙여넣기</li>
<li>Notion용 HTML/Markdown 결과 생성</li>
</ul>
</div>

<div class="box">
<h2>2. GPT ZIP 생성 기준</h2>
<p>GPT ZIP은 현재 저장된 캡처 파일을 기준으로 생성됩니다.</p>
<ul>
<li><b>images/</b>: 캡처 원본</li>
<li><b>CAPTURE_TIMELINE.md</b>: 캡처 순서</li>
<li><b>OCR_TIMELINE.md</b>: NVIDIA OCR 보조 텍스트(API 키가 있을 경우)</li>
<li><b>PROMPT_FOR_CHATGPT.txt</b>: GPT에게 보낼 지시문</li>
<li><b>html_flow_preview.html</b>: 이 HTML 흐름 미리보기</li>
</ul>
</div>

<div class="box">
<h2>3. GPT 결과물 양식</h2>
<pre># ClassFlowAI 수업 캡처 정리

## 전체 요약
- 수업 흐름을 3~5줄로 정리

## 캡처별 정리

### 1. 캡처 제목
- 이미지 파일:
- 이 화면에서 배운 핵심:
- 코드/화면 흐름 설명:
- 헷갈리기 쉬운 부분:
- 기억할 키워드:

## 오늘 복습할 것
- 다시 볼 개념 3개

## 한 줄 정리
- 오늘 내용을 한 문장으로 정리</pre>
</div>

<h2>4. 캡처 목록</h2>
"""]

    if not active:
        parts.append('<div class="box"><p>아직 캡처 기록이 없습니다.</p></div>')

    for idx, record in enumerate(active, 1):
        created_at = str(record.get("created_at") or "시간 확인 필요")
        image_path = Path(str(record.get("image_path") or ""))
        parts.append('<div class="capture">')
        parts.append(f"<h3>{idx}. {html.escape(created_at)}</h3>")
        parts.append(f'<p class="meta">이미지: {html.escape(image_path.name)}</p>')
        uri = _image_data_uri(image_path)
        if uri:
            parts.append(f'<p><img src="{uri}" alt="capture_{idx:03d}"></p>')
        else:
            parts.append("<p>[이미지 없음]</p>")
        parts.append("</div>")

    parts.append("</div></body></html>")
    return "\n".join(parts)


def export_preview_html(records: list[dict], out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_preview_html(records), encoding="utf-8")
    return out_path


def build_chatgpt_prompt(subject: str = "", prompt_template: str = "") -> str:
    template = (prompt_template or "").strip() or DEFAULT_PROMPT_TEMPLATE
    subject = (subject or "").strip()
    if subject:
        template += f"\n\n과목/주제 힌트: {subject}\n"
    return template


def export_chatgpt_handoff_zip(
    records: list[dict],
    out_dir: Path,
    subject: str = "",
    prompt_template: str = "",
) -> tuple[Path, str]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    base_name = _safe_filename(f"ClassFlowAI_GPT_ZIP_{now.strftime('%Y%m%d_%H%M%S')}")
    build_dir = out_dir / base_name
    if build_dir.exists():
        shutil.rmtree(build_dir)

    images_dir = build_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    active = _active_records(records)
    timeline_records = []
    for idx, record in enumerate(active, 1):
        copied = dict(record)
        image_path = Path(str(record.get("image_path") or ""))
        if image_path.exists() and image_path.is_file():
            suffix = image_path.suffix or ".png"
            dest = images_dir / f"capture_{idx:03d}{suffix}"
            shutil.copy2(image_path, dest)
            copied["image_path"] = str(dest)
            timeline_records.append(copied)

    timeline_md = build_capture_timeline_markdown(timeline_records, image_dir_name="images")
    prompt_text = build_chatgpt_prompt(subject=subject, prompt_template=prompt_template)

    (build_dir / "CAPTURE_TIMELINE.md").write_text(timeline_md, encoding="utf-8")
    if any(_clean_text(r.get("ocr_text") or "") or str(r.get("cap_text") or "").strip() for r in timeline_records):
        (build_dir / "OCR_TIMELINE.md").write_text(build_ocr_timeline_markdown(timeline_records), encoding="utf-8")
    (build_dir / "PROMPT_FOR_CHATGPT.txt").write_text(prompt_text, encoding="utf-8")
    (build_dir / "CAPTURE_FIRST_GUIDE.md").write_text(CAPTURE_FIRST_GUIDE, encoding="utf-8")
    (build_dir / "html_flow_preview.html").write_text(build_preview_html(timeline_records), encoding="utf-8")
    (build_dir / "README.txt").write_text(
        "1. ChatGPT 새 대화에 이 ZIP을 업로드하세요.\n"
        "2. PROMPT_FOR_CHATGPT.txt 내용은 앱에서 자동으로 클립보드에 복사됩니다.\n"
        "3. 새 세션에서 다시 복사해야 하면 COPY_PROMPT_TO_CLIPBOARD.bat을 실행하세요.\n"
        "4. html_flow_preview.html은 GPT 전달 전 스크린샷 순서 확인용입니다.\n"
        "5. OCR_TIMELINE.md가 있으면 NVIDIA OCR 보조 텍스트입니다.\n",
        encoding="utf-8"
    )

    bat = """@echo off
chcp 65001 >nul
title Copy ChatGPT Prompt

set "PROMPT_FILE=%~dp0PROMPT_FOR_CHATGPT.txt"

if not exist "%PROMPT_FILE%" (
    echo [ERROR] PROMPT_FOR_CHATGPT.txt 파일을 찾을 수 없습니다.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -Raw -Encoding UTF8 '%PROMPT_FILE%' | Set-Clipboard"

echo.
echo [OK] ChatGPT 지시 프롬프트를 클립보드에 복사했습니다.
echo 이제 ChatGPT 새 대화에 ZIP 파일을 업로드하고, 입력창에 Ctrl+V 하세요.
echo.
pause
exit /b 0
"""
    (build_dir / "COPY_PROMPT_TO_CLIPBOARD.bat").write_text(bat, encoding="utf-8")

    zip_path = out_dir / f"{base_name}.zip"
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in build_dir.rglob("*"):
            z.write(f, f.relative_to(build_dir))

    shutil.rmtree(build_dir, ignore_errors=True)
    return zip_path, prompt_text
