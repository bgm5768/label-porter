"""Read-only evidence collection after an atomic record has been rejected.

The normal evaluator remains authoritative. This pass continues through independent
checks on a disposable candidate; it never commits data or invents ambiguous matches.
"""
from __future__ import annotations

import copy
from collections import defaultdict

from .formats import MISSING, canonical_key, equivalent, get_pointer, valid_number
from .rules import _apply_rules, condition, expression

ERRORS = (ValueError, TypeError, KeyError, IndexError, OverflowError)


def observed(expr, context):
    spec = {"path": expr} if isinstance(expr, str) else expr
    raw_spec = {k: v for k, v in spec.items() if k != "transforms"}
    data = {"expression": copy.deepcopy(spec)}
    for name, current in (("raw", raw_spec), ("evaluated", spec)):
        try:
            data[name] = expression(current, context)
            data[name + "_present"] = True
        except ERRORS as exc:
            data[name + "_present"] = False
            data[name + "_error"] = str(exc)
    return data


def diagnose_record(original, result, config, meta, first_error):
    candidate = copy.deepcopy(original)
    context = {"source": candidate, "original": original, "result": result,
               "root_source": candidate, "root_original": original, "root_result": result, "meta": meta}
    issues, changes, notes = [], [], []

    def add(code, message, path="", **evidence):
        issues.append({"code": code, "message": message, "source_path": path, **copy.deepcopy(evidence)})

    def checks(items, ctx, path, code):
        for check in items:
            try:
                ok = condition(check, ctx)
                message = check.get("message", "검증 조건을 만족하지 않습니다.")
            except ERRORS as exc:
                ok, message = False, str(exc)
            if not ok:
                add(code, message, path, condition=check, comparison=observed(check["left"], ctx))

    def walk(rules, ctx, prefix="", result_prefix=""):
        for rule in rules:
            path = prefix + rule["to"]
            try:
                if not all(condition(c, ctx) for c in rule.get("when", [])):
                    continue
            except ERRORS as exc:
                add("when_error", str(exc), path, conditions=rule.get("when", []))
                continue
            if rule["kind"] == "field":
                checks(rule.get("checks", []), ctx, path, "rule_check")
                source_value = observed({"scope": "source", "path": rule["to"]}, ctx)
                platform_value = observed(rule["from"], ctx)
                if "expected" in rule:
                    expected = observed(rule["expected"], ctx)
                    if (not expected["evaluated_present"] or not source_value["evaluated_present"]
                            or (not equivalent(source_value.get("evaluated"), expected.get("evaluated"))
                                and (not platform_value["evaluated_present"] or not equivalent(source_value.get("evaluated"), platform_value.get("evaluated"))))):
                        add("expected_mismatch", "원본과 플랫폼 반입 당시 값이 다릅니다.", path,
                            source=source_value, platform=platform_value, expected=expected, result_item_path=result_prefix)
                try:
                    diagnostic_rule = {k: v for k, v in rule.items() if k not in ("checks", "expected")}
                    _apply_rules([diagnostic_rule], ctx, changes, notes, prefix)
                except ERRORS as exc:
                    evidence = {"source": observed({"scope": "source", "path": rule["to"]}, ctx),
                                "platform": observed(rule["from"], ctx), "rule": rule,
                                "result_item_path": result_prefix}
                    if "expected" in rule:
                        evidence["expected"] = observed(rule["expected"], ctx)
                    add("field_validation", str(exc), path, **evidence)
                continue
            if rule["kind"] == "rebuild_items":
                try:
                    _apply_rules([rule], ctx, changes, notes, prefix)
                except ERRORS as exc:
                    add("rebuild_validation", str(exc), path, rule=rule,
                        source=observed({"scope": "source", "path": rule["to"]}, ctx),
                        platform=observed(rule["from"], ctx))
                continue
            checks(rule.get("checks", []), ctx, path, "rule_check")
            before = get_pointer(ctx["source"], rule["to"])
            originals = get_pointer(ctx["original"], rule["to"])
            try:
                incoming = expression(rule["from"], ctx)
                if not all(isinstance(x, list) for x in (before, originals, incoming)):
                    raise ValueError("원본과 결과에 객체 배열이 필요합니다.")
            except ERRORS as exc:
                add("array_unavailable", str(exc), path,
                    source=observed({"scope": "source", "path": rule["to"]}, ctx),
                    platform=observed(rule["from"], ctx))
                continue
            spec = {"path": rule["from"]} if isinstance(rule["from"], str) else rule["from"]
            array_result_path = ("" if spec.get("scope") == "root_result" else result_prefix) + spec.get("path", "")
            if spec.get("transforms") or spec.get("scope", "result") not in ("result", "root_result"):
                array_result_path = "(computed array)"
            maps = []
            for side, items in (("source", before), ("result", incoming)):
                grouped = defaultdict(list)
                for index, item in enumerate(items):
                    try:
                        if not isinstance(item, dict):
                            raise ValueError("배열 항목이 객체가 아닙니다.")
                        sub = {**ctx, side: item}
                        if side == "source":
                            sub["original"] = originals[index]
                        value = expression(rule[side + "_key"], sub)
                        key = canonical_key(value)
                        grouped[key].append((index, item, value))
                    except ERRORS as exc:
                        add("invalid_item_key", str(exc), path, side=side, index=index, item=item)
                for entries in grouped.values():
                    if len(entries) > 1:
                        add("duplicate_item_key", f"{side} 배열의 ID가 중복됩니다.", path,
                            side=side, item_id=entries[0][2], items=[{"index": i, "value": x} for i, x, _ in entries])
                maps.append(grouped)
            sm, rm = maps
            for key, entries in sm.items():
                if key not in rm and rule.get("require_same_keys", True):
                    for index, item, value in entries:
                        add("missing_result_item", "원본 어절/객체 ID에 대응하는 결과가 없습니다.", f"{path}/{index}",
                            item_id=value, source_item=item, result_item=None,
                            source_count=len(before), result_count=len(incoming))
            for key, entries in rm.items():
                if key not in sm:
                    for index, item, value in entries:
                        add("new_result_item", "결과에만 있는 새 ID입니다. 추가·분할·병합 연결을 확인하세요.", path,
                            item_id=value, source_item=None, result_item=item,
                            result_item_path=f"{array_result_path}/{index}",
                            source_count=len(before), result_count=len(incoming))
            for key, entries in rm.items():
                if key not in sm or len(entries) != 1 or len(sm[key]) != 1:
                    continue
                ri, item, value = entries[0]
                index = sm[key][0][0]
                sub = {**ctx, "source": before[index], "original": originals[index], "result": item}
                item_path, incoming_path = f"{path}/{index}", f"{array_result_path}/{ri}"
                for guard in rule.get("identity_guards", []):
                    source_value, result_value = observed(guard["source"], sub), observed(guard["result"], sub)
                    if (not source_value["evaluated_present"] or not result_value["evaluated_present"]
                            or not equivalent(source_value.get("evaluated"), result_value.get("evaluated"))):
                        add("identity_mismatch", "동일성 검사값이 다릅니다.", item_path,
                            item_id=value, source=source_value, platform=result_value,
                            source_item=originals[index], result_item=item, result_item_path=incoming_path)
                # A diagnostic candidate shows evaluable proposed values even if a
                # guard failed. The engine still preserves the entire original.
                walk(rule["rules"], sub, item_path, incoming_path)

    checks(config.get("checks", []), context, "", "record_check")
    walk(config["rules"], context)
    for assertion in config.get("assertions", []):
        kind = assertion["kind"]
        if kind == "condition":
            checks([assertion["condition"]], context, "", "assertion_condition")
            continue
        if kind == "intervals":
            path = assertion["path"]
            items = get_pointer(candidate, path)
            if not isinstance(items, list):
                add("interval_array_missing", "시간 구간 배열이 없습니다.", path)
                continue
            duration = get_pointer(candidate, assertion["duration"]) if "duration" in assertion else None
            if "duration" in assertion and (not valid_number(duration) or duration < 0):
                add("invalid_duration", "전체 길이가 유효한 숫자가 아닙니다.", assertion["duration"],
                    duration=None if duration is MISSING else duration)
            previous = None
            for i, item in enumerate(items):
                start, end = get_pointer(item, assertion["start"]), get_pointer(item, assertion["end"])
                evidence = {"candidate_item": item,
                            "source_item": get_pointer(original, f"{path}/{i}", None)}
                if not valid_number(start) or not valid_number(end):
                    add("invalid_interval", "시작·종료 시간이 유효한 숫자가 아닙니다.", f"{path}/{i}", **evidence)
                    previous = None
                    continue
                if start < 0 or end < start:
                    add("invalid_interval", "0 ≤ 시작 ≤ 종료 조건을 만족하지 않습니다.", f"{path}/{i}", **evidence)
                if valid_number(duration) and end > duration + assertion.get("tolerance", 0):
                    add("duration_exceeded", "종료 시간이 전체 길이를 초과합니다.", f"{path}/{i}",
                        duration=duration, tolerance=assertion.get("tolerance", 0), **evidence)
                if previous is not None:
                    if assertion.get("ordered", True) and start < previous[0]:
                        add("interval_order", "이전 항목보다 시작 시간이 빠릅니다.", f"{path}/{i}",
                            previous_item=items[i-1], **evidence)
                    if assertion.get("non_overlapping", False) and start < previous[1]:
                        add("interval_overlap", "이전 종료 시간과 현재 시작 시간이 겹칩니다.", f"{path}/{i}",
                            previous_item=items[i-1], overlap=previous[1]-start, **evidence)
                previous = start, end
        elif kind == "references":
            targets, refs = get_pointer(candidate, assertion["targets"]), get_pointer(candidate, assertion["references"])
            if not isinstance(targets, list) or not isinstance(refs, list):
                add("reference_array_missing", "참조 검증에 필요한 배열이 없습니다.", assertion["references"], assertion=assertion)
                continue
            keys = defaultdict(list)
            for i, item in enumerate(targets):
                try:
                    keys[canonical_key(get_pointer(item, assertion["target_key"]))].append(i)
                except ERRORS as exc:
                    add("invalid_target_id", str(exc), f"{assertion['targets']}/{i}", candidate_item=item)
            for indexes in keys.values():
                if len(indexes) > 1:
                    add("duplicate_target_id", "참조 대상 ID가 중복됩니다.", assertion["targets"],
                        indexes=indexes, items=[targets[i] for i in indexes])
            for i, item in enumerate(refs):
                try:
                    value = get_pointer(item, assertion["reference_key"])
                    if canonical_key(value) not in keys:
                        raise ValueError("존재하지 않는 객체를 참조합니다.")
                except ERRORS as exc:
                    add("invalid_reference", str(exc), f"{assertion['references']}/{i}",
                        candidate_item=item, target_ids=[get_pointer(t, assertion["target_key"], None) for t in targets])
    if not issues:
        add("validation_error", str(first_error))
    return {"issues": issues, "source_record": original, "platform_record": result,
            "candidate_record": candidate, "final_record": original,
            "candidate_note": "검토용 부분 후보입니다. ID가 불명확하거나 값 계산이 실패한 부분은 적용하지 않았습니다. 실제 최종값은 원본 그대로입니다.",
            "proposed_changes": changes, "first_error": str(first_error)}
