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
            self.assertEqual(len(app.steps.tabs()), 4)
            self.assertEqual(str(app.steps.tab(1, "state")), "disabled")
            config = {"schema_version": 1, "name": "UI test", "source": {"key": "/id"}, "result": {"key": "/ref"},
                      "rules": [{"kind": "field", "from": "/new", "to": "/label", "type": "string"}]}
            app._set_config(config)
            app._advance_inputs()
            self.assertEqual(app.steps.index(app.steps.select()), 1)
            app.source.set(str(self.source)); app.result_paths = [str(self.rf)]
            app.output.set(str(self.base / "output")); app._render_results()
            self.confirm_connections(app)
            self.assertEqual(app.steps.index(app.steps.select()), 3)
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
            self.assertTrue(app.rule_ready)
            self.assertFalse(app.connection_confirmed)
            self.assertEqual(str(app.steps.tab(3, "state")), "disabled")
            self.assertIsNone(app.preview_fingerprint)
            self.assertEqual(app.current_step, 0)

    def config(self):
        return {"schema_version": 1, "name": "프로젝트", "source": {"key": "/id"}, "result": {"key": "/ref"},
                "rules": [{"kind": "field", "from": "/new", "to": "/label"}]}

    def configure_inputs(self, app):
        app.source.set(str(self.source)); app.result_paths = [str(self.rf)]
        app.output.set(str(self.base / "out")); app._render_results()

    def confirm_connections(self, app):
        app._wizard()
        board = app.mapping; self.boards.append(board)
        self.assertIsNotNone(board, app.status.get())
        self.until(lambda: board.preview.pair is not None or "실패" in board.preview.status.get())
        self.assertEqual(board.preview.pair["key"], "a", board.preview.status.get())
        board.apply_button.invoke()
        self.root.update()
        self.assertEqual(app.current_step, 3)

    def test_import_advances_to_inputs_then_automatically_loads_pair(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings") as save:
            app = App(self.root)
            rule_file = self.base / 'ai_rules.json'
            rule_file.write_text(json.dumps(self.config()), encoding='utf-8')
            with patch('label_porter.gui.filedialog.askopenfilename', return_value=str(rule_file)):
                app._load_profile()
            self.assertEqual(app.current_step, 1)
            self.assertIsNone(app.mapping)
            self.assertTrue(app.rule_ready)
            save.assert_called()
            self.configure_inputs(app)
            # The first source file is unmatched: auto preview must find a real pair.
            (self.source/'0.json').write_text('{"id":"missing"}', encoding='utf-8')
            app.primary_button.invoke(); self.root.update()
            board = app.mapping; self.boards.append(board)
            self.until(lambda: board.preview.pair is not None)
            self.assertTrue(board.preview.configured)
            self.assertEqual(board.preview.pair['key'], 'a')
            self.assertEqual(board.preview.source.get(), str(self.source))
            self.assertEqual(board.preview.results, [str(self.rf)])
            self.assertEqual(str(board.raw_box['state']), 'disabled')
            self.assertFalse(board.preview.spin.winfo_ismapped())
            board.apply_button.invoke(); self.root.update()
            self.assertEqual(app.current_step, 3)
            self.assertTrue(app.connection_confirmed)
            self.assertIsNone(app.preview_fingerprint)
            with patch('label_porter.workflow.run') as run:
                app._start(False)
                run.assert_not_called()
            self.assertFalse((self.base/'out').exists())

    def test_connections_require_inputs_and_changed_inputs_require_confirmation(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root); app._set_config(self.config())
            app._wizard()
            self.assertIsNone(app.mapping)
            self.assertEqual(app.current_step, 1)
            self.configure_inputs(app); self.confirm_connections(app)
            app.output.set(str(self.base/'other-output'))
            self.assertFalse(app.connection_confirmed)
            app._navigate(3)
            self.assertEqual(app.current_step, 2)
            self.boards.append(app.mapping)

    def test_inline_help_returns_to_exact_view_without_new_window(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root)
            self.assertFalse(hasattr(app, 'help_window'))
            self.assertEqual([w['text'] for w in app.toolbar.winfo_children()], ['JSON 뷰어'])
            self.assertFalse(any(isinstance(w, tk.Toplevel) for w in self.root.winfo_children()))
            app.help_button.invoke(); self.root.update()
            self.assertEqual(app.route, 'help')
            self.assertEqual(str(app.back_button['state']), 'normal')
            app.back_button.invoke()
            self.assertEqual(app.current_step, 0)
            self.assertEqual(str(app.back_button['state']), 'disabled')
            app._set_config(self.config()); self.configure_inputs(app); app._wizard()
            board = app.mapping; self.boards.append(board)
            self.until(lambda: board.preview.pair is not None)
            board.pages.select(1); board.preview.views.select(1)
            pair = board.preview.pair
            before = (app.page_title.get(), app.page_caption.get(), app.status.get())
            app._show_tools(0); app._show_tools(1); self.root.update()
            self.assertEqual(app.route, 'help')
            self.assertEqual(app.current_step, 2)
            self.assertFalse(board.closed)
            self.assertEqual(app.tabs.index(app.tabs.select()), 1)
            app.back_button.invoke(); self.root.update()
            self.assertEqual(app.route, 'mapping')
            self.assertIs(app.mapping, board)
            self.assertIs(board.preview.pair, pair)
            self.assertEqual(board.pages.index(board.pages.select()), 1)
            self.assertEqual(board.preview.views.index(board.preview.views.select()), 1)
            self.assertEqual((app.page_title.get(), app.page_caption.get(), app.status.get()), before)
            app.primary_button.invoke()
            self.assertEqual(app.current_step, 3)
            app.primary_button.invoke(); self.until(lambda: not app.running)
            fingerprint = app.preview_fingerprint
            app.help_button.invoke(); app.back_button.invoke()
            self.assertEqual(app.current_step, 3)
            self.assertEqual(app.preview_fingerprint, fingerprint)
            self.assertEqual(app.primary_button['text'], '복사본 저장')
            app.help_button.invoke(); app._navigate(1)
            self.assertEqual(app.route, 'workflow')
            self.assertEqual(app.current_step, 1)
            self.assertEqual(app.source.get(), str(self.source))

    def test_no_unique_example_still_allows_full_validation_and_reports_missing_pairs(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root); app._set_config(self.config()); self.configure_inputs(app)
            self.rf.write_text('{"ref":"other","new":"value"}', encoding="utf-8")
            app._wizard(); board = app.mapping; self.boards.append(board)
            self.until(lambda: "유일하게 연결" in board.preview.status.get())
            self.assertIsNone(board.preview.pair)
            board.apply_button.invoke(); self.root.update()
            self.assertEqual(app.current_step, 3)
            app.primary_button.invoke(); self.until(lambda: not app.running)
            self.assertEqual(app.last_summary["source_status_counts"]["no_result"], 1)
            self.assertEqual(app.last_summary["result_issue_counts"]["orphan_result"], 1)
            self.assertTrue(app.preview_fingerprint)

    def test_rule_and_inputs_persist_without_draft_json(self):
        with patch.object(App, "_load_settings"):
            app = App(self.root); app.settings_path = self.base/'settings.json'
            app._set_config(self.config()); self.configure_inputs(app)
            app.rule_file.set('saved-profile.json'); app._save_settings()
            app._set_config({**self.config(), 'name': 'other'})
            app.source.set(''); app.result_paths = []
        app._load_settings()
        self.assertEqual(app._get_config(), self.config())
        self.assertEqual(app.source.get(), str(self.source))
        self.assertEqual(app.result_paths, [str(self.rf)])
        self.assertEqual(app.rule_file.get(), 'saved-profile.json')
        self.assertFalse(app.connection_confirmed)

    def test_source_edit_after_preview_requires_revalidation_and_metrics_are_separate(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root); app._set_config(self.config()); app._advance_inputs()
            app.source.set(str(self.source)); app.result_paths = [str(self.rf)]
            app.output.set(str(self.base/'out')); app._render_results(); self.confirm_connections(app)
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
            self.configure_inputs(app)
            app._wizard(); board = app.mapping; self.boards.append(board)
            self.root.update()
            self.assertGreater(board.canvas.winfo_height(), 70)
            self.assertFalse(board.apply_button.winfo_ismapped())
            self.assertTrue(app.primary_button.winfo_ismapped())
            self.assertEqual(app.primary_button.winfo_height(), app.back_button.winfo_height())
            self.assertEqual(app.primary_button.winfo_width(), app.back_button.winfo_width())
            self.assertLessEqual(app.primary_button.winfo_rooty()+app.primary_button.winfo_height(), self.root.winfo_rooty()+self.root.winfo_height())
            for item in board.canvas.find_all():
                self.assertLessEqual(board.canvas.bbox(item)[2], board.canvas.winfo_width()+2)
            self.assertFalse(board.canvas.bind('<B1-Motion>'))
            self.assertFalse(board.canvas.bind('<Delete>'))
            board._close(); self.root.update()
            self.assertEqual(app.route, 'workflow')

    def test_navigation_positions_and_names_stay_consistent_across_steps(self):
        with patch.object(App, '_load_settings'), patch.object(App, '_save_settings'):
            app = App(self.root); self.root.geometry('1040x680'); self.root.deiconify()
            app._set_config(self.config()); self.configure_inputs(app)
            rectangles = []
            for index in (0, 1, 2):
                app._navigate(index); self.root.update()
                rectangles.append(tuple((b.winfo_x(), b.winfo_rooty(), b.winfo_width(), b.winfo_height()) for b in (app.back_button, app.primary_button)))
                self.assertEqual(app.primary_button['text'], '다음 단계 →')
                self.assertEqual(app.back_button['text'], '← 이전 단계')
            self.assertEqual(rectangles[0], rectangles[1])
            self.assertEqual(rectangles[1], rectangles[2])
            self.boards.append(app.mapping)
            before = app.back_button.winfo_rootx()
            app.help_button.invoke(); self.root.update()
            self.assertEqual(app.back_button.winfo_rootx(), before)
            app.back_button.invoke(); app.primary_button.invoke(); self.root.update()
            self.assertEqual(app.current_step, 3)
            self.assertEqual(app.primary_button['text'], '전체 데이터 검증')

    def test_import_replaces_project_inputs_but_invalid_import_preserves_them(self):
        with patch.object(App, '_load_settings'), patch.object(App, '_save_settings'):
            app = App(self.root); app._set_config(self.config()); self.configure_inputs(app)
            original = self.sf.read_bytes()
            path = self.base/'profile.json'; path.write_text('{}', encoding='utf-8')
            with patch('label_porter.gui.filedialog.askopenfilename', return_value=str(path)):
                app._load_profile()
                self.assertEqual(app.source.get(), str(self.source))
                self.assertEqual(app._get_config(), self.config())
                path.write_text(json.dumps({**self.config(), 'name': 'new rules'}), encoding='utf-8')
                app._load_profile()
            self.assertEqual(app.current_step, 1)
            self.assertEqual(app.source.get(), '')
            self.assertEqual(app.result_paths, [])
            self.assertEqual(app._get_config()['name'], 'new rules')
            self.assertEqual(self.sf.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
