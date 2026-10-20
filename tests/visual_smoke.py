"""Run the real Tk workflow on synthetic data and capture its own window only.

Optional developer check: python tests/visual_smoke.py
Pillow is needed for captures only; the application has no Pillow dependency.
"""
import json
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from label_porter.gui import App
from PIL import ImageGrab


def main():
    root = tk.Tk()
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    output = Path('outputs/ui-v211')
    output.mkdir(parents=True, exist_ok=True)
    def wait(predicate):
        end = time.monotonic()+20
        while not predicate() and time.monotonic()<end:
            root.update()
            time.sleep(.02)
        assert predicate(), 'UI action did not finish'
        root.update()
    def capture(name):
        root.update()
        time.sleep(.2)  # Allow the desktop window animation to settle before capture.
        root.update()
        ImageGrab.grab(window=int(root.frame(), 16)).save(output/(name+'.png'))
    with tempfile.TemporaryDirectory(prefix='porter_ui_') as temp:
        folder = Path(temp)
        originals = folder/'원본라벨'; originals.mkdir()
        for key in ('A001', 'A002', 'A003', 'A004'):
            (originals/(key+'.json')).write_text(json.dumps({'id': key, 'label': '기존 라벨', 'metadata': {'keep': True}}), encoding='utf-8')
        results = folder/'검수결과.json'
        results.write_text(json.dumps([{'ref': 'A001', 'new': '보완한 라벨'}, {'ref': 'A002', 'new': '기존 라벨'},
                                       {'ref': 'A004', 'new': ''}, {'ref': 'UNKNOWN', 'new': '연결할 원본 없음'}]), encoding='utf-8')
        before = {p.name:p.read_bytes() for p in originals.iterdir()}
        with patch.object(App, '_load_settings'), patch.object(App, '_save_settings'):
            app = App(root)
            root.geometry('1280x880+20+20'); root.update()
            capture('01-start')
            app._show_tools(0); root.update()
            capture('00-guide')
            app._show_tools(1); root.update()
            capture('00-help')
            app.back_button.invoke()
            assert app.current_step == 0
            profile = {'schema_version':1,'name':'문장 보완 프로젝트 · AI 규칙',
                       'source':{'key':'/id'},'result':{'key':'/ref'},
                       'rules':[{'kind':'field','from':'/new','to':'/label','type':'string','nonempty':True}]}
            rule_path = folder/'project_profile.json'
            rule_path.write_text(json.dumps(profile), encoding='utf-8')
            with patch('label_porter.gui.filedialog.askopenfilename', return_value=str(rule_path)):
                app._load_profile()
            assert app.current_step == 1
            app.source.set(str(originals)); app.result_paths = [str(results)]
            app.output.set(str(folder/'포팅결과')); app._render_results()
            capture('02-inputs')
            app.primary_button.invoke(); root.update(); assert app.current_step == 2
            board = app.mapping
            wait(lambda: board.preview.pair is not None)
            capture('03-connections')
            board.pages.select(1); root.update()
            assert board.preview.pair['key'] == 'A001', board.preview.status.get()
            capture('03-paired-values')
            board.preview.views.select(1); root.update()
            assert json.loads(board.preview.output_box.get('1.0', 'end-1c'))['label'] == '보완한 라벨'
            capture('03-json-pair')
            root.geometry('1040x680+20+20'); root.update()
            capture('03-small-preview')
            root.geometry('1280x880+20+20'); root.update()
            app.primary_button.invoke(); root.update()
            assert app.current_step == 3, (app.status.get(), errors)
            app.primary_button.invoke(); wait(lambda:not app.running)
            assert app.preview_fingerprint
            assert [v.get() for v in app.metric_values.values()]==['4','4','2','1','1','1','2','2']
            capture('05-validation')
            app.primary_button.invoke(); wait(lambda:not app.running)
            assert app._saved()
            assert before=={p.name:p.read_bytes() for p in originals.iterdir()}
            assert app.last_summary['partition_records']=={'changed':1,'unchanged':1,'examine':2}
            capture('06-saved')
            root.geometry('1040x680+20+20'); root.update()
            capture('07-small-window')
            app._close()
    assert not errors, errors
    print('Visual AI workflow passed: JSON import, read-only connections, next step, validation, copy, original preserved.')
    print('Captured:', output.resolve())


if __name__=='__main__': main()
