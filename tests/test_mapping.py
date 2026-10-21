from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from label_porter.engine import run
from label_porter.formats import DataError
from label_porter.mapping import (Scope, candidate_score, collect_fields, compile_rule,
                                  load_sample, preview_rule)
from label_porter.rules import ReviewRequired, apply_record


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.originals = self.base / "originals"
        self.originals.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def samples(self, source, result, sp="", rp=""):
        source_path, result_path = self.originals / "sample.json", self.base / "result.json"
        source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        self.source = load_sample(source_path, self.originals)
        self.result = load_sample(result_path)
        self.sp, self.rp = sp, rp
        self.scope = Scope(self.source.rows(sp), self.result.rows(rp), self.source.meta)
        return self.scope

    def config(self):
        return compile_rule(self.scope, self.source, self.result, self.sp, self.rp, "테스트")

    def simple(self, source=None, result=None):
        scope = self.samples(source or {"id": "a", "label": "old", "keep": {"v": 1}},
                             result or {"source_id": "a", "new": "new", "before": "old"})
        scope.connect("/id", "/source_id", "key")
        scope.connect("/label", "/new", "value")
        return scope

    def test_compiled_wires_port_copy_and_preserve_missing_records(self):
        scope = self.simple([{"id": "a", "label": "old", "keep": 7}, {"id": "b", "label": "same"}],
                            [{"source_id": "a", "new": "new", "before": "old"}])
        scope.connect("/label", "/before", "guard")
        config = self.config()
        before = self.source.path.read_bytes()
        preview = run(self.originals, [self.result.path], self.base / "out", config, preview=True)
        self.assertEqual(preview.summary["source_status_counts"], {"planned_update": 1, "no_result": 1})
        result = run(self.originals, [self.result.path], self.base / "out", config)
        actual = json.loads((result.directory / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(actual, [{"id": "a", "label": "new", "keep": 7}, {"id": "b", "label": "same"}])
        self.assertEqual(self.source.path.read_bytes(), before)
        self.assertEqual(result.summary["partition_records"], {"changed": 1, "unchanged": 0, "examine": 1})

    def test_guard_detects_wrong_original_and_repeat_run_is_unchanged(self):
        scope = self.simple()
        scope.connect("/label", "/before", "guard")
        config = self.config()
        original, incoming = scope.source_rows[0], scope.result_rows[0]
        actual, changes, _, status = apply_record(original, incoming, config, self.source.meta)
        self.assertEqual(status, "updated")
        self.assertEqual(actual["keep"], {"v": 1})
        self.assertEqual(original["label"], "old")
        self.assertEqual(changes[0]["path"], "/label")
        self.assertEqual(apply_record(actual, incoming, config, self.source.meta)[3], "unchanged")
        with self.assertRaises(ReviewRequired):
            apply_record({**original, "label": "different"}, incoming, config, self.source.meta)

    def test_nested_record_containers_and_escaped_field_names(self):
        scope = self.samples({"metadata": 1, "payload": {"rows": [{"id": "a", "a/b~": "old"}]}},
                             {"rows": [{"ref": "a", "value/": "new"}]}, "/payload/rows", "/rows")
        self.assertIn("/payload/rows", self.source.containers)
        scope.connect("/id", "/ref", "key")
        scope.connect("/a~1b~0", "/value~1", "value")
        config = self.config()
        actual = apply_record(scope.source_rows[0], scope.result_rows[0], config, self.source.meta)[0]
        self.assertEqual(actual["a/b~"], "new")
        self.assertEqual(config["source"]["records_path"], "/payload/rows")

    def test_singleton_wrapper_has_length_check_but_skipped_rule_needs_no_value(self):
        scope = self.samples({"id": "a", "label": "old"},
                             {"ref": "a", "state": "O", "answer": {"data": [{"value": "new"}]}})
        scope.connect("/id", "/ref", "key")
        wire = scope.connect("/label", "/answer/data/0/value", "value")
        wire.condition_path, wire.condition_value = "/state", "O"
        config = self.config()
        original, incoming = scope.source_rows[0], scope.result_rows[0]
        self.assertEqual(apply_record(original, incoming, config, {})[0]["label"], "new")
        with self.assertRaisesRegex(ReviewRequired, "배열 길이"):
            apply_record(original, {**incoming, "answer": {"data": [{"value": "new"}, {"value": "other"}]}}, config, {})
        self.assertEqual(apply_record(original, {"ref": "a", "state": "X"}, config, {})[3], "unchanged")

    def array_scope(self):
        scope = self.samples({"id": "a", "tags": [{"id": "t1", "label": "old1", "keep": 1}, {"id": "t2", "label": "old2", "keep": 2}]},
                             {"ref": "a", "objects": [{"ref": "t2", "value": "new2"}, {"ref": "t1", "value": "new1"}]})
        scope.connect("/id", "/ref", "key")
        wire = scope.connect("/tags", "/objects", "value")
        wire.child.connect("/id", "/ref", "key")
        wire.child.connect("/label", "/value", "value")
        return scope, wire

    def test_array_matches_ids_even_after_reordering(self):
        scope, _ = self.array_scope()
        actual = apply_record(scope.source_rows[0], scope.result_rows[0], self.config(), {})[0]
        self.assertEqual(actual["tags"], [{"id": "t1", "label": "new1", "keep": 1}, {"id": "t2", "label": "new2", "keep": 2}])

    def test_missing_array_item_preserved_and_new_or_duplicate_ids_block(self):
        scope, wire = self.array_scope()
        incoming = {"ref": "a", "objects": [scope.result_rows[0]["objects"][0]]}
        with self.assertRaises(ReviewRequired):
            apply_record(scope.source_rows[0], incoming, self.config(), {})
        wire.allow_missing = True
        actual, _, notes, _ = apply_record(scope.source_rows[0], incoming, self.config(), {})
        self.assertEqual(actual["tags"][0], scope.source_rows[0]["tags"][0])
        self.assertEqual(actual["tags"][1]["label"], "new2")
        self.assertTrue(notes)
        for items in ([{"ref": "new", "value": "x"}], incoming["objects"] * 2):
            with self.assertRaises(ReviewRequired):
                apply_record(scope.source_rows[0], {"ref": "a", "objects": items}, self.config(), {})

    def test_nested_arrays_and_item_key_wrapper_guards(self):
        scope = self.samples({"id": "a", "groups": [{"identity": [{"id": "g"}], "tags": [{"id": "t", "label": "old"}]}]},
                             {"ref": "a", "groups": [{"key": [{"id": "g"}], "tags": [{"ref": "t", "value": "new"}]}]})
        scope.connect("/id", "/ref", "key")
        outer = scope.connect("/groups", "/groups", "value").child
        outer.connect("/identity/0/id", "/key/0/id", "key")
        inner = outer.connect("/tags", "/tags", "value").child
        inner.connect("/id", "/ref", "key")
        inner.connect("/label", "/value", "value")
        actual = apply_record(scope.source_rows[0], scope.result_rows[0], self.config(), {})[0]
        self.assertEqual(actual["groups"][0]["tags"][0]["label"], "new")
        incoming = copy.deepcopy(scope.result_rows[0])
        incoming["groups"][0]["key"].append({"id": "second"})
        with self.assertRaises(ReviewRequired):
            apply_record(scope.source_rows[0], incoming, self.config(), {})

    def test_vector_length_guard_prevents_wrong_dimension(self):
        scope = self.samples({"id": "a", "xyz": [1, 2, 3]}, {"ref": "a", "point": [4, 5, 6]})
        scope.connect("/id", "/ref", "key")
        scope.connect("/xyz/0", "/point/0", "value")
        self.assertEqual(apply_record(scope.source_rows[0], scope.result_rows[0], self.config(), {})[0]["xyz"], [4, 2, 3])
        with self.assertRaises(ReviewRequired):
            apply_record(scope.source_rows[0], {"ref": "a", "point": [4, 5]}, self.config(), {})

    def test_key_type_mismatch_needs_explicit_conversion(self):
        scope = self.simple({"id": 1, "label": "old"}, {"source_id": "1", "new": "new"})
        report = preview_rule(self.config(), scope.source_rows, scope.result_rows, self.source.meta)
        self.assertEqual(report["counts"], {"no_result": 1})
        scope.wires[0].result_transform = "정수로"
        report = preview_rule(self.config(), scope.source_rows, scope.result_rows, self.source.meta)
        self.assertEqual(report["counts"], {"updated": 1})

    def test_file_metadata_key_and_basename_transform(self):
        scope = self.samples({"label": "old"}, {"file": "folder/sample.json", "new": "new"})
        key = scope.connect("@file_name", "/file", "key")
        key.result_transform = "파일명만"
        scope.connect("/label", "/new", "value")
        report = preview_rule(self.config(), scope.source_rows, scope.result_rows, self.source.meta)
        self.assertEqual(report["counts"], {"updated": 1})
        # A file name cannot disambiguate records within an aggregate JSON file.
        report = preview_rule(self.config(), scope.source_rows * 2, scope.result_rows, self.source.meta)
        self.assertEqual(report["counts"], {"duplicate": 2})

    def test_preview_detects_duplicate_result_and_invalid_id_without_mutation(self):
        scope = self.simple()
        original = copy.deepcopy(scope.source_rows)
        report = preview_rule(self.config(), scope.source_rows, scope.result_rows * 2 + [{}], self.source.meta)
        self.assertEqual(report["counts"], {"duplicate": 1})
        self.assertEqual(report["invalid_result_keys"], 1)
        self.assertEqual(scope.source_rows, original)

    def test_sample_limit_checks_ids_outside_displayed_subset(self):
        rows = [{"id": str(i), "label": "old"} for i in range(250)]
        incoming = [{"source_id": str(i), "new": "new"} for i in range(250)]
        scope = self.simple(rows, incoming)
        scope.result_rows.append(incoming[0].copy())
        report = preview_rule(self.config(), scope.source_rows, scope.result_rows, self.source.meta)
        self.assertEqual(report["sampled"], 120)
        self.assertEqual(report["counts"]["duplicate"], 1)

    def test_failed_field_rolls_back_entire_record(self):
        scope = self.simple()
        scope.connect("/keep/v", "/new", "value")
        original = copy.deepcopy(scope.source_rows[0])
        with self.assertRaises(ReviewRequired):
            apply_record(scope.source_rows[0], scope.result_rows[0], self.config(), {})
        self.assertEqual(scope.source_rows[0], original)

    def test_incomplete_or_conflicting_wires_fail_before_writing(self):
        scope = self.simple()
        with self.assertRaises(DataError):
            scope.connect("/label", "/before", "value")
        scope.wires[:] = [wire for wire in scope.wires if wire.kind != "key"]
        with self.assertRaisesRegex(DataError, "ID"):
            self.config()
        scope.connect("/id", "/source_id", "key")
        scope.connect("/id", "/source_id", "value")
        with self.assertRaisesRegex(DataError, "ID 자체"):
            self.config()
        scope.wires[:] = [wire for wire in scope.wires if wire.kind == "key"]
        scope.connect("/label", "/before", "guard")
        with self.assertRaisesRegex(DataError, "기존값 확인"):
            self.config()

    def test_overlapping_array_and_fixed_index_targets_are_rejected(self):
        scope = self.samples({"id": "a", "tags": [{"id": "t", "label": "old"}]},
                             {"ref": "a", "objects": [{"ref": "t", "value": "new"}]})
        scope.connect("/tags", "/objects", "value")
        with self.assertRaises(DataError):
            scope.connect("/tags/0/label", "/objects/0/value", "value")

    def test_field_candidates_include_paths_samples_and_types(self):
        source = collect_fields([{"id": "a", "label": "old", "tags": [{"id": "t"}]}])
        result = collect_fields([{"ref": "a", "corrected_label": "new"}])
        self.assertEqual(source["/label"].kind, "string")
        self.assertEqual(source["/label"].examples, ["old"])
        self.assertTrue(source["/tags"].object_array)
        self.assertGreaterEqual(candidate_score(source["/label"], result["/corrected_label"]), 40)
        self.assertGreaterEqual(candidate_score(source["/id"], result["/ref"]), 90)

    def test_json_stream_and_invalid_sample_files(self):
        path = self.base / "stream.json"
        path.write_text('{"id":"a"}\n{"id":"b"}', encoding="utf-8")
        self.assertEqual(len(load_sample(path).rows()), 2)
        for content in ("", "[]", "123", '{"id":1,"id":2}'):
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(DataError):
                load_sample(path)
        with self.assertRaisesRegex(DataError, "JSON/JSONL"):
            load_sample(self.base / "result.csv")


if __name__ == "__main__":
    unittest.main()
