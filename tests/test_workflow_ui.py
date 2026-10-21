import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from label_porter.gui import App
from label_porter.workflow import default_output_directory


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
            # Cancel scheduling; each widget deletes its own registered command.
            self.root.tk.call('after', 'cancel', job)
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
            self.assertEqual(app.primary_button["text"], "복사본 저장")
            self.assertTrue(app.preview_fingerprint)
            before = self.sf.read_bytes()
            self.assertFalse((app.last_run / "data").exists())
            self.assertTrue((self.base / "output" / "미리보기").exists())
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

    def test_symmetric_inputs_accept_multiple_files_or_folder_and_preview_both(self):
        with patch.object(App, '_load_settings'), patch.object(App, '_save_settings'):
            app = App(self.root); app._set_config(self.config())
            self.assertEqual([b['text'] for b in app.source_panel.buttons], [b['text'] for b in app.result_panel.buttons])
            with patch('label_porter.workflow.filedialog.askopenfilenames', return_value=[str(self.sf), str(self.sf)]):
                app._choose_source_files()
            self.assertEqual(app.source_files, [str(self.sf)])
            with patch('label_porter.workflow.filedialog.askdirectory', return_value=str(self.source)):
                app._choose_source()
            self.assertEqual(app.source_files, [])
            self.assertEqual(app.source.get(), str(self.source))
            with patch('label_porter.workflow.filedialog.askopenfilenames', return_value=[str(self.sf)]):
                app._choose_source_files()
            self.assertEqual(app.source.get(), '')
            folder = self.base/'corrected'; folder.mkdir()
            incoming = folder/'data.json'; incoming.write_bytes(self.rf.read_bytes())
            with patch('label_porter.workflow.filedialog.askdirectory', return_value=str(folder)):
                app._choose_result_folder()
            self.assertEqual(app._result_files(), [str(incoming)])
            with patch('label_porter.json_viewer.JsonViewer') as viewer:
                app._json_viewer('source')
                self.assertEqual(viewer.call_args.kwargs['paths'], [str(self.sf)])
                app._json_viewer('result')
                self.assertEqual(viewer.call_args.kwargs['paths'], [str(folder)])
            app.output.set(str(self.base/'out'))
            self.confirm_connections(app)
            self.assertEqual(app.mapping.preview.source_files, (str(self.sf),))
            app._start(False); self.until(lambda: not app.running)
            self.assertEqual(json.loads((app.last_run/'data/one.json').read_text(encoding='utf-8'))['label'], 'new')

    def test_file_selections_and_result_folder_restore_from_settings(self):
        with patch.object(App, '_load_settings'):
            app = App(self.root)
        app.settings_path = self.base/'settings.json'
        app._set_config(self.config()); app.source_files = [str(self.sf)]; app.result_folder = str(self.base)
        app._save_settings()
        app.source_files = []; app.result_folder = ''
        app._load_settings()
        self.assertEqual(app.source_files, [str(self.sf)])
        self.assertEqual(app.result_folder, str(self.base))
        self.assertEqual(app.source_list.get(0), str(self.sf))

    def configure_inputs(self, app):
        app.source.set(str(self.source)); app.result_paths = [str(self.rf)]
        app.output.set(str(self.base / "out")); app._render_results()

    def confirm_connections(self, app):
        app._wizard()
        self.until(lambda: not app.running)
        board = app.mapping; self.boards.append(board)
        self.assertIsNotNone(board, app.status.get())
        self.until(lambda: board.preview.pair is not None or "실패" in board.preview.status.get())
        self.assertEqual(board.preview.pair["key"], "a", board.preview.status.get())
        board.apply_button.invoke()
        self.root.update()
        self.assertEqual(app.current_step, 3)
        app._start(True)
        self.until(lambda: not app.running)

    def test_stage3_never_runs_full_engine_or_creates_temporary_results(self):
        with patch.object(App, '_load_settings'), patch.object(App, '_save_settings'):
            app = App(self.root); app._set_config(self.config()); self.configure_inputs(app)
            with patch('label_porter.engine.run', side_effect=AssertionError('full engine in stage 3')), \
                 patch('tempfile.TemporaryDirectory', side_effect=AssertionError('staged result directory')):
                app._wizard(); self.until(app._session_ready)
                self.boards.append(app.mapping)
                self.assertEqual(app.mapping.preview.pair['output']['label'], 'new')
                app.primary_button.invoke()
                self.assertEqual(app.current_step, 3)
            self.assertIsNone(app.last_summary)
            self.assertIsNone(app.preview_fingerprint)
            self.assertFalse((self.base/'out').exists())
            self.assertEqual(app.primary_button['text'], '전체 데이터 검증')
            # Stage 4 reads current inputs rather than publishing stage 3's example.
            self.rf.write_text('{"ref":"a","new":"latest"}', encoding='utf-8')
            app.primary_button.invoke(); self.until(lambda: not app.running)
            self.assertTrue(app.preview_fingerprint)
            self.assertTrue((app.last_run/'reports/report.html').is_file())
            self.assertFalse((app.last_run/'data').exists())
            app.primary_button.invoke(); self.until(lambda: not app.running)
            actual = json.loads((app.last_run/'data/one.json').read_text(encoding='utf-8'))
            self.assertEqual(actual['label'], 'latest')
            self.assertEqual(json.loads(self.sf.read_text(encoding='utf-8'))['label'], 'old')

    def test_own_reports_inside_result_folder_do_not_invalidate_validation(self):
        with patch.object(App, '_load_settings'), patch.object(App, '_save_settings'):
            app = App(self.root); app._set_config(self.config()); self.configure_inputs(app)
            folder = self.base/'incoming'; folder.mkdir()
            self.rf.replace(folder/'result.json')
            app.result_folder = str(folder); app.result_paths = []
            app.output.set(str(folder/'output')); app._render_results()
            self.confirm_connections(app)
            self.assertTrue(app.preview_fingerprint)
            app._start(False); self.until(lambda: not app.running)
            self.assertTrue(app._saved(), app.status.get())
            self.assertEqual(app.last_summary['result_records'], 1)

    def test_processing_blocks_all_next_routes_and_cancel_then_retry(self):
        from label_porter.rules import apply_record
        entered, release = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            entered.set()
            release.wait(5)
            return apply_record(*args, **kwargs)
        with patch.object(App, '_load_settings'), patch.object(App, '_save_settings'):
            app = App(self.root); app._set_config(self.config()); self.configure_inputs(app)
            with patch('label_porter.pair_preview.apply_record', side_effect=slow):
                app._wizard()
                try:
                    self.until(entered.is_set)
                    self.boards.append(app.mapping)
                    self.assertTrue(app.preparing)
                    self.assertEqual(app.primary_button['state'], 'disabled')
                    self.assertEqual(app.nav_buttons[3]['state'], 'disabled')
                    beats = []
                    self.root.after(0, lambda: beats.append(True)); self.root.update()
                    self.assertEqual(beats, [True])
                    app._primary(); app._navigate(3); app._finish_mapping()
                    self.assertEqual(app.current_step, 2)
                    app._cancel()
                finally: release.set()
                self.until(lambda: not app.running)
            self.assertFalse(app._session_ready())
            self.assertEqual(app.nav_buttons[3]['state'], 'disabled')
            self.assertFalse((self.base/'out').exists())
            app.primary_button.invoke()
            self.until(app._session_ready)
            self.assertEqual(app.nav_buttons[3]['state'], 'normal')
            app.primary_button.invoke()
            self.assertEqual(app.current_step, 3)
            self.assertEqual(app.metric_values['changed'].get(), '—')
            self.assertEqual(app.primary_button['text'], '전체 데이터 검증')

    def test_preview_search_paging_and_return_do_not_evaluate_again(self):
        originals = [{'id': str(i), 'label': 'old'} for i in range(65)]
        results = [{'ref': str(i), 'new': f'changed-{i}'} for i in range(65)]
        self.sf.write_text(json.dumps(originals), encoding='utf-8')
        self.rf.write_text(json.dumps(results), encoding='utf-8')
        with patch.object(App, '_load_settings'), patch.object(App, '_save_settings'):
            app = App(self.root); app._set_config(self.config()); self.configure_inputs(app)
            app._wizard(); self.until(app._session_ready)
            board = app.mapping; self.boards.append(board)
            self.until(lambda: board.preview.pair is not None)
            preview = board.preview
            self.assertEqual(len(preview.table.get_children()), 25)
            with patch('label_porter.engine.run', side_effect=AssertionError('full processing in preview')):
                preview._go_page(3)
                self.assertEqual(len(preview.table.get_children()), 15)
                preview.scope.set('보완 JSON'); preview.query.set('changed-64')
                self.until(lambda: len(preview.filtered) == 1 and preview.pair and preview.pair['key'] == '64')
                self.assertEqual(preview.pair['output']['label'], 'changed-64')
                app._finish_mapping(); app._back()
                self.assertIs(app.mapping.preview, preview)
                self.assertEqual(preview.query.get(), 'changed-64')

    def test_import_shows_rule_then_explicit_next_automatically_loads_pair(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings") as save:
            app = App(self.root)
            rule_file = self.base / 'ai_rules.json'
            rule_file.write_text(json.dumps(self.config()), encoding='utf-8')
            with patch('label_porter.gui.filedialog.askopenfilename', return_value=str(rule_file)):
                app._load_rule()
            self.assertEqual(app.current_step, 0)
            self.assertIsNotNone(app.rule_inspector)
            app.primary_button.invoke(); self.root.update()
            self.assertEqual(app.current_step, 1)
            self.assertIsNone(app.mapping)
            self.assertTrue(app.rule_ready)
            save.assert_called()
            self.configure_inputs(app)
            # The first source file is unmatched: auto preview must find a real pair.
            (self.source/'0.json').write_text('{"id":"missing"}', encoding='utf-8')
            app.primary_button.invoke(); self.until(lambda: not app.running)
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
            self.assertFalse((self.base/'out').exists())
            app._start(True); self.until(lambda: not app.running)
            self.assertTrue(app.preview_fingerprint)
            app._start(False); self.until(lambda: not app.running)
            self.assertTrue((app.last_run/'data').exists())

    def test_connections_require_inputs_and_changed_inputs_require_confirmation(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root); app._set_config(self.config())
            app._wizard()
            self.assertIsNone(app.mapping)
            self.assertEqual(app.current_step, 1)
            self.configure_inputs(app); self.confirm_connections(app)
            app.output.set(str(self.base/'other-output'))
            self.assertTrue(app.connection_confirmed)
            session = app.mapping.preview.cache
            app._navigate(3)
            self.assertEqual(app.current_step, 3)
            self.assertIs(app.mapping.preview.cache, session)

    def test_inline_help_returns_to_exact_view_without_new_window(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root)
            self.assertFalse(hasattr(app, 'help_window'))
            self.assertIs(app.viewer_button.master, app.help_button.master)
            self.assertFalse(hasattr(app, 'toolbar'))
            self.assertFalse(any(isinstance(w, tk.Toplevel) for w in self.root.winfo_children()))
            app.help_button.invoke(); self.root.update()
            self.assertEqual(app.route, 'help')
            self.assertEqual(str(app.back_button['state']), 'normal')
            app.back_button.invoke()
            self.assertEqual(app.current_step, 0)
            self.assertEqual(str(app.back_button['state']), 'disabled')
            app._set_config(self.config()); self.configure_inputs(app); app._wizard()
            self.until(lambda: not app.running)
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
            app.tabs.select(2); self.root.update()
            self.assertEqual(app.tabs.tab(2, "text"), "후처리 지원 범위")
            self.assertEqual(app.capabilities_box.source, app.capabilities_text)
            self.assertIn("<table>", app.capabilities_box.html_source)
            app.tabs.select(3); self.root.update()
            self.assertEqual(len(app.tabs.tabs()), 4)
            self.assertEqual(app.tabs.tab(3, "text"), "아키텍처 · 규칙 엔진")
            self.assertEqual(app.architecture_box.source, app.architecture_text)
            self.assertIn('project_rule.json', app.architecture_box.html_source)
            self.assertIn('<table>', app.architecture_box.html_source)
            app.architecture_box.query.set('규칙'); app.architecture_box.find()
            self.assertGreater(app.architecture_box.match_count, 0)
            app.back_button.invoke(); self.root.update()
            self.assertEqual(app.route, 'mapping')
            self.assertIs(app.mapping, board)
            self.assertIs(board.preview.pair, pair)
            self.assertEqual(board.pages.index(board.pages.select()), 1)
            self.assertEqual(board.preview.views.index(board.preview.views.select()), 1)
            self.assertEqual((app.page_title.get(), app.page_caption.get(), app.status.get()), before)
            app.primary_button.invoke()
            self.assertEqual(app.current_step, 3)
            fingerprint = app.preview_fingerprint
            app.help_button.invoke(); app.back_button.invoke()
            self.assertEqual(app.current_step, 3)
            self.assertEqual(app.preview_fingerprint, fingerprint)
            self.assertEqual(app.primary_button['text'], '전체 데이터 검증')
            app.help_button.invoke(); app._navigate(1)
            self.assertEqual(app.route, 'workflow')
            self.assertEqual(app.current_step, 1)
            self.assertEqual(app.source.get(), str(self.source))

    def test_no_unique_example_still_allows_full_validation_and_reports_missing_pairs(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root); app._set_config(self.config()); self.configure_inputs(app)
            self.rf.write_text('{"ref":"other","new":"value"}', encoding="utf-8")
            app._wizard(); self.until(lambda: not app.running); board = app.mapping; self.boards.append(board)
            self.until(lambda: board.preview.pair is not None)
            self.assertEqual(board.preview.pair["status"], "unpaired")
            self.assertEqual(len(board.preview.cache.catalog), 2)
            board.apply_button.invoke(); self.root.update()
            self.assertEqual(app.current_step, 3)
            app.primary_button.invoke(); self.until(lambda: not app.running)
            self.assertEqual(app.last_summary["source_status_counts"]["no_result"], 1)
            self.assertEqual(app.last_summary["result_issue_counts"]["orphan_result"], 1)
            self.assertTrue(app.preview_fingerprint)

    def test_preview_session_survives_steps_and_only_data_or_rule_changes_clear_it(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root); app._set_config(self.config()); self.configure_inputs(app)
            app._wizard(); self.until(lambda: not app.running); board = app.mapping; self.boards.append(board)
            self.until(lambda: board.preview.pair is not None)
            board.pages.select(1); board.preview.views.select(1)
            board.preview.query.set("a")
            self.until(lambda: board.preview.search_job is None)
            pair, cache = board.preview.pair, board.preview.cache
            with patch("label_porter.pair_preview_ui.load_preview", side_effect=AssertionError("reread")), \
                 patch("label_porter.pair_preview_ui.fingerprint", side_effect=AssertionError("rescan")):
                app.primary_button.invoke()
                self.assertEqual(app.current_step, 3)
                app._navigate(0); app._navigate(1); app._navigate(2); self.root.update()
                self.assertIs(app.mapping, board)
                self.assertIs(board.preview.cache, cache)
                self.assertIs(board.preview.pair, pair)
                self.assertEqual(board.preview.query.get(), "a")
                self.assertEqual(board.pages.index(board.pages.select()), 1)
                app._navigate(1); app.output.set(str(self.base/"other"))
                app._navigate(2)
                self.assertIs(app.mapping, board)
            app._navigate(1)
            other = self.base/"other.json"; other.write_bytes(self.rf.read_bytes())
            app.result_paths = [str(other)]; app._invalidate_preview()
            self.assertTrue(board.closed)
            app._wizard(); self.until(lambda: not app.running); replacement = app.mapping; self.boards.append(replacement)
            self.until(lambda: replacement.preview.pair is not None)
            self.assertIsNot(replacement.preview.cache, cache)
            app._navigate(0); app._set_config({**self.config(), "name": "new"})
            self.assertTrue(replacement.closed)

    def test_rule_and_inputs_persist_without_draft_json(self):
        with patch.object(App, "_load_settings"):
            app = App(self.root); app.settings_path = self.base/'settings.json'
            app._set_config(self.config()); self.configure_inputs(app)
            app.rule_file.set('saved-rule.json'); app._save_settings()
            app._set_config({**self.config(), 'name': 'other'})
            app.source.set(''); app.result_paths = []
        app._load_settings()
        self.assertEqual(app._get_config(), self.config())
        self.assertEqual(app.source.get(), str(self.source))
        self.assertEqual(app.result_paths, [str(self.rf)])
        self.assertEqual(app.rule_file.get(), 'saved-rule.json')
        self.assertFalse(app.connection_confirmed)

    def test_source_edit_after_preview_requires_revalidation_and_metrics_are_separate(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root); app._set_config(self.config()); app._advance_inputs()
            app.source.set(str(self.source)); app.result_paths = [str(self.rf)]
            app.output.set(str(self.base/'out')); app._render_results(); self.confirm_connections(app)
            self.assertEqual(app.metric_values['changed'].get(), '1')
            self.assertEqual(app.primary_button['text'], '복사본 저장')
            self.sf.write_text(json.dumps({'id':'a', 'label':'changed externally'}), encoding='utf-8')
            app.primary_button.invoke()
            self.until(lambda: not app.running)
            self.assertFalse(app.running)
            self.assertIsNone(app.preview_fingerprint)
            self.assertEqual('전체 데이터 검증', app.primary_button['text'])
            self.assertEqual(app.steps.index(app.steps.select()), 3)
            self.assertEqual(app.nav_buttons[3]['state'], 'normal')
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
            app._wizard(); self.until(lambda: not app.running); board = app.mapping; self.boards.append(board)
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
            self.until(lambda: board.preview.pair is not None)
            board.pages.select(1); board.preview.views.select(0); self.root.update()
            self.assertGreater(board.preview.table.winfo_height(), 100)
            self.assertTrue(board.preview.next_page.winfo_ismapped())
            self.assertLess(board.preview.next_page.winfo_rooty(), app.back_button.winfo_rooty())
            board.preview._open_selected(); self.root.update()
            self.assertEqual(board.preview.views.index(board.preview.views.select()), 1)
            board._close(); self.root.update()
            self.assertEqual(app.route, 'workflow')

    def test_navigation_positions_and_names_stay_consistent_across_steps(self):
        with patch.object(App, '_load_settings'), patch.object(App, '_save_settings'):
            app = App(self.root); self.root.geometry('1040x680'); self.root.deiconify()
            app._set_config(self.config()); self.configure_inputs(app)
            rectangles = []
            for index in (0, 1, 2):
                app._navigate(index); self.until(lambda: not app.running); self.root.update()
                rectangles.append(tuple((b.winfo_x(), b.winfo_rooty(), b.winfo_width(), b.winfo_height()) for b in (app.back_button, app.primary_button)))
                self.assertEqual(app.primary_button['text'], '전체 검증 단계로 →' if index == 2 else '다음 단계 →')
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
        with patch.object(App, '_load_settings'), patch.object(App, '_save_settings') as save:
            app = App(self.root); app._set_config(self.config()); self.configure_inputs(app)
            previous = app.rule_inspector
            original = self.sf.read_bytes()
            path = self.base/'rule.json'; path.write_text('{}', encoding='utf-8')
            with patch('label_porter.gui.filedialog.askopenfilename', return_value=str(path)), \
                 patch('label_porter.rule_review.PairPreview', side_effect=AssertionError('No data preview before inputs')):
                app._load_rule()
                self.assertEqual(app.source.get(), str(self.source))
                self.assertEqual(app._get_config(), self.config())
                self.assertIs(app.rule_inspector, previous)
                save.assert_not_called()
                path.write_text(json.dumps({**self.config(), 'name': 'new rules'}), encoding='utf-8')
                app._load_rule()
            self.assertEqual(app.current_step, 0)
            self.assertEqual(app.route, 'workflow')
            self.assertTrue(previous.closed)
            inspector = app.rule_inspector
            self.assertIsNone(inspector.preview)
            self.assertEqual([inspector.pages.tab(t, 'text') for t in inspector.pages.tabs()], ['필드 연결', '규칙 JSON'])
            self.assertEqual(json.loads(inspector.raw_box.get('1.0', 'end')), app._get_config())
            self.assertEqual(str(inspector.raw_box['state']), 'disabled')
            self.assertEqual(inspector.rows[0]['source'], '/id')
            self.assertEqual(inspector.rows[0]['result'], '/ref')
            self.assertEqual(inspector.rows[1]['source'], '/label')
            self.assertEqual(inspector.rows[1]['result'], '/new')
            save.assert_called_once()
            self.assertEqual(app.source.get(), '')
            self.assertEqual(app.result_paths, [])
            self.assertEqual(app._get_config()['name'], 'new rules')
            self.assertEqual(self.sf.read_bytes(), original)
            exported = self.base/'exported.json'
            with patch('label_porter.gui.filedialog.asksaveasfilename', return_value=str(exported)):
                app.save_rule_button.invoke()
            self.assertEqual(json.loads(exported.read_text(encoding='utf-8')), app._get_config())
            app.primary_button.invoke()
            self.assertEqual(app.current_step, 1)
            app.back_button.invoke()
            self.assertIs(app.rule_inspector, inspector)

    def test_output_default_and_selected_path_are_not_based_on_source_folder(self):
        with patch.object(App, "_load_settings"), patch.object(App, "_save_settings"):
            app = App(self.root); app._set_config(self.config())
            self.assertEqual(Path(app.output.get()), default_output_directory())
            chosen = self.base/"my-output"
            with patch("label_porter.workflow.filedialog.askdirectory", return_value=str(chosen)):
                app._choose_output()
            with patch("label_porter.workflow.filedialog.askdirectory", return_value=str(self.source)):
                app._choose_source()
            self.assertEqual(app.output.get(), str(chosen))
            app.result_paths = [str(self.rf)]
            app._check_inputs()
            self.assertEqual(Path(app.output.get()), chosen)
            app.output.set("")
            with patch("label_porter.workflow.default_output_directory", return_value=self.base/"program"/"results"):
                app._check_inputs()
            self.assertEqual(Path(app.output.get()), self.base/"program"/"results")
            self.assertFalse((self.base/"program").exists())
            # Rule import must preserve an explicit destination.
            app.output.set(str(chosen))
            rule = self.base/"rules.json"; rule.write_text(json.dumps(self.config()), encoding="utf-8")
            with patch("label_porter.gui.filedialog.askopenfilename", return_value=str(rule)):
                app._load_rule()
            self.assertEqual(app.output.get(), str(chosen))

    def test_frozen_output_default_is_next_to_executable_not_temporary_resources(self):
        with patch("label_porter.workflow.sys.frozen", True, create=True), \
             patch("label_porter.workflow.sys.executable", str(self.base/"program"/"LabelPorter.exe")), \
             patch("label_porter.workflow.sys._MEIPASS", str(self.base/"temporary"), create=True):
            self.assertEqual(default_output_directory(), self.base/"program"/"results")

    def test_default_destination_follows_program_after_restart_but_custom_path_does_not(self):
        with patch.object(App, "_load_settings"):
            app = App(self.root)
        app.settings_path = self.base/"settings.json"
        old_default = self.base/"old-program"/"results"
        new_default = self.base/"new-program"/"results"
        with patch("label_porter.workflow.default_output_directory", return_value=old_default):
            app.default_output_button.invoke()
            app._save_settings()
        self.assertEqual(json.loads(app.settings_path.read_text(encoding="utf-8"))["output_mode"], "default")
        with patch("label_porter.gui.default_output_directory", return_value=new_default):
            app._load_settings()
        self.assertEqual(Path(app.output.get()), new_default)
        self.assertEqual(app.output_mode, "default")
        # An explicitly selected destination stays exact, even if named '포팅결과'.
        chosen = self.base/"custom"/"포팅결과"
        with patch("label_porter.workflow.filedialog.askdirectory", return_value=str(chosen)):
            app._choose_output()
        app._save_settings()
        app._reset_output()
        app._load_settings()
        self.assertEqual(app.output.get(), str(chosen))
        self.assertEqual(app.output_mode, "custom")

    def test_legacy_destination_is_preserved_and_can_be_reset_or_cleared(self):
        with patch.object(App, "_load_settings"):
            app = App(self.root)
        app.settings_path = self.base/"settings.json"
        legacy = str(self.base/"포팅결과")
        app.settings_path.write_text(json.dumps({"output": legacy}), encoding="utf-8")
        app._load_settings()
        self.assertEqual(app.output.get(), legacy)
        app.default_output_button.invoke()
        self.assertEqual(Path(app.output.get()), default_output_directory())
        self.assertEqual(app.output_mode, "default")
        app.output.set(str(self.base/"typed-path"))
        self.assertEqual(app.output_mode, "custom")
        app.output.set("  ")
        app._save_settings()
        app._load_settings()
        self.assertEqual(Path(app.output.get()), default_output_directory())
        self.assertEqual(app.output_mode, "default")


if __name__ == "__main__":
    unittest.main()
