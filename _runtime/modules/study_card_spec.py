STUDY_CARD_SPEC = '''# STUDY_CARD_SPEC

## 목적

수업 화면, 코드, 오류 해결 과정, 자격시험 문제와 해설을 같은 형식의 학습카드로 저장합니다.

## 생성 단위

- 캡처 한 장마다 카드를 만들지 않습니다.
- 같은 개념이나 문제를 보여주는 연속 캡처를 먼저 하나의 학습 단위로 묶습니다.
- 문제, 정답, 해설이 나뉘어 있으면 문제 번호, 제목, 핵심 문구와 시간순 흐름으로 연결합니다.
- 단순 이동, 중복 캡처와 학습 내용이 없는 화면은 카드 생성에서 제외합니다.

## source_type

- `concept`: 개념·이론
- `code`: 코드와 실행 흐름
- `error_resolution`: 오류 발생부터 해결까지
- `exam_question`: 시험 문제
- `exam_explanation`: 정답·해설
- `table_diagram`: 표·구조도
- `mixed`: 두 가지 이상의 유형이 결합된 학습 단위

## card_type

- `active_recall`: 핵심 내용을 답 없이 떠올리는 카드
- `feynman`: 처음 배우는 사람에게 설명하듯 답하는 카드
- `exam_replay`: 문제를 다시 푸는 카드
- `code_prediction`: 코드 실행 결과나 흐름을 예측하는 카드
- `debugging`: 오류 원인, 확인 위치와 해결 방법을 설명하는 카드
- `comparison`: 비슷한 개념의 차이를 설명하는 카드

## 정답 신뢰도

- `confirmed_from_source`: 이미지의 정답·해설 또는 실행 결과에서 직접 확인됨
- `ai_suggested`: 공식 정답은 보이지 않으며 AI가 근거를 바탕으로 제안함
- `needs_verification`: 이미지와 보조 자료만으로 정답을 확정할 수 없음

`ai_suggested`와 `needs_verification`은 공식 정답처럼 표현하지 않고 검토 대상으로 표시합니다.

## 카드 생성 제한

- 학습 단위 하나당 기본 1장, 필요한 경우에만 최대 3장
- 시험 문제는 기본적으로 재풀이 카드 1장과 핵심 개념 카드 1장까지
- 질문만 바꾼 중복 카드는 생성하지 않음
- 한 카드에서는 한 가지 지식만 묻고 짧게 채점할 수 있게 작성
- 파인만 카드는 원리, 이유 또는 흐름을 설명할 가치가 있을 때만 생성
- 이미지에 보이는 문제나 자료를 필요 이상으로 길게 전사하지 않음

## study_cards.json

유효한 UTF-8 JSON으로 작성하고 주석이나 Markdown 코드 울타리를 넣지 않습니다.

```json
{
  "schema_version": 1,
  "subject": "과목 또는 주제",
  "generated_at": "ISO 8601 형식",
  "cards": [
    {
      "card_id": "card-001",
      "source_type": "concept",
      "card_type": "active_recall",
      "topic": "학습 주제",
      "question": "사용자에게 제시할 질문",
      "choices": [],
      "answer": "확인된 답 또는 빈 문자열",
      "key_points": ["채점에 필요한 핵심 요소"],
      "explanation": "답을 확인한 뒤 읽을 짧은 설명",
      "answer_status": "confirmed_from_source",
      "source_images": ["capture_001.png"],
      "tags": ["분류 태그"],
      "difficulty": 3,
      "review_required": false
    }
  ]
}
```

- `choices`: 객관식 선택지가 이미지에서 확인될 때만 사용
- `answer`: 확인할 수 없으면 빈 문자열
- `key_points`: 능동적 인출과 파인만 설명을 평가할 핵심 요소
- `source_images`: 판단 근거로 사용한 이미지 파일명
- `difficulty`: 1~5의 예상 난이도
- `review_required`: `ai_suggested`, `needs_verification`이면 `true`

## study_cards.md

사람이 결과를 검토하기 위한 문서입니다. 각 카드의 ID, 주제, 유형, 질문, 답, 핵심 요소, 정답 신뢰도, 검토 필요 여부와 근거 이미지를 표시합니다.

생성할 카드가 없다면 두 파일 모두 생성하되 JSON의 `cards`는 빈 배열로 두고 Markdown에는 제외 이유를 짧게 기록합니다.
'''
