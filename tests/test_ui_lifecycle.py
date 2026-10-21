"""Views release ownership on destruction, including worker/close races."""
import gc
import json
import queue
import tempfile
import threading
import time
import tkinter as tk
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

from label_porter.config import validate_config
from label_porter.easy_rules_ui import EasyRules
from label_porter.formats import Document
from label_porter.gui import App
from label_porter.json_viewer import JsonViewer
from label_porter.markdown_viewer import MarkdownViewer
from label_porter.pair_preview import load_preview
from label_porter.pair_preview_ui import PairPreview
from label_porter.rule_review import RuleReview
from label_porter.ui_lifecycle import UiEventQueue
from label_porter.ui import ScrollPage


class Payload(dict):
    """Weak-referenceable label record."""


class MailboxTests(unittest.TestCase):
    def test_close_releases_pending_results_and_rejects_late_publication(self):
        events = UiEventQueue()
        item = Payload(text="large record")
        ref = weakref.ref(item)
        events.put(item)
        del item
        self.assertIsNotNone(ref())
        events.close(); events.close()
        self.assertIsNone(ref())
        events.put(Payload(text="late result"))
        self.assertTrue(events.empty())
        with self.assertRaises(queue.Empty): events.get_nowait()


class ViewLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk(); self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source, self.result = self.base / "source.json", self.base / "result.json"
        self.source.write_text(json.dumps([{"id": i, "value": "old"} for i in range(30)]), encoding="utf-8")
        self.result.write_text(json.dumps([{"id": i, "value": "new"} for i in range(30)]), encoding="utf-8")
        self.rule = validate_config({"schema_version": 1, "name": "lifetime",
            "source": {"key": "/id"}, "result": {"key": "/id"},
            "rules": [{"kind": "field", "from": "/value", "to": "/value"}]})

    def tearDown(self):
        self.root.destroy()
        self.temp.cleanup()
        self.assertEqual(self.errors, [])

    def until(self, predicate, pump=True):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            if pump: self.root.update()
            time.sleep(.005)
        self.assertTrue(predicate())

    def collect(self, refs):
        self.root.update()
        gc.collect()
        self.assertEqual([ref() for ref in refs], [None] * len(refs))

    def test_twenty_closed_viewers_and_their_records_are_collectible(self):
        refs = []
        for _ in range(20):
            viewer = JsonViewer(self.root); viewer.window.withdraw()
            record = Payload(items=list(range(150)))
            viewer.display(Document([[record]], "test"))
            refs.extend((weakref.ref(viewer), weakref.ref(viewer.browser), weakref.ref(record)))
            query = viewer.browser.query
            status_ref = weakref.ref(viewer.status)
            query.set("pending search")
            viewer.window.destroy()
            self.assertFalse(query.trace_info())
            self.assertEqual(viewer.values, {})
            self.assertEqual(viewer.more, {})
            self.assertIsNone(viewer.browser.on_select)
            self.assertIsNone(status_ref())
            del viewer, record, query
        self.collect(refs)
        self.assertFalse(self.root.tk.call("after", "info"))

    def test_viewer_discards_document_completed_before_poll(self):
        viewer = JsonViewer(self.root)
        viewer.load(self.source)
        self.until(lambda: viewer.events.qsize() >= 2, pump=False)
        # Reading has published progress + done; no UI poll has consumed them.
        self.assertFalse(viewer.values)
        viewer.window.destroy()
        self.assertTrue(viewer.events.empty())
        ref = weakref.ref(viewer)
        del viewer
        self.collect([ref])

    def test_viewer_close_between_checkpoint_and_result_publication(self):
        viewer = JsonViewer(self.root)
        events = viewer.events
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original_put = events.put
        documents = []
        def paused_put(item):
            if item[1] == "done":
                documents.append(weakref.ref(item[2]))
                entered.set()
                if not release.wait(5): raise TimeoutError("test did not release publication")
            original_put(item)
            if item[1] == "done": finished.set()
        with patch.object(events, "put", side_effect=paused_put):
            viewer.load(self.source)
            try:
                self.assertTrue(entered.wait(5))
                viewer.window.destroy()
            finally:
                release.set()
            self.assertTrue(finished.wait(5))
        self.assertTrue(events.empty())
        self.until(lambda: not any(t.name == "json-viewer" for t in threading.enumerate()), pump=False)
        ref = weakref.ref(viewer)
        del viewer
        self.collect([ref, *documents])

    def test_file_browser_discards_scan_finishing_after_destroy(self):
        viewer = JsonViewer(self.root)
        browser = viewer.browser
        query = browser.query
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original_put = browser.events.put
        def paused_put(item):
            entered.set()
            if not release.wait(5): raise TimeoutError("test did not release scan")
            original_put(item); finished.set()
        with patch.object(browser.events, "put", side_effect=paused_put):
            browser.set_inputs([self.base])
            try:
                self.assertTrue(entered.wait(5))
                viewer.window.destroy()
            finally:
                release.set()
            self.assertTrue(finished.wait(5))
        self.assertTrue(browser.events.empty())
        self.assertEqual(browser.paths, [])
        self.assertFalse(query.trace_info())

    def test_pair_preview_releases_unconsumed_preloaded_data_on_parent_destroy(self):
        parent = tk.Toplevel(self.root)
        data = load_preview(self.rule, self.source, [self.result])
        preview = PairPreview(parent, self.rule, configured=True, preloaded=data)
        refs = [weakref.ref(preview), weakref.ref(data)]
        variables = [preview.query, preview.scope, preview.connection_state, preview.source, preview.root_path, preview.number]
        self.assertEqual(preview.queue.qsize(), 2)
        parent.destroy()
        self.assertTrue(preview.closed)
        self.assertTrue(preview.queue.empty())
        self.assertTrue(all(not variable.trace_info() for variable in variables))
        preview.close()
        del preview, data, parent, variables
        self.collect(refs)

    def test_pair_preview_close_during_selected_record_evaluation(self):
        data = load_preview(self.rule, self.source, [self.result])
        preview = PairPreview(self.root, self.rule)
        preview.cache = data
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        from label_porter.pair_preview import catalog_preview
        def delayed(data, index):
            pair = catalog_preview(data, index)
            entered.set()
            if not release.wait(5): raise TimeoutError("test did not release evaluation")
            return pair
        events, original_put = preview.queue, preview.queue.put
        def publish(item):
            original_put(item); finished.set()
        with patch("label_porter.pair_preview_ui.catalog_preview", side_effect=delayed), \
             patch.object(events, "put", side_effect=publish):
            preview._select_index(0)
            try:
                self.assertTrue(entered.wait(5))
                preview.destroy()
            finally:
                release.set()
            self.assertTrue(finished.wait(5))
        self.assertTrue(events.empty())
        self.assertIsNone(preview.cache)
        self.assertIsNone(preview.pair)
        self.assertEqual(preview.pair_cache, {})
        self.assertEqual(preview.filtered, [])

    def test_hiding_preview_preserves_session_and_search(self):
        data = load_preview(self.rule, self.source, [self.result])
        preview = PairPreview(self.root, self.rule, configured=True, preloaded=data)
        preview.pack()
        self.until(lambda: preview.pair is not None)
        preview.pack_forget(); self.root.update(); preview.pack()
        self.assertIs(preview.cache, data)
        self.assertFalse(preview.closed)
        preview.query.set("new")
        self.until(lambda: len(preview.filtered) == 30 and preview.pair is not None)
        self.assertEqual(preview.pair["output"]["value"], "new")
        preview.destroy()

    def test_rule_review_and_builder_parent_destruction_remove_owned_traces(self):
        parent = tk.Toplevel(self.root)
        review = RuleReview(parent, self.rule, lambda: None, lambda: None)
        builder = EasyRules(parent, lambda: None, lambda *args: None, lambda: None)
        data = load_preview(self.rule, self.source, [self.result])
        builder.inputs = builder.data = data
        builder.options = [Payload(before=data)]
        builder.queue.put((0, "unused", data))
        refs = [weakref.ref(review), weakref.ref(builder), weakref.ref(data)]
        variables = [review.query, review.filter_kind, builder.source, builder.query, builder.show_all]
        parent.destroy()
        self.assertTrue(review.closed and builder.closed)
        self.assertTrue(builder.queue.empty())
        self.assertIsNone(builder.inputs)
        self.assertEqual(builder.options, [])
        self.assertTrue(all(not variable.trace_info() for variable in variables))
        del review, builder, data, variables, parent
        self.collect(refs)

    def test_app_releases_only_the_viewer_that_was_closed(self):
        # Avoid real user settings and expensive help construction in this ownership test.
        app = object.__new__(App)
        older = JsonViewer(self.root, on_close=app._json_viewer_closed)
        newer = JsonViewer(self.root, on_close=app._json_viewer_closed)
        app.json_viewer = newer
        older.window.destroy()
        self.assertIs(app.json_viewer, newer)
        newer.window.destroy()
        self.assertIsNone(app.json_viewer)
        refs = [weakref.ref(older), weakref.ref(newer)]
        del older, newer
        self.collect(refs)

    def test_scrollpage_removes_only_its_own_root_callback(self):
        first, second = ScrollPage(self.root), ScrollPage(self.root)
        command, other = first.wheel_command, second.wheel_command
        ref = weakref.ref(first)
        first.destroy()
        self.assertFalse(self.root.tk.call("info", "commands", command))
        self.assertTrue(self.root.tk.call("info", "commands", other))
        self.assertTrue(second.bind_class(second.tag, "<MouseWheel>"))
        del first
        self.collect([ref])
        second.destroy()

    def test_markdown_destroy_releases_search_trace_and_timer(self):
        view = MarkdownViewer(self.root, "# Title\n\nExample", navigate=lambda _: None)
        query = view.query
        query.set("pending search")
        job = view.search_job
        view.destroy()
        self.assertFalse(query.trace_info())
        self.assertNotIn(job, self.root.tk.call("after", "info"))
        self.assertIsNone(view.navigate)
        self.assertEqual(view.html_source, "")


if __name__ == "__main__":
    unittest.main()
