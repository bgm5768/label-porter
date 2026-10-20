from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DataError(ValueError):
    pass


MISSING = object()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError(f"중복 JSON 필드: {key}")
        result[key] = value
    return result


def _bad_constant(value):
    raise DataError(f"JSON에서 허용되지 않는 숫자: {value}")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise DataError("유한하지 않은 JSON 숫자입니다.")
    return result


DECODER = json.JSONDecoder(object_pairs_hook=_object, parse_constant=_bad_constant, parse_float=_finite_float)


def parse_json(text: str) -> list[Any]:
    """Read one JSON value, JSONL, or whitespace-separated pretty JSON values."""
    text = text.lstrip("\ufeff")
    docs = []
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos == len(text):
            break
        try:
            value, end = DECODER.raw_decode(text, pos)
        except (ValueError, RecursionError) as exc:
            raise DataError(f"JSON 해석 실패 (문자 {pos}): {exc}") from exc
        docs.append(value)
        if end < len(text) and not text[end].isspace():
            raise DataError(f"JSON 문서 사이에 공백/줄바꿈이 필요합니다 (문자 {end}).")
        pos = end
    if not docs:
        raise DataError("비어 있는 JSON 파일입니다.")
    return docs


def tokens(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise DataError(f"필드 경로는 JSON Pointer 형식이어야 합니다: {pointer!r}")
    parts = pointer[1:].split("/")
    if any(re.search(r"~(?![01])", part) for part in parts):
        raise DataError(f"잘못된 경로 이스케이프: {pointer}")
    return [part.replace("~1", "/").replace("~0", "~") for part in parts]


def escape_token(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def get_pointer(value: Any, pointer: str, default=MISSING):
    try:
        for token in tokens(pointer):
            if isinstance(value, dict):
                value = value[token]
            elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
                value = value[int(token)]
            else:
                return default
        return value
    except (KeyError, IndexError, TypeError):
        return default


def set_pointer(value: Any, pointer: str, new_value: Any, allow_create=False):
    parts = tokens(pointer)
    if not parts:
        raise DataError("레코드 전체를 교체할 수 없습니다. 수정할 필드를 지정하세요.")
    parent_pointer = "/" + "/".join(escape_token(p) for p in parts[:-1]) if len(parts) > 1 else ""
    parent = get_pointer(value, parent_pointer)
    key = parts[-1]
    if isinstance(parent, dict):
        if key not in parent and not allow_create:
            raise DataError(f"원본에 대상 필드가 없습니다: {pointer}")
        parent[key] = new_value
    elif isinstance(parent, list) and re.fullmatch(r"0|[1-9][0-9]*", key) and int(key) < len(parent):
        parent[int(key)] = new_value
    else:
        raise DataError(f"원본에서 수정할 위치를 찾을 수 없습니다: {pointer}")


def equivalent(a, b):
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equivalent(a[key], b[key]) for key in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equivalent(left, right) for left, right in zip(a, b))
    return a == b


def valid_number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def canonical_key(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool) or value == "":
        raise DataError("연결키는 비어 있지 않은 문자열 또는 유한한 숫자여야 합니다.")
    if isinstance(value, (int, float)) and not math.isfinite(value):
        raise DataError("연결키에 유효하지 않은 숫자가 있습니다.")
    return json.dumps([type(value).__name__, value], ensure_ascii=False, allow_nan=False)


@dataclass
class Document:
    docs: list[Any]
    sha256: str
    jsonl: bool = False
    bom: bool = False
    encoding: str = "utf-8"


def read_document(path: Path, encoding="utf-8-sig", fmt="auto", csv_json_columns=(), *, progress=None, checkpoint=None) -> Document:
    fmt = ("csv" if path.suffix.lower() == ".csv" else "json") if fmt == "auto" else fmt
    if fmt != "csv" and (progress is not None or checkpoint is not None):
        from .stream_json import read_json_document
        return read_json_document(path, encoding, progress or (lambda *args: None), checkpoint or (lambda: None))
    if checkpoint:
        checkpoint()
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    text = raw.decode(encoding)
    if fmt == "csv":
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise DataError("CSV 헤더가 없거나 중복되었습니다.")
        rows = []
        for line, row in enumerate(reader, 2):
            if checkpoint:
                checkpoint()
            if None in row or any(value is None for value in row.values()):
                raise DataError(f"CSV {line}행의 열 수가 헤더와 다릅니다.")
            for field in csv_json_columns:
                if field not in row:
                    raise DataError(f"CSV에 JSON 해석 대상 열이 없습니다: {field}")
                parsed = parse_json(row[field])
                if len(parsed) != 1:
                    raise DataError(f"CSV {line}행 {field}: JSON 값은 하나여야 합니다.")
                row[field] = parsed[0]
            rows.append(row)
        docs = [rows]
    else:
        docs = parse_json(text)
    return Document(docs, digest, path.suffix.lower() in (".jsonl", ".ndjson"), raw.startswith(b"\xef\xbb\xbf"), encoding)


def records(document: Document, spec: dict):
    pointer = spec.get("records_path", "")
    mode = spec.get("record_mode", "auto")
    for doc_index, doc in enumerate(document.docs):
        container = get_pointer(doc, pointer)
        if container is MISSING:
            raise DataError(f"레코드 경로가 없습니다: {pointer}")
        if mode == "array" or (mode == "auto" and isinstance(container, list)):
            if not isinstance(container, list):
                raise DataError("레코드 컨테이너가 배열이 아닙니다.")
            for index, record in enumerate(container):
                yield (doc_index, pointer + "/" + str(index), record)
        else:
            if not isinstance(container, dict):
                raise DataError("레코드 컨테이너가 객체가 아닙니다.")
            yield (doc_index, pointer, container)


def atomic_text(path: Path, text: str, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".porter_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_document(path: Path, document: Document):
    indent = None if document.jsonl else 2
    body = "\n".join(json.dumps(doc, ensure_ascii=False, allow_nan=False, indent=indent) for doc in document.docs) + "\n"
    encoding = document.encoding
    if encoding.lower().replace("_", "-") in ("utf-8", "utf-8-sig", "utf8"):
        encoding = "utf-8-sig" if document.bom else "utf-8"
    atomic_text(path, body, encoding)
