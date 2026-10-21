import copy
import json
import tkinter as tk
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from label_porter.config import load_config
from label_porter.rule_review import RuleReview, profile_connections, describe_expression
from label_porter.ui import setup_style


class ReviewModelTests(unittest.TestCase):
    def test_nested_keys_guards_transforms_and_paths_are_shown_without_mutation(self):
        profile = load_config(Path('profiles/013_audio.json'))
        before = copy.deepcopy(profile)
        rows = profile_connections(profile)
        self.assertEqual([r['kind'] for r in rows], ['key', 'items', 'key', 'guard', 'field', 'field', 'field'])
        self.assertEqual(rows[4]['source'], '/Segmentation/eojeols[]/text')
        self.assertIn('/orthographic_1/data[]/value/name_J2REGU', rows[4]['result'])
        self.assertIn('정규식 추출', rows[2]['result'])
        self.assertIn('정수 변환', rows[2]['result'])
        self.assertIn('앞뒤 공백 제거', rows[3]['source'])
        rows[1]['raw']['require_same_keys'] = False
        self.assertEqual(profile, before)

    def test_expected_root_scopes_literals_and_computed_lists_are_not_hidden(self):
        profile = {'schema_version':1,'name':'complex','source':{'key':'/id'},'result':{'key':'/id'},'rules':[
            {'kind':'field','from':{'scope':'root_result','path':'/final','transforms':[{'op':'scale','value':.001}]},
             'to':'/time','expected':{'literal':0},'when':[{'left':'/ok','op':'eq','value':True}]},
            {'kind':'items','from':{'path':'/serialized','transforms':[{'op':'parse_json'}]},'to':'/parts',
             'source_key':'/id','result_key':'/id','rules':[{'kind':'field','from':'/value','to':'/text'}]}]}
        rows = profile_connections(profile)
        self.assertIn('전체 결과 레코드 · /final', rows[1]['result'])
        self.assertIn('0.001', rows[1]['result'])
        self.assertEqual(rows[2]['result'], '고정값 0')
        self.assertIn('적용 조건 1개', rows[1]['notes'])
        self.assertIn('[상위 목록 표현식의 결과][]', rows[-1]['result'])
        self.assertNotIn('/serialized[]', rows[-1]['result'])
        self.assertEqual(describe_expression({'scope':'root_original','path':'/id'}, source_prefix='/parts[]'), '전체 보완 전 레코드 · /id')

    def test_bundled_profiles_preserved_exactly(self):
        for path in Path('profiles').glob('*.json'):
            profile = load_config(path)
            before = copy.deepcopy(profile)
            rows = profile_connections(profile)
            self.assertTrue(rows, path)
            self.assertEqual(profile, before)


class ReviewUiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk(); self.root.withdraw()
        except tk.TclError:
            self.skipTest('Tk unavailable')
        setup_style(self.root)
        self.profile = load_config(Path('profiles/013_audio.json'))
        self.requests, self.accepted = [], []
        self.review = RuleReview(self.root, self.profile, lambda:self.accepted.append(True), lambda:None)
        self.root.update()

    def tearDown(self):
        self.review._close(); self.root.destroy()

    def test_search_select_and_all_tabs_leave_profile_identical(self):
        before = copy.deepcopy(self.profile)
        self.review.query.set('/start')
        self.assertEqual(len(self.review.visible_rows()), 1)
        self.review.query.set(''); self.review.filter_kind.set('기존값·동일성 검사')
        self.assertEqual(len(self.review.visible_rows()), 1)
        self.review.filter_kind.set('전체 연결')
        self.assertEqual(len(self.review.visible_rows()), len(self.review.rows))
        item = next(item for item in self.review.canvas.find_withtag('connection:1') if self.review.canvas.type(item) == 'rectangle')
        box = self.review.canvas.bbox(item)
        self.review._select(SimpleNamespace(x=box[0]+5, y=box[1]+5))
        self.assertIn('require_same_keys', self.review.detail.get('1.0','end'))
        self.assertEqual(json.loads(self.review.raw_box.get('1.0','end')), before)
        self.assertEqual([self.review.pages.tab(t, 'text') for t in self.review.pages.tabs()], ['필드 연결', '필드 연결 프리뷰', '규칙 JSON'])
        self.assertEqual(str(self.review.detail['state']), 'disabled')
        self.assertEqual(str(self.review.raw_box['state']), 'disabled')
        self.assertFalse(self.review.canvas.bind('<B1-Motion>'))
        self.assertEqual(self.review.profile, before)
        self.review.apply_button.invoke()
        self.assertEqual(self.accepted, [True])
        self.assertEqual(self.profile, before)

    def test_preview_is_optional_and_read_only(self):
        self.review.pages.select(1)
        self.review.preview.load_button.invoke()
        self.assertIn("선택하세요", self.review.preview.status.get())
        self.assertTrue(all(str(box["state"]) == "disabled" for box in self.review.preview.json_boxes))
        self.assertFalse(hasattr(self.review, "revision_text"))
        self.review.apply_button.invoke()
        self.assertEqual(self.accepted, [True])
        self.assertTrue(self.review.preview.closed)

    def test_profile_only_reuses_nested_connections_without_creating_a_data_preview(self):
        with patch('label_porter.rule_review.PairPreview', side_effect=AssertionError('Unexpected file loader')):
            panel = RuleReview(self.root, self.profile, lambda: None, lambda: None, profile_only=True, external_navigation=True)
        try:
            self.root.update()
            self.assertIsNone(panel.preview)
            self.assertEqual(panel.rows, profile_connections(self.profile))
            self.assertEqual(json.loads(panel.raw_box.get('1.0', 'end')), self.profile)
            panel.query.set('/start')
            self.assertEqual(len(panel.visible_rows()), 1)
            panel.query.set('')
            item = next(i for i in panel.canvas.find_withtag('connection:1') if panel.canvas.type(i) == 'rectangle')
            box = panel.canvas.bbox(item)
            panel._select(SimpleNamespace(x=box[0]+5, y=box[1]+5))
            self.assertEqual(panel.detail.frame.winfo_manager(), 'pack')
            self.assertIn('require_same_keys', panel.detail.get('1.0', 'end'))
        finally: panel._close(notify=False)
