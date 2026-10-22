from __future__ import annotations

import json
import re
from pathlib import Path

from .formats import DataError, parse_json, tokens, valid_number


def _keys(value, allowed, required=(), label="설정"):
    if not isinstance(value, dict):
        raise DataError(f"{label}: 객체가 필요합니다.")
    unknown = set(value) - set(allowed)
    missing = set(required) - set(value)
    if unknown:
        raise DataError(f"{label}: 지원하지 않는 항목 {sorted(unknown)}")
    if missing:
        raise DataError(f"{label}: 필수 항목 누락 {sorted(missing)}")


def _list(value, label):
    if not isinstance(value, list):
        raise DataError(f"{label}: 배열이 필요합니다.")
    return value


def _bools(value, names):
    for name in names:
        if name in value and not isinstance(value[name], bool):
            raise DataError(f"{name}: true 또는 false가 필요합니다.")


def _expr(expr):
    if isinstance(expr, str):
        tokens(expr)
        return
    _keys(expr, {"path", "scope", "meta", "literal", "transforms"}, label="값 읽기 규칙")
    if sum(key in expr for key in ("path", "meta", "literal")) != 1:
        raise DataError("값 읽기 규칙은 path, meta, literal 중 하나를 지정해야 합니다.")
    if "path" in expr:
        tokens(expr["path"])
    if expr.get("scope", "result") not in ("source", "original", "result", "root_source", "root_original", "root_result"):
        raise DataError("알 수 없는 scope입니다.")
    if "meta" in expr and expr["meta"] not in ("file_name", "relative_path", "file_stem", "result_file", "result_row", "item_number"):
        raise DataError("지원하지 않는 meta 값입니다.")
    options = {
        "strip": (), "string": (), "integer": (), "number": (), "basename": (), "parse_json": (), "length": (),
        "regex": ("pattern", "group"), "scale": ("value",), "offset": ("value",),
        "round": ("digits",), "truncate": ("digits",), "pluck": ("path",), "join": ("separator",), "map_values": ("mapping",),
    }
    for transform in _list(expr.get("transforms", []), "transforms"):
        if not isinstance(transform, dict) or transform.get("op") not in options:
            raise DataError(f"지원하지 않는 변환입니다: {transform}")
        op = transform["op"]
        required = {"regex": ("pattern",), "scale": ("value",), "offset": ("value",),
                    "pluck": ("path",), "map_values": ("mapping",), "truncate": ("digits",)}.get(op, ())
        _keys(transform, {"op", *options[op]}, {"op", *required}, "변환")
        if op == "regex":
            re.compile(transform["pattern"])
        if op in ("scale", "offset") and not valid_number(transform["value"]):
            raise DataError("변환 계수는 유한한 숫자여야 합니다.")
        if op == "round" and (not isinstance(transform.get("digits", 6), int) or isinstance(transform.get("digits"), bool)):
            raise DataError("round.digits는 정수여야 합니다.")
        if op == "truncate" and (type(transform["digits"]) is not int or transform["digits"] < 0):
            raise DataError("truncate.digits는 0 이상의 정수여야 합니다.")
        if op == "pluck":
            tokens(transform["path"])
        if op == "join" and not isinstance(transform.get("separator", " "), str):
            raise DataError("join.separator는 문자열이어야 합니다.")
        if op == "map_values" and not isinstance(transform["mapping"], dict):
            raise DataError("map_values.mapping은 객체여야 합니다.")


def _conditions(conditions):
    for condition in _list(conditions, "조건"):
        _keys(condition, {"left", "op", "value", "message"}, {"left", "op"}, "조건")
        _expr(condition["left"])
        if condition["op"] not in ("eq", "ne", "in", "nonempty", "exists", "regex"):
            raise DataError("지원하지 않는 조건 연산자입니다.")
        if condition["op"] in ("eq", "ne", "in", "regex") and "value" not in condition:
            raise DataError("조건에 비교할 value가 필요합니다.")
        if condition["op"] == "in" and not isinstance(condition["value"], list):
            raise DataError("in 조건의 value는 배열이어야 합니다.")
        if condition["op"] == "regex":
            re.compile(condition["value"])


def _rules(rules, depth=0):
    if depth > 16:
        raise DataError("규칙의 중첩 깊이가 너무 큽니다.")
    for rule in _list(rules, "rules"):
        if not isinstance(rule, dict):
            raise DataError("규칙은 객체여야 합니다.")
        kind = rule.get("kind")
        if kind == "field":
            _keys(rule, {"kind", "from", "to", "when", "checks", "type", "nonempty", "expected", "allow_create", "allow_container_replace"},
                  {"kind", "from", "to"}, "필드 규칙")
            if "expected" in rule:
                _expr(rule["expected"])
            if rule.get("type") not in (None, "string", "number", "integer", "boolean", "array", "object", "null"):
                raise DataError("지원하지 않는 자료형입니다.")
            _bools(rule, ("nonempty", "allow_create", "allow_container_replace"))
        elif kind == "items":
            _keys(rule, {"kind", "from", "to", "source_key", "result_key", "require_same_keys", "identity_guards", "rules", "when", "checks"},
                  {"kind", "from", "to", "source_key", "result_key", "rules"}, "배열 규칙")
            _expr(rule["source_key"])
            _expr(rule["result_key"])
            _bools(rule, ("require_same_keys",))
            for guard in _list(rule.get("identity_guards", []), "identity_guards"):
                _keys(guard, {"source", "result"}, {"source", "result"}, "배열 동일성 확인")
                _expr(guard["source"])
                _expr(guard["result"])
            _rules(rule["rules"], depth + 1)
        elif kind == "rebuild_items":
            _keys(rule, {"kind", "from", "to", "source_key", "result_key", "sort_by", "identity_guards",
                         "rules", "references", "when", "checks"},
                  {"kind", "from", "to", "source_key", "result_key", "rules"}, "목록 재구성")
            _expr(rule["source_key"])
            _expr(rule["result_key"])
            if "sort_by" in rule:
                _expr(rule["sort_by"])
            for guard in _list(rule.get("identity_guards", []), "identity_guards"):
                _keys(guard, {"source", "result"}, {"source", "result"}, "태그 연결 동일성")
                _expr(guard["source"]); _expr(guard["result"])
            _rules(rule["rules"], depth + 1)
            if not rule["rules"] or any(r["kind"] != "field" for r in rule["rules"]):
                raise DataError("목록 재구성의 내부 규칙은 필드 규칙이어야 합니다.")
            for ref in _list(rule.get("references", []), "references"):
                _keys(ref, {"path", "key", "target_key"}, {"path", "key", "target_key"}, "기존 태그 재연결")
                for key in ("path", "key", "target_key"):
                    if not tokens(ref[key]):
                        raise DataError("태그 재연결에는 비어 있지 않은 필드 경로가 필요합니다.")
                if not rule.get("identity_guards"):
                    raise DataError("기존 태그를 재연결하려면 identity_guards가 필요합니다.")
        else:
            raise DataError(f"지원하지 않는 규칙 kind: {kind}")
        _expr(rule["from"])
        if not tokens(rule["to"]):
            raise DataError("레코드 전체 대신 수정할 필드를 지정해야 합니다.")
        _conditions(rule.get("when", []))
        _conditions(rule.get("checks", []))


def validate_config(config):
    # Import v1.1 rules without reviving the removed media-copy feature.
    if isinstance(config, dict):
        config = {key: value for key, value in config.items() if key != "assets"}
    _keys(config, {"schema_version", "name", "description", "source", "result", "include_when", "checks", "rules", "assertions"},
          {"schema_version", "name", "source", "result", "rules"})
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise DataError("지원하는 schema_version은 1입니다.")
    if not isinstance(config["name"], str) or not config["name"].strip():
        raise DataError("설정 이름을 입력하세요.")
    for side in ("source", "result"):
        spec = config[side]
        allowed = {"records_path", "record_mode", "key", "encoding"}
        allowed |= {"glob"} if side == "source" else {"format", "csv_json_columns"}
        _keys(spec, allowed, {"key"}, side)
        _expr(spec["key"])
        tokens(spec.get("records_path", ""))
        if spec.get("record_mode", "auto") not in ("auto", "object", "array"):
            raise DataError("record_mode은 auto/object/array 중 하나여야 합니다.")
        if side == "source":
            pattern = spec.get("glob", "**/*.json")
            if not isinstance(pattern, str) or not pattern or ".." in pattern.replace("\\", "/").split("/") or Path(pattern).is_absolute() or ":" in pattern:
                raise DataError("source.glob에는 원본 폴더 내부의 상대 패턴만 사용할 수 있습니다.")
        else:
            if spec.get("format", "auto") not in ("auto", "json", "csv"):
                raise DataError("결과 파일 형식은 auto/json/csv만 지원합니다.")
            if any(not isinstance(column, str) for column in _list(spec.get("csv_json_columns", []), "csv_json_columns")):
                raise DataError("csv_json_columns에는 열 이름 문자열을 넣어야 합니다.")
    if not config["rules"]:
        raise DataError("적어도 하나의 반영 규칙이 필요합니다.")
    _conditions(config.get("include_when", []))
    _conditions(config.get("checks", []))
    _rules(config["rules"])
    for assertion in _list(config.get("assertions", []), "assertions"):
        if not isinstance(assertion, dict):
            raise DataError("검증 규칙은 객체여야 합니다.")
        kind = assertion.get("kind")
        if kind == "condition":
            _keys(assertion, {"kind", "condition", "message"}, {"kind", "condition"}, "검증")
            _conditions([assertion["condition"]])
        elif kind == "intervals":
            _keys(assertion, {"kind", "path", "start", "end", "duration", "tolerance", "ordered", "non_overlapping"},
                  {"kind", "path", "start", "end"}, "시간 검증")
            for key in ("path", "start", "end", "duration"):
                if key in assertion:
                    tokens(assertion[key])
            if not valid_number(assertion.get("tolerance", 0)) or assertion.get("tolerance", 0) < 0:
                raise DataError("tolerance는 0 이상의 숫자여야 합니다.")
            _bools(assertion, ("ordered", "non_overlapping"))
        elif kind == "references":
            _keys(assertion, {"kind", "targets", "references", "target_key", "reference_key"},
                  {"kind", "targets", "references", "target_key", "reference_key"}, "참조 검증")
            for key in ("targets", "references", "target_key", "reference_key"):
                tokens(assertion[key])
        else:
            raise DataError(f"지원하지 않는 검증 kind: {kind}")
    return config


def load_config(path: Path):
    docs = parse_json(path.read_text(encoding="utf-8-sig"))
    if len(docs) != 1:
        raise DataError("설정 파일은 JSON 객체 하나여야 합니다.")
    return validate_config(docs[0])
