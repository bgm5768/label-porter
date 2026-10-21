"""Bound JSON presentation work without truncating or modifying the data itself."""
import json


def json_prefix(value, limit, *, indent=2):
    """Return the exact json.dumps prefix and whether content was omitted.

    iterencode alone still allocates an entire scalar string. Slice strings before
    quoting as well, so a huge text field cannot bypass the presentation budget.
    """
    def scalar(item):
        return json.dumps(item[:limit + 1] if isinstance(item, str) else item, ensure_ascii=False)

    def chunks(item, depth=0):
        if not isinstance(item, (list, dict)):
            yield scalar(item)
            return
        mapping = isinstance(item, dict)
        yield '{' if mapping else '['
        if item:
            for number, (key, child) in enumerate(item.items() if mapping else enumerate(item)):
                if number: yield ', ' if indent is None else ','
                if indent is not None: yield '\n' + ' ' * (indent * (depth + 1))
                if mapping:
                    name = key if isinstance(key, str) else str(key) if isinstance(key, (int, float)) and not isinstance(key, bool) else json.dumps(key)
                    yield scalar(name)
                    yield ': '
                yield from chunks(child, depth + 1)
            if indent is not None: yield '\n' + ' ' * (indent * depth)
        yield '}' if mapping else ']'

    parts, size = [], 0
    for chunk in chunks(value):
        remaining = limit + 1 - size
        parts.append(chunk[:remaining])
        size += min(len(chunk), remaining)
        if size > limit:
            return ''.join(parts)[:limit], True
    return ''.join(parts), False
