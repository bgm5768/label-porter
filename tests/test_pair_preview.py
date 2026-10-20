import copy
import json
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from label_porter.config import validate_config
from label_porter.formats import DataError
from label_porter.pair_preview import load_preview, match_preview, matching_indices, search_catalog, catalog_preview
from label_porter.pair_preview_ui import PairPreview
from label_porter.rules import apply_record


def profile():
    return validate_config({
        "schema_version": 1, "name": "Example",
        "source": {"key": "/id"}, "result": {"key": "/id"},
        "rules": [{"kind": "field", "from": "/value", "to": "/value"}],
    })


class PairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source.json"
        self.result = self.base / "result.json"
        self.config = profile()

    def write(self, original, incoming):
        self.source.write_text(json.dumps(original), encoding="utf-8")
        self.result.write_text(json.dumps(incoming), encoding="utf-8")

    def load(self):
        return load_preview(self.config, self.source, [self.result])

    def test_key_order_value_precision_and_no_mutation(self):
        self.write({"id": "b", "value": 1.2}, [{"id": "a", "value": 9}, {"id": "b", "value": 2.86789}])
        before = (self.source.read_bytes(), self.result.read_bytes(), copy.deepcopy(self.config))
        data = self.load()
        pair = match_preview(data)
        self.assertEqual(pair["key"], "b")
        self.assertEqual(pair["candidate"]["value"], 2.86789)
        self.assertEqual(pair["output"], pair["candidate"])
        self.assertEqual(pair["rows"][1]["result"], "2.86789")
        self.assertEqual(pair["candidate"], apply_record(pair["source"], pair["result"], data.profile, {})[0])
        self.assertEqual(before, (self.source.read_bytes(), self.result.read_bytes(), self.config))

    def test_trailing_zeroes_removed_without_rounding(self):
        self.source.write_text('{"id":1,"value":0}', encoding="utf-8")
        self.result.write_text('[{"id":1,"value":1.20000}]', encoding="utf-8")
        self.assertEqual(match_preview(self.load())["rows"][1]["result"], "1.2")

    def test_missing_duplicate_and_type_sensitive_keys_never_guess(self):
        for incoming in ([{"id": 1, "value": 9}], [{"id": "1", "value": 2}, {"id": "1", "value": 3}], [{"value": 3}]):
            with self.subTest(incoming=incoming):
                self.write({"id": "1", "value": 0}, incoming)
                with self.assertRaises(DataError):
                    match_preview(self.load())
        self.write([{"id": 1, "value": 0}, {"id": 1, "value": 0}], {"id": 1, "value": 9})
        with self.assertRaisesRegex(DataError, "원본 파일"):
            match_preview(self.load())

    def test_duplicates_across_result_files(self):
        self.write({"id": 1, "value": 0}, {"id": 1, "value": 2})
        other = self.base / "other.json"
        other.write_bytes(self.result.read_bytes())
        with self.assertRaisesRegex(DataError, "2건"):
            match_preview(load_preview(self.config, self.source, [self.result, other]))

    def test_directory_preview_checks_global_uniqueness_and_source_glob(self):
        folder = self.base / "originals"; folder.mkdir()
        for name, key in (("0", "missing"), ("1", "duplicate"), ("2", "duplicate"), ("3", "valid")):
            (folder / (name + ".json")).write_text(json.dumps({"id": key, "value": 0}), encoding="utf-8")
        (folder / "skip.txt").write_text("not JSON", encoding="utf-8")
        self.result.write_text(json.dumps([{"id": "duplicate", "value": 9}, {"id": "valid", "value": 2}]), encoding="utf-8")
        data = load_preview(self.config, folder, [self.result])
        self.assertEqual(matching_indices(data), [3])
        self.assertEqual(match_preview(data, 3)["candidate"]["value"], 2)
        with self.assertRaisesRegex(DataError, "같은 KEY"):
            match_preview(data, 1)
        self.config["source"]["glob"] = "3.json"
        self.assertEqual(matching_indices(load_preview(self.config, folder, [self.result])), [0])

    def test_directory_relative_path_metadata_in_nested_files(self):
        folder = self.base / "originals"; (folder / "nested").mkdir(parents=True)
        (folder / "nested" / "one.json").write_text('{"value":0}', encoding="utf-8")
        self.config["source"]["key"] = {"meta": "relative_path"}
        self.result.write_text('[{"id":"nested/one.json","value":7}]', encoding="utf-8")
        pair = match_preview(load_preview(self.config, folder, [self.result]))
        self.assertEqual(pair["key"], "nested/one.json")
        self.assertEqual(pair["candidate"]["value"], 7)

    def test_filename_metadata_and_result_row(self):
        self.config["source"]["key"] = {"meta": "file_name"}
        self.config["result"]["key"] = "/file"
        self.config["rules"][0]["from"] = {"meta": "result_row"}
        self.write({"value": 0}, [{"file": "other"}, {"file": "source.json"}])
        self.assertEqual(match_preview(self.load())["candidate"]["value"], 2)
        self.config["source"]["key"] = {"meta": "relative_path"}
        self.assertEqual(match_preview(self.load())["key"], "source.json")

    def test_nested_records_and_key_transform(self):
        self.config["source"].update(records_path="/records", record_mode="array")
        self.config["result"].update(records_path="/rows", record_mode="array",
                                     key={"path": "/id", "transforms": [{"op": "string"}]})
        self.write({"records": [{"id": "5", "value": 0}]}, {"rows": [{"id": 5, "value": 7}]})
        self.assertEqual(match_preview(self.load())["candidate"]["value"], 7)

    def test_jsonl_and_strict_decoding_and_cancel(self):
        self.write({"id": 2, "value": 0}, [])
        self.result = self.base / "result.jsonl"
        self.result.write_text('{"id":1,"value":3}\n{"id":2,"value":4}\n', encoding="utf-8")
        self.assertEqual(match_preview(self.load())["candidate"]["value"], 4)
        with self.assertRaisesRegex(DataError, "중단"):
            load_preview(self.config, self.source, [self.result], cancel=lambda: True)
        self.result.write_text('{"id":2,"id":2,"value":4}\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.load()

    def test_conditions_and_validation_still_run(self):
        self.write({"id": 1, "value": 0}, {"id": 1, "value": 2})
        check = {"left": "/id", "op": "eq", "value": 99}
        self.config["rules"][0]["when"] = [check]
        pair = match_preview(self.load())
        self.assertEqual(pair["status"], "unchanged")
        self.assertIn("해당하지", pair["rows"][1]["note"])
        self.config["include_when"] = [check]
        excluded = match_preview(self.load())
        self.assertEqual(excluded["status"], "excluded")
        self.assertEqual(excluded["output"], excluded["source"])
        self.config.pop("include_when")
        self.config["assertions"] = [{"kind": "condition", "condition": check}]
        pair = match_preview(self.load())
        self.assertEqual(pair["status"], "review_required")
        self.assertIsNone(pair["candidate"])
        self.assertEqual(pair["output"], pair["source"])

    def test_items_match_by_id_and_rebuild_has_no_fake_original(self):
        rule = {"kind": "items", "from": "/parts", "to": "/parts", "source_key": {"scope": "source", "path": "/id"}, "result_key": "/id",
                "rules": [{"kind": "field", "from": "/value", "to": "/value"}]}
        self.config["rules"] = [rule]
        self.write({"id": 1, "parts": [{"id": 1, "value": "first"}, {"id": 2, "value": "second"}]},
                   {"id": 1, "parts": [{"id": 2, "value": "new second"}, {"id": 1, "value": "new first"}]})
        pair = match_preview(self.load())
        self.assertEqual(pair["rows"][2]["source_path"], "/parts/1/value")
        self.assertEqual(pair["rows"][2]["source"], '"second"')
        rule.update(kind="rebuild_items", sort_by="/id")
        rule["rules"][0]["allow_create"] = True
        rule["rules"].append({"kind": "field", "from": {"meta": "item_number"}, "to": "/id", "allow_create": True})
        self.write({"id": 1, "parts": [], "PronunciationErrorTags": [{"id": 9}]},
                   {"id": 1, "parts": [{"id": 4, "value": "four"}, {"id": 2, "value": "two"}]})
        pair = match_preview(self.load())
        self.assertEqual(pair["candidate"]["parts"], [{"value": "two", "id": 1}, {"value": "four", "id": 2}])
        self.assertEqual(pair["candidate"]["PronunciationErrorTags"], [{"id": 9}])
        self.assertEqual(pair["rows"][2]["source"], "필드 없음")
        self.assertEqual(pair["rows"][2]["source_path"], "/parts/0/value")
        self.assertEqual(pair["rows"][2]["result"], '"two"')

    def test_catalog_includes_every_record_without_guessing_duplicate_pairs(self):
        self.write([{"id": "ok", "value": "원본"}, {"id": "source-only"}, {"id": "dup"}, {"id": "dup"}, {"bad": True}],
                   [{"id": "ok", "value": "수정 문장"}, {"id": "result-only"}, {"id": "dup"}, {"bad": "결과"}])
        data = self.load()
        self.assertEqual(len(data.catalog), 8)
        self.assertEqual(sum(e["source_index"] is not None for e in data.catalog), 5)
        self.assertEqual(sum(e["incoming"] is not None for e in data.catalog), 4)
        self.assertEqual(len(search_catalog(data, state="KEY 중복")), 3)
        found = search_catalog(data, "수정 문장", "보완 JSON")
        self.assertEqual(len(found), 1)
        self.assertEqual(catalog_preview(data, found[0])["output"]["value"], "수정 문장")
        self.assertEqual(search_catalog(data, "수정", "원본 JSON"), [])
        self.assertEqual(len(search_catalog(data, "SOURCE.JSON", "파일명 / 위치")), 5)
        self.assertEqual(len(search_catalog(data, "RESULT-ONLY", "KEY")), 1)
        for index in search_catalog(data, state="KEY 중복"):
            pair = catalog_preview(data, index)
            self.assertEqual(pair["status"], "unpaired")
            self.assertEqual(pair["output"], pair["source"])
            self.assertFalse(pair["source"] is not None and pair["result"] is not None)


class PairUiTests(unittest.TestCase):
    setUp = PairTests.setUp
    write = PairTests.write

    def test_configured_preview_auto_loads_reuses_cache_and_cancels_pending_start(self):
        self.write([{"id": "missing"}, {"id": 1, "value": 0}, {"id": 2, "value": 0}],
                   [{"id": 2, "value": 2}, {"id": 1, "value": 1}])
        root = tk.Tk(); root.withdraw()
        self.addCleanup(root.destroy)
        preview = PairPreview(root, self.config, str(self.source), [str(self.result)], configured=True)
        self.addCleanup(preview.close)
        def wait():
            deadline = time.monotonic() + 10
            while preview.pair is None and time.monotonic() < deadline:
                root.update(); time.sleep(.01)
            self.assertIsNotNone(preview.pair, preview.status.get())
        wait()
        self.assertEqual(preview.pair["key"], 1)
        self.assertEqual(json.loads(preview.output_box.get("1.0", "end-1c")), {"id": 1, "value": 1})
        self.assertEqual(str(preview.output_box["state"]), "disabled")
        self.assertEqual(len(preview.cache.catalog), 3)
        with patch("label_porter.pair_preview_ui.load_preview", side_effect=AssertionError("cache missed")):
            preview._next_example(); wait()
        self.assertEqual(preview.pair["key"], 2)
        # Closing the step before after_idle runs must not start a background read.
        with patch("label_porter.pair_preview_ui.load_preview") as load:
            unopened = PairPreview(root, self.config, str(self.source), [str(self.result)], configured=True)
            unopened.close(); root.update()
            load.assert_not_called()

    def test_pagination_search_empty_results_refresh_and_stale_selection(self):
        self.write([{"id": i, "value": "old"} for i in range(81)],
                   [{"id": i, "value": f"보완 {i}"} for i in range(81)])
        root = tk.Tk(); root.withdraw(); self.addCleanup(root.destroy)
        preview = PairPreview(root, self.config, str(self.source), [str(self.result)], configured=True)
        self.addCleanup(preview.close)
        def wait(predicate):
            deadline = time.monotonic() + 10
            while not predicate() and time.monotonic() < deadline: root.update(); time.sleep(.01)
            self.assertTrue(predicate(), preview.status.get())
        wait(lambda: preview.pair is not None)
        self.assertEqual(len(preview.table.get_children()), 25)
        cache = preview.cache
        with patch("label_porter.pair_preview_ui.load_preview", side_effect=AssertionError("reread")), \
             patch("label_porter.pair_preview_ui.fingerprint", side_effect=AssertionError("rescan")):
            preview._go_page(4); wait(lambda: preview.pair is not None and preview.pair["key"] == 75)
            self.assertEqual(len(preview.table.get_children()), 6)
            preview.query.set("보완 80"); preview.scope.set("보완 JSON")
            wait(lambda: len(preview.filtered) == 1 and preview.pair is not None and preview.pair["key"] == 80)
            self.assertEqual(preview.page, 1)
            preview.query.set("없는문장")
            wait(lambda: not preview.filtered and preview.pair is None)
            self.assertEqual(preview.output_box.get("1.0", "end-1c"), "")
            preview._reset_search(); wait(lambda: len(preview.filtered) == 81 and preview.pair is not None)
            preview.page_size.set("50"); preview._go_page(1)
            self.assertEqual(len(preview.table.get_children()), 50)
            preview.page_number.set("999"); preview._jump_page()
            self.assertEqual(preview.page, 2)
            preview._select_index(79); preview._select_index(80)
            wait(lambda: preview.pair is not None and preview.pair["key"] == 80)
            self.assertIs(preview.cache, cache)
        self.write([{"id": 0, "value": "old"}], [{"id": 0, "value": "new read"}])
        preview.load_button.invoke()
        wait(lambda: preview.cache is not None and preview.cache is not cache and preview.pair is not None)
        self.assertEqual(preview.pair["output"]["value"], "new read")

    def test_async_load_cache_invalidation_and_close(self):
        self.write([{"id": 1, "value": 0}, {"id": 2, "value": 0}],
                   [{"id": 2, "value": 2}, {"id": 1, "value": 1}])
        root = tk.Tk(); root.withdraw()
        self.addCleanup(root.destroy)
        preview = PairPreview(root, self.config, str(self.source), [str(self.result)])
        self.addCleanup(preview.close)
        def wait():
            deadline = time.monotonic() + 10
            while preview.busy and time.monotonic() < deadline:
                root.update()
                time.sleep(.01)
            self.assertFalse(preview.busy)
        preview.load(); wait()
        self.assertEqual(preview.pair["key"], 1)
        self.assertTrue(all(str(b["state"]) == "disabled" for b in preview.json_boxes))
        preview.number.set("2")
        self.assertIsNone(preview.pair)
        with patch("label_porter.pair_preview_ui.load_preview", side_effect=AssertionError("cache missed")):
            preview.load(); wait()
        self.assertEqual(preview.pair["key"], 2)
        generation = preview.generation
        preview.cancel()
        preview.queue.put((generation, "pair", {"bad": True}))
        root.after(100, root.quit); root.mainloop()
        self.assertIsNone(preview.pair)
        preview.source.set(str(self.base / "missing.json"))
        self.assertIsNone(preview.cache)


if __name__ == "__main__":
    unittest.main()
