import copy
import json
import tempfile
import unittest
from pathlib import Path

from label_porter.config import load_config
from label_porter.formats import DataError
from label_porter.mapping import compile_rule, import_rule, load_sample, preview_rule
from label_porter.rules import apply_record


ROOT = Path(__file__).resolve().parents[1]


class RuleImportTests(unittest.TestCase):
    def config(self):
        return {"schema_version": 1, "name": "AI 규칙", "description": "기존 검증 보존",
                "source": {"glob": "sub/**/*.json", "key": "/id", "record_mode": "object"},
                "result": {"key": "/ref", "format": "auto", "csv_json_columns": ["annotation"]},
                "include_when": [{"left": "/status", "op": "in", "value": ["ready", "done"]}],
                "checks": [{"left": "/ref", "op": "nonempty"}],
                "rules": [{"kind": "field", "from": {"path": "/new", "transforms": [{"op": "strip"}]},
                           "to": "/label", "type": "string", "nonempty": True, "expected": "/old",
                           "when": [{"left": "/status", "op": "eq", "value": "ready"}],
                           "checks": [{"left": "/new", "op": "nonempty"}]}],
                "assertions": [{"kind": "condition", "condition": {"left": {"path": "/id", "scope": "source"}, "op": "nonempty"}}]}

    def compile(self, scope):
        config = scope.rule_config
        return compile_rule(scope, None, None, config["source"].get("records_path", ""),
                               config["result"].get("records_path", ""), config["name"])

    def test_every_bundled_rule_roundtrips_exactly_without_samples(self):
        for file in (ROOT / "rules").glob("*.json"):
            with self.subTest(file=file.name):
                config = load_config(file)
                self.assertEqual(self.compile(import_rule(config)), config)

    def test_arbitrary_transforms_conditions_checks_and_metadata_survive(self):
        config = self.config()
        original = copy.deepcopy(config)
        scope = import_rule(config)
        self.assertEqual(self.compile(scope), config)
        wire = next(w for w in scope.wires if w.kind == "value")
        wire.nonempty = False
        expected = copy.deepcopy(config)
        expected["rules"][0]["nonempty"] = False
        self.assertEqual(self.compile(scope), expected)
        self.assertEqual(config, original)

    def test_editing_transform_changes_only_that_expression(self):
        config = self.config()
        scope = import_rule(config)
        wire = next(w for w in scope.wires if w.kind == "value")
        wire.result_transform = "문자열로"
        expected = copy.deepcopy(config)
        expected["rules"][0]["from"] = {"path": "/new", "transforms": [{"op": "string"}]}
        self.assertEqual(self.compile(scope), expected)

    def test_same_target_conditional_rules_retain_separate_expected_values(self):
        config = self.config()
        second = copy.deepcopy(config["rules"][0])
        second.update(expected="/other_old", when=[{"left": "/status", "op": "eq", "value": "done"}])
        config["rules"].append(second)
        scope = import_rule(config)
        self.assertEqual(self.compile(scope), config)
        first = next(w for w in scope.wires if w.kind == "value")
        scope.wires[:] = [w for w in scope.wires if not (w.kind == "guard" and w.owner is first)]
        expected = copy.deepcopy(config)
        del expected["rules"][0]["expected"]
        self.assertEqual(self.compile(scope), expected)

    def test_new_manual_wire_keeps_imported_conditions_and_assertions(self):
        config = self.config()
        with tempfile.TemporaryDirectory() as directory:
            source, result = Path(directory) / "source.json", Path(directory) / "result.json"
            source.write_text(json.dumps({"id": "a", "label": "old", "score": 1}), encoding="utf-8")
            result.write_text(json.dumps({"ref": "a", "new": " new ", "old": "old", "score": 2, "status": "ready"}), encoding="utf-8")
            ss, rs = load_sample(source), load_sample(result)
            scope = import_rule(config, ss, rs)
            scope.connect("/score", "/score", "value")
            out = self.compile(scope)
            self.assertEqual(out["rules"][0], config["rules"][0])
            self.assertEqual(out["assertions"], config["assertions"])
            actual = apply_record(scope.source_rows[0], scope.result_rows[0], out, ss.meta)[0]
            self.assertEqual(actual, {"id": "a", "label": "new", "score": 2})
            self.assertEqual(preview_rule(out, scope.source_rows, scope.result_rows, ss.meta)["counts"], {"updated": 1})

    def test_literals_root_scopes_and_container_replacement_survive(self):
        config = self.config()
        config["rules"] = [
            {"kind": "field", "from": {"literal": "fixed"}, "to": "/label"},
            {"kind": "field", "from": {"path": "/labels", "scope": "root_result"}, "to": "/labels", "allow_container_replace": True, "type": "array"},
        ]
        self.assertEqual(self.compile(import_rule(config)), config)

    def test_nested_edit_keeps_identity_guards_interval_and_reference_checks(self):
        config = load_config(ROOT / "rules" / "013_audio.json")
        scope = import_rule(config)
        array = next(w for w in scope.wires if w.kind == "array")
        field = next(w for w in array.child.wires if w.kind == "value" and w.source == "/start")
        field.result_transform = "밀리초 → 초"
        out = self.compile(scope)
        expected = copy.deepcopy(config)
        expected["rules"][0]["rules"][1]["from"] = {"path": "/value/startTime", "transforms": [{"op": "number"}, {"op": "scale", "value": .001}]}
        self.assertEqual(out, expected)

    def test_key_replacement_keeps_all_other_rules(self):
        config = self.config()
        scope = import_rule(config)
        # These fields already exist as the two imported key ports.
        scope.connect("/id", "/ref", "key")
        out = self.compile(scope)
        self.assertEqual(out["rules"], config["rules"])
        self.assertEqual(out["source"]["key"], {"path": "/id"})

    def test_removing_last_value_or_leaving_orphan_guard_is_not_silently_saved(self):
        scope = import_rule(self.config())
        scope.wires[:] = [w for w in scope.wires if w.kind != "value"]
        with self.assertRaises(DataError):
            self.compile(scope)


if __name__ == "__main__":
    unittest.main()
