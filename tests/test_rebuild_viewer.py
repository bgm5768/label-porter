import copy
import json
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path

from label_porter.config import load_config, validate_config
from label_porter.engine import run
from label_porter.formats import Document
from label_porter.json_viewer import JsonViewer, bounded_preview
from label_porter.rule_review import RuleReview, profile_connections
from label_porter.rules import apply_record


PROFILE = Path(__file__).parent / "fixtures" / "platform_full.json"


def item(id, text, start):
    return {"objectID": f"orthographic_1_100_{id}",
            "value": {"name_J2REGU": text, "startTime": start, "endTime": start + .237}}


def original():
    return {"RecordingMetadata": {"orthographic": "keep removed change", "prompt": "preserve"},
            "Segmentation": {"unit": "eojeol", "eojeols": [
                {"eojeolID": 1, "text": "keep", "start": 1, "end": 2},
                {"eojeolID": 2, "text": "removed", "start": 2, "end": 3},
                {"eojeolID": 3, "text": "change", "start": 3, "end": 4}]},
            "PronunciationErrorTags": [{"eojeolID": i, "error_type": str(i)} for i in range(1, 4)],
            "metadata": {"must": "stay"}}


class RebuildTests(unittest.TestCase):
    def test_independent_tags_keep_original_values_ids_and_order_without_review(self):
        profile = load_config(PROFILE.with_name("platform_preserve_tags.json"))
        before = original()
        incoming = {"orthographic_1": {"data": [item(99, "entirely changed", 2.86)]}}
        notices = []
        after, changes, _, status = apply_record(before, incoming, profile, {}, advisories=notices)
        self.assertEqual(status, "updated")
        self.assertEqual(len(after["Segmentation"]["eojeols"]), 1)
        self.assertEqual(after["RecordingMetadata"]["orthographic"], "entirely changed")
        self.assertEqual(after["PronunciationErrorTags"], before["PronunciationErrorTags"])
        self.assertEqual(notices, [])
        self.assertTrue(all(c["path"] in ("/Segmentation/eojeols", "/RecordingMetadata/orthographic") for c in changes))

    def test_all_text_times_additions_deletions_and_reference_quarantine(self):
        profile = load_config(PROFILE)
        before = original(); snapshot = copy.deepcopy(before)
        incoming = {"orthographic_1": {"data": [item(99, "added", 4.123), item(3, "changed", 3.156), item(1, "keep", 2.86)]}}
        notices = []
        after, changes, notes, status = apply_record(before, incoming, profile, {}, advisories=notices)
        self.assertEqual(status, "updated")
        self.assertEqual(before, snapshot)
        self.assertEqual(after["RecordingMetadata"], {"orthographic": "keep changed added", "prompt": "preserve"})
        self.assertEqual(after["Segmentation"]["eojeols"], [
            {"eojeolID": 1, "text": "keep", "start": 2.9, "end": 3.1},
            {"eojeolID": 2, "text": "changed", "start": 3.2, "end": 3.4},
            {"eojeolID": 3, "text": "added", "start": 4.1, "end": 4.4}])
        self.assertEqual(after["metadata"], before["metadata"])
        self.assertEqual(after["PronunciationErrorTags"], [before["PronunciationErrorTags"][0]])
        self.assertEqual([n["source_item"] for n in notices], before["PronunciationErrorTags"][1:])

    def test_reordering_renumbers_only_certain_references(self):
        before = original()
        incoming = {"orthographic_1": {"data": [item(3, "change", 1), item(1, "keep", 2)]}}
        after, _, _, _ = apply_record(before, incoming, load_config(PROFILE), {}, advisories=[])
        self.assertEqual(after["PronunciationErrorTags"],
                         [{"eojeolID": 2, "error_type": "1"}, {"eojeolID": 1, "error_type": "3"}])

    def test_duplicate_text_or_ids_are_not_guessed_but_new_items_still_import(self):
        for duplicate_id in (False, True):
            with self.subTest(duplicate_id=duplicate_id):
                data = [item(1, "keep", 1), item(1 if duplicate_id else 99, "keep", 2)]
                notices = []
                after, _, _, status = apply_record(original(), {"orthographic_1": {"data": data}},
                                                   load_config(PROFILE), {}, advisories=notices)
                self.assertEqual(status, "updated")
                self.assertEqual(len(after["Segmentation"]["eojeols"]), 2)
                self.assertEqual(after["PronunciationErrorTags"], [])
                self.assertEqual(len(notices), 3)

    def test_empty_platform_list_explicitly_removes_old_words_and_archives_tags(self):
        notices = []
        after, _, _, _ = apply_record(original(), {"orthographic_1": {"data": []}}, load_config(PROFILE), {}, advisories=notices)
        self.assertEqual(after["Segmentation"]["eojeols"], [])
        self.assertEqual(after["RecordingMetadata"]["orthographic"], "")
        self.assertEqual(len(notices), 3)

    def test_unreadable_id_does_not_hide_duplicate_identity(self):
        data = [item(1, "keep", 1), item(99, "keep", 2)]
        data[1]["objectID"] = "new-object-without-numeric-id"
        notices = []
        after, _, _, _ = apply_record(original(), {"orthographic_1": {"data": data}},
                                       load_config(PROFILE), {}, advisories=notices)
        self.assertEqual(after["PronunciationErrorTags"], [])
        self.assertEqual(len(after["Segmentation"]["eojeols"]), 2)
        self.assertEqual(len(notices), 3)

    def test_real_output_and_reports_distinguish_imported_data_from_held_tags(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp); source = base / "source"; source.mkdir()
            before = original()
            path = source / "a.json"; path.write_text(json.dumps(before), encoding="utf-8")
            incoming = {"importData_ID_M4AAXZ": "a.json", "orthographic_1": {"data": [item(1, "keep", 2.86)]}}
            result = base / "result.json"; result.write_text(json.dumps(incoming), encoding="utf-8")
            outcome = run(source, [result], base / "out", load_config(PROFILE))
            self.assertEqual(outcome.summary["source_status_counts"], {"updated": 1})
            self.assertEqual(outcome.summary["advisory_rows"], 1)
            self.assertEqual(outcome.summary["held_references"], 2)
            actual = json.loads((outcome.directory / "data/a.json").read_text(encoding="utf-8"))
            self.assertEqual(len(actual["Segmentation"]["eojeols"]), 1)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), before)
            reports = outcome.directory / "reports"
            held = [json.loads(line) for line in (reports / "held_references.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([i["source_item"] for i in held], before["PronunciationErrorTags"][1:])
            detail = json.loads((reports / "review/000001.json").read_text(encoding="utf-8"))
            self.assertEqual(detail["final_record"], actual)
            self.assertTrue(detail["applied_with_advisories"])
            self.assertEqual(outcome.summary["partition_records"]["changed"], 1)

    def test_profile_review_and_validation(self):
        config = load_config(PROFILE)
        self.assertTrue(any(r["kind"] == "rebuild_items" for r in profile_connections(config)))
        config["rules"][0]["identity_guards"] = []
        with self.assertRaises(ValueError):
            validate_config(config)


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk(); self.root.withdraw()
        self.viewer = JsonViewer(self.root); self.viewer.window.withdraw()

    def tearDown(self):
        self.root.destroy()

    def test_counts_and_lazy_expansion_for_large_array(self):
        records = [{"items": list(range(137))} for _ in range(19934)]
        self.viewer.display(Document([records], "digest"))
        root = self.viewer.tree.get_children()[0]
        self.assertIn("19,934", self.viewer.status.get())
        self.assertEqual(len(self.viewer.tree.get_children(root)), 101)
        first = self.viewer.tree.get_children(root)[0]
        self.viewer.tree.focus(first); self.viewer.expand()
        array = self.viewer.tree.get_children(first)[0]
        self.viewer.tree.selection_set(array); self.viewer.select()
        self.assertIn("137개 항목", self.viewer.selection_info.get())
        self.viewer.tree.focus(array); self.viewer.expand()
        marker = self.viewer.tree.get_children(array)[-1]
        self.viewer.tree.focus(marker); self.viewer.load_more()
        self.assertEqual(len(self.viewer.tree.get_children(array)), 137)

    def test_background_load_and_stop_bind_to_current_job(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "items.json"
            path.write_text('[{"items":[1,2,3]}]', encoding="utf-8")
            self.viewer.load(path)
            self.viewer.stop_button.invoke()
            self.assertTrue(self.viewer.cancel.is_set())
            self.viewer.load(path)
            deadline = time.monotonic() + 5
            while self.viewer.loading and time.monotonic() < deadline:
                self.root.update(); time.sleep(.01)
            self.assertFalse(self.viewer.loading)
            self.assertIn("최상위 항목 1개", self.viewer.status.get())

    def test_new_profile_opens_readonly_review(self):
        review = RuleReview(self.root, load_config(PROFILE), lambda *args: None, lambda: None, lambda *args: None)
        self.root.update_idletasks()
        self.assertEqual(review.profile, load_config(PROFILE))
        review._close()

    def test_preview_is_bounded(self):
        text = json.dumps(bounded_preview([{"text": "a" * 100000}] * 20000))
        self.assertLess(len(text), 25000)


if __name__ == "__main__":
    unittest.main()
