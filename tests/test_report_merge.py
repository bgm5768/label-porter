import json
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from label_porter.engine import run
from label_porter.report_data import merge_reports, import_data, rows, validate_data
from tests.test_excel_report import sheets


class ReportMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'; self.source.mkdir()
        (self.source / 'one.json').write_text(json.dumps({'id':'001', 'text':'before', 'tags': []}), encoding='utf-8')
        self.rule = {'schema_version':1, 'name':'TC1 질의 적정성', 'source':{'key':'/id'}, 'result':{'key':'/id'}, 'rules':[{'kind':'field', 'from':'/text', 'to':'/text'}]}

    def generate(self, text, name, preview=False):
        path = self.base / (name + '.json')
        path.write_text(json.dumps({'id':'001', 'text':text}), encoding='utf-8')
        rule = {**self.rule, 'name':name}
        result = run(self.source, [path], self.base / name, rule, preview=preview)
        self.assertTrue(result.summary['status'].startswith('completed'), result.summary)
        return result.directory / 'reports/report.xlsx'

    def test_standalone_merge_nested_dedup_and_conflict(self):
        one = self.generate('first', 'TC1')
        two = self.generate('second', 'TC2')
        stand = self.base / 'standalone'; stand.mkdir()
        shutil.copyfile(one, stand / 'one.xlsx'); shutil.copyfile(two, stand / 'two.xlsx')
        first = merge_reports([stand/'one.xlsx', stand/'two.xlsx', stand/'one.xlsx'], stand/'merged.xlsx', {'project_id':'008', 'quality':'의미정확성'})
        self.assertEqual((first['runs'], first['duplicates_skipped']), (2, 1))
        tab = sheets(stand/'merged.xlsx')
        self.assertEqual(len(tab['보완 상세 이력'][1])-1, 2)
        self.assertEqual(len(tab['병합 확인'][1])-1, 2)
        self.assertEqual(tab['항목 및 보완 요약표'][1][1]['C3'], '008')
        second = merge_reports([stand/'merged.xlsx', stand/'one.xlsx'], stand/'again.xlsx')
        self.assertEqual((second['runs'], second['duplicates_skipped']), (2, 1))
        self.assertEqual(sheets(stand/'again.xlsx')['항목 및 보완 요약표'][1][4]['B7'], '의미정확성')
        data = import_data(stand/'again.xlsx', stand/'roundtrip.jsonl')
        changes = [r['data'] for r in rows(data) if r['type']=='change']
        self.assertEqual([r['after'] for r in changes], ['first', 'second'])
        self.assertEqual(json.loads((self.source/'one.json').read_text())['text'], 'before')

    def test_long_value_and_preview_preserved_through_excel_only(self):
        # Incompressible enough to exercise multiple hidden-sheet chunks.
        import random
        randomizer = random.Random(19)
        value = '_x000D_ literal & <tag> 한글 \n' + ''.join(randomizer.choice('0123456789abcdefghijklmnopqrstuvwxyz') for _ in range(90000))
        file = self.generate(value, 'long', preview=True)
        merged = merge_reports([file], self.base/'long-merged.xlsx')
        data = list(rows(merged['data_path']))
        self.assertEqual(next(e['data']['after'] for e in data if e['type']=='change'), value)
        detail = next(e['data'] for e in data if e['type']=='detail' and e['data'].get('scope')=='source')
        self.assertEqual(detail['final_record']['text'], 'before')
        self.assertEqual(detail['candidate_record']['text'], value)

    def test_old_folder_import_and_cancellation_keep_existing_output(self):
        workbook = self.generate('new', 'old')
        merged = merge_reports([workbook.parent/'summary.json'], self.base/'old-converted.xlsx')
        self.assertEqual(merged['runs'], 1)
        duplicate = merge_reports([workbook, workbook.parent/'summary.json'], self.base/'old-and-new.xlsx')
        self.assertEqual((duplicate['runs'], duplicate['duplicates_skipped']), (1, 1))
        output = self.base/'keep.xlsx'; output.write_bytes(b'original')
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(InterruptedError): merge_reports([workbook], output, cancel=cancel)
        self.assertEqual(output.read_bytes(), b'original')
        with self.assertRaises(ValueError): merge_reports([workbook], workbook)

    def test_truncated_or_changed_data_rejected(self):
        file = self.generate('x', 'data')
        data = import_data(file, self.base/'data.jsonl')
        text = data.read_text(encoding='utf-8')
        data.write_text(text.rsplit('\n', 2)[0]+'\n', encoding='utf-8')
        with self.assertRaises(ValueError): validate_data(data)
        data.write_text(text.replace('"after":"x"', '"after":"y"'), encoding='utf-8')
        with self.assertRaises(ValueError): validate_data(data)

    def test_locked_output_restores_previous_workbook_and_data(self):
        from label_porter.report_data import _publish_files
        staged = self.base/'new.xlsx'; staged.write_bytes(b'new')
        staged_data = self.base/'new.jsonl'; staged_data.write_bytes(b'new data')
        output = self.base/'out.xlsx'; output.write_bytes(b'old')
        sidecar = self.base/'out.data.jsonl'; sidecar.write_bytes(b'old data')
        import os
        replace = os.replace
        def locked(source, target):
            if source == staged_data: raise PermissionError('locked')
            return replace(source, target)
        with patch('label_porter.report_data.os.replace', side_effect=locked):
            with self.assertRaises(PermissionError):
                _publish_files([(staged, output), (staged_data, sidecar)], self.base)
        self.assertEqual(output.read_bytes(), b'old')
        self.assertEqual(sidecar.read_bytes(), b'old data')


if __name__ == '__main__': unittest.main()
