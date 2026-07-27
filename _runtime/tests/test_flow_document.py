import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from modules.flow_document import build_flow_document, save_flow_document


class FlowDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.first = self.workspace / "첫 화면.png"
        self.second = self.workspace / "다음 화면.jpg"
        Image.new("RGB", (13, 9), "red").save(self.first)
        Image.new("RGB", (17, 11), "blue").save(self.second)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_ocr_and_cap_use_image_then_text_blocks_without_cards(self):
        records = [
            {
                "record_id": "ocr-1",
                "mode": "ocr",
                "image_path": str(self.first),
                "display_order": 0,
                "ocr_raw_text": "화면에서 잘못 읽은 원문",
                "ocr_interpretation_text": "게시글 등록 화면을 엽니다.",
                "flow_interpretation_status": "done",
            },
            {
                "record_id": "cap-1",
                "mode": "capture",
                "image_path": str(self.second),
                "display_order": 1,
                "cap_text": "```java\nrepo.save(board);\n```\n저장 후 이동합니다.",
            },
        ]
        document = build_flow_document(records, "게시판 수업")
        self.assertEqual(document["sourceMode"], "mixed")
        self.assertEqual([item["type"] for item in document["sections"][0]["items"]], ["capture", "explanation"])
        self.assertEqual([item["type"] for item in document["sections"][1]["items"]], ["capture", "code", "explanation"])
        self.assertNotIn("화면에서 잘못 읽은 원문", json.dumps(document, ensure_ascii=False))

    def test_related_group_keeps_each_capture_next_to_its_text(self):
        records = [
            {"record_id": "a", "mode": "ocr", "group_id": "lesson-a", "image_path": str(self.first), "ocr_text": "첫 단계", "display_order": 0},
            {"record_id": "b", "mode": "cap", "group_id": "lesson-a", "image_path": str(self.second), "cap_text": "둘째 단계", "display_order": 1},
        ]
        section = build_flow_document(records)["sections"][0]
        self.assertEqual([item["type"] for item in section["items"]], ["capture", "note", "capture", "explanation"])

    def test_flow_title_is_section_title_and_duplicate_heading_is_removed(self):
        record = {
            "record_id": "a",
            "mode": "ocr",
            "image_path": str(self.first),
            "flow_title": "파이프라인 실행 순서",
            "flow_interpretation_text": "## 파이프라인 실행 순서\n### 실행 흐름\n입력 → 처리 → 결과",
            "ocr_interpretation_text": "레거시 본문",
            "flow_interpretation_status": "done",
            "display_order": 0,
        }
        section = build_flow_document([record])["sections"][0]
        self.assertEqual(section["title"], "파이프라인 실행 순서")
        rendered = json.dumps(section["items"], ensure_ascii=False)
        self.assertEqual(rendered.count("파이프라인 실행 순서"), 0)
        self.assertNotIn("###", rendered)
        self.assertIn("실행 흐름", rendered)
        self.assertIn("입력 → 처리 → 결과", rendered)
        self.assertNotIn("레거시 본문", rendered)

    def test_grouped_captures_use_first_flow_title_and_show_review_note(self):
        records = [
            {
                "record_id": "a",
                "mode": "ocr",
                "group_id": "a",
                "image_path": str(self.first),
                "flow_title": "첫 학습 블록",
                "flow_interpretation_text": "첫 설명",
                "flow_interpretation_status": "done",
                "display_order": 0,
            },
            {
                "record_id": "b",
                "mode": "ocr",
                "group_id": "a",
                "image_path": str(self.second),
                "flow_title": "연속 화면",
                "flow_interpretation_text": "```python\nprint('ok')\n```\n추가 설명",
                "flow_review_required": ["잘린 메서드 이름", "실행 결과 연결 확인"],
                "flow_interpretation_status": "done",
                "display_order": 1,
            },
        ]
        document = build_flow_document(records)
        self.assertEqual(len(document["sections"]), 1)
        section = document["sections"][0]
        self.assertEqual(section["title"], "첫 학습 블록")
        self.assertEqual(
            [item["type"] for item in section["items"]],
            ["capture", "explanation", "capture", "code", "explanation", "note"],
        )
        review_html = section["items"][-1]["html"]
        self.assertIn("확인 필요", review_html)
        self.assertIn("잘린 메서드 이름", review_html)

    def test_nonconsecutive_equal_group_ids_are_not_merged(self):
        third = self.workspace / "세 번째.png"
        Image.new("RGB", (10, 10), "green").save(third)
        records = [
            {"record_id": "a", "mode": "cap", "group_id": "same", "image_path": str(self.first), "cap_text": "첫째", "display_order": 0},
            {"record_id": "b", "mode": "cap", "group_id": "other", "image_path": str(self.second), "cap_text": "둘째", "display_order": 1},
            {"record_id": "c", "mode": "cap", "group_id": "same", "image_path": str(third), "cap_text": "셋째", "display_order": 2},
        ]
        self.assertEqual(len(build_flow_document(records)["sections"]), 3)

    def test_legacy_ocr_title_is_split_from_body(self):
        record = {
            "record_id": "legacy",
            "mode": "ocr",
            "image_path": str(self.first),
            "ocr_interpretation_text": "## 레거시 제목\n레거시 설명",
            "flow_interpretation_status": "done",
            "display_order": 0,
        }
        section = build_flow_document([record])["sections"][0]
        self.assertEqual(section["title"], "레거시 제목")
        self.assertNotIn("레거시 제목", json.dumps(section["items"], ensure_ascii=False))
        self.assertIn("레거시 설명", json.dumps(section["items"], ensure_ascii=False))

    def test_model_html_is_escaped_in_flow_items(self):
        record = {
            "record_id": "safe",
            "mode": "ocr",
            "image_path": str(self.first),
            "flow_title": "안전한 표시",
            "flow_interpretation_text": "<script>alert('x')</script> **굵게**",
            "flow_interpretation_status": "done",
            "display_order": 0,
        }
        rendered = json.dumps(build_flow_document([record]), ensure_ascii=False)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("<strong>굵게</strong>", rendered)

    def test_html_attributes_are_escaped_and_python_operators_are_not_bold(self):
        record = {
            "record_id": "operators",
            "mode": "ocr",
            "image_path": str(self.first),
            "flow_title": "연산자",
            "flow_interpretation_text": '<img src=x onerror=alert(1)>\na ** b ** c\n**kwargs는 그대로',
            "flow_interpretation_status": "done",
            "display_order": 0,
        }
        rendered = json.dumps(build_flow_document([record]), ensure_ascii=False)
        self.assertNotIn("<img ", rendered)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", rendered)
        self.assertIn("a ** b ** c", rendered)
        self.assertIn("**kwargs는 그대로", rendered)

    def test_code_fence_content_is_not_processed_as_markdown_or_html(self):
        code = "### literal\n**kwargs\n<tag>"
        record = {
            "record_id": "code",
            "mode": "ocr",
            "image_path": str(self.first),
            "flow_title": "코드 보존",
            "flow_interpretation_text": f"```text\n{code}\n```\n### 설명\n코드 다음 문단",
            "flow_interpretation_status": "done",
            "display_order": 0,
        }
        section = build_flow_document([record])["sections"][0]
        code_item = next(item for item in section["items"] if item["type"] == "code")
        explanation = next(item for item in section["items"] if item["type"] == "explanation")
        self.assertEqual(code_item["code"], code)
        self.assertNotIn("###", explanation["html"])
        self.assertIn("설명", explanation["html"])

    def test_raw_ocr_is_not_exposed_before_background_interpretation(self):
        document = build_flow_document([
            {
                "record_id": "a",
                "mode": "ocr",
                "image_path": str(self.first),
                "ocr_text": "빠른 OCR 결과",
                "flow_interpretation_status": "running",
                "display_order": 0,
            }
        ])
        value = document["sections"][0]["items"][1]["html"]
        self.assertNotIn("빠른 OCR 결과", value)
        self.assertIn("백그라운드에서 준비", value)

    def test_failed_interpretation_keeps_raw_ocr_out_of_flow(self):
        document = build_flow_document([{
            "record_id": "a",
            "mode": "ocr",
            "image_path": str(self.first),
            "ocr_text": "유지되는 빠른 OCR",
            "flow_interpretation_status": "failed",
            "flow_interpretation_error": "오류",
            "display_order": 0,
        }])
        value = document["sections"][0]["items"][1]["html"]
        self.assertNotIn("유지되는 빠른 OCR", value)
        self.assertIn("해석에 실패", value)

    def test_missing_api_key_shows_flow_only_status_message(self):
        document = build_flow_document([{
            "record_id": "a",
            "mode": "ocr",
            "image_path": str(self.first),
            "ocr_text": "현재 화면 전용 OCR",
            "flow_interpretation_status": "waiting_for_api_key",
            "display_order": 0,
        }])
        value = document["sections"][0]["items"][1]["html"]
        self.assertNotIn("현재 화면 전용 OCR", value)
        self.assertIn("API 키가 필요", value)
        self.assertIn("현재 결과 화면", value)

    def test_document_is_saved_as_utf8_structured_data(self):
        document = build_flow_document([
            {"record_id": "a", "mode": "cap", "image_path": str(self.first), "cap_text": "한글 해설", "display_order": 0}
        ])
        path = self.workspace / "state" / "flow_document.json"
        save_flow_document(path, document)
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["sections"][0]["items"][1]["type"], "explanation")


if __name__ == "__main__":
    unittest.main()
