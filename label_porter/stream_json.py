"""Incremental result reader. Keep decoded records, not a second whole-file string."""
import codecs
import hashlib
import json
import re

from .formats import DECODER, DataError, Document


SPACE = re.compile(r"\s*")
CHUNK = 256 * 1024


def read_json_document(path, encoding, progress, checkpoint):
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder(encoding)()
    total = path.stat().st_size
    buffer, pos, offset, consumed, count = "", 0, 0, 0, 0
    eof, bom, first = False, False, True

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
                    result, end = DECODER.raw_decode(buffer, pos)
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

        docs = []
        tick()
        space()
        while pos < len(buffer):
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
                docs.append(items)
            else:
                docs.append(value())
                count += 1
                if count % 128 == 0:
                    tick()
            if pos == len(buffer) and not eof:
                more()
            if pos < len(buffer) and not buffer[pos].isspace():
                raise DataError(f"JSON 문서 사이에 공백/줄바꿈이 필요합니다 (문자 {offset + pos}).")
            space()
        if not docs:
            raise DataError("비어 있는 JSON 파일입니다.")
        tick()
    return Document(docs, digest.hexdigest(), path.suffix.lower() in (".jsonl", ".ndjson"), bom, encoding)
