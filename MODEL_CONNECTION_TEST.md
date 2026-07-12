# OCR·CAP 모델 연결 테스트

## 테스트 개요

- 실행일: 2026-07-12 (KST)
- 대상 OCR 모델: `nvidia/nemotron-ocr-v2`
- 대상 CAP 모델: `qwen/qwen3.5-397b-a17b`
- 실제 NVIDIA API 키는 사용자 설정에서 읽되 값은 출력하거나 저장소에 기록하지 않았다.
- 실제 연결에는 저장소 밖의 임시 테스트 이미지만 사용했다.

## 라이브 연결 결과

### OCR

- 결과: 성공
- 소요 시간: 약 2.92초
- HTTP 응답을 OCR 결과로 정상 해석했다.
- 테스트 이미지의 `ClassFlowAI` 문구가 결과에 포함된 것을 확인했다.

### CAP

- 최초 진단: 기본 thinking 모드에서 35초 읽기 제한 시간 초과를 재현했다.
- 원인 확인: 현재 Qwen 3.5 모델은 thinking 모드가 기본이며, ClassFlowAI는 최종 해석 결과만 사용한다.
- 조치: Qwen 3.5 모델 계열 요청에 공식 `chat_template_kwargs.enable_thinking=false` 옵션을 적용했다.
- 최종 결과: `analyze_capture_image` 실제 앱 경로에서 성공
- 최종 소요 시간: 약 11.84초
- HTTP 200 응답을 CAP 텍스트로 정상 해석했다.

참고한 공식 문서:

- [NVIDIA Qwen 3.5 모델 페이지](https://build.nvidia.com/qwen/qwen3.5-397b-a17b)
- [NVIDIA Qwen 3.5 API 문서](https://docs.api.nvidia.com/nim/reference/qwen-qwen3-5-397b-a17b-infer)

## 오류·재시도 회귀 검사

실제 API를 추가 호출하지 않는 모킹 테스트로 다음 경로를 확인했다.

- OCR 인증 실패(HTTP 401) 안내
- OCR 모델·요청 오류(HTTP 400) 안내
- CAP 인증 실패(HTTP 401) 안내
- CAP 모델·요청 오류(HTTP 400) 안내
- CAP 읽기 시간 제한 후 설정 횟수만큼 재시도하고 최종 시간 초과 안내
- CAP 일시적 서버 오류(HTTP 500) 후 재시도 성공
- CAP 재시도 횟수 `0` 설정 존중
- Qwen 3.5 요청에 thinking 비활성 옵션 포함

재실행 명령:

```powershell
cd _runtime
python -m unittest discover -s tests -p "test_model_connections.py" -v
```

## 판정

OCR·CAP 현재 기본 모델의 실제 연결과 응답 형식, 주요 오류 및 재시도 경로가 모두 확인됐다. API 키와 테스트 결과 본문은 저장소에 남기지 않았다.
