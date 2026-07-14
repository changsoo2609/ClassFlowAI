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
