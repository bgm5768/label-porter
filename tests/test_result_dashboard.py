import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from label_porter.engine import run
from label_porter.report_paging import SCRIPT
from scripts.verify_run import verify


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.result = self.base / "result.json"
        self.config = {"schema_version": 1, "name": "처리 결과서 테스트",
                       "source": {"key": "/id"}, "result": {"key": "/id"},
                       "include_when": [{"left": "/enabled", "op": "eq", "value": True}],
                       "rules": [{"kind": "field", "from": "/label", "to": "/label", "nonempty": True}]}
        self.original = [{"id": "a", "label": "old"}, {"id": "b", "label": "same"},
                         {"id": "c", "label": "keep"}, {"id": "d", "label": "keep"},
                         {"id": "e", "label": "excluded"}, {"label": "invalid key"}]
        incoming = [{"id": key, "label": text, "enabled": key != "e"} for key, text in
                    (("a", "new"), ("b", "same"), ("d", ""), ("e", "changed"), ("z", "orphan"))]
        incoming.append({"label": "invalid result key"})
        (self.source / "all.json").write_text(json.dumps(self.original), encoding="utf-8")
        (self.source / "broken.json").write_text('{"broken":', encoding="utf-8")
        self.result.write_text(json.dumps(incoming), encoding="utf-8")

    def execute(self, **options):
        return run(self.source, [self.result], self.base / "output", self.config, **options)

    def test_all_scopes_reconcile_and_examine_is_disjoint(self):
        outcome = self.execute()
        s = outcome.summary
        self.assertEqual(s["status"], "completed_with_issues")
        self.assertEqual((s["source_records"], s["result_records"], s["output_records"], s["completed_records"]), (6, 6, 6, 2))
        self.assertEqual(s["partition_records"], {"changed": 1, "unchanged": 1, "examine": 4})
        self.assertEqual((s["unmatched_source_records"], s["unmatched_result_records"]), (1, 1))
        self.assertEqual(s["category_counts"]["review"], 5)
        self.assertEqual(s["category_counts"]["files"], 1)
        for group, expected in (("changed", ["a"]), ("unchanged", ["b"])):
            rows = json.loads((outcome.directory / group / "labels/all.json").read_text(encoding="utf-8"))
            self.assertEqual([r.get("id") for r in rows], expected)
        for category, expected in (("unmatched", ["c"]), ("review", ["d", None]), ("excluded", ["e"])):
            paths = sorted((outcome.directory / "examine" / category / "원본").rglob("*.json"))
            rows = [json.loads(p.read_text(encoding="utf-8")) for p in paths if p.is_file()]
            self.assertTrue(all(len(row) == 1 for row in rows))
            self.assertCountEqual([row[0].get("id") for row in rows], expected)
        self.assertEqual((outcome.directory / "examine/file_errors/원본/broken.json").read_bytes(), b'{"broken":')
        archive = [json.loads(p.read_text(encoding="utf-8")) for p in (outcome.directory / "examine").glob("*/details/*.json")]
        self.assertEqual(len(archive), 7)
        orphan = next(row for row in archive if row["status"] == "orphan_result")
        self.assertEqual(orphan["platform_record"]["id"], "z")
        self.assertEqual(json.loads((outcome.directory / orphan["examine_data"]).read_text(encoding="utf-8"))[0]["id"], "z")
        self.assertEqual(s["examine_category_counts"], {"review": 3, "file_errors": 1, "unmatched": 2, "excluded": 1})
        self.assertEqual(json.loads((self.source / "all.json").read_text()), self.original)
        verify(outcome.directory)

    def test_completed_details_and_all_input_results_have_working_links(self):
        outcome = self.execute()
        directory = outcome.directory / "reports"
        page = (directory / "report.html").read_text(encoding="utf-8")
        self.assertIn('id="report-field"', page)
        self.assertIn('id="report-field-hint"', page)
        catalog = json.loads((directory / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(sum("result" in r["groups"] for r in catalog), 6)
        self.assertEqual(sum("completed" in r["groups"] for r in catalog), 2)
        changed = next(r for r in catalog if "changes" in r["groups"])
        self.assertEqual(changed["위치"], "/label")
        for row in catalog:
            self.assertTrue(row["link"])
            url = urlsplit(row["link"])
            self.assertTrue((directory / url.path).exists())
            if url.path == "detail.html":
                query = parse_qs(url.query)
                chunk = (directory / "detail_data" / (query["chunk"][0] + ".js")).read_text(encoding="utf-8")
                records = json.loads(chunk.removeprefix("window.porterDetail = ").removesuffix(";"))
                self.assertIn(query["id"][0], records)
                if row is changed:
                    detail = json.loads(records[query["id"][0]])
                    self.assertEqual(len(detail['changes']), 1)
                    self.assertEqual(detail['final_record']['label'], 'new')
        details = [json.loads(line) for line in (directory / "record_details.jsonl").read_text(encoding="utf-8").splitlines()]
        completed = next(row for row in details if row["status"] == "updated")
        self.assertEqual(completed["source_record"]["label"], "old")
        self.assertEqual(completed["platform_record"]["label"], "new")
        self.assertEqual(completed["final_record"]["label"], "new")
        check = subprocess.run(["node", str(Path(__file__).with_name("detail_loader_harness.cjs")), str(directory)],
                               capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_examine_is_always_exported_even_if_normal_split_is_disabled(self):
        outcome = self.execute(split_output=False)
        self.assertTrue((outcome.directory / "examine/unmatched/원본/all.json/00000003.json").exists())
        self.assertFalse((outcome.directory / "unchanged").exists())
        self.assertFalse((outcome.directory / "changed").exists())
        self.assertTrue((outcome.directory / "reports/examine_index.jsonl").exists())

    def test_result_duplicates_and_file_errors_are_archived_without_filename_collisions(self):
        (self.source / "all.json").write_text('[{"id":"a","label":"old"}]', encoding="utf-8")
        other = self.base / "other"; other.mkdir()
        twin = other / self.result.name
        for path, label in ((self.result, "one"), (twin, "two")):
            path.write_text(json.dumps([{"id": "a", "label": label, "enabled": True}]), encoding="utf-8")
        broken = self.base / "broken-result.json"; broken.write_text("{broken", encoding="utf-8")
        outcome = run(self.source, [self.result, twin, broken], self.base / "output", self.config)
        archived = [json.loads(p.read_text(encoding="utf-8")) for p in (outcome.directory / "examine/review/보안").rglob("*.json")]
        self.assertCountEqual([r[0]["label"] for r in archived], ["one", "two"])
        raw = list((outcome.directory / "examine/file_errors/보안").rglob("broken-result.json"))
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0].read_bytes(), broken.read_bytes())
        self.assertFalse((outcome.directory / "examine/records.jsonl").exists())

    def test_preview_has_only_reports_and_next_run_refreshes_catalog(self):
        outcome = self.execute(preview=True)
        self.assertEqual(outcome.summary["partition_status"], "planned")
        self.assertFalse((outcome.directory / "examine").exists())
        self.assertFalse((outcome.directory / "data").exists())
        self.result.write_text("[]", encoding="utf-8")
        again = self.execute(preview=True, split_output=False)
        catalog = json.loads((again.directory / "reports/catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(sum("result" in r["groups"] for r in catalog), 0)
        self.assertEqual(sum("completed" in r["groups"] for r in catalog), 0)
        self.assertFalse((again.directory / "reports/partition_index.jsonl").exists())

    def test_cancelled_results_are_not_reported_as_completed(self):
        cancel = threading.Event()
        def progress(event):
            if event["phase"] == "apply":
                cancel.set()
        outcome = self.execute(preview=True, cancel=cancel, progress=progress)
        self.assertEqual(outcome.summary["status"], "cancelled")
        self.assertEqual(outcome.summary["completed_records"], 0)
        catalog = json.loads((outcome.directory / "reports/catalog.json").read_text(encoding="utf-8"))
        pending = [r for r in catalog if r["status"] == "pending"]
        self.assertEqual(len(pending), 5)
        self.assertTrue(all(r["link"] for r in pending))

    def test_dashboard_paging_categories_status_search_and_return_state(self):
        harness = Path(__file__).with_name("dashboard_paging_harness.cjs")
        check = subprocess.run(["node", str(harness)], input=SCRIPT, encoding="utf-8",
                               capture_output=True, timeout=20)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)


if __name__ == "__main__":
    unittest.main()
