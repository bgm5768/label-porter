import copy
import json
import tempfile
import time
import tkinter as tk
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from label_porter.config import validate_config
from label_porter.easy_rules import analyze_inputs, select_key, make_rule, connection_option, key_stats
from label_porter.formats import DataError
from label_porter.gui import App
from label_porter.rules import apply_record, ReviewRequired


class EasyRulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"; self.source.mkdir()
        self.result = self.base / "corrected.json"

    def write(self, path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def audio(self):
        before = {"sound_id": "A", "Segmentation": {"unit": "eojeol", "eojeols": [
            {"eojeolID": 1, "text": "old", "start": 0, "end": 1}]},
            "RecordingMetadata": {"orthographic": "old", "recorded_time_sec": 2},
            "PronunciationErrorTags": [{"eojeolID": 99, "error_type": "independent"}],
            "metadata": {"keep": "original"}}
        after = copy.deepcopy(before)
        after["Segmentation"]["eojeols"] = [
            {"eojeolID": 77, "text": "new", "start": 2.86789, "end": 3.45, "extra": {"keep": True}},
            {"eojeolID": 21, "text": "word", "start": 1.2, "end": 2.3}]
        after["RecordingMetadata"]["orthographic"] = "  new   word "
        after["PronunciationErrorTags"] = []
        after["metadata"] = {"keep": "do not import"}
        self.write(self.source/"a.json", before); self.write(self.result, [after])
        return before, after

    def prepare(self):
        inputs = analyze_inputs(self.source, [self.result])
        self.assertTrue(inputs.candidates[0]["recommended"])
        data, options = select_key(inputs, inputs.candidates[0]["path"])
        selected = [item["path"] for item in options if item["selected"]]
        return inputs, data, options, selected

    def test_audio_preserves_result_ids_precision_spacing_and_unknown_item_fields(self):
        before, after = self.audio()
        inputs, data, options, selected = self.prepare()
        self.assertEqual(inputs.candidates[0]["path"], "/sound_id")
        self.assertCountEqual(selected, ["/Segmentation/eojeols", "/RecordingMetadata/orthographic"])
        rule_config = make_rule(data, options, selected)
        validate_config(json.loads(json.dumps(rule_config)))
        actual, _, _, status = apply_record(before, after, rule_config, {})
        self.assertEqual(status, "updated")
        self.assertEqual(actual["Segmentation"]["eojeols"], after["Segmentation"]["eojeols"])
        self.assertEqual(actual["RecordingMetadata"]["orthographic"], "  new   word ")
        self.assertEqual(actual["PronunciationErrorTags"], before["PronunciationErrorTags"])
        self.assertEqual(actual["metadata"], before["metadata"])
        self.assertEqual(actual["RecordingMetadata"]["recorded_time_sec"], 2)
        # Explicit whole-list replacement also allows clearing the list.
        after["Segmentation"]["eojeols"] = []
        actual, *_ = apply_record(before, after, rule_config, {})
        self.assertEqual(actual["Segmentation"]["eojeols"], [])
        after["Segmentation"]["eojeols"] = [{"eojeolID": 1, "text": "bad", "start": 5, "end": 4}]
        with self.assertRaises(ReviewRequired): apply_record(before, after, rule_config, {})
        no_check = make_rule(data, options, selected, check_times=False)
        actual, *_ = apply_record(before, after, no_check, {})
        self.assertEqual(actual["Segmentation"]["eojeols"][0]["start"], 5)

    def test_wrappers_escaped_paths_and_duplicates_use_real_key_values(self):
        left = [{"id": "A", "a/b": {"~label": "old"}}, {"id": "B", "a/b": {"~label": "old"}}, {"id": "B", "a/b": {"~label": "dup"}}]
        right = [{"id": "A", "a/b": {"~label": "new"}}, {"id": "B", "a/b": {"~label": "new"}}, {"id": "C", "a/b": {"~label": "orphan"}}]
        self.write(self.source/"all.json", {"meta": {"version": 1}, "payload": {"rows": left}})
        self.write(self.result, {"records": right})
        inputs = analyze_inputs(self.source, [self.result])
        data, options = select_key(inputs, "/id")
        self.assertEqual(inputs.source_spec["records_path"], "/payload/rows")
        self.assertEqual(inputs.result_spec["records_path"], "/records")
        self.assertEqual(inputs.candidates[0]["source_duplicates"], 2)
        self.assertEqual(CounterStates(data), {"연결됨": 1, "KEY 중복": 3, "보완만 있음": 1})
        path = "/a~1b/~0label"
        config = make_rule(data, options, [path])
        actual, *_ = apply_record(left[0], right[0], config, {})
        self.assertEqual(actual["a/b"]["~label"], "new")
        with self.assertRaises(DataError): make_rule(data, options, ["/id"])
        with self.assertRaises(DataError): make_rule(data, options, [])

    def test_jsonl_typed_keys_and_missing_keys_are_not_guessed(self):
        (self.source/"rows.jsonl").write_text('{"id":1,"label":"old"}\n{"id":"1","label":"old"}\n{"label":"missing"}\n', encoding="utf-8")
        self.write(self.result, [{"id": "1", "label": "new"}, {"id": 1, "label": "numeric"}])
        inputs, data, options, selected = self.prepare()
        self.assertEqual(inputs.source_spec["glob"], "**/*.jsonl")
        self.assertEqual(inputs.candidates[0]["matched"], 2)
        self.assertEqual(CounterStates(data), {"연결됨": 2, "KEY 해석 불가": 1})
        self.assertEqual(data.results[data.source_keys[0]][0]["label"], "numeric")

    def test_unrecognized_or_ambiguous_data_and_cancellation_are_explicit(self):
        self.write(self.source/"one.json", {"id": "A", "text": "before"})
        self.write(self.result, [{"platform_id": "A", "value": "after"}])
        inputs = analyze_inputs(self.source, [self.result])
        self.assertEqual(inputs.candidates[0]["result_path"], "/platform_id")
        with self.assertRaisesRegex(DataError, "중단"): analyze_inputs(self.source, [self.result], cancel=lambda: True)
        self.write(self.result, {"data": [{"id": "A"}], "records": [{"id": "A"}]})
        with self.assertRaisesRegex(DataError, "여러 개"): analyze_inputs(self.source, [self.result])
        with self.assertRaisesRegex(DataError, "다른 파일"): analyze_inputs(self.source, [self.source/"one.json"])

    def test_platform_id_and_nested_correction_preserve_blank_and_other_fields(self):
        originals = [{"id": "A", "instruction": "old A", "output": "keep A"},
                     {"id": "B", "instruction": "old B", "output": "keep B"}]
        incoming = [{"dataID": str(i), "importData_id": row["id"], "importData_instruction": row["instruction"],
                     "instruction_error_modify": {"data": [{"value": "new A" if i == 0 else ""}]}}
                    for i, row in enumerate(originals)]
        self.write(self.source/"all.json", originals)
        # The platform uses whitespace-separated objects, despite the .json extension.
        self.result.write_text("\n".join(json.dumps(r) for r in incoming), encoding="utf-8")
        inputs = analyze_inputs(self.source, [self.result])
        self.assertEqual((inputs.candidates[0]["path"], inputs.candidates[0]["result_path"]), ("/id", "/importData_id"))
        self.assertEqual(inputs.candidates[0]["matched"], 2)
        data, options = select_key(inputs, "/id", result_path="/importData_id")
        selected = [o["path"] for o in options if o["selected"]]
        self.assertEqual(selected, ["/instruction"])
        rule_config = make_rule(data, options, selected)
        self.assertEqual(rule_config["rules"][0]["from"], "/instruction_error_modify/data/0/value")
        a, *_ = apply_record(originals[0], incoming[0], rule_config, {})
        b, *_ = apply_record(originals[1], incoming[1], rule_config, {})
        self.assertEqual(a, {**originals[0], "instruction": "new A"})
        self.assertEqual(b, originals[1])
        incoming[0]["instruction_error_modify"]["data"].append({"value": "do not silently discard"})
        with self.assertRaises(ReviewRequired): apply_record(originals[0], incoming[0], rule_config, {})
        missing = copy.deepcopy(incoming[1]); missing.pop("instruction_error_modify")
        actual, *_ = apply_record(originals[1], missing, rule_config, {})
        self.assertEqual(actual, originals[1])

    def test_manual_connections_without_recommendations_and_empty_value_policy(self):
        before = {"id": "A", "text": "old", "items": [{"label": "old"}]}
        after = {"dataID": "B", "new_text": "", "values": [{"label": "new"}]}
        self.write(self.source/"one.json", before); self.write(self.result, after)
        inputs = analyze_inputs(self.source, [self.result])
        self.assertEqual(inputs.candidates, [])
        data, options = select_key(inputs, "/id", result_path="/dataID")
        self.assertEqual(key_stats(inputs, "/id", "/dataID")["matched"], 0)
        self.assertEqual(CounterStates(data), {"원본만 있음": 1, "보완만 있음": 1})
        item = connection_option(inputs, data, "/text", "/new_text", skip_empty=False)
        rule_config = make_rule(data, [item], ["/text"])
        actual, *_ = apply_record(before, after, rule_config, {})
        self.assertEqual(actual["text"], "")
        item["skip_empty"] = True
        actual, *_ = apply_record(before, after, make_rule(data, [item], ["/text"]), {})
        self.assertEqual(actual["text"], "old")
        with self.assertRaisesRegex(DataError, "기준 필드"):
            connection_option(inputs, data, "/id", "/new_text")
        array = connection_option(inputs, data, "/items", "/values")
        child = connection_option(inputs, data, "/items/0/label", "/values/0/label")
        with self.assertRaisesRegex(DataError, "동시에"):
            make_rule(data, [array, child], ["/items", "/items/0/label"])


def CounterStates(data):
    from collections import Counter
    return dict(Counter(row["state"] for row in data.catalog))


class EasyRulesUiTests(unittest.TestCase):
    setUp = EasyRulesTests.setUp
    write = EasyRulesTests.write
    audio = EasyRulesTests.audio

    def test_manual_key_and_field_selection_remain_available_without_auto_matches(self):
        self.write(self.source/"a.json", {"id": "A", "question": "old", "answer": "keep"})
        incoming = {"dataID": "platform-9", "reference": "B", "correction": {"data": [{"value": "new"}]}}
        self.write(self.result, incoming)
        root = tk.Tk(); root.withdraw()
        errors = []
        root.report_callback_exception = lambda *args: errors.append("".join(traceback.format_exception(*args)))
        def wait(predicate):
            end = time.monotonic() + 10
            while not predicate() and time.monotonic() < end:
                root.update(); time.sleep(.01)
            self.assertTrue(predicate(), errors)
        try:
            with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
                app = App(root); app._show_builder(); panel = app.builder
                panel.source.set(str(self.source)); panel.results = [str(self.result)]; panel._render_results()
                panel.next(); wait(lambda: panel.inputs is not None and not panel.busy)
                self.assertEqual(panel.stage, 1)
                self.assertEqual(panel.inputs.candidates, [])
                self.assertTrue(panel.key_manual.winfo_manager())
                panel._choose_path(panel.source_key_box, "/id")
                panel._choose_path(panel.result_key_box, "/reference")
                panel._manual_key(); wait(lambda: panel.data is not None and not panel.busy)
                self.assertIn("연결 0쌍", panel.connection.get())
                self.assertTrue(panel.field_manual.winfo_manager())
                incoming["reference"] = "A"; self.write(self.result, incoming)
                panel.refresh(); wait(lambda: panel.inputs is not None and not panel.busy)
                panel._choose_path(panel.source_key_box, "/id")
                panel._choose_path(panel.result_key_box, "/reference")
                panel._manual_key(); wait(lambda: panel.data is not None and not panel.busy)
                panel._choose_path(panel.target_box, "/question")
                panel._choose_path(panel.incoming_box, "/correction/data/0/value")
                panel._apply_connection()
                self.assertEqual(panel.selected, {"/question"})
                panel.next(); wait(lambda: panel.preview.pair is not None)
                self.assertEqual(panel.preview.pair["output"], {"id": "A", "question": "new", "answer": "keep"})
                panel.back(); panel._open_connection(edit=True)
                self.assertEqual(panel._choice(panel.incoming_box), "/correction/data/0/value")
                self.assertTrue(panel.skip_empty.get())
                self.assertFalse(errors, errors)
        finally:
            if "app" in locals(): app._close_mapping(); app._close_builder()
            for job in root.tk.call("after", "info"): root.after_cancel(job)
            root.destroy()

    def test_complete_builder_uses_cached_data_and_existing_validation_workflow(self):
        before, after = self.audio()
        root = tk.Tk(); root.withdraw()
        errors = []
        root.report_callback_exception = lambda *args: (errors.append("".join(traceback.format_exception(*args))), traceback.print_exception(*args))
        def wait(predicate):
            end = time.monotonic() + 10
            while not predicate() and time.monotonic() < end:
                root.update(); time.sleep(.01)
            self.assertTrue(predicate())
        try:
            with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
                app = App(root)
                app._show_builder()
                panel = app.builder
                panel.source.set(str(self.source)); panel.results = [str(self.result)]; panel._render_results()
                app.primary_button.invoke()
                wait(lambda: panel.data is not None and not panel.busy)
                self.assertEqual(panel.stage, 1)
                cache = panel.data
                app._show_tools(1); app._return_from_help()
                self.assertEqual(app.route, "builder")
                self.assertIs(panel.data, cache)
                app.back_button.invoke()
                with patch("label_porter.easy_rules_ui.analyze_inputs", side_effect=AssertionError("No second read")):
                    app.primary_button.invoke()
                    self.assertIs(panel.data, cache)
                    with patch("label_porter.pair_preview_ui.load_preview", side_effect=AssertionError("No second read")):
                        app.primary_button.invoke()
                        wait(lambda: panel.preview is not None and panel.preview.pair is not None)
                        wait(lambda: str(app.primary_button["state"]) == "normal")
                        self.assertEqual(panel.stage, 2)
                        preview_cache = panel.preview.cache
                        app.primary_button.invoke()
                        wait(lambda: app.mapping is not None and app.mapping.preview.pair is not None)
                        self.assertEqual(app.route, "mapping")
                        self.assertIs(app.mapping.preview.cache, preview_cache)
                        self.assertTrue(app._session_ready())
                app.output.set(str(self.base/"saved"))
                app.mapping._apply()
                self.assertEqual(app.current_step, 3)
                self.assertIsNone(app.last_summary)
                app._start(True); wait(lambda: not app.running)
                self.assertEqual(app.last_summary["source_status_counts"], {"planned_update": 1})
                app._start(False); wait(lambda: not app.running)
                actual = json.loads((app.last_run/"data/a.json").read_text(encoding="utf-8"))
                self.assertEqual(actual["Segmentation"]["eojeols"], after["Segmentation"]["eojeols"])
                self.assertEqual(actual["PronunciationErrorTags"], before["PronunciationErrorTags"])
                self.assertEqual(json.loads((self.source/"a.json").read_text(encoding="utf-8")), before)
                # A second builder with the same rule and paths must replace the old preview.
                previous_mapping = app.mapping
                after["RecordingMetadata"]["orthographic"] = "second correction"
                self.write(self.result, [after])
                app._show_step(0); app._show_builder()
                panel = app.builder
                panel.next(); wait(lambda: panel.data is not None and not panel.busy)
                panel.next(); wait(lambda: panel.preview.pair is not None)
                # Reloading inside the preview also refreshes key and field recommendations.
                after["RecordingMetadata"]["orthographic"] = "refreshed correction"
                self.write(self.result, [after])
                panel.preview.load_button.invoke()
                wait(lambda: panel.data is not None and not panel.busy)
                self.assertEqual(panel.stage, 1)
                panel.next(); wait(lambda: panel.preview.pair is not None)
                updated_cache = panel.preview.cache
                panel.next(); wait(lambda: app.mapping is not None and app.mapping.preview.pair is not None)
                self.assertIsNot(app.mapping, previous_mapping)
                self.assertIs(app.mapping.preview.cache, updated_cache)
                self.assertTrue(app._session_ready())
                self.assertEqual(app.mapping.preview.pair["output"]["RecordingMetadata"]["orthographic"], "refreshed correction")
                self.assertFalse(errors, errors)
                app._close_mapping(); app._close_builder()
        finally:
            if "app" in locals():
                app._close_mapping(); app._close_builder()
            for job in root.tk.call("after", "info"): root.after_cancel(job)
            root.destroy()


if __name__ == "__main__":
    unittest.main()
