import json
import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from label_porter.job_history import JobHistory, export_job
from label_porter.engine import run

CONFIG = {'schema_version':1,'name':'보관함 규칙','source':{'key':'/id'},'result':{'key':'/id'},'rules':[{'kind':'field','from':'/value','to':'/value'}]}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.store=JobHistory(self.base/'library')
        self.source=self.base/'source';self.source.mkdir()
        (self.source/'one.json').write_text('{"id":"a","value":1}',encoding='utf-8')
        self.result=self.base/'corrected.json';self.result.write_text('{"id":"a","value":2}',encoding='utf-8')

    def result_run(self):return run(self.source,[self.result],self.base/'results',CONFIG,preview=True)

    def test_registration_discovery_and_draft_never_rewrite_result(self):
        result=self.result_run()
        before={p:(p.stat().st_mtime_ns,p.read_bytes()) for p in result.directory.rglob('*') if p.is_file()}
        key=self.store.register_run(result.directory,result.summary)
        self.store.discover([self.base/'results',self.base/'results'])
        self.assertEqual(self.store.list(reports_only=True)[1],1)
        settings=self.store.get(key)['settings']
        self.assertEqual(settings['results'],[str(self.result)])
        self.assertEqual(settings['config'],CONFIG)
        self.store.draft('draft-a',settings);first=self.store.get('draft-a')
        self.store.draft('draft-a',settings);self.assertEqual(first,self.store.get('draft-a'))
        self.assertEqual(before,{p:(p.stat().st_mtime_ns,p.read_bytes()) for p in before})
        self.assertEqual(JobHistory(self.base/'library').get(key)['settings'],settings)

    def test_search_pagination_and_rename(self):
        for i in range(57):self.store.draft(f'd{i}',{'config':{'name':f'작업 {i}'},'source':'원본'})
        rows,total,page=self.store.list(page=2);self.assertEqual((len(rows),total,page),(7,57,2))
        self.assertEqual(self.store.list('작업 56')[1],1)
        self.assertEqual(self.store.list('%')[1],0)
        self.store.rename('d1','내 작업')
        self.store.draft('d1',{'config':{'name':'다른 규칙'}})
        self.assertEqual(self.store.get('d1')['title'],'내 작업')
        self.assertEqual(self.store.list(reports_only=True)[1],0)

    def test_explicit_export_and_cancel_preserve_destination(self):
        result=self.result_run();key=self.store.register_run(result.directory,result.summary);job=self.store.get(key)
        target=self.base/'outside.xlsx';export_job(job,target,excel=True)
        self.assertEqual(target.read_bytes(),(result.directory/'reports/report.xlsx').read_bytes())
        archive=self.base/'all.zip';export_job(job,archive)
        with zipfile.ZipFile(archive) as z:self.assertEqual(z.read('rule.json'),(result.directory/'rule.json').read_bytes())
        target.write_bytes(b'previous')
        def cancel():raise InterruptedError('cancel')
        with self.assertRaises(InterruptedError):export_job(job,target,excel=True,checkpoint=cancel)
        self.assertEqual(target.read_bytes(),b'previous')
        with self.assertRaises(ValueError):export_job(job,result.directory/'nested.zip')
        missing={**job,'directory':str(self.base/'missing')}
        with self.assertRaises(FileNotFoundError):export_job(missing,self.base/'empty.zip')

    def test_threaded_registration(self):
        errors=[]
        def save(n):
            try:self.store.draft(str(n),{'config':{'name':str(n)}})
            except Exception as e:errors.append(e)
        workers=[threading.Thread(target=save,args=(n,)) for n in range(12)]
        for t in workers:t.start()
        for t in workers:t.join()
        self.assertEqual(errors,[]);self.assertEqual(self.store.list()[1],12)


class HistoryUiTests(HistoryTests):
    def setUp(self):
        super().setUp()
        self.env=patch.dict(os.environ,{'LOCALAPPDATA':str(self.base/'local'),'APPDATA':str(self.base/'roaming')})
        self.env.start();self.addCleanup(self.env.stop)
        try:self.root=tk.Tk();self.root.withdraw()
        except tk.TclError:self.skipTest('Tk unavailable')
        self.addCleanup(self.root.destroy)

    def until(self,condition):
        deadline=time.monotonic()+20
        while not condition() and time.monotonic()<deadline:self.root.update();time.sleep(.01)
        self.assertTrue(condition());self.root.update_idletasks()

    def test_resume_autosave_list_merge_and_return(self):
        from label_porter.gui import App
        with patch.object(App,'_load_settings'):app=App(self.root)
        app._set_config(CONFIG);app.source.set(str(self.source));app.result_paths=[str(self.result)];app._render_results()
        self.until(lambda:app.history.get(app.draft_id) is not None)
        old_draft=app.draft_id;old_settings=app.history.get(old_draft)['settings']
        result=self.result_run();key=app.history.register_run(result.directory,result.summary,old_settings)
        app.history_scanned=True
        app._show_history();self.assertEqual(app.page_title.get(),'작업 보관함')
        app.history_panel.list.table.selection_set(key)
        app.history_panel._load()
        self.assertEqual(app.current_step,3);self.assertEqual(app.result_paths,[str(self.result)])
        self.assertTrue(app.restored_result);self.assertEqual(app.primary_button['text'],'입력 다시 확인 →')
        self.assertNotEqual(app.draft_id,old_draft);self.assertIsNone(app.preview_fingerprint)
        self.assertEqual(app.history.get(old_draft)['settings'],old_settings)
        app._show_report_merge();merger=app.report_merger
        merger.history_list.table.selection_set(key);merger._add_history()
        self.assertEqual(merger.paths,[str(result.directory/'reports/summary.json')])
        with patch('label_porter.report_merge_ui.filedialog.asksaveasfilename',side_effect=AssertionError('No file dialog for managed save')):
            merger._save();self.until(lambda:not merger.busy)
        self.assertTrue(merger.saved.is_file(),merger.message.get())
        merged=app.history.list(kind='merge')[0];self.assertEqual(len(merged),1)
        app._return_from_help();self.assertEqual(app.route,'workflow')

    def test_gui_validations_keep_each_previous_result(self):
        from label_porter.gui import App
        with patch.object(App,'_load_settings'):app=App(self.root)
        app._set_config(CONFIG);app.source.set(str(self.source));app.result_paths=[str(self.result)];app._render_results()
        app._run_validation(True);self.until(lambda:not app.running)
        first=app.last_run;data=(first/'reports/summary.json').read_bytes()
        app._run_validation(True);self.until(lambda:not app.running)
        self.assertNotEqual(first,app.last_run);self.assertEqual((first/'reports/summary.json').read_bytes(),data)
        self.assertEqual(app.history.list(kind='preview')[1],2)
        app._run_validation(False);self.until(lambda:not app.running)
        self.assertEqual(app.history.list(kind='run')[1],1)
        self.assertEqual(json.loads((app.last_run/'data/one.json').read_text(encoding='utf-8'))['value'],2)
        self.assertEqual(json.loads((self.source/'one.json').read_text())['value'],1)
