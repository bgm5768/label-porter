"""Data-driven rule creation using the existing porter engine."""
from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .config import validate_config
from .formats import MISSING, DataError, canonical_key, equivalent, escape_token, get_pointer, read_document, records
from .pair_preview import PreviewData, _file_stamp, build_catalog, metadata, source_paths
from .mapping import collect_fields, _length_checks


NAMES = {
    "/sound_id": "음성 데이터 ID", "/id": "데이터 ID",
    "/Segmentation/eojeols": "어절 목록 · ID / 텍스트 / 시간",
    "/RecordingMetadata/orthographic": "문장 전체 전사",
    "/PronunciationErrorTags": "발음 오류 태그",
    "/instruction": "질의", "/input": "입력 문맥", "/output": "답변",
}
RECOMMENDED = {"/Segmentation/eojeols", "/RecordingMetadata/orthographic"}
COMMON_LABELS = {"label", "text", "transcript", "transcription", "category", "class", "answer",
                 "instruction", "output"}


def friendly(path):
    return NAMES.get(path, path.lstrip("/").replace("/", " › "))


def key_name(path):
    name = path.rsplit("/", 1)[-1].lower()
    return name in {"id", "key", "uuid", "filename", "file_name"} or name.endswith(("_id", "id"))


def leaves(value, prefix="", depth=0):
    if depth > 12:
        return {}
    if isinstance(value, dict) and value:
        result = {}
        for name, item in value.items():
            result.update(leaves(item, prefix + "/" + escape_token(name), depth + 1))
        return result
    return {prefix: value} if prefix else {}


def container(doc):
    """Root records win over annotation arrays. Only known wrapper names are inferred."""
    if isinstance(doc, list):
        return ""
    if not isinstance(doc, dict):
        raise DataError("레코드는 JSON 객체 또는 객체 목록이어야 합니다.")
    if any(key_name("/" + name) and isinstance(value, (str, int, float)) and not isinstance(value, bool)
           for name, value in doc.items()):
        return ""
    found = []
    def visit(value, prefix="", depth=0):
        if depth > 3 or not isinstance(value, dict):
            return
        for name, child in value.items():
            path = prefix + "/" + escape_token(name)
            if name.lower() in ("data", "rows", "records", "items", "results") and isinstance(child, list):
                if not child or all(isinstance(item, dict) for item in child[:20]):
                    found.append(path)
            elif isinstance(child, dict):
                visit(child, path, depth + 1)
    visit(doc)
    if len(found) > 1:
        raise DataError("레코드로 볼 수 있는 목록이 여러 개입니다. AI 규칙 작성 가이드로 목록 위치를 지정한 규칙을 만드세요.")
    return found[0] if found else ""


@dataclass
class Inputs:
    source: str
    result_files: list
    source_spec: dict
    result_spec: dict
    originals: list
    incoming: list
    fingerprint: tuple
    candidates: list
    source_fields: dict = field(default_factory=dict)
    result_fields: dict = field(default_factory=dict)


def key_values(rows, path, info=None, checkpoint=lambda: None):
    values, counts = [], Counter()
    for row, *_ in rows:
        checkpoint()
        try:
            if info and any(not isinstance(get_pointer(row, p), list) or len(get_pointer(row, p)) != n
                            for p, n in info.guards.items()):
                raise DataError("KEY 배열 길이가 다릅니다.")
            value = canonical_key(get_pointer(row, path))
        except (ValueError, TypeError):
            value = None
        values.append(value)
        if value is not None:
            counts[value] += 1
    return values, counts


def key_stats(inputs, source_path, result_path, checkpoint=lambda: None):
    _, left = key_values(inputs.originals, source_path, inputs.source_fields.get(source_path), checkpoint)
    _, right = key_values(inputs.incoming, result_path, inputs.result_fields.get(result_path), checkpoint)
    return stats(left, right, len(inputs.originals), len(inputs.incoming), source_path, result_path)


def stats(left, right, source_total, result_total, source_path, result_path):
    return {"path": source_path, "result_path": result_path,
            "matched": sum(left[k] == right[k] == 1 for k in left.keys() & right.keys()),
            "source_missing": source_total - sum(left.values()), "result_missing": result_total - sum(right.values()),
            "source_duplicates": sum(v for v in left.values() if v > 1),
            "result_duplicates": sum(v for v in right.values() if v > 1),
            "recommended": key_name(source_path) and key_name(result_path)}


def key_fields(fields):
    return {p: f for p, f in fields.items() if f.kinds - {"null"} and f.kinds <= {"string", "integer", "number", "null"}}


def normalized_name(path):
    return path.rsplit("/", 1)[-1].removeprefix("importData_").lower()


def recommend_keys(inputs, checkpoint):
    # Full counts are computed once per field, only after a name/sample suggests a pair.
    def short_fields(fields):
        return {p: f for p, f in key_fields(fields).items()
                if key_name(p) or any(len(str(v)) <= 256 for v in f.examples)}
    left_fields, right_fields = short_fields(inputs.source_fields), short_fields(inputs.result_fields)
    pairs = []
    for a, af in left_fields.items():
        for b, bf in right_fields.items():
            checkpoint()
            named = normalized_name(a) == normalized_name(b)
            ids = key_name(a) and key_name(b)
            examples = {canonical_key(v) for v in af.examples if isinstance(v, (str, int, float)) and v != ""}
            overlap = any(canonical_key(v) in examples for v in bf.examples
                          if isinstance(v, (str, int, float)) and v != "")
            if named or ids or overlap:
                pairs.append((not ids, not named, a, b))
    cache_left, cache_right, candidates = {}, {}, []
    for _, _, a, b in sorted(pairs)[:200]:
        checkpoint()
        if a not in cache_left:
            cache_left[a] = key_values(inputs.originals, a, left_fields[a], checkpoint)[1]
        if b not in cache_right:
            cache_right[b] = key_values(inputs.incoming, b, right_fields[b], checkpoint)[1]
        candidate = stats(cache_left[a], cache_right[b], len(inputs.originals), len(inputs.incoming), a, b)
        if candidate["matched"]: candidates.append(candidate)
    candidates.sort(key=lambda c: (-int(c["recommended"]), -c["matched"],
                                  normalized_name(c["path"]) != normalized_name(c["result_path"]), c["path"]))
    return candidates


def analyze_inputs(source, result_files, progress=lambda message: None, cancel=lambda: False):
    source = Path(source).expanduser().resolve()
    results = [Path(p).expanduser().resolve() for p in result_files]
    def checkpoint():
        if cancel():
            raise DataError("데이터 분석을 중단했습니다.")
    if not source.is_dir() or not results:
        raise DataError("원본 폴더와 보완 데이터 파일을 선택하세요.")
    if len(set(results)) != len(results):
        raise DataError("같은 보완 파일을 두 번 선택할 수 없습니다.")
    if any(p.suffix.lower() not in (".json", ".jsonl", ".ndjson") for p in results):
        raise DataError("간편 작성은 JSON / JSONL / NDJSON을 지원합니다. CSV는 AI 규칙을 불러오세요.")
    extensions = {p.suffix.lower() for p in source_paths(source, "**/*", checkpoint)
                  if p.suffix.lower() in (".json", ".jsonl", ".ndjson")}
    if len(extensions) != 1:
        raise DataError("원본 폴더에는 한 종류의 JSON 형식이 필요합니다. JSON, JSONL, NDJSON 중 한 형식의 폴더를 선택하세요.")
    pattern = "**/*" + next(iter(extensions))
    files = source_paths(source, pattern, checkpoint)
    if set(files).intersection(results):
        raise DataError("원본과 보완 데이터는 서로 다른 파일이어야 합니다.")
    stamp = _file_stamp([*files, *results], source, checkpoint)

    def load(paths, title, is_result):
        output, spec = [], None
        for number, path in enumerate(paths, 1):
            checkpoint()
            if number == 1 or number % 250 == 0:
                progress(f"{title} 읽기 · {number:,}/{len(paths):,}개 파일")
            last_percent = -1
            def reading(done, total, count):
                nonlocal last_percent
                percent = int(done * 100 / total) if total else 100
                if total >= 8 * 1024 * 1024 and percent != last_percent:
                    last_percent = percent
                    progress(f"{title} 읽기 · {path.name} · {percent}% · {count:,}개 항목 해석")
            document = read_document(path, progress=reading, checkpoint=checkpoint)
            pointer = container(document.docs[0])
            if spec is None:
                spec = {"records_path": pointer, "record_mode": "auto"}
            if pointer != spec["records_path"]:
                raise DataError(f"{title} 파일들의 레코드 구조가 다릅니다: {path.name}. 구조별로 나누거나 AI 규칙을 사용하세요.")
            for row_number, (doc_index, address, row) in enumerate(records(document, spec), 1):
                checkpoint()
                if not isinstance(row, dict):
                    raise DataError(f"{path.name}: 레코드에 객체가 아닌 값이 있습니다.")
                meta = metadata(path, result=is_result, number=row_number if is_result else "",
                                root=None if is_result else source)
                location = f"{path.name if is_result else path.relative_to(source).as_posix()} · doc:{doc_index}{address}"
                output.append((row, meta, location))
            del document
        if not output:
            raise DataError(f"{title}에 분석할 레코드가 없습니다.")
        return output, spec

    originals, source_spec = load(files, "원본", False)
    incoming, result_spec = load(results, "보완", True)
    source_spec["glob"] = pattern
    result_spec["format"] = "json"
    progress("같은 데이터를 찾을 기준을 비교하고 있습니다…")
    inputs = Inputs(str(source), [str(p) for p in results], source_spec, result_spec, originals, incoming, stamp, [],
                    collect_fields([row for row, *_ in originals]), collect_fields([row for row, *_ in incoming]))
    inputs.candidates = recommend_keys(inputs, checkpoint)
    if _file_stamp([*source_paths(source, pattern, checkpoint), *results], source, checkpoint) != stamp:
        raise DataError("분석 중 입력 파일이 바뀌었습니다. 다시 분석하세요.")
    return inputs


def select_key(inputs, path, cancel=lambda: False, result_path=None):
    result_path = result_path or path
    if path not in key_fields(inputs.source_fields) or result_path not in key_fields(inputs.result_fields):
        raise DataError("원본과 보완의 연결 기준을 각각 선택하세요.")
    def checkpoint():
        if cancel(): raise DataError("분석을 중단했습니다.")
    left, left_counts = key_values(inputs.originals, path, inputs.source_fields[path], checkpoint)
    right, right_counts = key_values(inputs.incoming, result_path, inputs.result_fields[result_path], checkpoint)
    result_records = [(key, *row) for key, row in zip(right, inputs.incoming)]
    found = {key: row for key, row in zip(right, inputs.incoming)
             if key is not None and right_counts[key] == 1 and key in left_counts}
    profile = {"schema_version": 1, "name": "간편 규칙", "source": {**inputs.source_spec, "key": path},
               "result": {**inputs.result_spec, "key": result_path}, "rules": [{"kind": "field", "from": result_path, "to": path}]}
    checks = _length_checks(inputs.source_fields[path], "source") + _length_checks(inputs.result_fields[result_path], "result")
    if checks: profile["checks"] = checks
    data = PreviewData(profile, inputs.originals, left, left_counts, found, right_counts,
                       len(inputs.incoming), sum(k is None for k in right), inputs.fingerprint, result_records)
    data.catalog = build_catalog(data, checkpoint)
    samples = [(row[0], found[key][0]) for key, row in zip(left, inputs.originals)
               if key is not None and left_counts[key] == right_counts[key] == 1][:200]
    fields = {}
    for original, incoming in samples:
        checkpoint()
        a, b = leaves(original), leaves(incoming)
        for field in a.keys() & b.keys():
            if field not in inputs.source_fields or field not in inputs.result_fields:
                continue
            if field == path or field.startswith(path + "/") or path.startswith(field + "/"):
                continue
            item = fields.setdefault(field, {"path": field, "label": friendly(field), "count": 0,
                                            "changed": 0, "types": set(), "before": a[field], "after": b[field]})
            item["count"] += 1
            item["changed"] += not equivalent(a[field], b[field])
            item["types"].update(type(v).__name__ for v in (a[field], b[field]))
    options = []
    for field, item in fields.items():
        if len(item["types"]) > 1 and not item["types"] <= {"int", "float"}:
            continue
        item["selected"] = field in RECOMMENDED or (field.rsplit("/", 1)[-1].lower() in COMMON_LABELS and item["changed"] > 0)
        item["sample_count"] = len(samples)
        options.append(item)
    options.sort(key=lambda item: (not item["selected"], item["path"]))
    # Platform correction controls have a different path from imported original values.
    targets = set(inputs.source_fields) - {item["path"] for item in options}
    for target in sorted(targets):
        checkpoint()
        if related(target, path): continue
        candidates = [f"/{target.lstrip('/')}{suffix}/data/0/value"
                      for suffix in ("_error_modify", "_modify", "_corrected")]
        candidates += ["/importData_" + target.lstrip("/")]
        for incoming_path in candidates:
            if incoming_path not in inputs.result_fields: continue
            try:
                item = connection_option(inputs, data, target, incoming_path, skip_empty=True)
            except DataError:
                continue
            item["selected"] = not incoming_path.startswith("/importData_")
            options.append(item)
            break
    options.sort(key=lambda item: (not item["selected"], item["path"]))
    return data, options


def related(a, b):
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def connection_option(inputs, data, target, incoming_path, skip_empty=False):
    """One explicit destination/source selection, compiled without guessed transforms."""
    if target not in inputs.source_fields or incoming_path not in inputs.result_fields:
        raise DataError("원본 대상과 가져올 보완 필드를 각각 선택하세요.")
    if related(target, data.profile["source"]["key"]):
        raise DataError("같은 데이터를 찾는 기준 필드는 수정할 수 없습니다.")
    a, b = inputs.source_fields[target], inputs.result_fields[incoming_path]
    kinds = (a.kinds | b.kinds) - ({"null"} if skip_empty else set())
    if len(kinds) > 1 and not kinds <= {"integer", "number"}:
        raise DataError(f"자료형이 다릅니다: 원본 {a.kind} / 보완 {b.kind}. 변환이 필요하면 AI 규칙을 사용하세요.")
    python_types = {"string": "str", "integer": "int", "number": "float", "boolean": "bool",
                    "array": "list", "object": "dict", "null": "NoneType"}
    pairs = [(row[0], data.results[key][0]) for key, row in zip(data.source_keys, data.originals)
             if key is not None and data.source_counts[key] == data.result_counts[key] == 1][:200]
    count = changed = empty = 0
    before = after = MISSING
    for original, incoming in pairs:
        av, bv = get_pointer(original, target), get_pointer(incoming, incoming_path)
        if av is MISSING or bv is MISSING: continue
        count += 1
        blank = bv is None or bv == [] or bv == {} or isinstance(bv, str) and not bv.strip()
        empty += bool(blank)
        changed += not equivalent(av, bv) and not (skip_empty and blank)
        if before is MISSING or (skip_empty and after in ("", None) and not blank):
            before, after = av, bv
    return {"path": target, "from": incoming_path, "label": friendly(target), "count": count, "changed": changed,
            "types": {python_types[k] for k in kinds or {"null"}}, "before": before, "after": after,
            "sample_count": len(pairs), "selected": False, "skip_empty": skip_empty, "empty": empty,
            "checks": _length_checks(a, "source") + _length_checks(b, "result")}


def make_profile(data, options, selected, name="데이터로 만든 규칙", check_times=True):
    selected = set(selected)
    available = {item["path"]: item for item in options}
    if not selected or not selected <= available.keys():
        raise DataError("반영할 항목을 한 개 이상 선택하세요.")
    profile = copy.deepcopy(data.profile)
    profile["name"] = name.strip() or "데이터로 만든 규칙"
    profile["description"] = "선택한 보완 필드의 값을 원본 대상에 복사합니다. 선택하지 않은 필드는 원본 유지. 목록은 전체 반영하며 ID·시간·전사를 재생성하지 않습니다. 빈 값의 처리 정책은 각 규칙을 따릅니다."
    types = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array", "dict": "object", "NoneType": "null"}
    profile["rules"] = []
    for path in sorted(selected):
        if any(other != path and related(path, other) for other in selected):
            raise DataError("목록 전체와 그 안의 개별 값을 동시에 선택할 수 없습니다. 한 가지 반영 방식을 선택하세요.")
        item = available[path]
        kinds = item["types"]
        incoming_path = item.get("from", path)
        rule = {"kind": "field", "from": incoming_path, "to": path,
                "type": "number" if kinds <= {"int", "float"} else types[next(iter(kinds))]}
        if item.get("checks"): rule["checks"] = copy.deepcopy(item["checks"])
        if item.get("skip_empty"):
            rule["when"] = [{"left": incoming_path, "op": "exists"}, {"left": incoming_path, "op": "nonempty"}]
        if kinds & {"dict", "list"}:
            rule["allow_container_replace"] = True
        profile["rules"].append(rule)
    if check_times and "/Segmentation/eojeols" in selected:
        profile["assertions"] = [{"kind": "intervals", "path": "/Segmentation/eojeols",
                                  "start": "/start", "end": "/end", "ordered": False}]
    return validate_config(profile)
