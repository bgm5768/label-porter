"""Lazily built, session-owned full-text index outside the Python object heap."""
import json
import mmap
import tempfile


class SearchIndex:
    def __init__(self, data, checkpoint):
        self.mapping = None
        self.file = tempfile.TemporaryFile(prefix='label-porter-search-')
        self.spans = []
        try:
            for entry in data.catalog:
                checkpoint()
                source = data.originals[entry['source_index']][0] if entry['source_index'] is not None else None
                incoming = entry['incoming'][1] if entry['incoming'] else None
                spans = []
                for value, present in ((source, entry['source_index'] is not None), (incoming, bool(entry['incoming']))):
                    start = self.file.tell()
                    if present:
                        text = json.dumps(value, ensure_ascii=False, separators=(',', ':')).casefold()
                        self.file.write(text.encode('utf-8', errors='surrogatepass'))
                    spans.append((start, self.file.tell()))
                self.spans.append(spans)
            self.file.flush()
            if self.file.tell():
                self.mapping = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
        except BaseException:
            self.close()
            raise

    def contains(self, index, field, term):
        start, end = self.spans[index][field - 2]
        return self.mapping is not None and self.mapping.find(term, start, end) >= 0

    def close(self):
        if self.mapping is not None: self.mapping.close()
        self.mapping = None
        if getattr(self, 'file', None) is not None: self.file.close()

    def __del__(self):
        self.close()
