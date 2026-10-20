import json
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from label_porter.gui import App


class WorkflowUiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError:
            self.skipTest("Tk display unavailable")
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "originals"
        self.source.mkdir()
        self.sf = self.source / "one.json"
        self.rf = self.base / "result.json"
        self.sf.write_text(json.dumps({"id": "a", "label": "old", "score": 1}), encoding="utf-8")
        self.rf.write_text(json.dumps({"ref": "a", "new": "new", "score": 2}), encoding="utf-8")
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
        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertTrue(predicate())
        self.root.update_idletasks()


    def test_main_steps_and_preview_requirement_then_actual_copy(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root)
            self.assertEqual(len(app.steps.tabs()), 3)
            self.assertEqual(str(app.steps.tab(1, "state")), "disabled")
            config = {"schema_version": 1, "name": "UI test", "source": {"key": "/id"}, "result": {"key": "/ref"},
                      "rules": [{"kind": "field", "from": "/new", "to": "/label", "type": "string"}]}
            app._set_config(config)
            app._advance_inputs()
            self.assertEqual(app.steps.index(app.steps.select()), 1)
            app.source.set(str(self.source)); app.result_paths = [str(self.rf)]
            app.output.set(str(self.base / "output")); app._render_results()
            app._advance_review()
            self.assertEqual(app.steps.index(app.steps.select()), 2)
            self.assertIn("전체 데이터 검증", app.primary_button["text"])
            self.assertIsNone(app.preview_fingerprint)
            before = self.sf.read_bytes()
            app._start(True)
            self.until(lambda: not app.running)
            self.assertEqual(str(app.run_button["state"]), "normal", app.status.get())
            self.assertFalse((app.last_run / "data").exists())
            app._start(False)
            self.until(lambda: not app.running)
            self.assertEqual(self.sf.read_bytes(), before)
            actual = json.loads((app.last_run / "data" / "one.json").read_text(encoding="utf-8"))
            self.assertEqual(actual["label"], "new")
            app._set_config({**config, "name": "Changed rule"})
            self.assertFalse(app.rule_ready)
            self.assertEqual(str(app.steps.tab(1, "state")), "disabled")
            self.assertIsNone(app.preview_fingerprint)
            self.assertEqual(app.current_step, 0)

    def config(self):
        return {"schema_version": 1, "name": "프로젝트", "source": {"key": "/id"}, "result": {"key": "/ref"},
                "rules": [{"kind": "field", "from": "/new", "to": "/label"}]}

    def test_ai_import_opens_readonly_connections_and_advances_without_extra_save(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root)
            rule_file = self.base/'ai_rules.json'
            rule_file.write_text(json.dumps(self.config()), encoding='utf-8')
            with patch('label_porter.gui.filedialog.askopenfilename', return_value=str(rule_file)):
                app._load_profile()
            board = app.mapping
            self.boards.append(board)
            self.assertIsInstance(board.window, __import__('tkinter').ttk.Frame)
            self.assertEqual(board.profile, self.config())
            self.assertEqual(len(board.rows), 2)
            self.assertEqual(str(board.raw_box['state']), 'disabled')
            board.apply_button.invoke()
            self.root.update()
            self.assertTrue(board.closed)
            self.assertEqual(app.current_step, 1)
            self.assertEqual(app.steps.index(app.steps.select()), 1)
            self.assertTrue(app.rule_ready)
            self.assertEqual(app.source.get(), '')
            app.source.set(str(self.source)); app.result_paths = [str(self.rf)]
            app.output.set(str(self.base/'out')); app._render_results()
            self.root.update()  # Deferred Tk events must not send the user back to step 1.
            self.assertEqual(app.current_step, 1)
            app.primary_button.invoke()
            self.root.update()
            self.assertEqual(app.current_step, 2)

    def test_imported_rule_without_samples_can_advance_but_cannot_skip_full_validation(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root)
            app._set_config(self.config())
            app._wizard()
            board = app.mapping; self.boards.append(board)
            self.root.update()
            board.apply_button.invoke(); self.root.update()
            self.assertEqual(app.current_step, 1)
            self.assertEqual(app._get_config(), self.config())
            app.source.set(str(self.source)); app.result_paths = [str(self.rf)]
            app.output.set(str(self.base/'out')); app._render_results(); app._advance_review()
            with patch('label_porter.workflow.run') as run:
                app._start(False)
                run.assert_not_called()
            self.assertIn("다시 실행", app.status.get())
            self.assertFalse((self.base/'out').exists())

    def test_json_draft_only_applies_explicitly_and_persists_custom_rule(self):
        with patch.object(App, "_load_settings"):
            app = App(self.root)
            app.settings_path = self.base/'settings.json'
            config = self.config()
            app._set_config(config)
            changed = {**config, "name": "custom saved"}
            app.editor.delete('1.0', 'end'); app.editor.insert('1.0', json.dumps(changed))
            self.root.update()
            self.assertEqual(app._get_config(), config)
            app._apply_json()
            self.boards.append(app.mapping)
            self.assertEqual(app._get_config(), changed)
            self.assertEqual(app.route, 'mapping')
            app.mapping._close()
            app._save_settings()
            app._set_config(config)
            # Read the persisted value through the actual loader after the patch exits.
        app._load_settings()
        self.assertEqual(app._get_config(), changed)

    def test_source_edit_after_preview_requires_revalidation_and_metrics_are_separate(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root); app._set_config(self.config()); app._advance_inputs()
            app.source.set(str(self.source)); app.result_paths = [str(self.rf)]
            app.output.set(str(self.base/'out')); app._render_results(); app._advance_review()
            app.primary_button.invoke(); self.until(lambda: not app.running)
            self.assertEqual(app.metric_values['changed'].get(), '1')
            self.assertEqual(app.primary_button['text'], '복사본 저장')
            self.sf.write_text(json.dumps({'id':'a', 'label':'changed externally'}), encoding='utf-8')
            app.primary_button.invoke()
            self.assertFalse(app.running)
            self.assertIsNone(app.preview_fingerprint)
            self.assertIn('전체 데이터 검증', app.primary_button['text'])
            self.assertFalse(any((self.base/'out').glob('????-??-??')))
            app._show_summary({'source_records':4,'result_records':3,'preview':True,'status':'completed_with_issues',
                              'source_status_counts':{'planned_update':1,'unchanged':1,'no_result':1,'review_required':1},
                              'result_issue_counts':{'orphan_result':2},'source_file_issue_counts':{'invalid_source':1},
                              'partition_records':{'changed':1,'unchanged':3}})
            self.assertEqual([v.get() for v in app.metric_values.values()], ['4','3','2','0','1','1','3','1'])
            self.assertIn('미변경 3건', app.review_note.get())

    def test_navigation_visible_at_small_window_and_embedded_canvas_fits(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root)
            self.root.geometry('1040x680'); self.root.deiconify(); self.root.update()
            for config in (None, self.config()):
                if config: app._set_config(config)
                self.root.update()
                button = app.primary_button
                self.assertGreater(button.winfo_width(), 80)
                self.assertLessEqual(button.winfo_rooty()+button.winfo_height(), self.root.winfo_rooty()+self.root.winfo_height())
            app._wizard(); board = app.mapping; self.boards.append(board)
            self.root.update()
            self.assertGreater(board.canvas.winfo_height(), 70)
            self.assertLessEqual(board.apply_button.winfo_rooty()+board.apply_button.winfo_height(), self.root.winfo_rooty()+self.root.winfo_height())
            for item in board.canvas.find_all():
                self.assertLessEqual(board.canvas.bbox(item)[2], board.canvas.winfo_width()+2)
            self.assertFalse(board.canvas.bind('<B1-Motion>'))
            self.assertFalse(board.canvas.bind('<Delete>'))
            board._close(); self.root.update()
            self.assertEqual(app.route, 'workflow')

    def test_new_project_clears_old_inputs_without_touching_files(self):
        with patch.object(App, '_load_settings'), patch.object(App, '_save_settings'):
            app = App(self.root); app._set_config(self.config()); app._advance_inputs()
            app.source.set(str(self.source)); app.output.set(str(self.base/'out'))
            app.result_paths = [str(self.rf)]; app._render_results()
            original = self.sf.read_bytes()
            app._new_project(); self.root.update()
            self.assertEqual(app.current_step, 0)
            self.assertEqual(app.source.get(), '')
            self.assertEqual(app.result_paths, [])
            self.assertEqual(app._config_text, '')
            self.assertEqual(self.sf.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
