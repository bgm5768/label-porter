"""Reuse the comparison renderer, in bounded offline chunks rather than 40k files."""
import json

from .formats import atomic_text
from .review_report import render_review
from .detail_renderer import RENDERER


def script_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


class DetailStore:
    def __init__(self, directory):
        self.directory = directory
        self.chunk = 1
        self.number = 0
        self.pending = {}
        self.bytes = 0

    def add(self, detail, serialized=None):
        self.number += 1
        identity = f"{self.number:08d}"
        body = serialized if serialized is not None else json.dumps(detail, ensure_ascii=False, allow_nan=False)
        link = f"detail.html?chunk={self.chunk:06d}&id={identity}"
        self.pending[identity] = body
        self.bytes += len(body)
        if len(self.pending) >= 100 or self.bytes >= 2_000_000:
            self.flush()
        return link

    def flush(self):
        if not self.pending:
            return
        atomic_text(self.directory / "detail_data" / f"{self.chunk:06d}.js",
                    "window.porterDetail = " + script_json(self.pending) + ";")
        self.pending = {}
        self.bytes = 0
        self.chunk += 1

    def close(self):
        self.flush()
        from .reports import STATUS
        template = render_review('', {}, download=False).split('<main>')[0]
        start, end = template.index('<title>'), template.index('</title>') + len('</title>')
        template = template[:start] + '<title></title>' + template[end:]
        renderer = 'const detailHeader = ' + script_json(template) + ';\nconst detailStatuses = ' + script_json(STATUS) + ';\n' + RENDERER
        atomic_text(self.directory / "detail.html", r"""<!doctype html><html lang="ko"><meta charset="utf-8">
<title>레코드 처리 상세</title><p id="loading">상세 내용을 불러오는 중입니다…</p>
<script>
""" + renderer + r"""
const params = new URLSearchParams(location.search);
const chunk = params.get('chunk'), id = params.get('id');
if (!/^\d{6}$/.test(chunk || '') || !/^\d{8}$/.test(id || '')) {
  document.getElementById('loading').textContent = '유효하지 않은 상세 주소입니다.';
} else {
  const script = document.createElement('script');
  script.src = 'detail_data/' + chunk + '.js';
  script.onerror = () => { document.getElementById('loading').textContent = '상세 파일을 찾을 수 없습니다. reports 폴더 전체를 함께 보관하세요.'; };
  script.onload = () => {
    const detail = window.porterDetail && window.porterDetail[id];
    if (!detail) { document.getElementById('loading').textContent = '해당 레코드가 없습니다.'; return; }
    const body = detail.startsWith('<!doctype') ? detail : renderDetail(id, parseDetail(detail));
    const parsed = new DOMParser().parseFromString(body, 'text/html');
    document.head.replaceChildren(...parsed.head.childNodes);
    document.body.replaceChildren(...parsed.body.childNodes);
    document.getElementById('back-to-list').href = 'report.html' + location.hash;
    delete window.porterDetail;
  };
  document.head.append(script);
}
</script></html>""")
