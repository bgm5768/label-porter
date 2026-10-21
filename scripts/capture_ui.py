"""Capture the application's own window with synthetic data, never user settings."""
import ctypes
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import ImageGrab
from label_porter.gui import App
from label_porter import widgets
from label_porter.config import validate_config


def main():
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('outputs/customtkinter-v224/screens')
    target.mkdir(parents=True, exist_ok=True)
    scale = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    widgets.ctk.set_widget_scaling(scale)
    widgets.ctk.set_window_scaling(scale)
    root = widgets.create_root()
    root.tk.call("tk", "scaling", 4 / 3 * scale)
    errors = []
    root.report_callback_exception = lambda *args: errors.append(str(args))
    with patch.object(App, '_load_settings'), patch.object(App, '_save_settings'):
        app = App(root)
        root.geometry('1320x900+20+20')
        def settle():
            end = time.monotonic() + .4
            while time.monotonic() < end:
                root.update(); time.sleep(.01)
        def shot(name, window=root):
            settle()
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            ImageGrab.grab(window=hwnd).save(target / (name + '.png'))
        shot('01-rules-empty')
        rule = validate_config({'schema_version': 1, 'name': '고객 응답 · 보완 라벨 반영',
            'source': {'key': '/id'}, 'result': {'key': '/id'},
            'rules': [{'kind': 'field', 'from': '/answer', 'to': '/answer'}]})
        app._set_config(rule); shot('02-rules-loaded')
        app._advance_inputs(); shot('03-inputs')
        source, result = target/'source.json', target/'result.json'
        source.write_text(json.dumps([{'id':f'QA-{n:03}', 'answer':'보완 전 응답'} for n in range(100)], ensure_ascii=False), encoding='utf-8')
        result.write_text(json.dumps([{'id':f'QA-{n:03}', 'answer':'보완된 응답'} for n in range(100)], ensure_ascii=False), encoding='utf-8')
        app.source_files = [str(source.resolve())]; app.result_paths = [str(result.resolve())]
        app._render_results(); app._wizard()
        deadline = time.monotonic()+15
        while app.running and time.monotonic()<deadline: settle()
        shot('04-connections')
        if app.mapping:
            app.mapping.pages.select(1); shot('05-preview-list')
        app._show_tools(3); shot('06-architecture')
        app.architecture_box.scale.set('115%'); app.architecture_box._scale()
        shot('06b-architecture-115')
        root.geometry('1040x680+20+20'); shot('07-help-small')
        app._return_from_help(); shot('08-preview-small')
        app._finish_mapping(); shot('09-results-small')
        root.geometry('1320x900+20+20')
        app._show_builder(); shot('10-builder')
        original_folder = target/'builder-originals'; original_folder.mkdir(exist_ok=True)
        (original_folder/'source.json').write_bytes(source.read_bytes())
        app.builder.source.set(str(original_folder.resolve())); app.builder.next()
        deadline = time.monotonic()+15
        while app.builder.busy and time.monotonic()<deadline: settle()
        shot('11-builder-fields')
        app.builder.next(); shot('12-builder-preview')
        from label_porter.json_viewer import JsonViewer
        viewer = JsonViewer(root, initial=str(source.resolve()))
        deadline = time.monotonic()+5
        while not viewer.values and time.monotonic()<deadline: settle()
        shot('13-json-viewer', viewer.window)
        viewer.window.destroy()
        app._close()
    (target/'errors.json').write_text(json.dumps(errors,ensure_ascii=False),encoding='utf-8')
    if errors: raise RuntimeError(errors)


if __name__ == '__main__': main()
