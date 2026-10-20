import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

from label_porter.engine import run
from label_porter.diagnostics import diagnose_record
from label_porter.rules import ReviewRequired, apply_record


class ReviewEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.config = {"schema_version": 1, "name": "Evidence", "source": {"key": "/id"}, "result": {"key": "/id"},
            "rules": [{"kind": "items", "from": "/items", "to": "/items",
                "source_key": {"scope": "original", "path": "/id"}, "result_key": "/id",
                "identity_guards": [{"source": {"scope": "original", "path": "/text", "transforms": [{"op": "strip"}]},
                                     "result": {"path": "/text", "transforms": [{"op": "strip"}]}}],
                "rules": [{"kind": "field", "from": {"path": "/start", "transforms": [{"op": "round", "digits": 1}]}, "to": "/start", "type": "number"},
                          {"kind": "field", "from": "/end", "to": "/end", "type": "number"}]}],
            "assertions": [{"kind": "intervals", "path": "/items", "start": "/start", "end": "/end", "duration": "/duration", "non_overlapping": True},
                           {"kind": "references", "targets": "/items", "target_key": "/id", "references": "/tags", "reference_key": "/id"}]}
        self.original = {"id": "a", "duration": 10, "items": [
            {"id": 1, "text": "one", "start": 0, "end": 1},
            {"id": 2, "text": "two", "start": 1, "end": 2},
            {"id": 3, "text": "three", "start": 2, "end": 3}], "tags": [{"id": 88}, {"id": 99}]}

    def test_collects_all_mismatches_intervals_references_and_preserves_original(self):
        incoming = {"id": "a", "items": [
            {"id": 2, "text": " changed two ", "start": 1.27, "end": 12},
            {"id": 1, "text": " changed one ", "start": 0.14, "end": 4},
            {"id": 4, "text": "new", "start": 5, "end": 6}]}
        before = copy.deepcopy(self.original)
        with self.assertRaises(ReviewRequired) as caught:
            apply_record(self.original, incoming, self.config, {})
        detail = diagnose_record(self.original, incoming, self.config, {}, caught.exception)
        codes = [i["code"] for i in detail["issues"]]
        self.assertEqual(codes.count("identity_mismatch"), 2)
        self.assertEqual(codes.count("invalid_reference"), 2)
        for code in ("new_result_item", "missing_result_item", "duration_exceeded", "interval_overlap"):
            self.assertIn(code, codes)
        issue = next(i for i in detail["issues"] if i["code"] == "identity_mismatch")
        self.assertEqual(issue["source_path"], "/items/1")
        self.assertEqual(issue["result_item_path"], "/items/0")
        self.assertEqual(issue["platform"]["raw"], " changed two ")
        self.assertEqual(issue["platform"]["evaluated"], "changed two")
        self.assertEqual(detail["candidate_record"]["items"][1]["start"], 1.3)
        self.assertEqual(detail["final_record"], before)
        self.assertEqual(self.original, before)

    def test_duplicate_ids_are_never_arbitrarily_matched(self):
        incoming = {"id": "a", "items": [{"id": 1, "text": "x", "start": 7, "end": 8}, {"id": 1, "text": "y", "start": 9, "end": 10}]}
        detail = diagnose_record(self.original, incoming, self.config, {}, "duplicates")
        self.assertIn("duplicate_item_key", [i["code"] for i in detail["issues"]])
        self.assertEqual(detail["candidate_record"], self.original)
        self.assertFalse(detail["proposed_changes"])

    def test_when_false_skips_checks_but_other_fields_are_diagnosed(self):
        config = {"rules": [
            {"kind": "field", "from": "/missing", "to": "/a", "when": [{"left": "/enabled", "op": "eq", "value": True}]},
            {"kind": "field", "from": "/new", "expected": "/old", "to": "/b"},
            {"kind": "field", "from": "/missing", "to": "/c"}]}
        detail = diagnose_record({"a": 0, "b": 2, "c": 3}, {"enabled": False, "old": 0, "new": 9}, config, {}, "failed")
        self.assertEqual([i["source_path"] for i in detail["issues"]], ["/b", "/c"])
        self.assertEqual(detail["issues"][0]["expected"]["raw"], 0)
        self.assertFalse(detail["issues"][1]["platform"]["raw_present"])

    def test_nested_items_keep_root_expressions_and_actual_paths(self):
        inner = self.config["rules"][0]
        config = {"rules": [{"kind": "items", "from": "/groups", "to": "/groups",
                  "source_key": {"scope": "source", "path": "/id"}, "result_key": "/id", "rules": [inner]}]}
        original = {"groups": [{"id": "g", "items": [{"id": 1, "text": "one", "start": 0, "end": 1}]}]}
        result = {"groups": [{"id": "g", "items": [{"id": 1, "text": "other", "start": 2, "end": 3}]}]}
        detail = diagnose_record(original, result, config, {}, "mismatch")
        self.assertEqual(detail["issues"][0]["source_path"], "/groups/0/items/0")
        self.assertEqual(detail["issues"][0]["result_item_path"], "/groups/0/items/0")

    def test_report_includes_every_record_over_100_and_full_values(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"; source.mkdir()
            originals = [{"id": str(i), "a": "old", "b": "keep"} for i in range(105)]
            incoming = [{"id": str(i), "a": "</pre><script>bad()</script>", "old": "conflict"} for i in range(105)]
            (source / "data.json").write_text(json.dumps(originals), encoding="utf-8")
            result = base / "result.json"; result.write_text(json.dumps(incoming), encoding="utf-8")
            config = {"schema_version": 1, "name": "Evidence", "source": {"key": "/id"}, "result": {"key": "/id"},
                      "rules": [{"kind": "field", "from": "/a", "expected": "/old", "to": "/a"},
                                {"kind": "field", "from": "/missing", "to": "/b"}]}
            outcome = run(source, [result], base / "output", config, preview=True)
            reports = outcome.directory / "reports"
            details = [json.loads(line) for line in (reports / "review_details.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(details), 105)
            self.assertEqual(outcome.summary["review_findings"], 210)
            self.assertEqual(outcome.summary["changed_fields"], 0)
            self.assertEqual(details[-1]["final_record"], originals[-1])
            html = (reports / "review" / "000105.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>", html)
            self.assertIn("&lt;script&gt;", html)
            self.assertIn("review/000105.html", (reports / "report.html").read_text(encoding="utf-8"))
            with (reports / "records.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["review_issue_count"], "2")
            self.assertIn("/b", rows[-1]["reason"])
            self.assertFalse((outcome.directory / "data").exists())


if __name__ == "__main__":
    unittest.main()
