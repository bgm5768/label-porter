import csv
import json
import tempfile
import unittest
from pathlib import Path

from label_porter.engine import run
from label_porter.label_counts import LabelCounts
from label_porter.result_dashboard import label_count_panel


def field(path="/labels", incoming="/labels"):
    return {"kind": "field", "to": path, "from": incoming, "type": "array", "allow_container_replace": True}


class LabelCountTests(unittest.TestCase):
    def test_engine_preview_and_saved_counts_include_preserved_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            source.mkdir()
            originals = [{"id": key, "labels": list(range(n))} for key, n in zip("abcde", range(1, 6))]
            original = source / "all.json"
            original.write_text(json.dumps(originals), encoding="utf-8")
            (source / "bad.json").write_text("{", encoding="utf-8")
            incoming = [{"id": k, "labels": v, "enabled": k != "d"} for k, v in
                        (("a", [1, 2, 3]), ("b", [0, 1]), ("c", {}), ("d", [1]), ("z", [1]*6))]
            incoming.append({"labels": [1, 2]})
            result = base / "incoming.json"
            result.write_text(json.dumps(incoming), encoding="utf-8")
            config = {"schema_version": 1, "name": "label count test", "source": {"key": "/id"},
                      "result": {"key": "/id"}, "include_when": [{"left": "/enabled", "op": "eq", "value": True}],
                      "rules": [field()]}
            snapshots = []
            for preview in (True, False):
                outcome = run(source, [result], base / "out", config, preview=preview)
                self.assertEqual(outcome.summary["status"], "completed_with_issues")
                rows = outcome.summary["label_counts"]
                self.assertEqual(len(rows), 1)
                row = rows[0]
                counts = row["counts"]
                self.assertEqual({k: v["items"] for k, v in counts.items()},
                                 dict(source=15, result=14, output=17, output_before=15, completed=5, examine=12))
                self.assertEqual(counts["result"]["records"], 6)
                self.assertEqual(counts["result"]["non_array"], 1)
                self.assertEqual(row["delta"], 2)
                report = outcome.directory / "reports"
                page = (report / "report.html").read_text(encoding="utf-8")
                self.assertIn('id="label-counts"', page)
                self.assertIn("반영 예정" if preview else "처리 후", page)
                with (report / "label_counts.csv").open(encoding="utf-8-sig", newline="") as handle:
                    self.assertEqual(len(list(csv.reader(handle))), 7)
                snapshots.append(rows)
            self.assertEqual(*snapshots)
            self.assertEqual(json.loads(original.read_text()), originals)

    def test_duplicate_targets_do_not_double_count_and_ambiguous_inputs_are_unknown(self):
        config = {"rules": [field(), field(), field(incoming="/other")]}
        counter = LabelCounts(config)
        counter.add("source", {"labels": [1, 2]})
        counter.add("result", {"labels": [1, 2], "other": [3]})
        rows = counter.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["counts"]["source"]["items"], 2)
        self.assertEqual(rows[0]["counts"]["result"]["unavailable"], 1)

    def test_nested_lists_platform_paths_and_empty_parent(self):
        config = {"rules": [{"kind": "items", "to": "/groups", "from": "/platform/groups",
                             "rules": [field("/tags", "/labels"),
                                       {"kind": "field", "to": "/text", "from": "/text"}]}]}
        counter = LabelCounts(config)
        counter.add("source", {"groups": [{"tags": [1, 2]}, {"tags": []}]})
        counter.add("source", {"groups": []})
        counter.add("result", {"platform": {"groups": [{"labels": [1, 2, 3]}]}})
        rows = {r["path"]: r for r in counter.rows()}
        self.assertEqual(set(rows), {"/groups", "/groups[]/tags"})
        child = rows["/groups[]/tags"]
        self.assertEqual(child["result_path"], "/platform/groups[]/labels")
        self.assertEqual(child["counts"]["source"]["items"], 2)
        self.assertEqual(child["counts"]["source"]["missing"], 0)
        self.assertEqual(child["counts"]["result"]["items"], 3)

    def test_missing_is_not_empty_and_does_not_produce_false_delta(self):
        counter = LabelCounts({"rules": [field()]})
        counter.add("output_before", {})
        counter.add("output", {"labels": []})
        row = counter.rows()[0]
        self.assertIsNone(row["delta"])
        self.assertEqual(row["counts"]["output"]["arrays"], 1)
        self.assertEqual(row["counts"]["output_before"]["missing"], 1)

    def test_scalar_fields_metadata_and_transform_expressions_are_not_inferred(self):
        config = {"rules": [{"kind": "field", "from": "/text", "to": "/text"},
                            field(incoming={"path": "/text", "transforms": [{"op": "parse_json"}]})]}
        counter = LabelCounts(config)
        counter.add("source", {"text": "hi", "labels": [1], "metadata": [1]*100})
        counter.add("result", {"text": "[1,2,3]"})
        row, = counter.rows()
        self.assertEqual(row["counts"]["source"]["items"], 1)
        self.assertEqual(row["counts"]["result"]["unavailable"], 1)
        self.assertIn("자동 집계 불가", label_count_panel({"preview": False, "label_counts": [row]}))

    def test_container_replace_detects_arrays_and_escapes_html(self):
        rule = field('/<script>')
        rule.pop("type")
        counter = LabelCounts({"rules": [rule]})
        counter.add("source", {"<script>": [1]})
        rows = counter.rows()
        page = label_count_panel({"preview": True, "label_counts": rows})
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_partial_input_and_rejected_candidate_not_counted_as_output(self):
        from label_porter.reports import Reports
        with tempfile.TemporaryDirectory() as tmp:
            reports = Reports(Path(tmp), preview=True, config={"rules": [field()]})
            original = {"labels": [1]}
            reports.labels.add("source", original)
            reports.labels.add("source", {"labels": [1, 2]})  # Read, not processed before cancellation.
            reports.record("source", "review_required", source_file="a.json", record_path="doc:0",
                evidence={"source_record": original, "final_record": original, "candidate_record": {"labels": [1]*10}})
            summary = reports.finish({"status": "cancelled", "preview": True, "started_at": "", "run_directory": tmp,
                "source_directory": tmp, "source_records": 2, "result_records": 0})
            row = summary["label_counts"][0]
            self.assertEqual(row["counts"]["source"]["items"], 3)
            self.assertEqual(row["counts"]["output"]["items"], 1)
            self.assertEqual(row["delta"], 0)
