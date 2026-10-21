import json
import tempfile
import unittest
from pathlib import Path

from label_porter.engine import run
from label_porter.formats import DataError
from label_porter.mapping import Scope, compile_rule, load_samples, preview_rule, sample_rows


class SampleCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.originals = self.base / "originals"
        self.originals.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def build(self, source, result, key="@file_name", result_key="/file"):
        scope = Scope(source.rows(), result.rows(), source.meta)
        scope.connect(key, result_key, "key")
        scope.connect("/label", "/new", "value")
        config = compile_rule(scope, source, result, "", "", "여러 파일")
        return scope, config

    def test_recursive_folder_preserves_each_file_identity(self):
        self.write(self.originals / "a.json", {"label": "old a"})
        self.write(self.originals / "sub" / "b.json", {"label": "old b"})
        self.write(self.originals / "sub" / "c.json", {"label": "keep"})
        (self.originals / "audio.wav").write_bytes(b"not json")
        incoming = self.write(self.base / "result.json", [{"file": "b.json", "new": "new b"}, {"file": "a.json", "new": "new a"}])
        source, result = load_samples(self.originals), load_samples(incoming)
        scope, config = self.build(source, result)
        self.assertEqual(source.file_count, 3)
        self.assertEqual([item["relative_path"] for item in scope.source_rows.metadata], ["a.json", "sub/b.json", "sub/c.json"])
        self.assertEqual(set(scope.source_fields["@file_name"].examples), {"a.json", "b.json", "c.json"})
        report = preview_rule(config, scope.source_rows, scope.result_rows, source.meta)
        self.assertEqual(report["counts"], {"updated": 2, "no_result": 1})
        self.assertEqual(report["all_source_key_counts"], {"matched": 2, "no_result": 1})
        actual = run(self.originals, [incoming], self.base / "output", config)
        self.assertEqual(actual.summary["partition_records"], {"changed": 2, "unchanged": 0, "examine": 1})
        self.assertEqual(json.loads((actual.directory / "data/sub/b.json").read_text())["label"], "new b")
        self.assertEqual(json.loads((self.originals / "sub/b.json").read_text())["label"], "old b")

    def test_same_basename_in_different_folders_requires_relative_path(self):
        left = self.write(self.originals / "left/same.json", {"label": "old"})
        right = self.write(self.originals / "right/same.json", {"label": "old"})
        incoming = self.write(self.base / "result.json", [{"file": "same.json", "path": "left/same.json", "new": "L"}, {"file": "same.json", "path": "right/same.json", "new": "R"}])
        source, result = load_samples([left, right]), load_samples(incoming)
        scope, config = self.build(source, result)
        self.assertEqual(preview_rule(config, scope.source_rows, scope.result_rows, source.meta)["counts"], {"duplicate": 2})
        scope, config = self.build(source, result, "@relative_path", "/path")
        report = preview_rule(config, scope.source_rows, scope.result_rows, source.meta)
        self.assertEqual(report["counts"], {"updated": 2})
        self.assertEqual(source.root, self.originals)

    def test_aggregate_files_and_multiple_exports_keep_result_row_metadata(self):
        for index in (1, 2):
            self.write(self.originals / f"{index}.json", [{"id": f"{index}a", "label": "old"}, {"id": f"{index}b", "label": "old"}])
            self.write(self.base / "exports" / f"{index}.json", [{"ref": f"{index}a", "new": "new"}, {"ref": f"{index}b", "new": "new"}])
        source, result = load_samples(self.originals), load_samples(self.base / "exports")
        scope, config = self.build(source, result, "/id", "/ref")
        config["include_when"] = [{"left": {"meta": "result_row"}, "op": "eq", "value": 1}]
        report = preview_rule(config, scope.source_rows, scope.result_rows, source.meta)
        self.assertEqual(source.file_count, 2)
        self.assertEqual(len(scope.source_rows), 4)
        self.assertEqual(report["counts"], {"updated": 2, "excluded": 2})

    def test_file_selection_deduplicates_paths_but_not_duplicate_record_ids(self):
        a = self.write(self.originals / "a.json", {"id": "same", "label": "old"})
        b = self.write(self.originals / "b.json", {"id": "same", "label": "old"})
        incoming = self.write(self.base / "result.json", {"ref": "same", "new": "new"})
        source, result = load_samples([a, a, b]), load_samples(incoming)
        scope, config = self.build(source, result, "/id", "/ref")
        self.assertEqual(source.file_count, 2)
        self.assertEqual(preview_rule(config, scope.source_rows, scope.result_rows, source.meta)["all_source_key_counts"], {"duplicate": 2})

    def test_invalid_file_and_invalid_record_container_report_file_name(self):
        self.write(self.originals / "good.json", {"records": [{"id": "a"}]})
        bad = self.originals / "bad.json"
        bad.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(DataError, "bad.json"):
            load_samples(self.originals)
        self.write(bad, {"id": "b"})
        source = load_samples(self.originals)
        with self.assertRaisesRegex(DataError, "bad.json"):
            source.rows("/records")

    def test_pattern_filter_progress_and_cancel(self):
        self.write(self.originals / "a.json", {"id": "a"})
        self.write(self.originals / "b.jsonl", {"id": "b"})
        progress = []
        source = load_samples(self.originals, pattern="**/*.json", progress=progress.append)
        self.assertEqual(source.file_count, 1)
        self.assertTrue(progress)
        with self.assertRaises(DataError):
            load_samples(self.originals, cancel=lambda: True)

    def test_small_partial_export_still_shows_a_matched_example(self):
        shown = set(sample_rows(list(range(250))))
        target = next(index for index in range(250) if index not in shown)
        original = self.write(self.originals / "all.json", [{"id": str(i), "label": "old"} for i in range(250)])
        incoming = self.write(self.base / "result.json", {"ref": str(target), "new": "new"})
        source, result = load_samples(original), load_samples(incoming)
        scope, config = self.build(source, result, "/id", "/ref")
        report = preview_rule(config, scope.source_rows, scope.result_rows, source.meta)
        self.assertEqual(report["sampled"], 120)
        self.assertEqual(report["counts"]["updated"], 1)
        self.assertEqual(report["all_source_key_counts"], {"matched": 1, "no_result": 249})

    def test_duplicate_keys_across_result_files_and_outside_sample_are_counted(self):
        self.write(self.originals / "a.json", [{"id": str(i), "label": "old"} for i in range(250)])
        target = next(index for index in range(250) if index not in set(sample_rows(list(range(250)))))
        first = self.write(self.base / "r1.json", [{"ref": str(i), "new": "new"} for i in range(250)])
        second = self.write(self.base / "r2.json", {"ref": str(target), "new": "other"})
        source, result = load_samples(self.originals), load_samples([first, second])
        scope, config = self.build(source, result, "/id", "/ref")
        report = preview_rule(config, scope.source_rows, scope.result_rows, source.meta)
        self.assertEqual(report["all_source_key_counts"], {"matched": 249, "duplicate": 1})
        self.assertEqual(report["duplicate_result_rows"], 2)


if __name__ == "__main__":
    unittest.main()
