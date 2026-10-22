"""Search decoded JSON without serializing/caching a second copy of the document."""
def search_json(value, query, checkpoint=lambda: None):
    """Yield relative token paths matching all terms in a key or scalar value.

    Iterator frames keep traversal memory proportional to depth, not array size.
    Paths are token tuples so slash/tilde keys remain unambiguous.
    """
    terms = query.casefold().split()
    if not terms:
        return
    stack = [iter((((), '', value),))]
    while stack:
        checkpoint()
        try:
            path, key, current = next(stack[-1])
        except StopIteration:
            stack.pop(); continue
        scalar = ''
        if not isinstance(current, (dict, list)):
            scalar = ('null' if current is None else 'true' if current is True else
                      'false' if current is False else str(current))
        text = (str(key) + '\n' + scalar).casefold()
        if all(term in text for term in terms):
            yield path
        if isinstance(current, dict):
            children = current.items()
        elif isinstance(current, list):
            children = enumerate(current)
        else:
            continue
        # Bind the parent's tokens now (generator closures would see the next path).
        stack.append(map(lambda pair, base=path: (base + (pair[0],), pair[0], pair[1]), children))


def value_at(value, tokens):
    for token in tokens:
        value = value[token]
    return value
