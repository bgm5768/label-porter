"""Sample inspection and compilation for the visual field wiring editor."""
from __future__ import annotations

import copy
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .config import validate_config
from .formats import DataError, canonical_key, escape_token, get_pointer, read_document, records
from .rules import apply_record, expression

SAMPLE_LIMIT = 120
FIELD_LIMIT = 400
TRANSFORMS = {
    "그대로": [], "문자열로": [{"op": "string"}], "정수로": [{"op": "integer"}],
    "숫자로": [{"op": "number"}], "파일명만": [{"op": "basename"}],
    "끝 숫자 ID": [{"op": "regex", "pattern": r"(\d+)$"}],
    "밀리초 → 초": [{"op": "number"}, {"op": "scale", "value": 0.001}],
    "초 → 밀리초": [{"op": "number"}, {"op": "scale", "value": 1000}],
    "소수 1자리 반올림": [{"op": "number"}, {"op": "round", "digits": 1}],
}


def sample_rows(rows, limit=SAMPLE_LIMIT):
    if len(rows) <= limit:
        return rows
    return [rows[int(i * (len(rows) - 1) / (limit - 1))] for i in range(limit)]


def value_kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def compact(value, limit=85):
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = text.replace("\n", " ↵ ").replace("\r", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


@dataclass
class Field:
    path: str
    kinds: set = field(default_factory=set)
    examples: list = field(default_factory=list)
    guards: dict = field(default_factory=dict)
    count: int = 0
    object_array: bool = False
    meta: str | None = None
    caption: str = ""
    original_expr: object = None

    @property
    def label(self):
        return self.caption or {"file_name": "파일 이름", "file_stem": "확장자 없는 이름", "relative_path": "하위 폴더 + 파일 이름"}.get(self.meta, self.path)

    @property
    def kind(self):
        kinds = self.kinds - {"null"}
        if kinds <= {"integer", "number"} and kinds:
            return "number" if "number" in kinds else "integer"
        return next(iter(kinds)) if len(kinds) == 1 else ("null" if not kinds else "mixed")

    def expr(self, transform="그대로", scope=None):
        if transform == "규칙 설정 유지" and self.original_expr is not None:
            return copy.deepcopy(self.original_expr)
        value = {"meta": self.meta} if self.meta else {"path": self.path}
        if scope and not self.meta:
            value["scope"] = scope
        if transform not in TRANSFORMS:
            raise DataError("지원하지 않는 값 변환입니다.")
        if TRANSFORMS[transform]:
            value["transforms"] = copy.deepcopy(TRANSFORMS[transform])
        return value


def collect_fields(rows, meta=None):
    found = {}

    def visit(value, path, guards, seen, depth=0):
        if depth > 12:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, path + "/" + escape_token(key), guards, seen, depth + 1)
            return
        if not path or (path not in found and len(found) >= FIELD_LIMIT):
            return
        info = found.setdefault(path, Field(path))
        info.kinds.add(value_kind(value))
        info.guards.update(guards)
        if path not in seen:
            info.count += 1
            seen.add(path)
        if len(info.examples) < 4:
            display_value = value if not isinstance(value, list) else f"{len(value)}개 항목 · " + compact(value[:1], 60)
            if display_value not in info.examples:
                info.examples.append(display_value)
        if isinstance(value, list):
            if value and all(isinstance(item, dict) for item in value):
                info.object_array = True
            # Singleton wrappers and small coordinate vectors can be wired directly.
            # Generated length checks prevent silently treating repeated arrays as wrappers.
            if len(value) == 1 or (0 < len(value) <= 8 and all(not isinstance(item, (dict, list)) for item in value)):
                for index, child in enumerate(value):
                    visit(child, path + f"/{index}", {**guards, path: len(value)}, seen, depth + 1)

    for row in sample_rows(rows):
        visit(row, "", {}, set())
    if meta:
        for key in ("file_name", "file_stem", "relative_path"):
            examples = list(dict.fromkeys(row_meta.get(key, "") for row_meta in sample_rows(getattr(rows, "metadata", []) or [meta])))[:4]
            info = Field("@" + key, {"string"}, examples, count=len(rows), meta=key)
            found[info.path] = info
    return found


class SampleRows(list):
    """Rows keep file provenance out of the user's label JSON."""
    def __init__(self, values=(), metadata=()):
        super().__init__(values)
        self.metadata = list(metadata)


def row_metadata(rows, index, fallback=None):
    metadata = getattr(rows, "metadata", [])
    return metadata[index] if index < len(metadata) else (fallback or {})


@dataclass
class SampleFile:
    path: Path
    document: object
    containers: list[str]
    meta: dict

    def rows(self, pointer="", record_mode="auto"):
        values = [row for _, _, row in records(self.document, {"records_path": pointer, "record_mode": record_mode})]
        if not values or any(not isinstance(row, dict) for row in values):
            raise DataError("선택한 위치에 라벨 객체가 없습니다. ‘파일 안에서 한 건씩 들어 있는 위치’를 확인하세요.")
        return SampleRows(values, [{**self.meta, "result_file": str(self.path), "result_row": index}
                                   for index in range(1, len(values) + 1)])

    @property
    def file_count(self):
        return 1


@dataclass
class SampleCollection:
    files: list[SampleFile]
    selection: object
    root: Path

    @property
    def path(self):
        return self.files[0].path

    @property
    def file_count(self):
        return len(self.files)

    @property
    def meta(self):
        return self.files[0].meta

    @property
    def containers(self):
        return list(dict.fromkeys(pointer for file in self.files for pointer in file.containers))

    @property
    def source_glob(self):
        suffixes = {file.path.suffix.lower() for file in self.files}
        return "**/*" + next(iter(suffixes)) if len(suffixes) == 1 else "**/*json*"

    def rows(self, pointer="", record_mode="auto"):
        values, metadata = [], []
        for file in self.files:
            try:
                part = file.rows(pointer, record_mode)
            except (ValueError, TypeError) as exc:
                raise DataError(f"{file.path}: {exc}") from exc
            values.extend(part)
            metadata.extend(part.metadata)
        return SampleRows(values, metadata)


def load_samples(selection, source_root=None, progress=None, cancel=None, pattern=None):
    """Read every selected JSON file; never label a subset as the whole selection."""
    allowed = {".json", ".jsonl", ".ndjson"}
    selections = [selection] if isinstance(selection, (str, Path)) else list(selection)
    if not selections or any(not str(item).strip() for item in selections):
        raise DataError("JSON 파일 여러 개 또는 라벨 폴더를 선택하세요.")
    paths = []
    selected_folder = None
    for item in selections:
        path = Path(item).expanduser().resolve()
        if path.is_dir():
            if len(selections) != 1:
                raise DataError("폴더 하나 또는 여러 JSON 파일을 선택하세요.")
            selected_folder = path
            if progress:
                progress("폴더의 JSON 파일 목록을 찾는 중입니다…")
            for found in path.glob(pattern or "**/*"):
                if cancel and cancel():
                    raise DataError("파일 읽기를 중단했습니다.")
                if found.is_file() and found.suffix.lower() in allowed:
                    paths.append(found.resolve())
        else:
            if not path.is_file() or path.suffix.lower() not in allowed:
                raise DataError(f"JSON/JSONL 파일을 찾을 수 없습니다: {path}")
            paths.append(path)
    paths = sorted(set(paths), key=lambda path: str(path).casefold())
    if not paths:
        raise DataError("선택한 위치에 규칙의 대상 패턴과 일치하는 JSON 파일이 없습니다.")
    if selected_folder:
        root = selected_folder
    elif source_root and all(path.is_relative_to(Path(source_root).resolve()) for path in paths):
        root = Path(source_root).resolve()
    else:
        try:
            root = Path(os.path.commonpath([str(path.parent) for path in paths]))
        except ValueError as exc:
            raise DataError("여러 파일은 같은 드라이브에서 선택하세요.") from exc
    files = []
    for index, path in enumerate(paths, 1):
        if cancel and cancel():
            raise DataError("파일 읽기를 중단했습니다.")
        try:
            files.append(load_sample(path, root, progress=progress, cancel=cancel))
        except (OSError, ValueError, UnicodeError) as exc:
            raise DataError(f"파일 읽기 실패: {path}\n{exc}") from exc
        if progress and (index == 1 or index % 200 == 0 or index == len(paths)):
            progress(f"JSON 파일 {index:,} / {len(paths):,}개 읽음")
    return SampleCollection(files, copy.deepcopy(selection), root)


def load_sample(path, source_root=None, *, progress=None, cancel=None):
    path = Path(path).resolve()
    if path.suffix.lower() not in (".json", ".jsonl", ".ndjson"):
        raise DataError("선 연결에는 JSON/JSONL 샘플을 선택하세요. CSV 규칙은 AI 가이드와 고급 편집에서 만들 수 있습니다.")
    def checkpoint():
        if cancel and cancel():
            raise DataError("파일 읽기를 중단했습니다.")
    document = read_document(
        path, "utf-8-sig", "json",
        progress=(lambda done, total, count: progress(f"{path.name} 읽는 중 · {done / max(total, 1):.0%} · {count:,}건")) if progress else None,
        checkpoint=checkpoint if cancel else None)
    candidates = []

    def discover(value, pointer, depth=0):
        if depth > 4:
            return
        if isinstance(value, dict):
            if pointer == "":
                candidates.append(pointer)
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    discover(child, pointer + "/" + escape_token(key), depth + 1)
        elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            candidates.append(pointer)

    discover(document.docs[0], "")
    if not candidates:
        raise DataError("JSON에서 원본/결과 레코드로 사용할 객체나 객체 배열을 찾지 못했습니다.")
    relative = path.name
    if source_root and path.is_relative_to(Path(source_root).resolve()):
        relative = path.relative_to(Path(source_root).resolve()).as_posix()
    return SampleFile(path, document, list(dict.fromkeys(candidates)),
                      {"file_name": path.name, "file_stem": path.stem, "relative_path": relative})


@lru_cache(maxsize=2048)
def _path_words(path):
    return set(re.findall(r"[a-z0-9가-힣]+", path.lower())) - {"data", "value", "0", "importdata"}


def candidate_score(left: Field, right: Field):
    if (left.kind == "array") != (right.kind == "array"):
        return 0
    a, b = _path_words(left.path), _path_words(right.path)
    score = int(70 * len(a & b) / max(1, len(a)))
    if left.path.rsplit("/", 1)[-1] == right.path.rsplit("/", 1)[-1]:
        score = max(score, 75)
    if left.kind != "array":
        comparable = {str(v) for v in left.examples if v is not None and str(v)}
        if comparable & {str(v) for v in right.examples if v is not None and str(v)}:
            score = max(score, 90)
    if left.meta and right.kind == "string" and any(str(v).replace("\\", "/").split("/")[-1] in left.examples for v in right.examples):
        score = 95
    elif left.meta and right.kind == "string" and any(Path(str(v)).suffix.lower() == Path(str(left.examples[0])).suffix.lower() != "" for v in right.examples):
        score = max(score, 45)
    return score


@dataclass(eq=False)
class Wire:
    kind: str
    source: str
    result: str
    source_transform: str = "그대로"
    result_transform: str = "그대로"
    nonempty: bool = True
    condition_path: str = ""
    condition_value: object = None
    allow_missing: bool = False
    child: object = None
    raw_rule: dict | None = None
    baseline: dict = field(default_factory=dict)
    original_keys: tuple | None = None
    owner: object = None


@dataclass(eq=False)
class Scope:
    source_rows: list
    result_rows: list
    meta: dict | None = None
    title: str = "전체 데이터"
    wires: list = field(default_factory=list)
    imported: bool = False
    rule_config: dict | None = None

    def __post_init__(self):
        self.source_fields = collect_fields(self.source_rows, self.meta)
        self.result_fields = collect_fields(self.result_rows)
        self._scores = {}

    def score(self, left, right):
        key = (left.path, right.path)
        if key not in self._scores:
            self._scores[key] = candidate_score(left, right)
        return self._scores[key]

    def connect(self, source, result, mode, owner=None):
        left, right = self.source_fields[source], self.result_fields[result]
        if mode not in ("key", "value", "guard"):
            raise DataError("연결 종류를 선택하세요.")
        if mode != "key" and left.meta:
            raise DataError("파일 이름은 데이터를 찾는 ID 연결에만 사용합니다.")
        kind = mode
        child = None
        if left.kind == "array" or right.kind == "array":
            if mode != "value" or not left.object_array or not right.object_array:
                raise DataError("반복 객체 배열끼리 연결하거나, 배열 안의 고정 숫자 항목을 선택하세요.")
            kind = "array"
            def children(rows, path):
                for row in sample_rows(rows):
                    values = get_pointer(row, path, [])
                    if isinstance(values, list):
                        yield from (item for item in values if isinstance(item, dict))
            source_rows = list(children(self.source_rows, source))
            result_rows = list(children(self.result_rows, result))
            child = Scope(source_rows, result_rows, title=f"{source} ↔ {result}")
        elif left.kind in ("mixed", "null") or right.kind in ("mixed", "null"):
            raise DataError("샘플의 자료형이 섞여 있거나 값이 비어 있습니다. 다른 샘플이나 AI 규칙 편집으로 의미를 확인하세요.")
        if kind != "guard":
            for wire in self.wires:
                if wire.kind in ("value", "array") and kind in ("value", "array"):
                    if source == wire.source or source.startswith(wire.source + "/") or wire.source.startswith(source + "/"):
                        raise DataError("이미 연결된 수정 위치입니다. 기존 선을 삭제한 뒤 다시 연결하세요.")
        if kind == "key":
            self.wires[:] = [wire for wire in self.wires if wire.kind != "key"]
        if kind == "guard":
            self.wires[:] = [wire for wire in self.wires if not (wire.kind == "guard" and wire.source == source and wire.owner is owner)]
        wire = Wire(kind, source, result, child=child, owner=owner)
        self.wires.append(wire)
        return wire


def _length_checks(info, scope):
    checks = []
    for path, length in info.guards.items():
        checks.append({"left": {"scope": scope, "path": path, "transforms": [{"op": "length"}]},
                       "op": "eq", "value": length,
                       "message": f"고정 위치로 연결한 배열 길이가 샘플({length}개)과 다릅니다: {path}"})
    return checks


def compile_scope(scope, root=True):
    if scope.imported:
        return _compile_imported(scope, root)
    keys = [wire for wire in scope.wires if wire.kind == "key"]
    if len(keys) != 1:
        raise DataError(f"{scope.title}: 파란 ID 선을 먼저 연결하세요.")
    key = keys[0]
    source_key = scope.source_fields[key.source].expr(key.source_transform, None if root else "source")
    result_key = scope.result_fields[key.result].expr(key.result_transform)
    if scope.source_fields[key.source].kind in ("boolean", "null", "array") or scope.result_fields[key.result].kind in ("boolean", "null", "array"):
        raise DataError("ID 연결에는 문자열이나 숫자를 사용하세요.")
    checks = _length_checks(scope.source_fields[key.source], "source") + _length_checks(scope.result_fields[key.result], "result")
    rules = []
    guards = {wire.source: wire for wire in scope.wires if wire.kind == "guard"}
    for wire in scope.wires:
        if wire.kind not in ("value", "array"):
            continue
        if wire.source == key.source or key.source.startswith(wire.source + "/"):
            raise DataError("데이터를 찾는 ID 자체는 수정 연결에서 제외하세요.")
        left, right = scope.source_fields[wire.source], scope.result_fields[wire.result]
        rule_checks = _length_checks(left, "source") + _length_checks(right, "result")
        if wire.kind == "array":
            sk, rk, inner_rules, inner_checks = compile_scope(wire.child, False)
            if inner_checks:
                # Item identity guards implement equality checks before child edits.
                identities = [{"source": check["left"], "result": {"literal": check["value"]}} for check in inner_checks]
            else:
                identities = []
            rule = {"kind": "items", "from": wire.result, "to": wire.source, "source_key": sk, "result_key": rk,
                    "require_same_keys": not wire.allow_missing, "rules": inner_rules}
            if identities:
                rule["identity_guards"] = identities
        else:
            rule = {"kind": "field", "from": right.expr(wire.result_transform), "to": wire.source,
                    "type": left.kind, "nonempty": bool(wire.nonempty)}
            if wire.source in guards:
                guard = guards.pop(wire.source)
                guard_field = scope.result_fields[guard.result]
                rule_checks.extend(_length_checks(guard_field, "result"))
                rule["expected"] = guard_field.expr(guard.result_transform)
        if wire.condition_path:
            condition_field = scope.result_fields[wire.condition_path]
            checks.extend(_length_checks(condition_field, "result"))
            rule["when"] = [{"left": condition_field.expr(), "op": "eq", "value": wire.condition_value}]
        if rule_checks:
            rule["checks"] = rule_checks
        rules.append(rule)
    if guards:
        raise DataError("기존값 확인 선만 있는 필드가 있습니다. 해당 필드의 초록 수정값 선도 연결하세요.")
    if not rules:
        raise DataError(f"{scope.title}: 초록 수정값 선을 하나 이상 연결하세요.")
    checks = list({json.dumps(check, sort_keys=True, ensure_ascii=False): check for check in checks}.values())
    return source_key, result_key, rules, checks


def compile_rule(scope, source_sample, result_sample, source_pointer, result_pointer, name):
    sk, rk, rules, checks = compile_scope(scope)
    if scope.rule_config is not None:
        config = copy.deepcopy(scope.rule_config)
        config["name"] = name.strip() or config["name"]
        for side, pointer, key in (("source", source_pointer, sk), ("result", result_pointer, rk)):
            config[side]["key"] = key
            if pointer != config[side].get("records_path", ""):
                config[side]["records_path"] = pointer
        config["rules"] = rules
        if checks:
            config["checks"] = config.get("checks", []) + checks
        return validate_config(config)
    config = {"schema_version": 1, "name": name.strip() or "선 연결 프로젝트",
              "description": "샘플 비교와 필드 선 연결로 작성한 규칙입니다. 전체 데이터는 미리보기로 확인하세요.",
              "source": {"glob": getattr(source_sample, "source_glob", "**/*" + source_sample.path.suffix.lower()), "records_path": source_pointer, "key": sk},
              "result": {"format": "json", "records_path": result_pointer, "key": rk}, "rules": rules}
    if checks:
        config["checks"] = checks
    return validate_config(config)


def wire_state(wire):
    return {name: copy.deepcopy(getattr(wire, name)) for name in
            ("source", "result", "source_transform", "result_transform", "nonempty",
             "condition_path", "condition_value", "allow_missing")}


def _import_field(scope, expr, side, kind="string"):
    """Keep the full expression, including transforms and root scopes, on its port."""
    spec = {"path": expr} if isinstance(expr, str) else expr
    fields = scope.source_fields if side == "source" else scope.result_fields
    path = "@" + spec["meta"] if "meta" in spec else spec.get("path")
    expected_scope = "source" if side == "source" else "result"
    ordinary = spec.get("scope", expected_scope) == expected_scope
    if path is None or not ordinary:
        path = f"@expression:{len(fields)}"
        caption = compact(expr, 180)
    else:
        caption = ""
    if path not in fields:
        fields[path] = Field(path, {kind}, [], meta=spec.get("meta"), caption=caption)
    # A path can be used with different transformations by several wires.
    # Expression ownership therefore stays on each wire, not on shared fields.
    return path


def _children(rows, expr, side):
    found = []
    for row in sample_rows(rows):
        try:
            values = expression(expr, _context(row, {}))
            if isinstance(values, list):
                found.extend(item for item in values if isinstance(item, dict))
        except (ValueError, TypeError, KeyError, IndexError):
            pass
    return found


def import_scope(source_rows, result_rows, source_key, result_key, rules, meta=None, title="전체 데이터"):
    scope = Scope(source_rows, result_rows, meta, title, imported=True)
    key = Wire("key", _import_field(scope, source_key, "source"), _import_field(scope, result_key, "result"),
               source_transform="규칙 설정 유지", result_transform="규칙 설정 유지",
               original_keys=(copy.deepcopy(source_key), copy.deepcopy(result_key)))
    key.baseline = wire_state(key)
    scope.wires.append(key)
    for rule in rules:
        kind = "array" if rule["kind"] == "items" else "value"
        left = _import_field(scope, {"path": rule["to"], "scope": "source"}, "source", rule.get("type", "array" if kind == "array" else "string"))
        right = _import_field(scope, rule["from"], "result", rule.get("type", "array" if kind == "array" else "string"))
        wire = Wire(kind, left, right, result_transform="규칙 설정 유지", nonempty=rule.get("nonempty", False),
                    allow_missing=not rule.get("require_same_keys", True), raw_rule=copy.deepcopy(rule))
        if rule.get("when"):
            wire.condition_path = "규칙의 조건 유지"
        if kind == "array":
            wire.child = import_scope(_children(source_rows, {"path": rule["to"]}, "source"),
                                      _children(result_rows, rule["from"], "result"),
                                      rule["source_key"], rule["result_key"], rule["rules"], title=rule["to"])
        scope.wires.append(wire)
        if "expected" in rule:
            guard = Wire("guard", left, _import_field(scope, rule["expected"], "result"),
                         result_transform="규칙 설정 유지", raw_rule={"expected": copy.deepcopy(rule["expected"])}, owner=wire)
            guard.baseline = wire_state(guard)
            scope.wires.append(guard)
        wire.baseline = wire_state(wire)
    return scope


def import_rule(config, source_sample=None, result_sample=None, source_pointer=None, result_pointer=None):
    config = validate_config(copy.deepcopy(config))
    sp = config["source"].get("records_path", "") if source_pointer is None else source_pointer
    rp = config["result"].get("records_path", "") if result_pointer is None else result_pointer
    source_rows = source_sample.rows(sp, config["source"].get("record_mode", "auto")) if source_sample else []
    result_rows = result_sample.rows(rp, config["result"].get("record_mode", "auto")) if result_sample else []
    scope = import_scope(source_rows, result_rows, config["source"]["key"], config["result"]["key"],
                         config["rules"], source_sample.meta if source_sample else None)
    scope.rule_config = config
    return scope


def _wire_expr(scope, wire, side, original, root=True):
    path = wire.source if side == "source" else wire.result
    transform = wire.source_transform if side == "source" else wire.result_transform
    if transform == "규칙 설정 유지" and path == wire.baseline.get(side):
        return copy.deepcopy(original)
    if transform == "규칙 설정 유지":
        transform = "그대로"
    fields = scope.source_fields if side == "source" else scope.result_fields
    if path.startswith("@expression:"):
        raise DataError("이 표현식은 ‘고급 규칙 원문’에서 수정하세요. 원래 규칙은 그대로 보존됩니다.")
    return fields[path].expr(transform, "source" if side == "source" and not root else None)


def _compile_imported(scope, root=True):
    keys = [wire for wire in scope.wires if wire.kind == "key"]
    if len(keys) != 1:
        raise DataError(f"{scope.title}: 같은 건을 찾을 기준을 연결하세요.")
    key = keys[0]
    originals = key.original_keys or (None, None)
    sk = _wire_expr(scope, key, "source", originals[0], root)
    rk = _wire_expr(scope, key, "result", originals[1], root)
    guards = [wire for wire in scope.wires if wire.kind == "guard"]
    rules, checks = [], []
    for wire in scope.wires:
        if wire.kind not in ("value", "array"):
            continue
        guard = next((item for item in guards if item.owner is wire), None)
        if guard is None:
            guard = next((item for item in guards if item.owner is None and item.source == wire.source), None)
        if wire.raw_rule is None:
            fresh = copy.copy(scope)
            fresh.imported = False
            # Fresh compilation uses plain key expressions only for validation.
            fresh_key = copy.copy(key)
            fresh_key.source_transform = fresh_key.result_transform = "그대로"
            fresh.wires = [fresh_key, wire] + ([guard] if guard and guard.raw_rule is None else [])
            _, _, generated, extra = compile_scope(fresh, root)
            rule = generated[0]
            checks.extend(extra)
            if guard and guard.raw_rule:
                rule["expected"] = _wire_expr(scope, guard, "result", guard.raw_rule["expected"])
        else:
            rule = copy.deepcopy(wire.raw_rule)
            rule["from"] = _wire_expr(scope, wire, "result", rule["from"])
            rule["to"] = wire.source
            if wire.kind == "array":
                child_sk, child_rk, child_rules, child_checks = compile_scope(wire.child, False)
                rule.update(source_key=child_sk, result_key=child_rk, rules=child_rules)
                if child_checks:
                    rule["identity_guards"] = rule.get("identity_guards", []) + [
                        {"source": check["left"], "result": {"literal": check["value"]}} for check in child_checks]
                if wire.allow_missing != wire.baseline["allow_missing"]:
                    rule["require_same_keys"] = not wire.allow_missing
            else:
                if wire.nonempty != wire.baseline["nonempty"]:
                    rule["nonempty"] = wire.nonempty
                if guard:
                    rule["expected"] = _wire_expr(scope, guard, "result", (guard.raw_rule or {}).get("expected"))
                else:
                    rule.pop("expected", None)
            if (wire.condition_path, wire.condition_value) != (wire.baseline["condition_path"], wire.baseline["condition_value"]):
                if wire.condition_path and wire.condition_path != "규칙의 조건 유지":
                    rule["when"] = [{"left": scope.result_fields[wire.condition_path].expr(), "op": "eq", "value": wire.condition_value}]
                elif not wire.condition_path:
                    rule.pop("when", None)
        if guard:
            guards.remove(guard)
        rules.append(rule)
    if guards:
        raise DataError("이전값 대조만 남은 연결이 있습니다. 바꿀 값 연결도 필요합니다.")
    if not rules:
        raise DataError("반영할 값 연결이 하나 이상 필요합니다.")
    return sk, rk, rules, checks


def _context(row, meta):
    return {"source": row, "original": row, "result": row, "root_source": row,
            "root_original": row, "root_result": row, "meta": meta}


def preview_rule(config, source_rows, result_rows, meta):
    index, original_counts = defaultdict(list), Counter()
    errors = []
    invalid_results = 0
    source_keys = []
    for number, row in enumerate(result_rows):
        try:
            result_meta = row_metadata(result_rows, number)
            index[canonical_key(expression(config["result"]["key"], _context(row, result_meta)))].append((row, result_meta))
        except (ValueError, TypeError, KeyError):
            invalid_results += 1
    for number, row in enumerate(source_rows):
        try:
            source_meta = {**row_metadata(source_rows, number, meta), "result_file": "", "result_row": ""}
            key = canonical_key(expression(config["source"]["key"], _context(row, source_meta)))
            source_keys.append(key)
            original_counts[key] += 1
        except (ValueError, TypeError, KeyError):
            source_keys.append(None)
    key_counts = Counter()
    matched_indices = []
    for number, key in enumerate(source_keys):
        if key is None:
            key_counts["invalid_source"] += 1
        elif original_counts[key] > 1 or len(index.get(key, [])) > 1:
            key_counts["duplicate"] += 1
        elif key not in index:
            key_counts["no_result"] += 1
        else:
            key_counts["matched"] += 1
            matched_indices.append(number)
    counts = Counter()
    examples = []
    sampled = sample_rows(list(range(len(source_rows))))
    # A small partial export must still get a useful before/after example.
    if matched_indices and not set(sampled).intersection(matched_indices):
        sampled[-1] = matched_indices[0]
    for number in sampled:
        original = source_rows[number]
        source_meta = {**row_metadata(source_rows, number, meta), "result_file": "", "result_row": ""}
        try:
            key = canonical_key(expression(config["source"]["key"], _context(original, source_meta)))
            matches = index.get(key, [])
            if original_counts[key] > 1 or len(matches) > 1:
                counts["duplicate"] += 1
            elif not matches:
                counts["no_result"] += 1
            else:
                incoming, result_meta = matches[0]
                combined_meta = {**source_meta, **{name: result_meta[name] for name in ("result_file", "result_row") if name in result_meta}}
                candidate, changes, _, status = apply_record(original, incoming, config, combined_meta)
                counts[status] += 1
                if changes and len(examples) < 8:
                    examples.append({"id": json.loads(key)[1], "changes": changes[:8], "source_file": source_meta.get("relative_path", "")})
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            counts["review_required"] += 1
            if len(errors) < 8:
                errors.append(str(exc))
    return {"sampled": len(sampled), "source_loaded": len(source_rows), "result_loaded": len(result_rows),
            "invalid_result_keys": invalid_results, "counts": dict(counts), "examples": examples, "errors": errors,
            "all_source_key_counts": dict(key_counts), "duplicate_result_rows": sum(len(rows) for rows in index.values() if len(rows) > 1)}
