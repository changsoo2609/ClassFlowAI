# ClassFlowAI 개발 및 배포

## 지원 환경

- Windows 10 또는 11
- Python 3.10~3.12
- Tkinter가 포함된 표준 Python 설치
- NVIDIA API를 사용할 수 있는 네트워크 연결

## 배포본으로 처음 실행

1. 배포 ZIP을 원하는 폴더에 압축 해제한다.
2. 루트의 `INSTALL_FIRST.bat`을 실행한다.
3. 설치 완료 후 `START_HERE.bat`을 실행한다.
4. CMD 진행 표시가 실행 완료가 될 때까지 기다린다.
5. 프로그램 설정에서 개인 NVIDIA API 키와 저장 위치를 입력한다.

API 키가 저장되는 `_runtime/config.json`을 공유하거나 Git에 추가하지 않는다.

## 개발 환경 준비

프로젝트 루트에서 Windows PowerShell 또는 CMD를 사용한다.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r _runtime\requirements.txt
```

Tkinter는 pip 패키지가 아니므로 Python 설치 단계에서 포함되어 있어야 한다.

## 개발 실행

배포 실행 흐름을 검증할 때는 다음 파일을 사용한다.

```text
START_HERE.bat
```

콘솔 오류를 직접 확인하는 개발 실행은 프로젝트 루트에서 다음과 같이 수행한다.

```powershell
cd _runtime
..\.venv\Scripts\python.exe app.py
```

가상환경을 사용하지 않는 경우 설치된 Python 명령으로 대체할 수 있다.

```powershell
cd _runtime
py -3.12 app.py
```

## 정적 검사

### 전체 Python 컴파일

프로젝트 루트에서 실행한다.

```powershell
py -3.12 -m compileall -q _runtime
```

성공 시 출력 없이 종료 코드 `0`을 반환한다.

### `app.py` import 검사

UI를 실행하지 않고 모듈 import만 확인한다.

```powershell
cd _runtime
py -3.12 -c "import app; print('IMPORT_OK')"
```

성공 시 다음 문구가 출력된다.

```text
IMPORT_OK
```

### 필수 패키지 검사

```powershell
py -3.12 -c "import PIL, pynput, requests, tkinter; print('DEPENDENCIES_OK')"
```

### 모델 오류·재시도 회귀 검사

실제 API 키나 네트워크를 사용하지 않는다.

```powershell
cd _runtime
py -3.12 -m unittest discover -s tests -p "test_model_connections.py" -v
```

실제 OCR·CAP 기본 모델 연결 결과는 `MODEL_CONNECTION_TEST.md`에 기록한다.

### 학습카드 검증기 검사

모듈 import와 전체 표준 `unittest`를 실행한다.

```powershell
cd _runtime
py -3.12 -c "import modules.study_card_validator; print('STUDY_CARD_VALIDATOR_IMPORT_OK')"
py -3.12 -m unittest discover -s tests -p "test_*.py" -v
```

ChatGPT 결과물은 다음과 같이 검증한다.

```powershell
cd _runtime
py -3.12 validate_study_cards.py "study_cards.json" --images-dir "images"
```

- 오류가 없으면 종료 코드 `0`
- 구조 오류, JSON 파싱 실패 또는 파일 읽기 실패는 종료 코드 `1`
- 경고와 중복 카드는 보고하지만 원본 JSON을 수정하지 않음

## 변경 후 기본 검증 순서

1. 전체 Python 컴파일
2. `app.py` import
3. `START_HERE.bat` 실행과 준비 완료 확인
4. 메인 창과 미니 상태창 표시 확인
5. 단축키 일시정지·모드 전환·창 전환 확인
6. OCR 캡처 후 결과 표시와 자동 복사 확인
7. OCR 보정과 내용 해석 확인
8. CAP 캡처 후 원본 이미지 클립보드 유지 확인
9. CAP 결과 수동 복사 확인
10. GPT 전달 ZIP 생성과 내부 구조 확인
11. 전체 `unittest`와 학습카드 CLI 정상·오류 종료 코드 확인

API 호출이 필요한 검사는 테스트용 개인 키로 수행하고 키와 사용자 설정 파일을 커밋하지 않는다.

## 사용자 데이터와 Git

다음 파일과 폴더는 커밋하지 않는다.

- `_runtime/config.json`
- `APP_STARTED.flag`
- `STARTUP_ERROR.log`
- `ClassFlowAI_startup.log`
- `__pycache__/`와 `*.pyc`
- 사용자 캡처 이미지 폴더
- 사용자 기록 JSON과 이벤트 기록
- 수업별 `lessons/` 폴더와 `.classflow_current_lesson.json` 포인터
- 생성된 GPT 전달 ZIP 및 배포 ZIP

커밋 전 확인:

```powershell
git status --short
git check-ignore -v _runtime/config.json
```

## 배포 ZIP 구성

사용자에게 전달하는 ZIP 루트에는 다음 세 파일만 보이도록 한다.

```text
ClassFlowAI/
├─ START_HERE.bat
├─ INSTALL_FIRST.bat
├─ README.txt
└─ _runtime/
```

`_runtime/`에는 실행 코드, 모듈, requirements와 PowerShell 실행·설치 스크립트가 들어간다. 다음 항목은 배포 ZIP에 포함하지 않는다.

- 실제 API 키가 저장된 설정
- 사용자 캡처와 기록
- 런타임 플래그와 로그
- Python 캐시
- 개발용 가상환경과 Git 메타데이터

현재 배포본은 `config.json`에 기본값을 포함하고 있으므로 배포 생성 전 API 키와 개인 경로가 비어 있는지 반드시 확인한다. Git에서는 사용자 설정 파일 전체를 제외한다.

## Baseline 커밋 후보 절차

검사가 모두 통과한 뒤 프로젝트 루트에서 실행한다.

```powershell
git init
git add .
git status --short
git commit -m "chore: establish ClassFlowAI baseline"
```

커밋 전에 `config.json`, 로그, 캡처, 기록 JSON과 ZIP이 staged 목록에 없는지 확인한다.
