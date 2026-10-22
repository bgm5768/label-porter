from __future__ import annotations

import copy
import json
import re
from decimal import Decimal
from pathlib import PurePosixPath

from .formats import (MISSING, DataError, canonical_key, equivalent, escape_token,
                      get_pointer, set_pointer, valid_number)


class ReviewRequired(DataError):
    pass


def truncate_decimal(value, digits):
    """Drop decimal digits toward zero, without binary scaling or rounding up.

    Use the parsed number's decimal spelling, not its exact binary expansion.
    Tuple slicing also avoids Decimal context precision and huge powers of ten.
    """
    if type(digits) is not int or digits < 0:
        raise ValueError("truncate.digits는 0 이상의 정수여야 합니다")
    if not valid_number(value):
        raise ValueError("유한한 숫자가 아닙니다")
    if isinstance(value, int):
        return value
    sign, coefficient, exponent = Decimal(str(value)).as_tuple()
    if exponent >= -digits:
        return value
    keep = len(coefficient) + exponent + digits
    if keep <= 0:
        return 0.0
    return float(Decimal((sign, coefficient[:keep], -digits)))


def expression(expr, context):
    if isinstance(expr, str):
        expr = {"path": expr}
    if "literal" in expr:
        value = copy.deepcopy(expr["literal"])
    elif "meta" in expr:
        value = context["meta"].get(expr["meta"], MISSING)
    else:
        value = get_pointer(context[expr.get("scope", "result")], expr["path"])
    if value is MISSING:
        raise ReviewRequired(f"필수 값이 없습니다: {expr}")
    for transform in expr.get("transforms", []):
        op = transform["op"]
        try:
            if op == "length":
                if not isinstance(value, list):
                    raise ValueError("배열이 아닙니다")
                value = len(value)
            elif op == "strip":
                if not isinstance(value, str):
                    raise ValueError("문자열이 아닙니다")
                value = value.strip()
            elif op == "string":
                if isinstance(value, (dict, list)) or value is None:
                    raise ValueError("스칼라 값이 아닙니다")
                value = str(value)
            elif op in ("integer", "number"):
                if isinstance(value, bool) or value is None:
                    raise ValueError("숫자가 아닙니다")
                if op == "integer":
                    if isinstance(value, str) and not re.fullmatch(r"[+-]?\d+", value.strip()):
                        raise ValueError("정수 문자열이 아닙니다")
                    if not isinstance(value, str) and (not valid_number(value) or int(value) != value):
                        raise ValueError("정수가 아닙니다")
                    value = int(value)
                else:
                    value = float(value)
                if not valid_number(value):
                    raise ValueError("유한한 숫자가 아닙니다")
            elif op == "basename":
                if not isinstance(value, str):
                    raise ValueError("문자열이 아닙니다")
                value = PurePosixPath(value.replace("\\", "/")).name
            elif op == "regex":
                match = re.search(transform["pattern"], value)
                if not match:
                    raise ValueError("패턴과 일치하지 않습니다")
                value = match.group(transform.get("group", 1))
            elif op == "truncate":
                value = truncate_decimal(value, transform["digits"])
            elif op in ("scale", "offset", "round"):
                if not valid_number(value):
                    raise ValueError("숫자가 아닙니다")
                if op == "scale":
                    value *= transform["value"]
                elif op == "offset":
                    value += transform["value"]
                else:
                    value = round(value, transform.get("digits", 6))
                if not valid_number(value):
                    raise ValueError("계산 결과가 유한하지 않습니다")
            elif op == "pluck":
                if not isinstance(value, list):
                    raise ValueError("배열이 아닙니다")
                value = [get_pointer(item, transform["path"]) for item in value]
                if any(item is MISSING for item in value):
                    raise ValueError("배열 항목의 필드가 없습니다")
            elif op == "join":
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ValueError("문자열 배열이 아닙니다")
                value = transform.get("separator", " ").join(value)
            elif op == "map_values":
                value = transform["mapping"][str(value)]
            elif op == "parse_json":
                from .formats import parse_json
                parsed = parse_json(value)
                if len(parsed) != 1:
                    raise ValueError("JSON 값은 하나여야 합니다")
                value = parsed[0]
            else:
                raise ValueError(f"지원하지 않는 변환: {op}")
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            raise ReviewRequired(f"{op} 변환 실패: {exc}") from exc
    return value


def condition(check, context):
    op = check["op"]
    try:
        left = expression(check["left"], context)
    except ReviewRequired:
        if op == "exists":
            return False
        raise
    right = check.get("value")
    if op == "exists":
        return True
    if op == "eq":
        return equivalent(left, right)
    if op == "ne":
        return not equivalent(left, right)
    if op == "in":
        return any(equivalent(left, item) for item in right)
    if op == "nonempty":
        return left is not None and left != [] and left != {} and (bool(left.strip()) if isinstance(left, str) else True)
    if op == "regex":
        return isinstance(left, str) and re.fullmatch(right, left) is not None
    raise ReviewRequired(f"지원하지 않는 조건: {op}")


def validate_value(value, rule):
    kind = rule.get("type")
    good = {"string": lambda: isinstance(value, str), "number": lambda: valid_number(value),
            "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
            "boolean": lambda: isinstance(value, bool), "array": lambda: isinstance(value, list),
            "object": lambda: isinstance(value, dict), "null": lambda: value is None}
    if kind and not good[kind]():
        raise ReviewRequired(f"{rule['to']}: 값의 자료형이 {kind}이 아닙니다.")
    if rule.get("nonempty") and (value is None or value == [] or value == {} or (isinstance(value, str) and not value.strip())):
        raise ReviewRequired(f"{rule['to']}: 비어 있는 수정값은 자동 반영하지 않습니다.")


def _apply_rules(rules, context, changes, notes, prefix=""):
    for rule in rules:
        active = all(condition(item, context) for item in rule.get("when", []))
        if context.get("_observe"):
            context["_observe"](rule, context, context.get("_preview_prefix", "") + prefix, active)
        if not active:
            continue
        for check in rule.get("checks", []):
            if not condition(check, context):
                raise ReviewRequired(check.get("message", "규칙의 입력 검증 조건을 만족하지 않습니다."))
        if rule["kind"] == "field":
            value = expression(rule["from"], context)
            validate_value(value, rule)
            old = get_pointer(context["source"], rule["to"])
            if old is MISSING and not rule.get("allow_create", False):
                raise ReviewRequired(f"원본에 대상 필드가 없습니다: {prefix}{rule['to']}")
            if "expected" in rule:
                expected = expression(rule["expected"], context)
                if not equivalent(old, expected) and not equivalent(old, value):
                    raise ReviewRequired(f"원본과 플랫폼에 가져왔던 값이 다릅니다: {prefix}{rule['to']}")
            if old is not MISSING and equivalent(old, value):
                continue
            if isinstance(old, (list, dict)) and not rule.get("allow_container_replace", False):
                raise ReviewRequired(f"배열/객체 전체 교체는 명시적 설정이 필요합니다: {prefix}{rule['to']}")
            set_pointer(context["source"], rule["to"], copy.deepcopy(value), rule.get("allow_create", False))
            changes.append({"path": prefix + rule["to"], "operation": "add" if old is MISSING else "replace",
                            "before": None if old is MISSING else old, "after": value})
        elif rule["kind"] == "rebuild_items":
            from .rebuild import rebuild_items
            rebuild_items(rule, context, changes, notes, prefix)
        elif rule["kind"] == "items":
            before = get_pointer(context["source"], rule["to"])
            originals = get_pointer(context["original"], rule["to"])
            incoming = expression(rule["from"], context)
            if not isinstance(before, list) or not isinstance(incoming, list) or not isinstance(originals, list):
                raise ReviewRequired(f"배열 필드가 필요합니다: {prefix}{rule['to']}")
            source_map, result_map = {}, {}
            for index, item in enumerate(before):
                if not isinstance(item, dict):
                    raise ReviewRequired("원본 배열 항목이 객체가 아닙니다.")
                sub = {**context, "source": item, "original": originals[index]}
                key = canonical_key(expression(rule["source_key"], sub))
                if key in source_map:
                    raise ReviewRequired("원본 배열의 연결키가 중복되었습니다.")
                source_map[key] = index
            for item in incoming:
                if not isinstance(item, dict):
                    raise ReviewRequired("작업 결과 배열 항목이 객체가 아닙니다.")
                key = canonical_key(expression(rule["result_key"], {**context, "result": item}))
                if key in result_map:
                    raise ReviewRequired("작업 결과 배열의 연결키가 중복되었습니다.")
                result_map[key] = item
            if result_map.keys() - source_map.keys():
                raise ReviewRequired(f"배열에 새 ID가 있습니다. 분할/병합 연결 규칙이 필요합니다: {prefix}{rule['to']}")
            missing = source_map.keys() - result_map.keys()
            if missing and rule.get("require_same_keys", True):
                raise ReviewRequired(f"배열의 항목 수/ID가 다릅니다 (원본 {len(before)}, 결과 {len(incoming)}). 원본을 보존합니다.")
            if missing:
                notes.append(f"{prefix}{rule['to']}: 결과에 없는 배열 항목 {len(missing)}개를 보존했습니다.")
            for key, item in result_map.items():
                index = source_map[key]
                sub = {**context, "source": before[index], "original": originals[index], "result": item}
                for guard in rule.get("identity_guards", []):
                    if not equivalent(expression(guard["source"], sub), expression(guard["result"], sub)):
                        raise ReviewRequired(f"배열 항목의 동일성을 확인할 수 없습니다: {prefix}{rule['to']}/{index}. 텍스트 변경/재정렬 여부를 확인하세요.")
                _apply_rules(rule["rules"], sub, changes, notes, prefix + rule["to"] + "/" + str(index))


def _assertions(assertions, candidate, result, context):
    for assertion in assertions:
        kind = assertion["kind"]
        if kind == "condition":
            if not condition(assertion["condition"], context):
                raise ReviewRequired(assertion.get("message", "반영 후 검증 조건을 만족하지 않습니다."))
        elif kind == "intervals":
            items = get_pointer(candidate, assertion["path"])
            if not isinstance(items, list):
                raise ReviewRequired("시간 구간 배열이 없습니다.")
            duration = get_pointer(candidate, assertion["duration"]) if "duration" in assertion else None
            if "duration" in assertion and (not valid_number(duration) or duration < 0):
                raise ReviewRequired("원본의 전체 길이가 유효하지 않습니다.")
            last_start = last_end = None
            for item in items:
                start, end = get_pointer(item, assertion["start"]), get_pointer(item, assertion["end"])
                if not valid_number(start) or not valid_number(end) or start < 0 or end < start:
                    raise ReviewRequired("유효하지 않은 시간 구간입니다.")
                if duration is not None and end > duration + assertion.get("tolerance", 0):
                    raise ReviewRequired("오디오 길이를 넘는 시간 구간입니다.")
                if assertion.get("ordered", True) and last_start is not None and start < last_start:
                    raise ReviewRequired("시간 구간의 순서가 뒤바뀌었습니다.")
                if assertion.get("non_overlapping", False) and last_end is not None and start < last_end:
                    raise ReviewRequired("시간 구간이 겹칩니다.")
                last_start, last_end = start, end
        elif kind == "references":
            targets = get_pointer(candidate, assertion["targets"])
            refs = get_pointer(candidate, assertion["references"])
            if not isinstance(targets, list) or not isinstance(refs, list):
                raise ReviewRequired("참조 검증에 필요한 배열이 없습니다.")
            keys = [canonical_key(get_pointer(item, assertion["target_key"])) for item in targets]
            if len(keys) != len(set(keys)):
                raise ReviewRequired("참조 대상 ID가 중복되었습니다.")
            for item in refs:
                if canonical_key(get_pointer(item, assertion["reference_key"])) not in keys:
                    raise ReviewRequired("존재하지 않는 객체를 가리키는 태그가 있습니다.")


def apply_record(original, result, config, meta, advisories=None, *, observer=None):
    candidate = copy.deepcopy(original)
    context = {"source": candidate, "original": original, "result": result,
               "root_source": candidate, "root_original": original, "root_result": result, "meta": meta,
               "advisories": advisories if advisories is not None else []}
    if observer is not None:
        context["_observe"] = observer
    if not all(condition(check, context) for check in config.get("include_when", [])):
        return original, [], ["반영 대상 조건에 해당하지 않아 원본을 보존했습니다."], "excluded"
    for check in config.get("checks", []):
        if not condition(check, context):
            raise ReviewRequired(check.get("message", "입력 검증 조건을 만족하지 않습니다."))
    changes, notes = [], []
    _apply_rules(config["rules"], context, changes, notes)
    _assertions(config.get("assertions", []), candidate, result, context)
    # Classification is based on the final value, even when rules undo earlier edits.
    if equivalent(original, candidate):
        changes = []
    # Every successful record is committed as a whole; exceptions discard candidate.
    return candidate, changes, notes, "updated" if changes else "unchanged"
