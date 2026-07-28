# ClassFlowAI

ClassFlowAI는 Windows 화면을 캡처해 OCR 또는 이미지 해석으로 처리하고,
수업별 캡처와 학습 흐름을 관리하는 Tkinter 데스크톱 프로그램입니다.

## 현재 기본 모델

- OCR: `nvidia/nemotron-ocr-v2`
- CAP 이미지 해석 및 OCR 수업 흐름 해석: `google/diffusiongemma-26b-a4b-it`

API 요청은 Google Cloud가 아닌 NVIDIA NIM endpoint로 전송됩니다.
NVIDIA 개발용 무료 endpoint는 체험 크레딧과 호출 제한이 있으며 영구·무제한 무료 서비스는 아닙니다.

## 처음 실행

1. `INSTALL_FIRST.bat`을 실행합니다.
2. 설치가 끝나면 `START_HERE.bat`을 실행합니다.
3. 설정에서 NVIDIA API 키를 입력합니다.

기본 단축키:

- `Ctrl + Shift + S`: 화면 캡처
- 휠클릭: OCR/CAP 모드 전환
- `Ctrl + 휠클릭`: 감지 일시정지·재개
- `Shift + 휠클릭`: 메인 창 표시·최소화

## 정확한 OCR 복사

OCR 모드에서는 중간 원문을 결과창에 즉시 표시하되 클립보드에 바로 복사하지 않습니다.

1. 빠른 OCR 원문 추출
2. 원본 이미지와 OCR 원문을 함께 사용한 자동 보정
3. 보정된 최종 텍스트만 클립보드에 한 번 복사

보정에 실패하면 부정확할 수 있는 원문을 자동 복사하지 않습니다.
결과창의 `다시 시도` 또는 `OCR 원문 복사`를 사용자가 직접 선택할 수 있습니다.

## CAP 모드

- 캡처 원본 이미지의 클립보드 상태를 유지합니다.
- CAP 해석 텍스트는 자동 복사하지 않습니다.
- 필요한 경우 `CAP 해석 복사`를 사용합니다.
- 저장된 OCR/CAP 캡처의 원본 이미지를 우클릭 메뉴에서 다시 복사할 수 있습니다.

## 수업 기록

- 수업별 캡처 이미지와 처리 결과 저장
- 사용자 학습 흐름에 맞춘 캡처 순서 조정
- 원본 파일명·촬영 시각·캡처 ID와 별도의 정렬 순서 보존
- `결과 다시 수정`으로 원본 이미지를 재분석해 OCR/CAP 최신 결과 자동 교정
- 재분석 실패 시 기존 정상 결과 유지, 최초 결과와 직전 성공 결과 내부 보존
- 재분석 후 `직전 결과 복원`으로 바로 이전 정상 결과를 모달 없이 복원
- GPT 전달용 ZIP, 수업 흐름 문서와 학습 카드 자료 생성

## 사용자 데이터와 보안

사용자 설정과 API 키는 저장소가 아닌 다음 위치에 저장됩니다.

- 설정: `%LOCALAPPDATA%\ClassFlowAI\settings.json`
- API 키: `%LOCALAPPDATA%\ClassFlowAI\secrets.json`
- 모델 요청 진단: `%LOCALAPPDATA%\ClassFlowAI\model_request_diagnostics.jsonl`

API 키, Authorization 값, base64 이미지와 전체 모델 응답은 진단 로그에 기록하지 않습니다.
사용자 캡처 이미지, 수업 기록, 설정, 로그와 생성 ZIP은 Git에서 제외됩니다.

## 문서

- 자세한 Windows 사용 안내: [README.txt](README.txt)
- 기능 목록: [FEATURES.md](FEATURES.md)
- 개발 및 검증 안내: [DEVELOPMENT.md](DEVELOPMENT.md)
- 코드 구조: [ARCHITECTURE.md](ARCHITECTURE.md)
