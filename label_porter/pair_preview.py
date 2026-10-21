"""Read-only, key-matched examples using the same decoder and rule evaluator as runs."""
from __future__ import annotations

import copy
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .config import validate_config
from .diagnostics import observed
from .formats import MISSING, DataError, canonical_key, get_pointer, read_document, records
from .rules import apply_record, expression
from .input_files import source_paths, source_layout

ERRORS = (ValueError, TypeError, KeyError, IndexError, OverflowError)
ROW_LIMIT = 200


def display(value, limit=20000):
    if value is MISSING:
        return "필드 없음"
    text = json.dumps(value, ensure_ascii=False, indent=2)
    return text if len(text) <= limit else text[:limit] + "\n… (표시 한도 초과 · 값은 수정하지 않음)"


def context(row, meta):
    return {"source": row, "original": row, "result": row, "root_source": row,
            "root_original": row, "root_result": row, "meta": meta}


def metadata(path, *, result=False, number="", root=None):
    relative = path.relative_to(root) if root else path
    return {"file_name": path.name, "file_stem": path.stem, "relative_path": relative.as_posix(),
            "result_file": str(path) if result else "", "result_row": number if result else ""}


def _file_stamp(paths, root, checkpoint=None):
    entries = []
    for path in paths:
        if checkpoint: checkpoint()
        stat = path.stat()
        entries.append((str(path), stat.st_size, stat.st_mtime_ns))
    return (str(Path(root).expanduser().resolve()) if root else "",
            tuple(entries))


def fingerprint(source, results, root="", pattern="**/*.json", checkpoint=None):
    layout = source_layout(source, pattern, root, checkpoint)
    paths = [*layout.files.values(), *[Path(p).expanduser().resolve() for p in results]]
    return _file_stamp(paths, layout.root, checkpoint)


def matching_indices(data):
    return [i for i, key in enumerate(data.source_keys)
            if key is not None and data.source_counts[key] == 1 and data.result_counts[key] == 1]


@dataclass
class PreviewData:
    profile: dict
    originals: list
    source_keys: list
    source_counts: Counter
    results: dict
    result_counts: Counter
    result_total: int
    invalid_result_keys: int
    fingerprint: tuple
    result_records: list = field(default_factory=list)
    catalog: list = field(default_factory=list)


def load_preview(profile, source, results, root="", progress=None, cancel=None):
    """Index all selected inputs; ambiguous keys are never resolved by row order."""
    profile = copy.deepcopy(validate_config(profile))
    paths = [Path(path).expanduser().resolve() for path in results]
    def checkpoint():
        if cancel and cancel():
            raise DataError("프리뷰 불러오기를 중단했습니다.")
    if progress: progress("원본 파일 목록과 변경 여부를 확인하고 있습니다…")
    if not paths:
        raise DataError("플랫폼 결과 또는 보완 데이터 파일을 선택하세요.")
    if len(set(paths)) != len(paths):
        raise DataError("같은 결과 파일을 두 번 선택했습니다.")
    layout = source_layout(source, profile["source"].get("glob", "**/*.json"), root, checkpoint)
    sources = list(layout.files.values())
    if not sources:
        raise DataError("규칙의 파일 패턴에 맞는 원본 JSON이 없습니다.")
    if set(sources).intersection(paths):
        raise DataError("원본과 보완 결과는 서로 다른 파일을 선택하세요.")
    root = layout.root
    stamp = _file_stamp([*sources, *paths], root, checkpoint)
    last_progress = [0.0]
    def emit(message):
        if progress and time.monotonic() - last_progress[0] >= .15:
            last_progress[0] = time.monotonic()
            progress(message)
    def read(path, spec, title):
        checkpoint()
        def report(done, total, count):
            emit(f"{title} 읽는 중 · {done / max(total, 1):.0%} · {count:,}건")
        return read_document(path, spec.get("encoding", "utf-8-sig"), spec.get("format", "json"),
                             spec.get("csv_json_columns", []), progress=report, checkpoint=checkpoint)
    originals, source_keys, source_counts = [], [], Counter()
    for file_number, (relative, source_file) in enumerate(layout.files.items(), 1):
        source_meta = metadata(relative)
        for doc, pointer, row in records(read(source_file, profile["source"], f"원본 {file_number:,}/{len(sources):,}개"), profile["source"]):
            checkpoint()
            if not isinstance(row, dict):
                raise DataError("원본 레코드가 JSON 객체가 아닙니다.")
            originals.append((row, source_meta, f"{relative.as_posix()} · doc:{doc}{pointer}"))
            try:
                key = canonical_key(expression(profile["source"]["key"], context(row, source_meta)))
                source_counts[key] += 1
            except ERRORS:
                key = None
            source_keys.append(key)
    if not originals:
        raise DataError("규칙에서 지정한 위치에 원본 레코드가 없습니다.")
    found, counts, total, invalid = {}, Counter(), 0, 0
    result_records = []
    for path in paths:
        spec = profile["result"]
        # Results default to auto, as in engine.run (including CSV JSON cells).
        document = read(path, {**spec, "format": spec.get("format", "auto")}, "보완 결과")
        for number, (doc, pointer, row) in enumerate(records(document, spec), 1):
            checkpoint(); total += 1
            meta = metadata(path, result=True, number=number)
            location = f"{path.name} · {number:,}번 · doc:{doc}{pointer}"
            try:
                if not isinstance(row, dict):
                    raise DataError("결과 레코드가 JSON 객체가 아닙니다.")
                key = canonical_key(expression(spec["key"], context(row, meta)))
            except ERRORS:
                invalid += 1
                result_records.append((None, row, meta, location))
                continue
            result_records.append((key, row, meta, location))
            counts[key] += 1
            if counts[key] == 1 and key in source_counts:
                found[key] = (row, meta, f"{path.name} · {number:,}번 · doc:{doc}{pointer}")
            else:
                found.pop(key, None)
            if progress and number % 2000 == 0:
                progress(f"보완 결과 KEY 확인 · {total:,}건")
    checkpoint()
    if progress: progress("읽은 데이터의 변경 여부를 마지막으로 확인하고 있습니다…")
    if fingerprint(source, paths, root, profile["source"].get("glob", "**/*.json"), checkpoint) != stamp:
        raise DataError("읽는 동안 파일이 바뀌었습니다. 다시 불러오세요.")
    data = PreviewData(profile, originals, source_keys, source_counts, found, counts, total, invalid, stamp,
                       result_records)
    if progress: progress("전체 레코드의 검색 목록을 준비하고 있습니다…")
    data.catalog = build_catalog(data, checkpoint)
    return data


def build_catalog(data, checkpoint=lambda: None):
    """One row per unique pair; ambiguous/unmatched records stay individually visible."""
    catalog = []
    def add(source_index, result_record, key, state):
        original = data.originals[source_index] if source_index is not None else None
        incoming = result_record
        source_location = original[2] if original else ""
        result_location = incoming[3] if incoming else ""
        key_text = str(json.loads(key)[1]) if key is not None else "KEY 해석 불가"
        def searchable(value):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":")).casefold()
        catalog.append({
            "source_index": source_index, "incoming": incoming, "key": key_text, "state": state,
            "source_location": source_location, "result_location": result_location,
            "search": (key_text.casefold(), (source_location + " " + result_location).casefold(),
                       searchable(original[0]) if original else "",
                       searchable(incoming[1]) if incoming else "", state.casefold()),
        })
    for i, key in enumerate(data.source_keys):
        checkpoint()
        unique = key is not None and data.source_counts[key] == 1 and data.result_counts[key] == 1
        incoming = (key, *data.results[key]) if unique else None
        state = ("연결됨" if unique else "KEY 해석 불가" if key is None else
                 "KEY 중복" if data.source_counts[key] > 1 or data.result_counts[key] > 1 else "원본만 있음")
        add(i, incoming, key, state)
    for record in data.result_records:
        checkpoint()
        key = record[0]
        if key is not None and data.source_counts[key] == 1 and data.result_counts[key] == 1:
            continue
        state = ("KEY 해석 불가" if key is None else
                 "KEY 중복" if data.source_counts[key] > 1 or data.result_counts[key] > 1 else "보완만 있음")
        add(None, record, key, state)
    return catalog


SEARCH_FIELDS = {"전체": None, "KEY": 0, "파일명 / 위치": 1, "원본 JSON": 2, "보완 JSON": 3}


def search_catalog(data, query="", scope="전체", state="전체 상태", checkpoint=lambda: None):
    terms = query.casefold().split()
    field_index = SEARCH_FIELDS[scope]
    found = []
    for i, entry in enumerate(data.catalog):
        if i % 256 == 0: checkpoint()
        if state != "전체 상태" and entry["state"] != state:
            continue
        fields = entry["search"] if field_index is None else (entry["search"][field_index],)
        if all(any(term in text for text in fields) for term in terms):
            found.append(i)
    return found


def catalog_preview(data, index):
    entry = data.catalog[index]
    if entry["state"] == "연결됨":
        return match_preview(data, entry["source_index"])
    original = data.originals[entry["source_index"]][0] if entry["source_index"] is not None else None
    incoming = entry["incoming"][1] if entry["incoming"] is not None else None
    return {"key": entry["key"], "source": original, "result": incoming, "candidate": None,
            "output": original, "source_location": entry["source_location"] or "원본 없음",
            "result_location": entry["result_location"] or "보완 없음", "status": "unpaired",
            "message": entry["state"] + " · 임의로 연결하지 않습니다. 전체 검증에서 처리 사유를 확인하세요.",
            "rows": [], "total_connections": 0, "source_total": len(data.originals),
            "result_total": data.result_total, "invalid_result_keys": data.invalid_result_keys}


def rebuilt_comparison(info, number, cache):
    """Compare actual originals for display only; rebuilding still starts with {}."""
    cache_key = id(info)
    if cache_key not in cache:
        indexes, counts, result_keys = {}, Counter(), []
        for side in ('source', 'result'):
            items = info['original'] if side == 'source' else info['incoming']
            for index, item in enumerate(items):
                ctx = {**info['context'], side: item}
                if side == 'source': ctx['original'] = item
                try: key = canonical_key(expression(info['rule'][side + '_key'], ctx))
                except ERRORS: key = None
                if side == 'source':
                    if key is not None: indexes.setdefault(key, []).append(index)
                else:
                    result_keys.append(key)
                    if key is not None: counts[key] += 1
        cache[cache_key] = info, indexes, counts, result_keys
    _, indexes, counts, result_keys = cache[cache_key]
    key = result_keys[number]
    candidates = indexes.get(key, [])
    if key is not None and len(candidates) == 1 and counts[key] == 1:
        old_index, mode = candidates[0], 'id'
        note = '같은 ID의 원본 항목을 비교합니다. 목록 재구성은 보완 목록을 기준으로 진행합니다.'
    elif number < len(info['original']):
        old_index, mode = number, 'position'
        note = 'ID로 한 항목을 확정할 수 없어 같은 위치의 원본을 참고로 표시합니다. 동일 항목으로 연결했다는 뜻은 아닙니다.'
    else:
        return MISSING, '', 'missing', '대응 ID가 없거나 중복이며, 이 위치에도 원본 항목이 없습니다. 보완값으로 새 항목을 구성합니다.'
    return info['original'][old_index], info['prefix'] + '/' + str(old_index), mode, note


def match_preview(data, index=0):
    if not 0 <= index < len(data.originals):
        raise DataError(f"원본 항목 번호는 1~{len(data.originals):,} 사이로 입력하세요.")
    original, source_meta, source_location = data.originals[index]
    key = data.source_keys[index]
    if key is None:
        raise DataError("선택한 원본에서 규칙의 KEY를 읽을 수 없습니다. 규칙 JSON의 source.key를 확인하세요.")
    if data.source_counts[key] != 1:
        raise DataError("선택한 원본 파일 또는 폴더 안에 같은 KEY가 여러 건입니다. 임의로 연결하지 않습니다.")
    count = data.result_counts[key]
    if count == 0:
        raise DataError("같은 KEY의 보완 결과가 없습니다." +
                        (f" 결과 {data.invalid_result_keys:,}건은 KEY를 읽을 수 없었습니다." if data.invalid_result_keys else ""))
    if count != 1:
        raise DataError(f"같은 KEY의 보완 결과가 {count:,}건입니다. 임의로 연결하지 않습니다.")
    incoming, result_meta, result_location = data.results[key]
    meta = {**source_meta, "result_file": result_meta["result_file"], "result_row": result_meta["result_row"]}
    rows = [{
        "kind": "key", "source_path": data.profile["source"]["key"], "result_expr": data.profile["result"]["key"],
        "source": display(expression(data.profile["source"]["key"], context(original, source_meta))),
        "result": display(expression(data.profile["result"]["key"], context(incoming, result_meta))),
        "evaluated": "", "note": "규칙의 KEY가 같은 레코드 한 쌍입니다.",
    }]
    visited = 0
    rebuilt_cache = {}
    def observe(rule, ctx, prefix, active):
        nonlocal visited
        visited += 1
        if len(rows) >= ROW_LIMIT:
            return
        values = observed(rule["from"], ctx) if active else {}
        before = get_pointer(ctx["original"], rule["to"])
        source_path = prefix + rule['to']
        extras = {}
        note = "원본에 반영하기 전 읽기 전용 예시입니다."
        if prefix:
            note = "해당 목록 항목의 값입니다. 항목 순서로 임의 연결하지 않습니다."
        if ctx.get("_preview_rebuilt"):
            item, old_prefix, mode, note = rebuilt_comparison(ctx['_preview_rebuild_info'], ctx['meta']['item_number'] - 1, rebuilt_cache)
            before = get_pointer(item, rule['to']) if item is not MISSING else MISSING
            source_path = old_prefix + rule['to'] if old_prefix else '대응하는 원본 항목 없음'
            extras = {'source_label': {'id': '원본 값 · ID 기준 비교', 'position': '원본 같은 위치 · 참고', 'missing': '원본 항목'}[mode],
                      'comparison': mode, 'target_path': prefix + rule['to']}
            if mode == 'missing': extras['source'] = '대응하는 원본 항목 없음'
        if not active:
            note = "이 규칙의 적용 조건에 해당하지 않아 반영하지 않습니다."
        rows.append({
            "kind": rule["kind"], "source_path": source_path, "result_expr": copy.deepcopy(rule["from"]),
            "source": display(before), "result": display(values["raw"]) if values.get("raw_present") else values.get("raw_error", "조건 제외"),
            "evaluated": display(values["evaluated"]) if values.get("evaluated_present") else values.get("evaluated_error", ""),
            "note": note, "nested": bool(prefix), **extras,
        })
    advisories = []
    try:
        candidate, changes, notes, status = apply_record(original, incoming, data.profile, meta,
                                                        advisories, observer=observe)
        message = {"updated": f"반영 시 변경 {len(changes):,}곳", "unchanged": "반영할 값이 원본과 같습니다.",
                   "excluded": "반영 조건에 해당하지 않아 원본을 유지합니다."}[status]
        if advisories:
            message += f" · 별도 검토 {len(advisories):,}건"
    except ERRORS as exc:
        candidate, status, message = None, "review_required", f"이 한 쌍은 검토가 필요합니다: {exc}"
    return {"key": json.loads(key)[1], "source": original, "result": incoming, "candidate": candidate,
            "output": original if candidate is None else candidate,
            "source_location": source_location, "result_location": result_location,
            "status": status, "message": message, "rows": rows, "total_connections": visited + 1,
            "source_total": len(data.originals), "result_total": data.result_total,
            "invalid_result_keys": data.invalid_result_keys}
