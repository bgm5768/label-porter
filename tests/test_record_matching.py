import copy
import json
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from label_porter.gui import App
from label_porter.mapping import SampleRows, load_samples, preview_profile
from label_porter.record_matching import RecordMatching


class SelectedPreviewTests(unittest.TestCase):
    def config(self):
        return {"schema_version": 1, "name": "한 건",
                "source": {"key": "/id"}, "result": {"key": "/ref"},
                "rules": [{"kind": "field", "from": "/new", "to": "/label"}]}

    def test_selected_missing_record_never_substitutes_another_matched_record(self):
        report = preview_profile(self.config(), [{"id": "a", "label": "old"}, {"id": "b", "label": "old"}],
                                 [{"ref": "b", "new": "new"}], {}, sample_indices=[0])
        self.assertEqual(report["sampled"], 1)
        self.assertEqual(report["counts"], {"no_result": 1})
        self.assertEqual(report["examples"], [])
        self.assertEqual(report["all_source_key_counts"], {"no_result": 1, "matched": 1})

    def test_selected_record_still_detects_duplicates_outside_selection(self):
        for source, result in (
            ([{"id": "a", "label": "old"}] * 2, [{"ref": "a", "new": "new"}]),
            ([{"id": "a", "label": "old"}], [{"ref": "a", "new": "new"}] * 2),
        ):
            with self.subTest(source=len(source), result=len(result)):
                report = preview_profile(self.config(), source, result, {}, sample_indices=[0])
                self.assertEqual(report["counts"], {"duplicate": 1})
                self.assertFalse(report["examples"])

    def test_matching_uses_key_and_selected_file_provenance(self):
        config = self.config()
        config["source"]["key"] = {"meta": "file_name"}
        rows = SampleRows([{"label": "A"}, {"label": "B"}],
                          [{"file_name": "a.json"}, {"file_name": "b.json", "relative_path": "sub/b.json"}])
        report = preview_profile(config, rows, [{"ref": "b.json", "new": "NEW B"}, {"ref": "a.json", "new": "NEW A"}],
                                 {}, sample_indices=[1])
        self.assertEqual(report["examples"][0]["source_file"], "sub/b.json")
        self.assertEqual(report["examples"][0]["changes"][0]["after"], "NEW B")

    def test_sample_load_progress_and_cancellation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "large.json"
            path.write_text(json.dumps([{"id": i} for i in range(2000)]), encoding="utf-8")
            progress = []
            sample = load_samples(path, progress=progress.append)
            self.assertEqual(len(sample.rows()), 2000)
            self.assertTrue(any("%" in item for item in progress))
            with self.assertRaisesRegex(ValueError, "중단"):
                load_samples(path, cancel=lambda: True)


class OptionalMatchingUiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError:
            self.skipTest("Tk display unavailable")
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.originals = self.base / "originals"
        self.originals.mkdir()
        self.source = self.originals / "all.json"
        self.result = self.base / "result.json"
        self.original = [
            {"id": "a", "label": "old A", "time": 1.0, "tags": ["keep"]},
            {"id": "b", "label": "old B", "time": 2.0, "tags": ["keep B"]},
        ]
        self.incoming = [
            {"ref": "b", "new": "new B", "time": 2.86},
            {"ref": "a", "new": "new A", "time": 3.497333},
        ]
        self.source.write_text(json.dumps(self.original), encoding="utf-8")
        self.result.write_text(json.dumps(self.incoming), encoding="utf-8")
        self.boards = []

    def tearDown(self):
        for board in self.boards:
            if not board.closed:
                board._close()
        for job in self.root.tk.call("after", "info"):
            self.root.after_cancel(job)
        self.root.destroy()
        self.temp.cleanup()

    def until(self, predicate):
        deadline = time.monotonic() + 15
        while not predicate() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertTrue(predicate())
        self.root.update_idletasks()

    def load(self, board):
        self.boards.append(board)
        board.source_path.set(str(self.source))
        board.result_path.set(str(self.result))
        board.load_files()
        self.until(lambda: not board.busy)
        self.assertFalse(board.dirty, board.status.get())

    def connect(self, board, left, right):
        board._pick(("source", left)); board._pick(("result", right))

    def test_optional_flow_applies_rule_then_requires_full_validation_before_copy(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root)
            self.assertFalse(app.manual_options.get())
            self.assertFalse(app.manual_options_frame.winfo_manager())
            app.manual_options.set(True); app._toggle_manual_options()
            app.manual_match_button.invoke()
            board = app.mapping
            self.load(board)
            self.assertEqual(len(board.root_scope.source_rows), 1)
            self.assertEqual(board.root_scope.result_rows[0]["ref"], "b")
            self.connect(board, "/id", "/ref")
            self.connect(board, "/label", "/new")
            self.connect(board, "/time", "/time")
            board.result_transform.set("소수 1자리 반올림"); board._save_options()
            self.assertEqual(str(board.apply_button["state"]), "disabled")
            board._test(); self.until(lambda: not board.busy)
            # Result row 1 is b, but source a must use result row 2.
            self.assertIn("new A", board.preview_box.get("1.0", "end"))
            self.assertNotIn("new B", board.preview_box.get("1.0", "end"))
            self.assertEqual(str(board.apply_button["state"]), "normal")
            board._apply()
            self.assertTrue(board.closed)
            self.assertEqual(app.current_step, 1)
            self.assertEqual(app.source.get(), "")
            self.assertEqual(app.result_paths, [])
            self.assertIsNone(app.preview_fingerprint)
            app.source.set(str(self.originals)); app.result_paths = [str(self.result)]
            app.output.set(str(self.base / "output")); app._render_results()
            app._advance_review()
            app._start(False)
            self.assertFalse(app.running)
            self.assertIsNone(app.last_run)
            before = self.source.read_bytes()
            app._start(True); self.until(lambda: not app.running)
            self.assertIsNotNone(app.preview_fingerprint)
            app._start(False); self.until(lambda: not app.running)
            actual = json.loads((app.last_run / "data" / "all.json").read_text(encoding="utf-8"))
            self.assertEqual(actual[0]["label"], "new A")
            self.assertEqual(actual[0]["time"], 3.5)
            self.assertEqual(actual[1]["time"], 2.9)
            self.assertEqual(actual[0]["tags"], ["keep"])
            self.assertEqual(self.source.read_bytes(), before)

    def test_switching_one_record_keeps_partial_wires_and_invalidates_check(self):
        board = RecordMatching(self.root, lambda *_: None)
        board.window.withdraw(); self.load(board)
        self.connect(board, "/id", "/ref")
        board.source_number.set("2"); board.result_number.set("1"); board._select_pair()
        self.assertEqual(len(board.root_scope.wires), 1)
        self.assertEqual(board.root_scope.source_fields["/id"].examples, ["b"])
        self.connect(board, "/label", "/new")
        board._test(); self.until(lambda: not board.busy)
        self.assertIsNotNone(board.last_checked)
        board.source_number.set("1"); board._select_pair()
        self.assertIsNone(board.last_checked)
        self.assertEqual(str(board.apply_button["state"]), "disabled")
        self.assertEqual(len(board.root_scope.wires), 2)
        board.source_number.set("999"); board._select_pair()
        self.assertEqual(board.active_pair, (0, 0))
        self.assertIn("범위", board.status.get())

    def test_find_same_key_selects_matching_result_before_value_wiring(self):
        board = RecordMatching(self.root, lambda *_: None)
        board.window.withdraw(); self.load(board)
        self.connect(board, "/id", "/ref")
        board._find_result(); self.until(lambda: not board.busy)
        self.assertEqual(board.active_pair, (0, 1))
        self.assertEqual(board.root_scope.result_fields["/new"].examples, ["new A"])
        self.assertEqual(len(board.root_scope.wires), 1)
        board.result_rows.append(board.result_rows[1])
        board._find_result(); self.until(lambda: not board.busy)
        self.assertIn("중복", board.status.get())
        self.assertEqual(board.active_pair, (0, 1))

    def test_cancel_does_not_replace_existing_ai_rule_or_inputs(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root)
            config = SelectedPreviewTests().config()
            app._set_config(config)
            app.source.set(str(self.originals)); app.result_paths = [str(self.result)]
            before = copy.deepcopy(app._get_config())
            app._manual_match()
            board = app.mapping; self.boards.append(board)
            board._close()
            self.assertEqual(app.route, "workflow")
            self.assertEqual(app._get_config(), before)
            self.assertEqual(app.source.get(), str(self.originals))
            self.assertEqual(app.result_paths, [str(self.result)])

    def test_nested_container_selection_and_array_wiring_survive_pair_switch(self):
        self.source.write_text(json.dumps({"records": [
            {"id": "a", "words": [{"id": 1, "text": "old"}]},
            {"id": "b", "words": [{"id": 2, "text": "old b"}]}]}), encoding="utf-8")
        self.result.write_text(json.dumps({"items": [
            {"ref": "a", "words": [{"id": 1, "text": "new"}]},
            {"ref": "b", "words": [{"id": 2, "text": "new b"}]}]}), encoding="utf-8")
        board = RecordMatching(self.root, lambda *_: None)
        board.window.withdraw(); self.load(board)
        self.connect(board, "/id", "/ref")
        self.connect(board, "/words", "/words")
        board._enter_array()
        self.connect(board, "/id", "/id"); self.connect(board, "/text", "/text")
        board._go_back()
        before = board._config()
        board.source_number.set("2"); board.result_number.set("2"); board._select_pair()
        self.assertEqual(board._config(), before)
        child = board.root_scope.wires[1].child
        self.assertEqual(child.source_fields["/text"].examples, ["old b"])
        board._test(); self.until(lambda: not board.busy)
        self.assertIn("new b", board.preview_box.get("1.0", "end"))


if __name__ == "__main__":
    unittest.main()
