import contextlib
import io
import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from label_porter.config import load_config
from label_porter.engine import run
from label_porter.gui import App
from label_porter.result_dashboard import write_dashboard
from scripts.verify_run import verify

ROOT = Path(__file__).resolve().parents[1]


class RuleNamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        (self.source / 'a.json').write_text('{"id":"a","label":"old"}', encoding='utf-8')
        self.result = self.base / 'result.json'
        self.result.write_text('{"source_id":"a","corrected_label":"new"}', encoding='utf-8')
        self.rule = load_config(ROOT / 'rules/generic.json')

    def test_run_uses_rule_filename_summary_and_report_download(self):
        outcome = run(self.source, [self.result], self.base / 'out', self.rule)
        self.assertEqual(outcome.summary['rule'], self.rule['name'])
        self.assertNotIn('profile', outcome.summary)
        self.assertTrue((outcome.directory / 'rule.json').is_file())
        self.assertFalse((outcome.directory / 'profile.json').exists())
        html = (outcome.directory / 'reports/report.html').read_text(encoding='utf-8')
        self.assertIn('../rule.json', html)
        self.assertNotIn('../profile.json', html)
        with contextlib.redirect_stdout(io.StringIO()):
            verify(outcome.directory)
        # Historical runs remain readable without migrating their stored files.
        (outcome.directory / 'rule.json').rename(outcome.directory / 'profile.json')
        legacy_summary = {**outcome.summary, 'profile': outcome.summary['rule']}
        legacy_summary.pop('rule')
        with contextlib.redirect_stdout(io.StringIO()):
            verify(outcome.directory)
        reports = outcome.directory / 'reports'
        catalog = json.loads((reports / 'catalog.json').read_text(encoding='utf-8'))
        write_dashboard(reports, legacy_summary, catalog, [])
        self.assertIn('../profile.json', (reports / 'report.html').read_text(encoding='utf-8'))
        self.assertFalse((outcome.directory / 'rule.json').exists())

    def test_owned_preview_replaces_obsolete_rule_copy(self):
        folder = run(self.source, [self.result], self.base / 'out', self.rule, preview=True).directory
        (folder / 'rule.json').rename(folder / 'profile.json')
        old_bytes = (folder / 'profile.json').read_bytes()
        again = run(self.source, [self.result], self.base / 'out', self.rule, preview=True)
        self.assertEqual(folder, again.directory)
        self.assertEqual((folder / 'rule.json').read_bytes(), old_bytes)
        self.assertFalse((folder / 'profile.json').exists())

    def test_legacy_setting_and_filename_import_export(self):
        root = tk.Tk(); root.withdraw()
        self.addCleanup(root.destroy)
        with patch.object(App, '_load_settings'), patch.object(App, '_save_settings'):
            app = App(root)
        self.addCleanup(lambda: root.after_cancel(app.poll_job))
        app.settings_path = self.base / 'settings.json'
        app.settings_path.write_text(json.dumps({'profile': self.rule['name']}), encoding='utf-8')
        app._load_settings()
        self.assertEqual(app._get_config(), self.rule)
        legacy = self.base / 'profile.json'
        legacy.write_text(json.dumps(self.rule), encoding='utf-8')
        with patch('label_porter.gui.filedialog.askopenfilename', return_value=str(legacy)):
            app._load_rule()
        self.assertEqual(app._get_config(), self.rule)
        destination = self.base / 'project_rule.json'
        with patch('label_porter.gui.filedialog.asksaveasfilename', return_value=str(destination)) as dialog:
            app._save_rule()
        self.assertEqual(dialog.call_args.kwargs['initialfile'], 'project_rule.json')
        self.assertEqual(load_config(destination), self.rule)
