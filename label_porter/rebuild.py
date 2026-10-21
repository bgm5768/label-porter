"""Explicit full-list replacement and conservative migration of existing references."""
import copy
import json
from collections import Counter, defaultdict

from .formats import MISSING, canonical_key, equivalent, get_pointer, set_pointer, valid_number
from .rules import ReviewRequired, _apply_rules, expression


def rebuild_items(rule, context, changes, notes, prefix):
    original = get_pointer(context["original"], rule["to"])
    incoming = expression(rule["from"], context)
    if not isinstance(original, list) or not isinstance(incoming, list):
        raise ReviewRequired("목록 재구성에는 원본과 결과 배열이 필요합니다.")
    if any(not isinstance(item, dict) for item in incoming):
        raise ReviewRequired("결과 배열 항목은 객체여야 합니다.")
    incoming = list(incoming)
    if "sort_by" in rule:
        ranked = [(expression(rule["sort_by"], {**context, "result": item}), item) for item in incoming]
        if any(not valid_number(key) for key, _ in ranked):
            raise ReviewRequired("목록 정렬값은 유한한 숫자여야 합니다.")
        incoming = [item for _, item in sorted(ranked, key=lambda pair: pair[0])]
    rebuilt = []
    preview_info = None
    if context.get("_observe"):
        preview_info = {"rule": rule, "context": context, "original": original, "incoming": incoming,
                        "prefix": context.get("_preview_prefix", "") + prefix + rule["to"]}
    for number, item in enumerate(incoming, 1):
        target = {}
        sub = {**context, "source": target, "original": {}, "result": item,
               "meta": {**context["meta"], "item_number": number}}
        if context.get("_observe"):
            sub["_preview_prefix"] = context.get("_preview_prefix", "") + prefix + rule["to"] + "/" + str(number - 1)
            sub["_preview_rebuilt"] = True
            sub["_preview_rebuild_info"] = preview_info
        _apply_rules(rule["rules"], sub, [], [])
        rebuilt.append(target)

    def replace(path, value):
        old = get_pointer(context["source"], path)
        if old is MISSING:
            raise ReviewRequired(f"원본에 대상 필드가 없습니다: {prefix}{path}")
        if not equivalent(old, value):
            set_pointer(context["source"], path, value)
            changes.append({"path": prefix + path, "operation": "replace", "before": old, "after": value})

    replace(rule["to"], rebuilt)
    if not rule.get("references"):
        return
    grouped = {}
    signatures = {}
    frequencies = {}
    for side, items in (("source", original), ("result", incoming)):
        grouped[side] = defaultdict(list)
        signatures[side] = {}
        for index, item in enumerate(items):
            sub = {**context, side: item}
            if side == "source":
                sub["original"] = item
            try:
                key = canonical_key(expression(rule[side + "_key"], sub))
                grouped[side][key].append(index)
            except (ValueError, TypeError, KeyError, IndexError):
                pass
            try:
                signature = [expression(guard[side], sub) for guard in rule["identity_guards"]]
                signatures[side][index] = json.dumps(signature, ensure_ascii=False, sort_keys=True, allow_nan=False)
            except (ValueError, TypeError, KeyError, IndexError):
                # An unreadable legacy ID/identity must never prevent new text/time
                # from being imported. Its references will be quarantined instead.
                continue
        frequencies[side] = Counter(signatures[side].values())
    correspondence = {}
    for key, indexes in grouped["source"].items():
        candidates = grouped["result"].get(key, [])
        if len(indexes) != 1 or len(candidates) != 1:
            continue
        old_index, new_index = indexes[0], candidates[0]
        signature = signatures["source"].get(old_index)
        if (signature is not None and signature == signatures["result"].get(new_index)
                and frequencies["source"][signature] == frequencies["result"][signature] == 1):
            correspondence[key] = new_index
    for ref in rule["references"]:
        tags = get_pointer(context["original"], ref["path"])
        if not isinstance(tags, list):
            raise ReviewRequired(f"기존 태그 배열이 없습니다: {ref['path']}")
        kept, held = [], 0
        for index, tag in enumerate(tags):
            old_id = get_pointer(tag, ref["key"])
            try:
                key = canonical_key(old_id)
                new_index = correspondence[key]
                new_id = get_pointer(rebuilt[new_index], ref["target_key"])
                canonical_key(new_id)
                updated = copy.deepcopy(tag)
                set_pointer(updated, ref["key"], new_id)
                kept.append(updated)
            except (ValueError, TypeError, KeyError, IndexError):
                held += 1
                context.get("advisories", []).append({
                    "code": "held_reference", "message": "새 어절과 확실히 연결되지 않는 기존 발음 오류 태그를 별도 보관했습니다. 어절·시간·전사는 반영합니다.",
                    "source_path": prefix + ref["path"] + "/" + str(index),
                    "item_id": None if old_id is MISSING else old_id, "source_item": copy.deepcopy(tag),
                    "reference_path": prefix + ref["path"],
                })
        replace(ref["path"], kept)
        if held:
            notes.append(f"{prefix}{ref['path']}: 연결 불확실 태그 {held}개 별도 보관")
