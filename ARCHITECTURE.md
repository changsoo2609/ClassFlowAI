# ClassFlowAI 아키텍처

## 목적

ClassFlowAI는 Windows 화면 캡처를 시간순으로 저장하고, OCR 또는 이미지 추론 결과를 기록한 뒤 ChatGPT 전달용 ZIP으로 내보내는 Tkinter 데스크톱 프로그램이다.

현재 구조는 `app.py`가 프로그램 전체 흐름을 조정하고, `_runtime/modules/`의 기능 모듈을 호출하는 형태다. 이 문서는 현재 기준 코드를 설명하며 구조 변경을 제안하거나 반영하지 않는다.

## 배포 구조

```text
ClassFlowAI/
├─ START_HERE.bat
├─ INSTALL_FIRST.bat
├─ README.txt
└─ _runtime/
   ├─ app.py
   ├─ config.json               # 사용자별 설정, Git 제외
   ├─ requirements.txt
   ├─ start_classflow.ps1
   ├─ install_classflow.ps1
   └─ modules/
      ├─ clipboard_watcher.py
      ├─ storage.py
      ├─ ocr_engine.py
      ├─ nvidia_cap_reasoner.py
      └─ chatgpt_handoff_exporter.py
```

## 실행 흐름

1. 사용자가 `START_HERE.bat`을 실행한다.
2. 배치 파일이 `_runtime/start_classflow.ps1`을 호출한다.
3. PowerShell 실행기가 Python 3.10~3.12, Tkinter와 필수 패키지를 확인한다.
4. `pythonw.exe`로 `_runtime/app.py`를 실행한다.
5. `app.py`가 설정을 읽고 작업 폴더 및 UI를 초기화한다.
6. 클립보드 감시 스레드와 전역 단축키 리스너가 시작된다.
7. 준비가 끝나면 `APP_STARTED.flag`가 생성되어 실행기가 시작 성공을 확인한다.
8. 시작 중 예외가 발생하면 `STARTUP_ERROR.log`에 기록된다.

최초 설치 시에는 `INSTALL_FIRST.bat`이 `_runtime/install_classflow.ps1`을 호출한다. 설치 스크립트는 사용 가능한 Python을 찾거나 Python 3.12 설치를 안내하고, `requirements.txt`의 패키지를 설치한 뒤 import를 검증한다.

## `app.py`의 책임

`ClassFlowAIApp`은 현재 애플리케이션 조정자이자 UI 컨트롤러다.

- 설정 기본값 구성, 로드 및 저장
- Tkinter 메인 UI, 설정창, 결과 영역, 미니 상태창 구성
- 새 수업 생성, 이전 수업 선택과 현재 수업 전환
- OCR/CAP 모드와 실행 상태 관리
- 클립보드 이미지 감시 및 새 캡처 등록
- 시간순 캡처 레코드 로드·저장·탐색·삭제·초기화
- OCR 실행, 보정, 내용 해석과 결과 복사 흐름 조정
- CAP 이미지 추론과 수동 결과 복사 흐름 조정
- 전역 키보드·마우스 단축키 감지
- Windows 캡처 도구 실행 및 창 표시 상태 관리
- HTML 흐름 미리보기, 캡처 폴더 열기, GPT 전달 ZIP 생성
- 실행 시간과 상태 메시지 갱신
- 정상 시작 플래그 및 시작 오류 기록

`app.py`는 외부 API 요청의 세부 구현이나 ZIP 파일 조립 자체를 직접 수행하기보다 해당 모듈을 호출하고 UI에 결과를 반영한다.

## 모듈별 책임

### `modules/clipboard_watcher.py`

- Windows 클립보드에서 이미지 읽기
- 이미지 해시 계산으로 중복 판별 지원
- 캡처 이미지를 지정 경로에 저장

### `modules/storage.py`

- 기본 작업 폴더 결정
- 캡처·출력·기록용 폴더 구성 보장
- 시간 기반 파일명과 표시 시간 생성
- JSON Lines 형식 이벤트 기록 추가
- 알려진 불필요 산출물 정리

### `modules/ocr_engine.py`

- OCR용 이미지 크기 조정과 전처리
- NVIDIA OCR API 요청 데이터 구성 및 호출
- API 응답에서 텍스트 추출
- OCR 결과 후처리와 사용자용 오류 메시지 반환
- 설정 또는 환경변수에서 API 키 확인

### `modules/nvidia_cap_reasoner.py`

- CAP 이미지 해석 프롬프트 구성
- 원본 이미지 기반 CAP 추론 API 호출
- 원본 이미지와 기존 OCR을 이용한 OCR 보정
- 모델 응답 정리와 사용자용 오류 메시지 반환
- 설정 또는 환경변수에서 API 키 확인

### `modules/chatgpt_handoff_exporter.py`

- 활성 캡처의 시간순 Markdown 생성
- OCR/CAP 보조 결과 타임라인 생성
- 이미지가 포함된 HTML 미리보기 생성
- 이미지와 안내 문서를 모아 ChatGPT 전달용 ZIP 생성
- 파일명 정규화와 내보내기용 임시 구조 관리

## 주요 데이터 흐름

### OCR 모드

```text
Windows 캡처 도구
→ 클립보드 이미지 감지
→ 원본 이미지 저장 및 레코드 생성
→ ocr_engine.extract_text_from_image
→ OCR 결과 기록 및 화면 표시
→ 텍스트 자동 복사
→ 필요 시 nvidia_cap_reasoner.correct_ocr_with_image
→ 보정 결과 기록 및 자동 복사
→ 필요 시 이미지와 보정 OCR 내용 해석
```

### CAP 모드

```text
Windows 캡처 도구
→ 클립보드 이미지 감지
→ 원본 이미지 저장 및 레코드 생성
→ nvidia_cap_reasoner.analyze_capture_image
→ CAP 결과 기록 및 화면 표시
→ 원본 이미지 클립보드 유지
→ 사용자가 요청할 때만 해석 텍스트 복사
```

### GPT 전달 ZIP

```text
활성 시간순 레코드와 원본 이미지
→ OCR/CAP 보조 타임라인 재구성
→ 이미지 우선 해석 안내와 프롬프트 추가
→ chatgpt_handoff_exporter.export_chatgpt_handoff_zip
→ 전달용 ZIP 생성
```

## 설정과 사용자 데이터

- `_runtime/config.json`: API 키, 모델, 프롬프트, 단축키, 저장 위치 등 사용자 설정
- 저장 위치의 `.classflow_current_lesson.json`: 해당 저장 위치에서 마지막으로 연 수업 폴더
- 저장 위치의 `lessons/lesson_YYYY-MM-DD_HH-MM-SS/`: 새 수업별 독립 작업 폴더
- 수업 폴더의 `state/lesson.json`: 새 수업 식별 정보
- 작업 폴더의 캡처 이미지: 수업 중 생성된 사용자 자료
- 작업 폴더의 JSON 기록: 캡처 순서, 처리 결과와 상태
- 런타임 플래그와 로그: 시작 성공 또는 실패 확인용 파일

위 파일은 소스가 아니며 사용자별 값이나 실행 결과를 포함하므로 Git에서 제외한다.

기존 버전에서 저장 위치 바로 아래에 `captures`, `state`, `outputs`, `logs`가 생성된 경우에는 해당 저장 위치 자체를 기존 수업으로 계속 사용한다. 새 수업부터는 `lessons/` 아래에 독립 폴더를 만들며, 수업을 전환해도 이전 수업의 기록과 이미지는 이동하거나 삭제하지 않는다. OCR/CAP 처리가 진행 중일 때는 완료 콜백이 다른 수업에 저장되는 일을 막기 위해 수업 전환을 허용하지 않는다.

## 향후 분리 후보

대규모 리팩터링 없이 안정성을 확보한 뒤 다음 순서로 검토하는 것이 적절하다.

1. `config_manager.py`: 기본 설정, 스키마 보정, 읽기와 원자적 저장
2. `record_manager.py`: 레코드 로드·저장·탐색·삭제·수업 초기화
3. `hotkey_manager.py`: 키·마우스 토큰 정규화와 전역 리스너
4. `capture_controller.py`: 캡처 도구 실행, 클립보드 감지, 이미지 등록
5. `processing_controller.py`: OCR·보정·해석·CAP 비동기 작업 상태 관리
6. `ui/` 패키지: 메인 창, 설정창, 미니 상태창, 결과 패널

우선순위는 설정과 레코드 저장 로직이다. 데이터 손실 위험을 테스트로 고정한 뒤 UI 분리를 진행해야 한다.
