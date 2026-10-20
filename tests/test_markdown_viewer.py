import copy
import json
import re
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from label_porter.config import validate_config
from label_porter.markdown_viewer import MarkdownViewer, render_document
from label_porter.pair_preview import load_preview, match_preview
from label_porter.rules import apply_record, ReviewRequired

ROOT = Path(__file__).resolve().parents[1]


class MarkdownTests(unittest.TestCase):
    def test_structured_markdown_renders_tables_and_escapes_active_content(self):
        text = '# 제목\n\n**강조**와 `값`\n\n| 필드 | 설명 |\n|---|---|\n| KEY | 연결 |\n\n```json\n{"x":"<script>"}\n```\n\n<script>alert(1)</script>\n\n![image](https://example.com/image.png)\n\n[문서](사용설명.md) [외부](https://example.com)'
        rendered, headings = render_document(text)
        self.assertIn("<table>", rendered)
        self.assertIn("<strong>강조</strong>", rendered)
        self.assertIn("<code>값</code>", rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<img", rendered)
        self.assertNotIn('href="https:', rendered)
        self.assertEqual(headings, [("section-0", "제목", 1)])

    def test_all_bundled_documents_render_with_real_headings(self):
        for name in ("AI_규칙작성_가이드.md", "사용설명.md", "후처리_지원범위.md"):
            with self.subTest(name=name):
                rendered, headings = render_document((ROOT/name).read_text(encoding="utf-8"))
                self.assertGreater(len(headings), 10)
                self.assertIn("<h1", rendered)
                self.assertNotIn("<script", rendered)
                self.assertNotIn("<img", rendered)

    def test_reader_search_toc_zoom_links_and_source_preserved(self):
        root = tk.Tk(); root.withdraw(); self.addCleanup(root.destroy)
        navigation = []
        source = "# First\n\nSearch [literal] twice [literal].\n\n## Second\n\n**Bold**"
        reader = MarkdownViewer(root, source, navigate=navigation.append)
        root.update()
        self.assertEqual(reader.source, source)
        self.assertEqual(reader.browser.document.getElementById("section-1").textContent, "Second")
        reader.toc.current(1); reader._jump()
        reader.query.set("[literal]"); reader.find()
        self.assertEqual(reader.match_count, 2)
        self.assertEqual(reader.match_index, 1)
        reader.find(); self.assertEqual(reader.match_index, 2)
        reader.find(); self.assertEqual(reader.match_index, 1)
        reader.find(-1); self.assertEqual(reader.match_index, 2)
        reader.query.set("nothing"); reader.find()
        self.assertEqual(reader.match_label.get(), "결과 없음")
        reader.query.set(""); reader.find()
        self.assertEqual(reader.match_count, 0)
        reader.scale.set("130%"); reader._scale()
        self.assertEqual(reader.browser.cget("fontscale"), 1.3)
        reader._link("후처리_지원범위.md")
        self.assertEqual(navigation, [2])
        self.assertFalse(reader.browser.cget("javascript_enabled"))
        with self.assertRaises(ValueError): reader._deny_request("https://example.com")
        reader.query.set("Search")
        reader.destroy(); root.update()


class GuideExamplesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = (ROOT/"AI_규칙작성_가이드.md").read_text(encoding="utf-8")
        cls.examples = [json.loads(s) for s in re.findall(r"```json\s*\n(.*?)\n```", text, re.S)
                        if '"schema_version"' in s]

    def test_every_complete_guide_example_passes_actual_schema(self):
        self.assertEqual(len(self.examples), 5)
        for example in self.examples:
            with self.subTest(name=example["name"]): validate_config(example)

    def test_rebuild_example_add_delete_precision_transcript_and_independent_tags(self):
        config = next(x for x in self.examples if "별도 보완본" in x["name"])
        original = {"id": "a", "words": [{"id": 1, "text": "old", "start": 0, "end": 1},
                                        {"id": 2, "text": "old", "start": 1, "end": 2}],
                    "transcript": "old old", "word_count": 2, "PronunciationErrorTags": [{"eojeolID": 77, "note": "keep"}],
                    "metadata": {"keep": True}}
        incoming = {"id": "a", "words": [
            {"id": 9, "text": "셋", "start": 4.1, "end": 5},
            {"id": 7, "text": "하나", "start": 1.2, "end": 2},
            {"id": 8, "text": "둘", "start": 2.86789, "end": 3.497333333333333}]}
        before = copy.deepcopy(original)
        result, _, _, status = apply_record(original, incoming, config, {})
        self.assertEqual(status, "updated")
        self.assertEqual(result["transcript"], "하나 둘 셋")
        self.assertEqual(result["word_count"], 3)
        self.assertEqual(result["words"][1]["start"], 2.86789)
        self.assertEqual([x["id"] for x in result["words"]], [1, 2, 3])
        self.assertEqual(result["PronunciationErrorTags"], before["PronunciationErrorTags"])
        incoming["words"] = incoming["words"][1:2]
        result, *_ = apply_record(original, incoming, config, {})
        self.assertEqual(result["word_count"], 1)
        incoming["words"] = []
        result, *_ = apply_record(original, incoming, config, {})
        self.assertEqual((result["words"], result["word_count"], result["transcript"]), ([], 0, ""))
        self.assertEqual(original, before)
        incoming["words"] = [{"id": 1, "text": "bad", "start": 3, "end": 2}]
        with self.assertRaises(ReviewRequired): apply_record(original, incoming, config, {})
        self.assertEqual(original, before)

    def test_text_objects_and_time_examples_match_expected_values(self):
        text = next(x for x in self.examples if x["name"] == "텍스트 라벨 프로젝트 v1")
        self.assertEqual(apply_record({"id": 1, "label": "old"}, {"source_id": 1, "corrected_label": "new"}, text, {})[0]["label"], "new")
        objects = next(x for x in self.examples if x["name"] == "객체 라벨 프로젝트 v1")
        source = {"id": 1, "labels": [{"object_id": 1, "class": "old", "position": {"x": 0, "y": 0}},
                                     {"object_id": 2, "class": "old", "position": {"x": 0, "y": 0}}]}
        incoming = {"source_id": 1, "objects": [{"object_id": 2, "category": "B", "x": 2, "y": 3},
                                               {"object_id": 1, "category": "A", "x": 4, "y": 5}]}
        actual = apply_record(source, incoming, objects, {})[0]["labels"]
        self.assertEqual([r["class"] for r in actual], ["A", "B"])
        self.assertEqual(actual[0]["position"], {"x": 4, "y": 5})
        timing = next(x for x in self.examples if x["name"] == "시간 구간 프로젝트 v1")
        source = {"duration": 10, "segments": [{"segment_id": "s", "start": 0, "end": 1}]}
        incoming = {"source_file": "one.json", "segments": [{"segment_id": "s", "start_ms": "2860", "end_ms": "3497"}]}
        actual = apply_record(source, incoming, timing, {})[0]["segments"][0]
        self.assertAlmostEqual(actual["start"], 2.86)
        self.assertAlmostEqual(actual["end"], 3.497)

    def test_jsonl_example_uses_real_decoder_and_preserves_unmapped_fields(self):
        config = next(x for x in self.examples if "JSONL" in x["name"])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); originals = base/"source"; originals.mkdir()
            source = originals/"data.jsonl"; result = base/"result.jsonl"
            source.write_text(json.dumps({"id": "x", "messages": [], "preference": {}, "keep": True}) + "\n", encoding="utf-8")
            incoming = {"id": "x", "messages": [{"role": "assistant", "tool_calls": [{"function": {"name": "lookup", "arguments": "{}"}}]}],
                        "preference": {"chosen": "a", "rejected": "b"}}
            result.write_text(json.dumps(incoming) + "\n", encoding="utf-8")
            original_bytes = source.read_bytes()
            pair = match_preview(load_preview(config, originals, [result]))
            self.assertEqual(pair["candidate"], {**incoming, "keep": True})
            self.assertEqual(source.read_bytes(), original_bytes)
