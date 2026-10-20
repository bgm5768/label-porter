"""Deterministic, same-schema rule creation using the existing porter engine."""
from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .config import validate_config
from .formats import MISSING, DataError, canonical_key, equivalent, escape_token, get_pointer, read_document, records
from .pair_preview import PreviewData, _file_stamp, build_catalog, metadata, source_paths


NAMES = {
    "/sound_id": "음성 데이터 ID", "/id": "데이터 ID",
    "/Segmentation/eojeols": "어절 목록 · ID / 텍스트 / 시간",
    "/RecordingMetadata/orthographic": "문장 전체 전사",
    "/PronunciationErrorTags": "발음 오류 태그",
}
RECOMMENDED = {"/Segmentation/eojeols", "/RecordingMetadata/orthographic"}
COMMON_LABELS = {"label", "text", "transcript", "transcription", "category", "class", "answer"}


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


def key_values(rows, path):
    values, counts = [], Counter()
    for row, *_ in rows:
        try:
            value = canonical_key(get_pointer(row, path))
        except (ValueError, TypeError):
            value = None
        values.append(value)
        if value is not None:
            counts[value] += 1
    return values, counts


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
        raise DataError("간편 작성은 JSON / JSONL / NDJSON을 지원합니다. CSV·플랫폼 전용 구조는 AI 규칙을 불러오세요.")
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
    source_fields = set().union(*(leaves(row) for row, *_ in originals[:100]))
    result_fields = set().union(*(leaves(row) for row, *_ in incoming[:100]))
    common = sorted(source_fields & result_fields, key=lambda p: (not key_name(p), p.count("/"), p))
    candidates = []
    for path in common[:50]:
        checkpoint()
        _, left = key_values(originals, path)
        _, right = key_values(incoming, path)
        matched = sum(left[k] == right[k] == 1 for k in left.keys() & right.keys())
        if not matched:
            continue
        candidates.append({"path": path, "matched": matched,
                           "source_missing": len(originals) - sum(left.values()),
                           "result_missing": len(incoming) - sum(right.values()),
                           "source_duplicates": sum(v for v in left.values() if v > 1),
                           "result_duplicates": sum(v for v in right.values() if v > 1),
                           "recommended": key_name(path)})
    candidates.sort(key=lambda c: (-int(c["recommended"]), -c["matched"], c["path"].count("/"), c["path"]))
    if not candidates:
        raise DataError("같은 경로에서 유일하게 연결되는 값을 찾지 못했습니다. 플랫폼 전용 구조나 다른 필드명은 AI 규칙 작성 가이드를 사용하세요.")
    if _file_stamp([*source_paths(source, pattern, checkpoint), *results], source, checkpoint) != stamp:
        raise DataError("분석 중 입력 파일이 바뀌었습니다. 다시 분석하세요.")
    return Inputs(str(source), [str(p) for p in results], source_spec, result_spec, originals, incoming, stamp, candidates)


def select_key(inputs, path, cancel=lambda: False):
    if path not in {c["path"] for c in inputs.candidates}:
        raise DataError("연결 기준을 선택하세요.")
    def checkpoint():
        if cancel(): raise DataError("분석을 중단했습니다.")
    left, left_counts = key_values(inputs.originals, path)
    right, right_counts = key_values(inputs.incoming, path)
    result_records = [(key, *row) for key, row in zip(right, inputs.incoming)]
    found = {key: row for key, row in zip(right, inputs.incoming)
             if key is not None and right_counts[key] == 1 and key in left_counts}
    profile = {"schema_version": 1, "name": "간편 규칙", "source": {**inputs.source_spec, "key": path},
               "result": {**inputs.result_spec, "key": path}, "rules": [{"kind": "field", "from": path, "to": path}]}
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
    if not options:
        raise DataError("연결 기준 외에 같은 구조로 반영할 항목이 없습니다. AI 규칙을 사용하세요.")
    return data, options


def make_profile(data, options, selected, name="데이터로 만든 규칙", check_times=True):
    selected = set(selected)
    available = {item["path"]: item for item in options}
    if not selected or not selected <= available.keys():
        raise DataError("반영할 항목을 한 개 이상 선택하세요.")
    profile = copy.deepcopy(data.profile)
    profile["name"] = name.strip() or "데이터로 만든 규칙"
    profile["description"] = "같은 경로의 보완값을 그대로 복사합니다. 선택하지 않은 필드는 원본을 유지합니다. 목록은 추가·삭제와 내부 필드를 포함해 전체 반영하며 ID·시간값·전사를 임의로 재생성하지 않습니다."
    types = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array", "dict": "object", "NoneType": "null"}
    profile["rules"] = []
    for path in sorted(selected):
        kinds = available[path]["types"]
        rule = {"kind": "field", "from": path, "to": path,
                "type": "number" if kinds <= {"int", "float"} else types[next(iter(kinds))]}
        if kinds & {"dict", "list"}:
            rule["allow_container_replace"] = True
        profile["rules"].append(rule)
    if check_times and "/Segmentation/eojeols" in selected:
        profile["assertions"] = [{"kind": "intervals", "path": "/Segmentation/eojeols",
                                  "start": "/start", "end": "/end", "ordered": False}]
    return validate_config(profile)
