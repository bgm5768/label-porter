import gc
import json
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from label_porter.formats import read_document
from label_porter.input_check import InputRequest, inspect_inputs
from label_porter.json_display import json_prefix
from label_porter.pair_preview import load_preview, search_catalog


class PerformanceSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base/'source'; self.source.mkdir()
        self.sf = self.source/'one.json'; self.rf = self.base/'result.json'
        self.old = {'id': '가A', 'label': 'Straße 원본', 'n': 123456789012345678901234567890}
        self.new = {'id': '가A', 'label': '수정 Straße'}
        self.sf.write_text(json.dumps(self.old, ensure_ascii=False), encoding='utf-8')
        self.rf.write_text(json.dumps([self.new, {'id': 'other', 'label': '홀로'}], ensure_ascii=False), encoding='utf-8')
        self.config = {'schema_version': 1, 'name': 'test', 'source': {'key': '/id'}, 'result': {'key': '/id'},
                       'rules': [{'kind': 'field', 'from': '/label', 'to': '/label'}]}
        self.request = InputRequest(self.config, str(self.source), (str(self.rf),), '', str(self.base/'out'), True)

    def test_prefix_matches_standard_dump_and_stops_traversal(self):
        values = [self.old, [], {}, ['한글\n\t"\\', True, False, None, -0.0, 1e100], {'': {'a': list(range(30))}},
                  '\\'*100000, {'very long key'*10000: 'x'}]
        for value in values:
            for indent in (None, 2):
                expected = json.dumps(value, ensure_ascii=False, indent=indent)
                for limit in (0, 1, 20, 200, len(expected)):
                    text, truncated = json_prefix(value, limit, indent=indent)
                    self.assertEqual(text, expected[:limit])
                    self.assertEqual(truncated, len(expected) > limit)
        class GuardedList(list):
            def __iter__(self):
                for i, item in enumerate(super().__iter__()):
                    if i > 100: raise AssertionError('Traversed hidden items')
                    yield item
        self.assertTrue(json_prefix(GuardedList(range(100000)), 100)[1])

    def test_lazy_full_text_search_matches_previous_semantics(self):
        data = load_preview(self.config, self.source, [self.rf])
        search_catalog(data, '가a', 'KEY')
        search_catalog(data, 'one', '파일명 / 위치')
        self.assertIsNone(data.search_index)
        old_fields = []
        for entry in data.catalog:
            fields = list(entry['search'])
            if entry['source_index'] is not None:
                fields[2] = json.dumps(data.originals[entry['source_index']][0], ensure_ascii=False, separators=(',', ':')).casefold()
            if entry['incoming']:
                fields[3] = json.dumps(entry['incoming'][1], ensure_ascii=False, separators=(',', ':')).casefold()
            old_fields.append(fields)
        for scope, field in [('전체', None), ('KEY', 0), ('파일명 / 위치', 1), ('원본 JSON', 2), ('보완 JSON', 3)]:
            for query in ('', 'STRASSE', '가a 수정', '원본 수정', '123456789012345678901234567890', '홀로', '없음', '"label":'):
                for state in ('전체 상태', '연결됨', '보완만 있음'):
                    expected = [i for i, fields in enumerate(old_fields) if (state == '전체 상태' or data.catalog[i]['state'] == state)
                                and all(any(term in text for text in (fields if field is None else [fields[field]])) for term in query.casefold().split())]
                    self.assertEqual(search_catalog(data, query, scope, state), expected)
        self.assertIsNotNone(data.search_index)
        handle = data.search_index.file
        del data; gc.collect()
        self.assertTrue(handle.closed)

    def test_cancelled_search_index_does_not_publish_partial_index(self):
        data = load_preview(self.config, self.source, [self.rf])
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            if calls == 3: raise InterruptedError('stop')
        with self.assertRaises(InterruptedError): search_catalog(data, '수정', checkpoint=cancel)
        self.assertIsNone(data.search_index)
        self.assertEqual(search_catalog(data, '수정'), [0])

    def test_stamps_detect_labels_results_and_copied_auxiliary_files(self):
        _, initial = inspect_inputs(self.request, stamp=True)
        for path in (self.sf, self.rf, self.source/'audio.wav'):
            before = path.read_bytes() if path.exists() else None
            path.write_bytes(b'changed external data')
            self.assertNotEqual(inspect_inputs(self.request, stamp=True)[1], initial)
            if before is None: path.unlink()
            else: path.write_bytes(before)
            initial = inspect_inputs(self.request, stamp=True)[1]
        self.assertEqual(inspect_inputs(self.request, stamp=True)[1], initial)
        for output in (self.source, self.source/'inside', self.sf):
            with self.assertRaises(ValueError): inspect_inputs(replace(self.request, output=str(output)))
        self.assertFalse((self.base/'out').exists())

    def test_nested_large_json_streaming_and_strict_keys(self):
        path = self.base/'wrapped.json'
        value = {'data': {'items': [self.old]*200}, 'other': [1, True, None]}
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        with patch('label_porter.stream_json.CHUNK', 31):
            parsed = read_document(path, progress=lambda *args: None)
        self.assertEqual(parsed.docs, [value])
        for invalid in ('{"data":{"rows":[],"rows":[]}}', '{"a":1,}', '{"a" 2}', '{"a": [NaN]}'):
            path.write_text(invalid)
            with patch('label_porter.stream_json.CHUNK', 3), self.assertRaises(ValueError):
                read_document(path, progress=lambda *args: None)

    def test_large_reader_shares_only_keys_without_changing_values(self):
        path = self.base/'pooled.jsonl'
        path.write_text('[{"shared":9007199254740993},{"shared":-0.0}]\n{"shared":1.2000000000000002}')
        expected = read_document(path)
        with patch('label_porter.stream_json.KEY_POOL_THRESHOLD', 0), patch('label_porter.stream_json.CHUNK', 7):
            actual = read_document(path, progress=lambda *args: None)
            self.assertEqual(json.dumps(actual.docs), json.dumps(expected.docs))
            self.assertIs(next(iter(actual.docs[0][0])), next(iter(actual.docs[1])))
            path.write_text('[{"shared":1,"shared":2}]')
            with self.assertRaises(ValueError): read_document(path, progress=lambda *args: None)

    def test_report_renderer_preserves_numbers_and_escapes_html(self):
        from label_porter.detail_renderer import RENDERER
        detail = {'status': 'updated', 'key': 123456789012345678901234567890,
                  'source_record': {'__proto__': '</script><img src=x onerror=alert(1)>', 'x': 1.0},
                  'final_record': {'x': 1.2000000000000002}, 'changes': [{'path': '/x', 'before': 1.0, 'after': 1.2000000000000002}]}
        code = 'const detailHeader="<title></title>", detailStatuses={};\n' + RENDERER
        code += '\nconst data=parseDetail('+json.dumps(json.dumps(detail))+'); console.log(renderDetail("00000001", data));'
        result = subprocess.run(['node', '-'], input=code, text=True, encoding='utf-8', capture_output=True, check=True)
        self.assertIn('123456789012345678901234567890', result.stdout)
        self.assertIn('1.2000000000000002', result.stdout)
        self.assertIn('1.0', result.stdout)
        self.assertIn('&lt;/script&gt;&lt;img', result.stdout)
        self.assertNotIn('<img', result.stdout)
        self.assertIn('변경 필드 1개', result.stdout)


class AsyncInputTests(unittest.TestCase):
    # Explicitly reuse the fixture only, not the non-UI tests above.
    def setUp(self):
        PerformanceSafetyTests.setUp(self)
        from label_porter.gui import App
        gc.collect()
        self.addCleanup(gc.collect)
        self.root = tk.Tk(); self.root.withdraw()
        self.addCleanup(self.root.destroy)
        with patch.object(App, '_load_settings'), patch.object(App, '_save_settings'):
            self.app = App(self.root)
        self.app._save_settings = lambda: None
        self.app._set_config(self.config)
        self.app.source.set(str(self.source)); self.app.result_paths = [str(self.rf)]
        self.app.output.set(str(self.base/'out'))

    def until(self, predicate):
        deadline = time.monotonic()+5
        while not predicate() and time.monotonic() < deadline:
            self.root.update(); time.sleep(.01)
        self.assertTrue(predicate())

    def test_slow_check_leaves_ui_responsive_and_rejects_changed_selection(self):
        entered, release = threading.Event(), threading.Event()
        main = threading.get_ident()
        def slow(request, checkpoint, **kwargs):
            self.assertNotEqual(threading.get_ident(), main)
            entered.set(); release.wait(5)
            return inspect_inputs(request, checkpoint, **kwargs)
        with patch('label_porter.workflow.inspect_inputs', side_effect=slow):
            self.app._wizard()
            try:
                self.assertTrue(entered.wait(2))
                beat = []
                self.root.after(0, lambda: beat.append(True)); self.root.update()
                self.assertEqual(beat, [True])
                self.app.output.set(str(self.base/'changed'))
            finally: release.set()
            self.until(lambda: not self.app.running)
        self.assertIsNone(self.app.mapping)
        self.assertIn('바뀌었습니다', self.app.status.get())

    def test_cancel_before_run_creates_no_output(self):
        entered, release = threading.Event(), threading.Event()
        def slow(request, checkpoint, **kwargs):
            entered.set(); release.wait(5)
            return inspect_inputs(request, checkpoint, **kwargs)
        self.app.connection_confirmed = True
        with patch('label_porter.workflow.inspect_inputs', side_effect=slow), patch('label_porter.engine.run') as run:
            self.app._start(True)
            try:
                self.assertTrue(entered.wait(2)); self.app._cancel()
            finally: release.set()
            self.until(lambda: not self.app.running)
            run.assert_not_called()
        self.assertFalse((self.base/'out').exists())

    def test_bulk_file_selection_does_not_resolve_files_on_ui_thread(self):
        paths = [str(self.sf)] * 20000
        with patch('label_porter.workflow.filedialog.askopenfilenames', return_value=paths), \
             patch.object(Path, 'resolve', side_effect=AssertionError('UI filesystem check')):
            self.app._choose_source_files()
            self.app._choose_results()
        self.assertEqual(self.app.source_files, [str(self.sf)])
        self.assertEqual(self.app.result_paths, [str(self.rf), str(self.sf)])
