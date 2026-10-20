import hashlib
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from label_porter.engine import run
from label_porter.formats import DataError, read_document
from label_porter.report_paging import SCRIPT


class LargeResultTests(unittest.TestCase):
    def test_chunk_boundaries_encodings_and_numbers_match_existing_reader(self):
        values = [
            '[{"한글":"문자\\\\\\"열","n":1.25e+23},true,null,123456789012345678901234567890,[],{}]',
            '{"nested":{"rows":[1,2,3]}}\n {"id":2}',
            '[1,2]\r\n[3,4]\n{"x":2}',
            ' [1e-2,-2.3,4e+12,1.0] \n',
            '\ufeff[]',
        ]
        with tempfile.TemporaryDirectory() as temp, patch("label_porter.stream_json.CHUNK", 7):
            path = Path(temp) / "input.json"
            for encoding in ("utf-8", "utf-8-sig", "utf-16", "cp949"):
                for value in values:
                    if encoding == "cp949" and "\ufeff" in value:
                        continue
                    with self.subTest(encoding=encoding, value=value):
                        raw = value.encode(encoding)
                        path.write_bytes(raw)
                        expected = read_document(path, encoding)
                        actual = read_document(path, encoding, progress=lambda *args: None)
                        self.assertEqual(json.dumps(actual.docs), json.dumps(expected.docs))
                        self.assertEqual(actual.sha256, hashlib.sha256(raw).hexdigest())
                        self.assertEqual(actual.bom, expected.bom)

    def test_strict_errors_at_chunk_boundaries(self):
        invalid = ['', '[1,]', '[1 2]', '[', '[1', '[1,,2]', '[{"x":1,"x":2}]',
                   '[NaN]', '[1e999]', '{}{}', '[1]{}', '[1e+]', '[truefalse]', '{"a":']
        with tempfile.TemporaryDirectory() as temp, patch("label_porter.stream_json.CHUNK", 7):
            path = Path(temp) / "input.json"
            for value in invalid:
                with self.subTest(value=value):
                    path.write_text(value, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        read_document(path, progress=lambda *args: None)

    def test_cancel_during_read_never_accepts_partial_file(self):
        self._cancel_run("result_read")

    def test_cancel_during_invalid_key_index_keeps_all_completed_evidence(self):
        self._cancel_run("result_index")

    def _cancel_run(self, phase):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"; source.mkdir()
            original = b'{"id":"a","label":"original"}'
            (source / "a.json").write_bytes(original)
            result = base / "result.json"
            result.write_text(json.dumps([{"missing_key": i, "label": "new"} for i in range(1000)]), encoding="utf-8")
            config = {"schema_version": 1, "name": "cancel",
                      "source": {"key": "/id"}, "result": {"key": "/id"},
                      "rules": [{"kind": "field", "from": "/label", "to": "/label"}]}
            cancel = threading.Event()
            events = []
            def progress(event):
                events.append(event)
                if event["phase"] == phase and event["done"] > 0:
                    cancel.set()
            outcome = run(source, [result], base / "output", config, preview=True, progress=progress, cancel=cancel)
            self.assertEqual(outcome.summary["status"], "cancelled")
            self.assertTrue(any(e["phase"] == phase and e["done"] > 0 for e in events))
            self.assertEqual((source / "a.json").read_bytes(), original)
            count = outcome.summary["result_records"]
            self.assertLess(count, 1000)
            details = (outcome.directory / "reports" / "review_details.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(details), count)
            for detail in map(json.loads, details):
                self.assertTrue((outcome.directory / "reports" / "review" / (detail["review_id"] + ".html")).is_file())

    def test_pagination_behavior_with_node_dom_harness(self):
        harness = Path(__file__).with_name("report_paging_harness.cjs")
        completed = subprocess.run(["node", str(harness)], input=SCRIPT, text=True, encoding="utf-8",
                                   capture_output=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_background_report_failure_cannot_be_reported_as_success(self):
        with tempfile.TemporaryDirectory() as temp, patch("label_porter.reports.write_review", side_effect=OSError("disk failure")):
            base = Path(temp)
            source = base / "source"; source.mkdir()
            (source / "a.json").write_text('{"id":"a","label":"old"}')
            result = base / "result.json"; result.write_text('[{"missing":"key"}]')
            config = {"schema_version": 1, "name": "failure",
                      "source": {"key": "/id"}, "result": {"key": "/id"},
                      "rules": [{"kind": "field", "from": "/label", "to": "/label"}]}
            outcome = run(source, [result], base / "output", config, preview=True)
            self.assertEqual(outcome.summary["status"], "failed")
            self.assertIn("disk failure", outcome.summary["error"])
            for path in ("run_status.json", "manifest.json", "reports/summary.json"):
                self.assertEqual(json.loads((outcome.directory / path).read_text(encoding="utf-8"))["status"], "failed")


if __name__ == "__main__":
    unittest.main()
