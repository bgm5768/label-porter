import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from label_porter.engine import Cancelled, run
from label_porter.formats import DataError
from label_porter.input_check import InputRequest
from label_porter.pair_preview import catalog_preview, search_catalog, load_preview, match_preview
from label_porter.prepared_session import PreparedSession
from label_porter.rules import apply_record


class PreparedSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'; self.source.mkdir()
        self.sf = self.source / 'all.json'
        self.rf = self.base / 'incoming.json'
        self.sf.write_text('[{"id":"a","label":"old"}]', encoding='utf-8')
        self.rf.write_text('[{"id":"a","label":"new","enabled":true}]', encoding='utf-8')
        self.config = {'schema_version': 1, 'name': 'prepared test', 'source': {'key': '/id'},
                       'result': {'key': '/id'}, 'rules': [{'kind': 'field', 'from': '/label', 'to': '/label', 'nonempty': True}],
                       'include_when': [{'left': '/enabled', 'op': 'eq', 'value': True}]}
        self.request = InputRequest(self.config, str(self.source), (str(self.rf),), '', str(self.base / 'out'), True)

    def prepare(self):
        session = PreparedSession.prepare(self.request, lambda e: None, threading.Event())
        self.addCleanup(session.temp.cleanup)
        return session

    def test_once_only_and_preview_values_equal_saved_output(self):
        before = self.sf.read_bytes()
        (self.source / 'audio.wav').write_bytes(b'\x00original audio')
        (self.source / 'empty').mkdir()
        with patch('label_porter.engine.apply_record', wraps=apply_record) as apply:
            session = self.prepare()
            self.assertEqual(apply.call_count, 1)
            self.assertFalse((self.base / 'out').exists())
            for _ in range(3):
                pair = catalog_preview(session, 0)
                self.assertEqual(pair['rows'][1]['source'], '"old"')
                self.assertEqual(pair['rows'][1]['evaluated'], '"new"')
                self.assertEqual(search_catalog(session, 'new', '보완 JSON'), [0])
                self.assertEqual(search_catalog(session, 'old', '원본 JSON'), [0])
                self.assertEqual(search_catalog(session, 'absent'), [])
            with patch('label_porter.prepared_session.run', side_effect=AssertionError('second execution')):
                result = session.publish(self.request, lambda e: None, threading.Event())
            self.assertEqual(apply.call_count, 1)
        self.assertEqual(result.summary['status'], 'completed')
        self.assertEqual(json.loads((result.directory / 'data/all.json').read_text())[0], pair['output'])
        self.assertEqual((result.directory / 'data/audio.wav').read_bytes(), b'\x00original audio')
        self.assertTrue((result.directory / 'data/empty').is_dir())
        self.assertEqual(self.sf.read_bytes(), before)
        report = (result.directory / 'reports/report.html').read_text(encoding='utf-8')
        self.assertNotIn(str(session.result.directory), report)
        self.assertIn(str(result.directory), report)
        self.assertFalse((result.directory / 'preview.sqlite').exists())
        reference = match_preview(load_preview(self.config, str(self.source), [str(self.rf)]), 0)
        self.assertEqual(pair['rows'], reference['rows'])

    def test_all_categories_and_original_shapes_equal_existing_engine(self):
        originals = [{'id': key, 'label': 'original', 'keep': [True, 1.23456789]} for key in ('ok', 'review', 'excluded', 'missing', 'dup', 'dup')]
        incoming = [{'id': key, 'label': value, 'enabled': key != 'excluded'} for key, value in
                    [('ok', 'done'), ('review', ''), ('excluded', 'skip'), ('orphan', 'extra'), ('dup', 'uncertain')]]
        self.sf.write_text(json.dumps(originals), encoding='utf-8')
        self.rf.write_text(json.dumps(incoming), encoding='utf-8')
        (self.source / 'broken.json').write_bytes(b'{broken')
        broken_result = self.base / 'broken.json'; broken_result.write_bytes(b'[invalid')
        self.request = replace(self.request, result_paths=(str(self.rf), str(broken_result)))
        session = self.prepare()
        actual = session.publish(self.request, lambda e: None, threading.Event())
        reference = run(self.source, list(self.request.result_paths), self.base / 'reference', self.config)
        self.assertEqual(actual.summary['status'], 'completed_with_issues')
        for key in ('source_status_counts', 'result_issue_counts', 'source_file_issue_counts', 'examine_category_counts', 'partition_records'):
            self.assertEqual(actual.summary[key], reference.summary[key], key)
        self.assertEqual(set(actual.summary['examine_category_counts']), {'unmatched', 'review', 'excluded', 'file_errors'})
        for prefix in ('data', 'changed', 'unchanged', 'examine'):
            expected = {p.relative_to(reference.directory): p.read_bytes() for p in (reference.directory / prefix).rglob('*') if p.is_file()}
            saved = {p.relative_to(actual.directory): p.read_bytes() for p in (actual.directory / prefix).rglob('*') if p.is_file()}
            self.assertEqual(saved, expected, prefix)
        source_statuses = [session.prepared_pair(i)['status'] for i in range(len(session.catalog))]
        self.assertIn('review_required', source_statuses)
        self.assertIn('excluded', source_statuses)
        self.assertTrue(search_catalog(session, state='파일 오류'))

    def test_same_size_same_timestamp_input_edit_blocks_save(self):
        session = self.prepare()
        stat = self.sf.stat()
        self.sf.write_bytes(self.sf.read_bytes().replace(b'old', b'bad'))
        os.utime(self.sf, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with self.assertRaisesRegex(DataError, '입력 내용'):
            session.publish(self.request, lambda e: None, threading.Event())
        self.assertFalse(session.ready)
        self.assertFalse((self.base / 'out').exists())

    def test_deleted_input_invalidates_the_prepared_session(self):
        session = self.prepare()
        self.rf.unlink()
        with self.assertRaises(OSError): session.publish(self.request, lambda e: None, threading.Event())
        self.assertFalse(session.ready)
        self.assertFalse((self.base / 'out').exists())

    def test_input_addition_and_rule_edit_invalidate_but_destination_change_reuses(self):
        session = self.prepare()
        changed = replace(self.request, config={**self.config, 'name': 'changed'})
        with self.assertRaises(DataError): session.publish(changed, lambda e: None, threading.Event())
        moved = replace(self.request, output=str(self.base / 'elsewhere'))
        with patch('label_porter.engine.apply_record', side_effect=AssertionError('rerun')):
            result = session.publish(moved, lambda e: None, threading.Event())
        self.assertTrue(result.directory.is_relative_to(self.base / 'elsewhere'))
        (self.source / 'extra.json').write_text('{"id":"extra"}')
        with self.assertRaises(DataError): session.publish(self.request, lambda e: None, threading.Event())
        self.assertFalse(session.ready)

    def test_cancel_preparation_does_not_produce_ready_session(self):
        cancel = threading.Event()
        def progress(event):
            if event['phase'] == 'apply': cancel.set()
        with self.assertRaises(Cancelled): PreparedSession.prepare(self.request, progress, cancel)
        self.assertFalse((self.base / 'out').exists())

    def test_publication_inside_result_folder_excludes_only_own_output(self):
        folder = self.base / 'incoming'; folder.mkdir()
        self.rf.rename(folder / 'incoming.json')
        self.request = replace(self.request, result_folder=str(folder), result_paths=(), output=str(folder / 'saved'))
        session = self.prepare()
        for _ in range(2):
            result = session.publish(self.request, lambda e: None, threading.Event())
            self.assertEqual(result.summary['status'], 'completed')
        (folder / 'new-user-input.json').write_text('{"id":"other","label":"added","enabled":true}')
        with self.assertRaises(DataError): session.publish(self.request, lambda e: None, threading.Event())
        self.assertFalse(session.ready)

    def test_cancel_publication_and_corrupt_staging_are_not_completed(self):
        # Enough artifact files to cancel after a visible copy progress event.
        for i in range(105): (self.source / f'asset-{i}.bin').write_bytes(b'asset')
        session = self.prepare()
        cancel = threading.Event()
        def progress(event):
            if event['phase'] == 'publish': cancel.set()
        result = session.publish(self.request, progress, cancel)
        self.assertEqual(result.summary['status'], 'cancelled')
        self.assertIn('저장 미완료', (result.directory / 'reports/report.html').read_text(encoding='utf-8'))
        self.assertEqual(json.loads((result.directory / 'manifest.json').read_text())['status'], 'cancelled')
        self.assertTrue(session.ready)  # Clean, sealed data is reusable after a cancelled copy.
        (session.result.directory / 'data/all.json').write_text('tampered')
        result = session.publish(self.request, lambda e: None, threading.Event())
        self.assertEqual(result.summary['status'], 'failed')
        self.assertFalse(session.ready)

    def test_jsonl_precision_and_list_rebuild_capture(self):
        self.sf = self.source / 'all.jsonl'
        (self.source / 'all.json').unlink()
        self.sf.write_text('{"id":"a","items":[{"id":1,"value":0.12}]}\n', encoding='utf-8-sig')
        self.rf.write_text('[{"id":"a","items":[{"id":1,"value":1.23456789},{"id":2,"value":2.0}]}]')
        cfg = {**self.config, 'source': {'key': '/id', 'glob': '**/*.jsonl'}, 'rules': [
            {'kind': 'rebuild_items', 'to': '/items', 'from': '/items',
             'source_key': {'scope': 'source', 'path': '/id'}, 'result_key': '/id', 'rules': [
                 {'kind': 'field', 'from': '/id', 'to': '/id', 'allow_create': True}, {'kind': 'field', 'from': '/value', 'to': '/value', 'allow_create': True}]}]}
        cfg.pop('include_when')
        self.request = replace(self.request, config=cfg)
        session = self.prepare()
        pair = session.prepared_pair(0)
        result = session.publish(self.request, lambda e: None, threading.Event())
        saved = result.directory / 'data/all.jsonl'
        self.assertTrue(saved.read_bytes().startswith(b'\xef\xbb\xbf'))
        self.assertEqual(json.loads(saved.read_text(encoding='utf-8-sig')), pair['output'])
        self.assertEqual(pair['output']['items'][0]['value'], 1.23456789)
        self.assertTrue(any(row.get('comparison') == 'id' and row['source'] == '0.12' for row in pair['rows']))
