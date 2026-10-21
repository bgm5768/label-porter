import contextlib
import io
import json
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from label_porter.engine import run
from label_porter.formats import DataError
from label_porter.input_files import folder_files, source_layout
from label_porter.pair_preview import load_preview, catalog_preview
from label_porter.json_viewer import JsonViewer
from scripts.verify_run import verify


CONFIG = {'schema_version': 1, 'name': 'Selected files', 'source': {'key': '/id'},
          'result': {'key': '/id'}, 'rules': [{'kind': 'field', 'from': '/label', 'to': '/label'}]}


class SelectedFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def write(self, relative, value):
        path = self.base / relative; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8'); return path

    def test_only_selected_files_saved_and_duplicate_names_keep_paths(self):
        a = self.write('source/a/item.json', {'id': 'a', 'label': 'old'})
        b = self.write('source/b/item.json', {'id': 'b', 'label': 'old'})
        self.write('source/a/ignored.json', {'id': 'ignored'})
        corrected = self.write('corrected.json', [{'id': 'a', 'label': 'new'}])
        before = {p: p.read_bytes() for p in (a, b)}
        data = load_preview(CONFIG, [a, b], [corrected])
        self.assertEqual(len(data.originals), 2)
        self.assertIn('a/item.json', data.originals[0][2])
        self.assertEqual(catalog_preview(data, 0)['output']['label'], 'new')
        preview = run([a, b], [corrected], self.base/'out', CONFIG, preview=True)
        actual = run([a, b], [corrected], self.base/'out', CONFIG)
        for result in (preview, actual):
            self.assertEqual(result.summary['source_records'], 2)
            self.assertNotEqual(result.summary['status'], 'failed', result.summary)
        self.assertEqual({p.relative_to(actual.directory/'data').as_posix() for p in (actual.directory/'data').rglob('*.json')}, {'a/item.json', 'b/item.json'})
        archived = actual.directory/'examine/unmatched/labels/b/item.json/00000001.json'
        self.assertEqual(archived.read_bytes(), before[b])
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        with contextlib.redirect_stdout(io.StringIO()): verify(actual.directory)

    def test_explicit_jsonl_selection_overrides_folder_glob(self):
        a = self.base/'a.jsonl'; a.write_text('{"id":1,"label":0}\n{"id":2,"label":0}\n', encoding='utf-8')
        corrected = self.write('result.json', [{'id': 1, 'label': 1}, {'id': 2, 'label': 2}])
        result = run([a], [corrected], self.base/'out', CONFIG)
        self.assertEqual(result.summary['source_status_counts'], {'updated': 2})
        self.assertEqual(len(load_preview(CONFIG, [a], [corrected]).originals), 2)
        with contextlib.redirect_stdout(io.StringIO()): verify(result.directory)

    def test_duplicate_overlap_missing_and_preview_inputs_rejected(self):
        a = self.write('a.json', {'id': 'a', 'label': 0})
        with self.assertRaises(DataError): source_layout([a, a])
        with self.assertRaises(DataError): source_layout([self.base/'missing.json'])
        with self.assertRaises(DataError): run([a], [a], self.base/'out', CONFIG)
        unsafe = self.write('out/미리보기/keep.json', {'id': 'a', 'label': 0})
        before = unsafe.read_bytes()
        with self.assertRaises(DataError): run([unsafe], [a], self.base/'out', CONFIG, preview=True)
        self.assertEqual(unsafe.read_bytes(), before)

    def test_folder_listing_ignores_media_and_includes_nested_formats(self):
        expected = [self.write('folder/a.json', []), self.write('folder/sub/b.JSONL', {})]
        (self.base/'folder/media.wav').write_bytes(b'audio')
        self.assertEqual(set(folder_files(self.base/'folder')), set(expected))

    def test_relative_path_keys_use_same_base_in_preview_and_output(self):
        a = self.write('original/left/a.json', {'label': 'old'})
        b = self.write('original/right/b.json', {'label': 'old'})
        result = self.write('result.json', [{'id': 'left/a.json', 'label': 'A'}, {'id': 'right/b.json', 'label': 'B'}])
        config = {**CONFIG, 'source': {'key': {'meta': 'relative_path'}}}
        data = load_preview(config, [a, b], [result])
        self.assertEqual([catalog_preview(data, i)['output']['label'] for i in range(2)], ['A', 'B'])
        output = run([a, b], [result], self.base/'out', config)
        self.assertEqual(output.summary['source_status_counts'], {'updated': 2})

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows drive layout')
    def test_separate_drives_get_distinct_relative_paths(self):
        paths = [Path('C:/labels/a.json'), Path('D:/labels/a.json')]
        with patch('label_porter.input_files.source_paths', return_value=paths):
            layout = source_layout(paths)
        self.assertIsNone(layout.root)
        self.assertEqual(set(layout.files), {Path('drive_C/labels/a.json'), Path('drive_D/labels/a.json')})


class FileBrowserTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk(); self.root.withdraw()
        self.temp = tempfile.TemporaryDirectory(); self.base = Path(self.temp.name)
        self.viewer = JsonViewer(self.root); self.viewer.window.withdraw()

    def tearDown(self):
        self.root.destroy(); self.temp.cleanup()

    def until(self, condition):
        end = time.monotonic()+8
        while not condition() and time.monotonic() < end:
            self.root.update(); time.sleep(.01)
        self.assertTrue(condition()); self.root.update()

    def test_search_paging_and_selected_file_loading(self):
        for i in range(105): (self.base/f'item-{i:03}.json').write_text(json.dumps({'index': i}), encoding='utf-8')
        browser = self.viewer.browser
        browser.set_inputs([self.base])
        self.until(lambda: len(browser.paths) == 105 and not self.viewer.loading and bool(self.viewer.values))
        self.assertEqual(len(browser.table.get_children()), 100)
        browser.move(1); self.assertEqual(len(browser.table.get_children()), 5)
        browser.table.selection_set('104')
        self.until(lambda: self.viewer.path.get().endswith('item-104.json') and not self.viewer.loading)
        root = self.viewer.tree.get_children()[0]
        self.assertEqual(self.viewer.values[root][0], {'index': 104})
        browser.query.set('ITEM-042'); browser.filter()
        self.assertEqual(len(browser.table.get_children()), 1)
        browser.table.selection_set('42')
        self.until(lambda: self.viewer.path.get().endswith('item-042.json') and not self.viewer.loading)
        browser.query.set('not-present'); browser.filter()
        self.assertEqual(len(browser.table.get_children()), 0)
        self.assertEqual(browser.page_info.get(), '0 / 0')

    def test_folder_scan_does_not_parse_every_file_and_csv_preview(self):
        csv = self.base/'data.csv'; csv.write_text('id,label\na,new\n', encoding='utf-8')
        (self.base/'bad.json').write_text('bad json', encoding='utf-8')
        with patch('label_porter.json_viewer.read_document', wraps=__import__('label_porter.formats', fromlist=['read_document']).read_document) as reader:
            self.viewer.browser.set_inputs([self.base], initial=csv)
            self.until(lambda: len(self.viewer.browser.paths) == 2 and '최상위 항목' in self.viewer.status.get())
            self.assertEqual(reader.call_count, 1)
            self.assertIn('new', self.viewer.preview.get('1.0', 'end'))
