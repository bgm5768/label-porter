import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import xlsxwriter

from label_porter.engine import run
from label_porter.excel_report import MAX_CELL, _SheetRows, _text


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def sheets(path):
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        names = [s.attrib["name"] for s in workbook.findall(f".//{{{MAIN}}}sheet")]
        content = {}
        for number, name in enumerate(names, 1):
            tree = ET.fromstring(archive.read(f"xl/worksheets/sheet{number}.xml"))
            values = []
            for row in tree.findall(f".//{{{MAIN}}}sheetData/{{{MAIN}}}row"):
                cells = {}
                for cell in row.findall(f"{{{MAIN}}}c"):
                    text = "".join(t.text or "" for t in cell.findall(f".//{{{MAIN}}}t"))
                    if cell.attrib.get("t") != "inlineStr":
                        text = cell.findtext(f"{{{MAIN}}}v") or ""
                    cells[cell.attrib["r"]] = text
                values.append(cells)
            content[name] = (tree, values)
        return content


class ExcelReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.source = self.base / "source"; self.source.mkdir()
        self.original = [{"id": k, "labels": values} for k, values in
                         (("=1+1", ["old"]), ("review", ["stay"]), ("no-pair", ["original"]))]
        (self.source / "all.json").write_text(json.dumps(self.original), encoding="utf-8")
        self.result = self.base / "result.json"
        self.result.write_text(json.dumps([
            {"id": "=1+1", "labels": ["new", "label"]},
            {"id": "review", "labels": "wrong type"},
            {"id": "orphan", "labels": ["incoming"]},
        ]), encoding="utf-8")
        self.rule = {"schema_version": 1, "name": "Excel test", "source": {"key": "/id"},
                     "result": {"key": "/id"}, "rules": [{"kind": "field", "from": "/labels",
                         "to": "/labels", "type": "array", "allow_container_replace": True}]}

    def test_excel_matches_html_counts_and_detail_streams(self):
        outcome = run(self.source, [self.result], self.base / "output", self.rule)
        self.assertEqual(outcome.summary["status"], "completed_with_issues")
        directory = outcome.directory / "reports"
        self.assertIn('href="report.xlsx"', (directory / "report.html").read_text(encoding="utf-8"))
        tabs = sheets(directory / "report.xlsx")
        self.assertEqual(list(tabs)[:3], ["항목 및 보완 요약표", "보완 상세 이력", "보완 예시"])
        overview = {address: value for row in tabs["항목 및 보완 요약표"][1] for address, value in row.items()}
        self.assertEqual(overview["D6"], "원본 레코드")
        self.assertEqual(overview["D7"], "3")
        self.assertEqual(overview["E6"], "보완 레코드")
        self.assertEqual(overview["E7"], "3")
        self.assertEqual(tabs["라벨 수"][1][1]["G2"], "4")
        self.assertEqual(tabs["라벨 수"][1][1]["J2"], "1")
        catalog = json.loads((directory / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(len(tabs["전체 목록"][1]) - 1, sum(row["scope"] != "change" for row in catalog))
        self.assertEqual(len(tabs["보완 상세 이력"][1]) - 1, outcome.summary["changed_fields"])
        self.assertEqual(len(tabs["검토 사유"][1]) - 1, outcome.summary["review_findings"])
        detail_lines = (directory / "record_details.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(tabs["레코드 JSON"][1]) - 1, len(detail_lines))
        self.assertTrue(any(c.get("E" + str(row)) == "=1+1"
                            for row, c in enumerate(tabs["전체 목록"][1], 1)))
        self.assertTrue(any('"new"' in cell.get("K" + str(row), "")
                            for row, cell in enumerate(tabs["레코드 JSON"][1], 1)))
        for tree, _ in tabs.values():
            self.assertFalse(tree.findall(f".//{{{MAIN}}}f"), "User data was interpreted as an Excel formula")
        for title in ("전체 목록", "보완 상세 이력", "검토 사유", "레코드 JSON"):
            tree = tabs[title][0]
            self.assertIsNotNone(tree.find(f"{{{MAIN}}}autoFilter"))
            self.assertIsNotNone(tree.find(f".//{{{MAIN}}}pane"))
        self.assertEqual(json.loads((self.source / "all.json").read_text()), self.original)

    def test_preview_marks_candidate_separately_from_preserved_original(self):
        outcome = run(self.source, [self.result], self.base / "output", self.rule, preview=True)
        rows = sheets(outcome.directory / "reports/report.xlsx")["레코드 JSON"][1]
        target = next(c for c in rows if c.get("D" + str(rows.index(c) + 1)) == "=1+1")
        self.assertIn('"old"', target["K" + str(rows.index(target) + 1)])
        self.assertIn('"new"', target["L" + str(rows.index(target) + 1)])
        self.assertFalse((outcome.directory / "data").exists())

    def test_long_values_are_marked_and_whole_jsonl_remains_available(self):
        raw = "a" * (MAX_CELL + 100)
        shown = _text(raw)
        self.assertLessEqual(len(shown), MAX_CELL)
        self.assertIn("JSONL", shown)
        self.assertEqual(len(raw), MAX_CELL + 100)

    def test_sheet_overflow_repeats_headers_without_dropping_rows(self):
        path = self.base / "parts.xlsx"
        book = xlsxwriter.Workbook(path, {"constant_memory": True})
        formats = {"head": book.add_format(), "text": book.add_format(), "number": book.add_format(),
                   "link": book.add_format()}
        rows = _SheetRows(book, "목록", ["KEY", "값"], [25, 25], formats, max_rows=3)
        for n in range(5): rows.append([f"id-{n}", n])
        rows.finish(); book.close()
        content = sheets(path)
        self.assertEqual(list(content), ["목록", "목록_2", "목록_3"])
        self.assertEqual([len(rows) for _, rows in content.values()], [3, 3, 2])
        self.assertEqual(content["목록_3"][1][1]["A2"], "id-4")

    def test_excel_failure_marks_run_failed_and_keeps_html_reason(self):
        with patch("label_porter.reports.write_excel_report", side_effect=OSError("disk full")):
            outcome = run(self.source, [self.result], self.base / "output", self.rule)
        self.assertEqual(outcome.summary["status"], "failed")
        self.assertIn("Excel 결과서 저장 실패", outcome.summary["error"])
        page = (outcome.directory / "reports/report.html").read_text(encoding="utf-8")
        self.assertNotIn('href="report.xlsx"', page)
        self.assertIn("Excel 결과서 저장 실패", page)


if __name__ == "__main__":
    unittest.main()
