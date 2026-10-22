import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from label_porter.formats import Document
from label_porter.json_search import search_json
from label_porter.json_viewer import JsonViewer


class SearchTests(unittest.TestCase):
    def test_nested_keys_values_types_and_escaped_tokens(self):
        data = {'items': [{'a/b~': '한글 NEEDLE', 'n': 123, 'flag': True, 'nil': None}]}
        self.assertEqual(list(search_json(data, '한글 needle')), [('items', 0, 'a/b~')])
        self.assertEqual(list(search_json(data, 'a/b~')), [('items', 0, 'a/b~')])
        self.assertEqual(list(search_json(data, '123')), [('items', 0, 'n')])
        self.assertEqual(list(search_json(data, 'true')), [('items', 0, 'flag')])
        self.assertEqual(list(search_json(data, 'null')), [('items', 0, 'nil')])
        self.assertEqual(list(search_json(data, '')), [])

    def test_cancellation_and_deep_arrays_without_recursive_stack(self):
        data = 'needle'
        for _ in range(1500): data = [data]
        self.assertEqual(len(next(search_json(data, 'needle'))), 1500)
        def cancel(): raise InterruptedError()
        with self.assertRaises(InterruptedError): list(search_json(data, 'needle', cancel))


class ViewerSearchTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk(); self.root.withdraw()
        self.viewer = JsonViewer(self.root); self.viewer.window.withdraw()
        self.temp = tempfile.TemporaryDirectory(); self.base = Path(self.temp.name)

    def tearDown(self):
        self.root.destroy(); self.temp.cleanup()

    def until(self, predicate):
        end = time.monotonic() + 8
        while not predicate() and time.monotonic() < end:
            self.root.update(); time.sleep(.01)
        self.assertTrue(predicate()); self.root.update()

    def file(self, name, value):
        path = self.base / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8'); return path

    def test_array_search_paging_jump_and_selected_scope(self):
        viewer = self.viewer
        data = {'items': [{'a/b~': 'needle ' + str(i)} for i in range(250)], 'other': ['needle']}
        viewer.display(Document([data], 'test'))
        viewer.content_query.set('NEEDLE'); viewer.start_search()
        self.until(lambda: len(viewer.search_hits) == 251)
        self.assertEqual(len(viewer.results.get_children()), 100)
        viewer.move_search(2)
        self.assertEqual(len(viewer.results.get_children()), 51)
        viewer.results.selection_set('249'); viewer.select_search_result()
        self.assertIn('/items/249/a~1b~0', viewer.selection_info.get())
        self.assertIn('needle 249', viewer.preview.get('1.0', 'end'))
        # Jumping to index 249 materializes only one extra array item, not 250.
        array = next(i for i, (_, path) in viewer.values.items() if path == '/items')
        self.assertLess(len(viewer.tree.get_children(array)), 110)
        viewer.tree.selection_set(array)
        viewer.content_scope.set('선택 항목 내부'); viewer.start_search()
        self.until(lambda: len(viewer.search_hits) == 250)
        self.assertTrue(all(path[0] == 'items' for path in viewer.search_hits))
        viewer.content_query.set('missing'); viewer.start_search()
        self.until(lambda: '0개 일치' in viewer.search_info.get())
        self.assertEqual(viewer.search_page_info.get().split('쪽')[0], '0 / 0')

    def test_folder_and_multi_file_replace_same_tree_and_json(self):
        a = self.file('old/nested/same.json', {'old': 1})
        b = self.file('new/left/same.json', {'new': 2})
        c = self.file('new/right/same.json', {'new': 3})
        viewer = self.viewer
        with patch('label_porter.json_viewer.filedialog.askdirectory', return_value=str(self.base/'old')):
            viewer.choose_folder()
        self.until(lambda: viewer.document_value == {'old': 1})
        with patch('label_porter.json_viewer.filedialog.askopenfilenames', return_value=(str(c), str(b))):
            viewer.choose()
        self.assertIsNone(viewer.document_value)
        self.until(lambda: viewer.document_value == {'new': 3})
        self.assertEqual(set(viewer.browser.paths), {b, c})
        self.assertNotIn(a, viewer.browser.paths)
        tree = viewer.browser.table
        self.assertNotEqual(tree.parent('0'), tree.parent('1'))
        self.assertEqual(tree.item('0', 'text'), tree.item('1', 'text'))
        tree.selection_set('0')
        self.until(lambda: viewer.document_value == {'new': 2})
        self.assertEqual(viewer.path.get(), str(b))
        viewer.browser.query.set('nothing'); viewer.browser.filter()
        self.assertIsNone(viewer.document_value)
        self.assertEqual(viewer.values, {})
        # Canceling either dialog leaves the current inputs unchanged.
        with patch('label_porter.json_viewer.filedialog.askopenfilenames', return_value=()): viewer.choose()
        self.assertEqual(set(viewer.browser.paths), {b, c})

    def test_late_search_cannot_replace_new_file_and_close_discards_results(self):
        viewer = self.viewer
        viewer.display(Document([[{'old': 'needle'}]], 'old'))
        entered, release = threading.Event(), threading.Event()
        def delayed(*args):
            entered.set(); release.wait(5); return iter(((0, 'old'),))
        with patch('label_porter.json_viewer.search_json', side_effect=delayed):
            viewer.content_query.set('needle'); viewer.start_search()
            self.assertTrue(entered.wait(3))
            path = self.file('new.json', {'fresh': 1})
            viewer.browser.set_inputs([path])
            release.set()
            self.until(lambda: viewer.document_value == {'fresh': 1})
        self.assertEqual(viewer.search_hits, [])
        self.assertNotIn('old', viewer.preview.get('1.0', 'end'))
        viewer.content_query.set('fresh'); viewer.start_search()
        viewer.window.destroy()
        self.assertTrue(viewer.search_cancel.is_set())
        self.assertEqual(viewer.search_hits, [])
        self.assertIsNone(viewer.document_value)

    def test_scan_generation_discards_previous_folder(self):
        old = self.file('old.json', {'old': True})
        new = self.file('new.json', {'new': True})
        entered, release = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            entered.set(); release.wait(5); return [old]
        with patch('label_porter.file_browser.folder_files', side_effect=slow):
            self.viewer.browser.set_inputs([self.base])
            self.assertTrue(entered.wait(3))
            self.viewer.browser.set_inputs([new]); release.set()
            self.until(lambda: self.viewer.document_value == {'new': True})
        self.assertEqual(self.viewer.browser.paths, [new])


if __name__ == '__main__': unittest.main()
