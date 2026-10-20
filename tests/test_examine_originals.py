import csv
import json
import tempfile
import unittest
from pathlib import Path

from label_porter.engine import run


class ExamineOriginalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.result = self.base / "incoming.json"
        self.config = {
            "schema_version": 1, "name": "Original examine inputs",
            "source": {"key": "/id", "records_path": "/payload/items"},
            "result": {"key": "/id", "records_path": "/export/rows"},
            "include_when": [{"left": "/enabled", "op": "eq", "value": True}],
            "rules": [{"kind": "field", "from": "/label", "to": "/label", "nonempty": True}],
        }

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def execute(self, **options):
        outcome = run(self.source, [self.result], self.base / "out", self.config, **options)
        self.assertNotEqual(outcome.summary["status"], "failed", outcome.summary.get("error"))
        return outcome.directory

    def test_nested_inputs_preserve_wrappers_and_only_one_record_on_both_sides(self):
        source_rows = [{"id": k, "label": k, "private": {"keep": [True, None]}} for k in ("a", "b", "c", "d")]
        incoming = [{"id": k, "label": text, "enabled": k != "b", "extra": [1, {"x": 2}]}
                    for k, text in (("a", ""), ("b", "changed"), ("d", "done"), ("z", "orphan"))]
        original = {"header": "원본", "payload": {"version": 3, "items": source_rows}}
        final = {"metadata": {"project": 7}, "export": {"rows": incoming, "schema": "kept"}}
        path = self.source / "all.json"
        self.write(path, original)
        self.write(self.result, final)
        original_bytes, result_bytes = path.read_bytes(), self.result.read_bytes()
        folder = self.execute(split_output=False)
        for category, source_key, result_key in (("review", "a", "a"), ("excluded", "b", "b"), ("unmatched", "c", "z")):
            sources = list((folder / "examine" / category / "labels").rglob("*.json"))
            results = list((folder / "examine" / category / "results").rglob("*.json"))
            sources = [p for p in sources if p.is_file()]
            self.assertEqual((len(sources), len(results)), (1, 1))
            saved_source = json.loads(sources[0].read_text(encoding="utf-8"))
            saved_result = json.loads(results[0].read_text(encoding="utf-8"))
            self.assertEqual(saved_source, {**original, "payload": {**original["payload"], "items": [next(r for r in source_rows if r["id"] == source_key)]}})
            self.assertEqual(saved_result, {**final, "export": {**final["export"], "rows": [next(r for r in incoming if r["id"] == result_key)]}})
        for file in (folder / "examine").glob("*/details/*.json"):
            detail = json.loads(file.read_text(encoding="utf-8"))
            for relative in detail["examine_sources"] + detail["examine_results"]:
                self.assertTrue((folder / relative).is_file(), relative)
            if detail["status"] == "no_result":
                self.assertEqual(detail["examine_results"], [])
            if detail["status"] == "orphan_result":
                self.assertEqual(detail["examine_sources"], [])
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(self.result.read_bytes(), result_bytes)

    def test_duplicate_candidates_are_separate_and_not_arbitrarily_paired(self):
        rows = [{"id": "x", "label": "original-1"}, {"id": "x", "label": "original-2"}]
        incoming = [{"id": "x", "label": text, "enabled": True} for text in ("first", "second")]
        self.write(self.source / "all.json", {"payload": {"items": rows}})
        self.write(self.result, {"export": {"rows": incoming}})
        folder = self.execute()
        originals = [p for p in (folder / "examine/review/labels").rglob("*.json") if p.is_file()]
        results = list((folder / "examine/review/results").rglob("*.json"))
        self.assertEqual((len(originals), len(results)), (2, 2))
        self.assertCountEqual([json.loads(p.read_text())["export"]["rows"][0]["label"] for p in results], ["first", "second"])
        for p in (folder / "examine/review/details").glob("*.json"):
            detail = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual((len(detail["examine_sources"]), len(detail["examine_results"])), (2, 2))

    def test_jsonl_preserves_line_format_metadata_and_bom(self):
        self.config["source"] = {"key": "/id", "glob": "**/*.jsonl"}
        self.config["result"] = {"key": "/id"}
        self.result = self.base / "incoming.ndjson"
        source_rows = [{"id": "a", "label": "old"}, {"id": "b", "label": "old"}]
        incoming = [{"id": "a", "label": "", "enabled": True}, {"id": "b", "label": "new", "enabled": True}]
        for path, rows in ((self.source / "all.jsonl", source_rows), (self.result, incoming)):
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8-sig")
        folder = self.execute()
        for subdir, suffix, expected in (("labels", "*.jsonl", source_rows[0]), ("results", "*.ndjson", incoming[0])):
            files = [p for p in (folder / "examine/review" / subdir).rglob(suffix) if p.is_file()]
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].read_bytes().startswith(b"\xef\xbb\xbf"))
            text = files[0].read_text(encoding="utf-8-sig")
            self.assertEqual(len(text.splitlines()), 1)
            self.assertEqual(json.loads(text), expected)

    def test_csv_json_cells_remain_csv_strings(self):
        self.config["source"] = {"key": "/id"}
        self.config["result"] = {"key": "/id", "csv_json_columns": ["payload"]}
        self.config.pop("include_when")
        self.config["rules"][0]["from"] = "/payload/label"
        self.write(self.source / "one.json", {"id": "a", "label": "old"})
        self.result = self.base / "incoming.csv"
        rows = [{"id": "a", "payload": '{ "label": "", "precise": 1.20000 }'},
                {"id": "z", "payload": '{"label":"extra"}'}]
        with self.result.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["id", "payload"])
            writer.writeheader()
            writer.writerows(rows)
        folder = self.execute()
        for category, expected in (("review", rows[0]), ("unmatched", rows[1])):
            files = list((folder / "examine" / category / "results").rglob("*.csv"))
            self.assertEqual(len(files), 1)
            with files[0].open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [expected])

    def test_unreadable_files_keep_original_bytes_and_preview_writes_no_examine(self):
        self.config["source"] = {"key": "/id"}
        self.config["result"] = {"key": "/id"}
        (self.source / "broken.json").write_bytes(b'{"broken":')
        self.result.write_bytes(b'[{"broken":')
        preview = self.execute(preview=True)
        self.assertFalse((preview / "examine").exists())
        folder = self.execute()
        self.assertEqual((folder / "examine/file_errors/files/source/broken.json").read_bytes(), b'{"broken":')
        files = list((folder / "examine/file_errors/files/results").rglob("incoming.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), self.result.read_bytes())


if __name__ == "__main__":
    unittest.main()
