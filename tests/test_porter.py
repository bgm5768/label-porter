from __future__ import annotations

import copy
import csv
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from label_porter.config import load_config, validate_config
from label_porter.engine import run
from label_porter.formats import DataError, equivalent, get_pointer, parse_json, read_document
from label_porter.reports import csv_safe
from label_porter.rules import ReviewRequired, apply_record


ROOT = Path(__file__).resolve().parents[1]


class PorterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "original"
        self.source.mkdir()
        self.result = self.root / "result.json"
        self.output = self.root / "output"
        self.config = load_config(ROOT / "profiles" / "generic.json")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def execute(self, original, results, **kwargs):
        self.write(self.source / "data.json", original)
        self.write(self.result, results)
        return run(self.source, [self.result], self.output, self.config, **kwargs)

    def data(self, result, filename="data.json"):
        return json.loads((result.directory / "data" / filename).read_text(encoding="utf-8-sig"))

    def report_rows(self, result):
        with (result.directory / "reports" / "records.csv").open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_preserves_original_missing_rows_and_unmapped_metadata(self):
        original = [{"id": "a", "label": "before", "meta": {"v": [1, True, None]}}, {"id": "b", "label": "keep"}]
        self.write(self.source / "data.json", original)
        before = (self.source / "data.json").read_bytes()
        (self.source / "asset.wav").write_bytes(b"binary\x00\xff")
        (self.source / "empty").mkdir()
        self.write(self.result, [{"source_id": "a", "corrected_label": "after"}])
        result = run(self.source, [self.result], self.output, self.config)
        expected = copy.deepcopy(original)
        expected[0]["label"] = "after"
        self.assertEqual(self.data(result), expected)
        self.assertEqual((self.source / "data.json").read_bytes(), before)
        self.assertEqual((result.directory / "data" / "asset.wav").read_bytes(), b"binary\x00\xff")
        self.assertTrue((result.directory / "data" / "empty").is_dir())
        self.assertEqual(result.summary["source_status_counts"], {"no_result": 1, "updated": 1})
        manifest = json.loads((result.directory / "manifest.json").read_text(encoding="utf-8"))
        item = next(item for item in manifest["files"] if item["path"] == "data.json")
        self.assertEqual(item["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertNotEqual(item["source_sha256"], item["output_sha256"])

    def test_preview_creates_reports_only(self):
        result = self.execute({"id": "a", "label": "old"}, {"source_id": "a", "corrected_label": "new"}, preview=True)
        self.assertFalse((result.directory / "data").exists())
        self.assertEqual(result.summary["status_counts"], {"planned_update": 1})
        self.assertEqual(json.loads((self.source / "data.json").read_text())["label"], "old")

    def test_duplicate_results_across_files_never_last_wins(self):
        self.write(self.source / "data.json", {"id": "a", "label": "old"})
        self.write(self.result, {"source_id": "a", "corrected_label": "one"})
        second = self.root / "result2.json"
        self.write(second, {"source_id": "a", "corrected_label": "two"})
        result = run(self.source, [self.result, second], self.output, self.config)
        self.assertEqual(self.data(result)["label"], "old")
        self.assertEqual(result.summary["status"], "completed_with_issues")
        self.assertEqual(result.summary["source_status_counts"]["duplicate_result"], 1)
        self.assertEqual(result.summary["result_issue_counts"]["duplicate_result"], 2)

    def test_duplicate_source_ids_block_all_matches(self):
        result = self.execute([{"id": "a", "label": "one"}, {"id": "a", "label": "two"}], {"source_id": "a", "corrected_label": "new"})
        self.assertEqual([row["label"] for row in self.data(result)], ["one", "two"])
        self.assertEqual(result.summary["source_status_counts"]["duplicate_source_key"], 2)

    def test_orphan_and_invalid_result_are_reported(self):
        result = self.execute({"id": "a", "label": "old"}, [{"source_id": "missing", "corrected_label": "new"}, {"corrected_label": "x"}])
        self.assertEqual(result.summary["status_counts"]["orphan_result"], 1)
        self.assertEqual(result.summary["status_counts"]["invalid_result"], 1)
        self.assertEqual(result.summary["status_counts"]["no_result"], 1)

    def test_blank_or_missing_modification_preserves_original(self):
        for incoming in ({"source_id": "a", "corrected_label": "  "}, {"source_id": "a"}):
            result = self.execute({"id": "a", "label": "old"}, incoming)
            self.assertEqual(self.data(result)["label"], "old")
            self.assertEqual(result.summary["source_status_counts"], {"review_required": 1})

    def test_record_is_atomic_when_later_rule_fails(self):
        self.config["rules"].append({"kind": "field", "from": "/nonexistent", "to": "/other"})
        original = {"id": "a", "label": "old", "other": 1}
        result = self.execute(original, {"source_id": "a", "corrected_label": "new"})
        self.assertEqual(self.data(result), original)
        self.assertEqual(result.summary["changed_fields"], 0)

    def test_preimage_guard_and_repeat_run(self):
        self.config["rules"][0]["expected"] = "/old_label"
        incoming = {"source_id": "a", "corrected_label": "new", "old_label": "old"}
        first = self.execute({"id": "a", "label": "old"}, incoming)
        second = run(first.directory / "data", [self.result], self.output, self.config)
        self.assertEqual(second.summary["status_counts"], {"unchanged": 1})
        conflict = self.execute({"id": "a", "label": "someone else's version"}, incoming)
        self.assertEqual(conflict.summary["status_counts"], {"review_required": 1})

    def test_json_stream_and_nested_source_container(self):
        self.config["source"]["records_path"] = "/payload/rows"
        self.write(self.source / "data.json", {"version": 7, "payload": {"rows": [{"id": "a", "label": "old"}, {"id": "b", "label": "old2"}]}})
        self.result.write_text(json.dumps({"source_id": "b", "corrected_label": "new2"}, indent=2) + "\n\n" + json.dumps({"source_id": "a", "corrected_label": "new"}, indent=2), encoding="utf-8")
        result = run(self.source, [self.result], self.output, self.config)
        self.assertEqual(self.data(result)["version"], 7)
        self.assertEqual([row["label"] for row in self.data(result)["payload"]["rows"]], ["new", "new2"])

    def test_jsonl_source_stays_jsonl(self):
        self.config["source"]["glob"] = "**/*.jsonl"
        path = self.source / "rows.jsonl"
        path.write_text('{"id":"a","label":"old"}\n{"id":"b","label":"keep"}\n', encoding="utf-8")
        self.write(self.result, {"source_id": "a", "corrected_label": "new"})
        result = run(self.source, [self.result], self.output, self.config)
        lines = (result.directory / "data" / "rows.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["label"], "new")

    def test_csv_preserves_zero_prefixed_identifiers_and_parses_selected_json(self):
        self.write(self.source / "data.json", {"id": "001", "label": "old"})
        self.result = self.root / "results.csv"
        self.config["result"]["csv_json_columns"] = ["payload"]
        self.config["rules"][0]["from"] = "/payload/text"
        with self.result.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["source_id", "payload"])
            writer.writerow(["001", json.dumps({"text": "new,\nline"})])
        result = run(self.source, [self.result], self.output, self.config)
        self.assertEqual(self.data(result)["label"], "new,\nline")

    def test_scalar_key_types_do_not_accidentally_match(self):
        result = self.execute({"id": 1, "label": "old"}, {"source_id": "1", "corrected_label": "new"})
        self.assertEqual(self.data(result)["label"], "old")
        self.assertEqual(result.summary["status_counts"]["orphan_result"], 1)

    def test_nested_array_ids_not_positions_and_partial_missing_items_preserved(self):
        self.config = load_config(ROOT / "profiles" / "example_objects.json")
        original = {"id": "a", "labels": [{"object_id": "x", "class": "old", "position": {"x": 1, "y": 2, "z": 3}}, {"object_id": "y", "class": "keep", "position": {"x": 5, "y": 6}}]}
        result = self.execute(original, {"source_id": "a", "objects": [{"object_id": "x", "category": "new", "x": 10, "y": 20}]})
        actual = self.data(result)
        self.assertEqual(actual["labels"][0]["position"], {"x": 10, "y": 20, "z": 3})
        self.assertEqual(actual["labels"][1], original["labels"][1])
        self.assertIn("배열 항목 1개", self.report_rows(result)[0]["reason"])

    def test_new_array_id_causes_whole_record_review(self):
        self.config = load_config(ROOT / "profiles" / "example_objects.json")
        original = {"id": "a", "labels": [{"object_id": "x", "class": "old", "position": {"x": 1, "y": 2}}]}
        result = self.execute(original, {"source_id": "a", "objects": [{"object_id": "new", "category": "new", "x": 10, "y": 20}]})
        self.assertEqual(self.data(result), original)
        self.assertEqual(result.summary["status_counts"]["review_required"], 1)

    def test_audio_guard_intervals_and_tag_references(self):
        config = load_config(ROOT / "profiles" / "013_audio.json")
        original = {"RecordingMetadata": {"recorded_time_sec": 10}, "Segmentation": {"eojeols": [{"eojeolID": 1, "text": "hello", "start": 0, "end": 2}]}, "PronunciationErrorTags": [{"eojeolID": 1, "subtype": "keep"}]}
        incoming = {"unableToWork": 0, "orthographic_1": {"data": [{"objectID": "tag_987_1", "value": {"name_J2REGU": "hello ", "startTime": 1, "endTime": 3}}]}}
        candidate, changes, _, _ = apply_record(original, incoming, config, {})
        self.assertEqual(candidate["Segmentation"]["eojeols"][0]["start"], 1)
        self.assertEqual(candidate["PronunciationErrorTags"], original["PronunciationErrorTags"])
        self.assertEqual(len(changes), 2)
        bad = copy.deepcopy(incoming)
        bad["orthographic_1"]["data"][0]["value"]["name_J2REGU"] = "different"
        with self.assertRaises(ReviewRequired):
            apply_record(original, bad, config, {})
        bad = copy.deepcopy(incoming)
        bad["orthographic_1"]["data"][0]["value"]["endTime"] = 11
        with self.assertRaises(ReviewRequired):
            apply_record(original, bad, config, {})
        bad_original = copy.deepcopy(original)
        bad_original["PronunciationErrorTags"][0]["eojeolID"] = 7
        with self.assertRaises(ReviewRequired):
            apply_record(bad_original, incoming, config, {})
        self.assertEqual(original["Segmentation"]["eojeols"][0]["start"], 0)

    def test_refuses_output_inside_original(self):
        self.write(self.source / "data.json", {"id": "a", "label": "old"})
        self.write(self.result, {"source_id": "a", "corrected_label": "new"})
        with self.assertRaises(DataError):
            run(self.source, [self.result], self.source / "output", self.config)
        self.assertFalse((self.source / "output").exists())

    def test_separate_runs_never_overwrite(self):
        one = self.execute({"id": "a", "label": "old"}, {"source_id": "a", "corrected_label": "new"})
        two = run(self.source, [self.result], self.output, self.config)
        self.assertNotEqual(one.directory, two.directory)
        self.assertEqual(self.data(one), self.data(two))

    def test_cancelled_run_is_marked_and_original_untouched(self):
        event = threading.Event()
        event.set()
        result = self.execute({"id": "a", "label": "old"}, {"source_id": "a", "corrected_label": "new"}, cancel=event)
        self.assertEqual(result.summary["status"], "cancelled")
        self.assertEqual(json.loads((self.source / "data.json").read_text())["label"], "old")

    def test_malformed_source_preserved_and_reported_as_file_issue(self):
        (self.source / "broken.json").write_text('{"broken":', encoding="utf-8")
        self.write(self.result, [{"source_id": "a", "corrected_label": "new"}])
        result = run(self.source, [self.result], self.output, self.config)
        self.assertEqual(result.summary["source_file_issue_counts"], {"invalid_source": 1})
        self.assertEqual((result.directory / "data" / "broken.json").read_bytes(), (self.source / "broken.json").read_bytes())

    def test_no_partial_acceptance_of_broken_export(self):
        self.write(self.source / "data.json", {"id": "a", "label": "old"})
        self.result.write_text('{"source_id":"a","corrected_label":"new"}\n{broken}', encoding="utf-8")
        result = run(self.source, [self.result], self.output, self.config)
        self.assertEqual(self.data(result)["label"], "old")
        self.assertEqual(result.summary["result_issue_counts"], {"invalid_result": 1})

    def test_report_html_escapes_untrusted_values(self):
        result = self.execute({"id": "<script>alert(1)</script>", "label": "old"}, {"source_id": "<script>alert(1)</script>", "corrected_label": ""})
        html = (result.directory / "reports" / "report.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertEqual(csv_safe("=1+1"), "'=1+1")

    def test_config_rejects_unknown_ops_and_typos(self):
        for edit in (lambda c: c.update({"rulez": []}), lambda c: c["rules"][0].update({"to": "label"}), lambda c: c["rules"][0].update({"kind": "delete"}), lambda c: c["source"].update({"glob": "../*.json"})):
            config = copy.deepcopy(self.config)
            edit(config)
            with self.assertRaises(DataError):
                validate_config(config)

    def test_all_bundled_profiles_validate(self):
        for path in (ROOT / "profiles").glob("*.json"):
            self.assertIsInstance(load_config(path), dict)

    def test_invalid_json_values_and_duplicate_fields_rejected(self):
        for text in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', '{}garbage'):
            with self.assertRaises(DataError):
                parse_json(text)

    def test_json_pointer_escaped_names_and_numeric_dict_keys(self):
        self.assertEqual(get_pointer({"a/b": {"~": {"01": 3}}}, "/a~1b/~0/01"), 3)
        self.assertFalse(equivalent({"x": True}, {"x": 1}))

    def test_explicit_scaling_does_not_change_other_coordinates(self):
        self.config["rules"] = [{"kind": "field", "from": {"path": "/ms", "transforms": [{"op": "number"}, {"op": "scale", "value": 0.001}]}, "to": "/start", "type": "number"}]
        result = self.execute({"id": "a", "start": 0, "end": 10}, {"source_id": "a", "ms": "1500"})
        self.assertEqual(self.data(result), {"id": "a", "start": 1.5, "end": 10})

    def test_split_twenty_thousand_records_into_two_disjoint_halves(self):
        original = [{"id": str(i), "label": "old", "metadata": {"preserve": i}} for i in range(20000)]
        incoming = [{"source_id": str(i), "corrected_label": "new"} for i in range(10000)]
        result = self.execute(original, incoming)
        changed = json.loads((result.directory / "changed" / "labels" / "data.json").read_text(encoding="utf-8"))
        unchanged = json.loads((result.directory / "unchanged" / "labels" / "data.json").read_text(encoding="utf-8"))
        self.assertEqual(result.summary["partition_records"], {"changed": 10000, "unchanged": 10000})
        self.assertEqual([row["id"] for row in changed], [str(i) for i in range(10000)])
        self.assertEqual(unchanged, original[10000:])
        self.assertTrue(all(row["label"] == "new" and row["metadata"]["preserve"] == int(row["id"]) for row in changed))
        self.assertEqual(len(self.data(result)), 20000)
        self.assertEqual(json.loads((self.source / "data.json").read_text()), original)

    def test_split_individual_files_preserves_names_and_unchanged_reasons(self):
        originals = {
            "a/one.json": {"id": "a", "label": "old"},
            "b/one.json": {"id": "b", "label": "same"},
            "c/missing.json": {"id": "c", "label": "keep"},
            "d/review.json": {"id": "d", "label": "keep"},
        }
        for name, value in originals.items():
            self.write(self.source / name, value)
        self.write(self.result, [{"source_id": "a", "corrected_label": "new"}, {"source_id": "b", "corrected_label": "same"}, {"source_id": "d", "corrected_label": ""}])
        (self.source / "media.wav").write_bytes(b"unchanged media")
        result = run(self.source, [self.result], self.output, self.config)
        self.assertEqual(result.summary["partition_records"], {"changed": 1, "unchanged": 3})
        self.assertEqual({p.relative_to(result.directory / "changed").as_posix() for p in (result.directory / "changed").rglob("*") if p.is_file()}, {"labels/a/one.json"})
        for name in ("b/one.json", "c/missing.json", "d/review.json"):
            self.assertEqual((result.directory / "unchanged" / "labels" / name).read_bytes(), (self.source / name).read_bytes())
        self.assertFalse((result.directory / "changed" / "media.wav").exists())
        self.assertTrue((result.directory / "data" / "media.wav").exists())
        states = {row["key"]: (row["status"], row["output_group"]) for row in self.report_rows(result)}
        self.assertEqual(states["c"], ("no_result", "unchanged"))
        self.assertEqual(states["d"], ("review_required", "unchanged"))

    def test_split_nested_wrapper_preserves_metadata_and_record_order(self):
        self.config["source"]["records_path"] = "/payload/rows"
        original = {"meta": {"schema": "keep"}, "payload": {"other": [1, 2], "rows": [{"id": "a", "label": "old"}, {"id": "b", "label": "same"}, {"id": "c", "label": "old"}]}}
        result = self.execute(original, [{"source_id": "c", "corrected_label": "new_c"}, {"source_id": "a", "corrected_label": "new_a"}])
        changed = json.loads((result.directory / "changed" / "labels" / "data.json").read_text())
        unchanged = json.loads((result.directory / "unchanged" / "labels" / "data.json").read_text())
        self.assertEqual(changed["meta"], original["meta"])
        self.assertEqual(changed["payload"]["other"], [1, 2])
        self.assertEqual([item["id"] for item in changed["payload"]["rows"]], ["a", "c"])
        self.assertEqual(unchanged["payload"]["rows"], [original["payload"]["rows"][1]])

    def test_split_jsonl_reindexes_documents_without_losing_records(self):
        self.config["source"]["glob"] = "**/*.jsonl"
        path = self.source / "rows.jsonl"
        path.write_text('{"id":"a","label":"keep"}\n{"id":"b","label":"old"}\n{"id":"c","label":"keep"}\n', encoding="utf-8")
        self.write(self.result, {"source_id": "b", "corrected_label": "new"})
        result = run(self.source, [self.result], self.output, self.config)
        changed = read_document(result.directory / "changed" / "labels" / "rows.jsonl")
        unchanged = read_document(result.directory / "unchanged" / "labels" / "rows.jsonl")
        self.assertEqual(changed.docs, [{"id": "b", "label": "new"}])
        self.assertEqual([item["id"] for item in unchanged.docs], ["a", "c"])
        index = [json.loads(line) for line in (result.directory / "reports" / "partition_index.jsonl").read_text().splitlines()]
        item = next(row for row in index if row["group"] == "changed")
        self.assertEqual((item["source_record_path"], item["partition_record_path"]), ("doc:1", "doc:0"))

    def test_split_unparseable_files_are_not_claimed_as_unchanged_records(self):
        (self.source / "broken.json").write_bytes(b'{"broken":')
        self.write(self.source / "no_key.json", {"label": "keep"})
        self.write(self.result, [])
        result = run(self.source, [self.result], self.output, self.config)
        self.assertEqual(result.summary["partition_records"], {"changed": 0, "unchanged": 1})
        self.assertEqual(result.summary["partition_files"], {"changed": 0, "unchanged": 1, "unclassified": 1})
        self.assertEqual((result.directory / "unclassified" / "labels" / "broken.json").read_bytes(), b'{"broken":')
        self.assertTrue((result.directory / "unchanged" / "labels" / "no_key.json").exists())

    def test_split_preview_counts_but_creates_no_subset_files(self):
        result = self.execute([{"id": "a", "label": "old"}, {"id": "b", "label": "keep"}], {"source_id": "a", "corrected_label": "new"}, preview=True)
        self.assertEqual(result.summary["partition_records"], {"changed": 1, "unchanged": 1})
        self.assertEqual(result.summary["partition_status"], "planned")
        for name in ("data", "changed", "unchanged"):
            self.assertFalse((result.directory / name).exists())

    def test_split_can_be_disabled(self):
        result = self.execute({"id": "a", "label": "old"}, {"source_id": "a", "corrected_label": "new"}, split_output=False)
        self.assertEqual(result.summary["partition_status"], "disabled")
        self.assertFalse((result.directory / "changed").exists())
        self.assertFalse((result.directory / "reports" / "partition_index.jsonl").exists())

    def test_net_no_change_goes_to_unchanged_even_if_rules_temporarily_modify(self):
        self.config["rules"].append({"kind": "field", "from": {"literal": "old"}, "to": "/label"})
        result = self.execute({"id": "a", "label": "old"}, {"source_id": "a", "corrected_label": "temporary"})
        self.assertEqual(result.summary["partition_records"], {"changed": 0, "unchanged": 1})
        self.assertEqual(result.summary["changed_fields"], 0)
        self.assertEqual((result.directory / "unchanged" / "labels" / "data.json").read_bytes(), (self.source / "data.json").read_bytes())

    def test_old_asset_config_is_removed_and_external_media_not_required(self):
        self.config["assets"] = {"key": "/missing_media_path", "match": "relative_path"}
        result = self.execute({"id": "a", "label": "old"}, {"source_id": "a", "corrected_label": "new"})
        self.assertEqual(result.summary["status"], "completed")
        self.assertNotIn("assets", result.summary)
        self.assertNotIn("assets", json.loads((result.directory / "profile.json").read_text(encoding="utf-8")))
        self.assertFalse(any(path.name in ("assets", "assets.csv") for path in result.directory.rglob("*")))
        self.assertEqual(self.data(result)["label"], "new")


if __name__ == "__main__":
    unittest.main()
