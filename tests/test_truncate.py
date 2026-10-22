import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from label_porter.config import validate_config
from label_porter.engine import run
from label_porter.formats import DataError
from label_porter.pair_preview import load_preview, match_preview
from label_porter.rules import ReviewRequired, apply_record, expression


def rule(digits=3):
    return {'schema_version': 1, 'name': 'Explicit decimal truncation',
            'source': {'key': '/id'}, 'result': {'key': '/id'},
            'rules': [{'kind': 'field', 'to': '/value', 'type': 'number',
                       'from': {'path': '/value', 'transforms': [{'op': 'truncate', 'digits': digits}]}}]}


def calculate(value, digits):
    return expression({'literal': value, 'transforms': [{'op': 'truncate', 'digits': digits}]}, {})


class TruncateTests(unittest.TestCase):
    def test_cut_digits_without_rounding_or_flooring(self):
        cases = [(3.323333, 3, 3.323), (3.323333, 1, 3.3), (3.329999, 3, 3.329),
                 (3.39999, 1, 3.3), (-3.329999, 3, -3.329), (-3.99, 0, -3.0),
                 (3.99, 0, 3.0), (-.009, 2, 0.0), (1.20000, 3, 1.2),
                 (1.15, 2, 1.15), (1.005, 2, 1.0), (1.239e-5, 7, .0000123),
                 (5e-324, 323, 0.0), (5e-324, 324, 5e-324),
                 (1.7976931348623157e308, 0, 1.7976931348623157e308)]
        for value, digits, expected in cases:
            with self.subTest(value=value, digits=digits):
                self.assertEqual(calculate(value, digits), expected)
                self.assertLessEqual(abs(calculate(value, digits)), abs(value))

    def test_integer_precision_and_large_digit_setting_do_not_scale_numbers(self):
        integer = 9007199254740993
        self.assertEqual(calculate(integer, 3), integer)
        self.assertIsInstance(calculate(integer, 3), int)
        self.assertEqual(calculate(3.323333, 10**100), 3.323333)
        self.assertEqual(math.copysign(1, calculate(-0.0, 3)), -1)

    def test_digits_must_be_explicit_nonnegative_integer(self):
        for digits in (-1, 1.5, '3', None, True):
            with self.subTest(digits=digits):
                with self.assertRaises(DataError): validate_config(rule(digits))
                with self.assertRaises(ReviewRequired): calculate(3.323333, digits)
        config = rule(); del config['rules'][0]['from']['transforms'][0]['digits']
        with self.assertRaises(DataError): validate_config(config)
        for digits in (0, 1, 3, 10**100): validate_config(rule(digits))

    def test_invalid_values_require_review_and_strings_need_explicit_number(self):
        for value in ('3.323333', True, None, [], {}, float('nan'), float('inf')):
            with self.subTest(value=value):
                with self.assertRaises(ReviewRequired): calculate(value, 3)
        self.assertEqual(expression({'literal': '3.329999', 'transforms': [
            {'op': 'number'}, {'op': 'truncate', 'digits': 3}]}, {}), 3.329)

    def test_unconfigured_fields_and_old_round_behavior_are_unchanged(self):
        source = {'id': 'a', 'value': 0, 'keep': 1.234567}
        incoming = {'id': 'a', 'value': 3.329999, 'keep': 9.99999}
        output, changes, _, _ = apply_record(source, incoming, validate_config(rule()), {})
        self.assertEqual(output, {'id': 'a', 'value': 3.329, 'keep': 1.234567})
        self.assertEqual([c['path'] for c in changes], ['/value'])
        self.assertEqual(incoming['value'], 3.329999)
        self.assertEqual(source['value'], 0)
        self.assertEqual(expression({'literal': 3.329999, 'transforms': [{'op': 'round', 'digits': 3}]}, {}), 3.33)
        self.assertEqual(expression({'literal': 3.329999}, {}), 3.329999)

    def test_preview_and_saved_output_match_and_inputs_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp); original = base/'original'; original.mkdir()
            source = original/'a.json'; result = base/'result.json'
            source.write_text('{"id":"a","value":0}', encoding='utf-8')
            result.write_text('[{"id":"a","value":3.329999}]', encoding='utf-8')
            before = source.read_bytes(), result.read_bytes()
            config = validate_config(rule())
            pair = match_preview(load_preview(config, original, [result]))
            self.assertEqual(pair['output']['value'], 3.329)
            actual = run(original, [result], base/'out', config)
            saved = json.loads((actual.directory/'data/a.json').read_text(encoding='utf-8'))
            self.assertEqual(saved, pair['output'])
            self.assertEqual((source.read_bytes(), result.read_bytes()), before)

    def test_rebuilt_items_share_the_same_truncation(self):
        config = rule()
        config['rules'][0]['allow_create'] = True
        config['rules'] = [{'kind': 'rebuild_items', 'from': '/items', 'to': '/items',
                            'source_key': '/value', 'result_key': '/value',
                            'rules': copy.deepcopy(config['rules'])}]
        config = validate_config(config)
        source = {'id': 'a', 'items': []}
        incoming = {'id': 'a', 'items': [{'value': 3.323333}, {'value': -3.329999}]}
        output = apply_record(source, incoming, config, {})[0]
        self.assertEqual(output['items'], [{'value': 3.323}, {'value': -3.329}])


if __name__ == '__main__': unittest.main()
