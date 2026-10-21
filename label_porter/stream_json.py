"""Incremental result reader. Keep decoded records, not a second whole-file string."""
import codecs
import hashlib
import json
import re

from .formats import DECODER, DataError, Document, _bad_constant, _finite_float


SPACE = re.compile(r"\s*")
CHUNK = 256 * 1024
KEY_POOL_THRESHOLD = 8 * 1024 * 1024


def read_json_document(path, encoding, progress, checkpoint):
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder(encoding)()
    total = path.stat().st_size
    buffer, pos, offset, consumed, count = "", 0, 0, 0, 0
    eof, bom, first = False, False, True
    pool_keys = total >= KEY_POOL_THRESHOLD
    # The standard scanner clears its memo after each array item. Intern only
    # short field names within this read, with a hard cap; never global intern().
    keys = {}
    def object_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result: raise DataError(f"중복 JSON 필드: {key}")
            pooled = keys.get(key)
            if pooled is not None:
                key = pooled
            elif len(keys) < 1024 and len(key) <= 64:
                keys[key] = key
            result[key] = item
        return result
    strict = (json.JSONDecoder(object_pairs_hook=object_pairs, parse_constant=_bad_constant, parse_float=_finite_float)
              if pool_keys else DECODER)

    def tick():
        checkpoint()
        progress(consumed, total, count)

    with path.open("rb") as handle:
        def more(size=CHUNK):
            nonlocal buffer, pos, offset, consumed, eof, bom, first
            checkpoint()
            raw = handle.read(size)
            digest.update(raw)
            consumed += len(raw)
            eof = not raw
            text = decoder.decode(raw, final=eof)
            if first:
                bom = raw.startswith(b"\xef\xbb\xbf")
                text = text.lstrip("\ufeff")
                first = False
            offset += pos
            buffer = buffer[pos:] + text
            pos = 0
            tick()

        def space():
            nonlocal pos
            while True:
                pos = SPACE.match(buffer, pos).end()
                if pos < len(buffer) or eof:
                    return
                more()

        def value():
            nonlocal pos
            size = CHUNK
            while True:
                checkpoint()
                try:
                    result, end = strict.raw_decode(buffer, pos)
                    # A number can be a valid prefix of a number in the next chunk.
                    if not eof and (end == len(buffer) or
                                    isinstance(result, (int, float)) and not isinstance(result, bool)
                                    and buffer[end] in ".eE+-0123456789"):
                        more(size)
                        size *= 2
                        continue
                    pos = end
                    return result
                except json.JSONDecodeError as exc:
                    if eof:
                        raise DataError(f"JSON 해석 실패 (문자 {offset + pos}): {exc}") from exc
                    more(size)
                    size *= 2

        def container():
            nonlocal pos, count
            checkpoint()
            if buffer[pos] == "[":
                # Large exports commonly contain a root array. Decode each item
                # with the strict standard decoder to keep cancellation responsive.
                pos += 1
                items = []
                space()
                if pos < len(buffer) and buffer[pos] == "]":
                    pos += 1
                else:
                    while True:
                        items.append(value())
                        count += 1
                        if count % 128 == 0:
                            tick()
                        space()
                        if pos >= len(buffer) or buffer[pos] not in ",]":
                            raise DataError(f"JSON 배열에 쉼표 또는 닫는 괄호가 필요합니다 (문자 {offset + pos}).")
                        delimiter = buffer[pos]
                        pos += 1
                        if delimiter == "]":
                            break
                        space()
                return items
            elif buffer[pos] == '{':
                # Wrapper exports such as {"data":{"items":[...]}} stream the
                # inner array as well, without repeatedly decoding the prefix.
                pos += 1
                obj = {}
                space()
                if pos < len(buffer) and buffer[pos] == '}':
                    pos += 1
                    return obj
                while True:
                    if pos >= len(buffer) or buffer[pos] != '"':
                        raise DataError('JSON 객체에 필드 이름이 필요합니다.')
                    key = value()
                    if key in obj: raise DataError(f'중복 JSON 필드: {key}')
                    if pool_keys:
                        pooled = keys.get(key)
                        if pooled is not None:
                            key = pooled
                        elif len(keys) < 1024 and len(key) <= 64:
                            keys[key] = key
                    space()
                    if pos >= len(buffer) or buffer[pos] != ':':
                        raise DataError('JSON 객체에 콜론이 필요합니다.')
                    pos += 1
                    space()
                    if pos >= len(buffer): raise DataError('JSON 값이 없습니다.')
                    obj[key] = container() if buffer[pos] in '[{' else value()
                    space()
                    if pos >= len(buffer) or buffer[pos] not in ',}':
                        raise DataError('JSON 객체에 쉼표 또는 닫는 괄호가 필요합니다.')
                    delimiter = buffer[pos]
                    pos += 1
                    if delimiter == '}': return obj
                    space()
            else:
                return value()

        docs = []
        tick()
        space()
        while pos < len(buffer):
            array = buffer[pos] == '['
            if buffer[pos] == '{':
                # Most individual labels / JSONL rows fit in the current chunk.
                # Use the fast strict decoder there; stream an incomplete wrapper.
                try:
                    document, end = strict.raw_decode(buffer, pos)
                except json.JSONDecodeError:
                    docs.append(container())
                else:
                    docs.append(document)
                    pos = end
            else:
                docs.append(container())
            if not array: count += 1
            if count % 128 == 0: tick()
            if pos == len(buffer) and not eof:
                more()
            if pos < len(buffer) and not buffer[pos].isspace():
                raise DataError(f"JSON 문서 사이에 공백/줄바꿈이 필요합니다 (문자 {offset + pos}).")
            space()
        if not docs:
            raise DataError("비어 있는 JSON 파일입니다.")
        tick()
    return Document(docs, digest.hexdigest(), path.suffix.lower() in (".jsonl", ".ndjson"), bom, encoding)
