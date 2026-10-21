import gc
import re
import time
import tkinter as tk
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

from label_porter.markdown_viewer import MarkdownViewer, render_document
from label_porter.mermaid_assets import ASSET_PREFIX, diagram_id, manifest, read_diagram
from scripts.build_mermaid import check_assets, diagrams

ROOT = Path(__file__).resolve().parents[1]


class MermaidTests(unittest.TestCase):
    def test_closing_reader_restores_live_readers_shared_wheel_binding(self):
        root = tk.Tk(); root.withdraw(); self.addCleanup(root.destroy)
        first = MarkdownViewer(root, '# First')
        original = root.bind_class('Html', '<Button-4>')
        second = MarkdownViewer(root, '# Second')
        second.destroy(); root.update()
        self.assertEqual(root.bind_class('Html', '<Button-4>'), original)
        first.destroy(); root.update()
        first_ref, second_ref = weakref.ref(first), weakref.ref(second)
        del first, second
        gc.collect()
        self.assertIsNone(first_ref()); self.assertIsNone(second_ref())

    def test_current_documents_have_source_matched_assets_and_top_diagram(self):
        self.assertEqual(check_assets(), 3)
        source = (ROOT/'아키텍처_및_규칙엔진.md').read_text(encoding='utf-8')
        rendered, _ = render_document(source)
        self.assertLess(rendered.index('<img'), rendered.index('기준:'))
        self.assertEqual(rendered.count('<img'), 3)
        self.assertNotIn('language-mermaid', rendered)
        self.assertIn('라벨 포터 전체 아키텍처', rendered)
        self.assertEqual(diagram_id('  flowchart LR\r\n A --> B\r\n'), diagram_id('flowchart LR\n A --> B\n'))

    def test_unknown_mermaid_is_inert_source_and_other_images_are_blocked(self):
        with patch('label_porter.markdown_viewer.diagram_info', return_value=('unknown', None)):
            rendered, _ = render_document('```mermaid\nflowchart LR\n A["<script>bad</script>"]\n```\n![img](file:///secret.png)')
        self.assertIn('아직 준비되지 않은', rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('<img', rendered)

    def test_request_callback_reads_only_bundled_diagrams(self):
        identity = next(iter(manifest()))
        url = ASSET_PREFIX + identity + '.png'
        final, raw, mime, code = read_diagram(url)
        self.assertEqual((final, mime, code), (url, 'image/png', 200))
        self.assertTrue(raw.startswith(b'\x89PNG'))
        for bad in ('https://example.com/image.png', 'file:///secret', ASSET_PREFIX+'../manifest.json',
                    ASSET_PREFIX+'0'*64+'.png', url+'?x=1'):
            with self.assertRaises(ValueError): read_diagram(bad)
        with self.assertRaises(ValueError): read_diagram(url, method='POST')

    def test_real_tk_loads_images_zoom_search_and_disposes(self):
        gc.collect()
        root = tk.Tk(); root.geometry('1200x850'); root.withdraw()
        self.addCleanup(gc.collect); self.addCleanup(root.destroy)
        loaded = []
        source = (ROOT/'아키텍처_및_규칙엔진.md').read_text(encoding='utf-8')
        with patch.object(MarkdownViewer, '_request_resource', side_effect=lambda *args: (loaded.append(args[0]), read_diagram(*args))[1]):
            viewer = MarkdownViewer(root, source)
            root.update()
            self.assertEqual(loaded, [])
            self.assertEqual(viewer.browser._html.images, {})
            root.attributes('-alpha', 0)
            root.deiconify(); root.update()
        self.assertEqual(len(loaded), 3)
        self.assertTrue(viewer.browser.cget('images_enabled'))
        self.assertFalse(viewer.browser.cget('javascript_enabled'))
        identity = next(iter(diagrams()))
        node = viewer.browser.document.getElementById('mermaid-'+identity)
        self.assertIs(node.widget, viewer.diagram_widgets[identity])
        fitted_width = node.getBoundingClientRect().width
        self.assertGreater(node.getBoundingClientRect().height, 50)
        self.assertLessEqual(node.getBoundingClientRect().width, viewer.browser.winfo_width())
        self.assertEqual(len(viewer.diagram_widgets), 3)
        self.assertEqual(viewer.browser._html.images, {})
        for label in viewer.diagram_widgets.values():
            self.assertEqual(label._diagram_image.width(), label._diagram_width)
        viewer.diagram_scale.set('150%'); viewer._scale_diagrams(); root.update()
        self.assertIn('max-width:none', node.getAttribute('style'))
        self.assertGreater(node.getBoundingClientRect().width, fitted_width)
        viewer.diagram_scale.set('화면에 맞춤'); viewer._scale_diagrams(); root.update()
        self.assertIn('max-width:100%', node.getAttribute('style'))
        image_count = len(root.tk.call('image', 'names'))
        viewer.scale.set('115%'); viewer._scale(); root.update()
        for width in (900, 1100, 800, 1200):
            root.geometry(f'{width}x850'); root.update()
            viewer._scale_diagrams(); root.update()
            label = viewer.diagram_widgets[identity]
            # HTML must display the already-filtered bitmap at its exact size.
            self.assertEqual(round(node.getBoundingClientRect().width), label._diagram_image.width())
            self.assertLessEqual(label._diagram_image.width(), viewer.browser.winfo_width())
            self.assertLessEqual(len(root.tk.call('image', 'names')), image_count)
        viewer.query.set('규칙'); viewer.find()
        self.assertGreater(viewer.match_count, 0)
        viewer.destroy(); root.update()
        self.assertEqual(viewer.diagrams, {})
        self.assertEqual(viewer.diagram_widgets, {})
        self.assertIsNone(viewer.diagram_job)
        self.assertEqual(viewer.source, '')
        reference = weakref.ref(viewer)
        del viewer, node, label
        gc.collect()
        self.assertIsNone(reference())
