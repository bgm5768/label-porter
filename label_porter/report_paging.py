"""Offline pagination: only the current page's rows are mounted in the DOM."""

STYLE = """
.review-tools,.pager{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:16px 0}
.review-tools label:first-child{flex:1;min-width:220px}
.review-tools input{box-sizing:border-box;width:100%;max-width:520px}
.review-tools input,.review-tools select,.pager input,.pager button{
font:inherit;color:inherit;border:1px solid #cbd4df;background:white;border-radius:6px;padding:7px 10px}
.pager button{cursor:pointer}.pager button:disabled{opacity:.4;cursor:default}
.pager input{width:65px}.pager{justify-content:flex-end}.pager .range{margin-right:auto}
.table-scroll{overflow-x:auto}#review-table{min-width:780px;table-layout:fixed}
#review-table th:nth-child(1){width:10%}#review-table th:nth-child(2){width:17%}
#review-table th:nth-child(3){width:17%}#review-table th:nth-child(5){width:11%}
#review-table td{vertical-align:top}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #285cc7;outline-offset:2px}
"""

CONTROLS = """
<div class="review-tools">
<label for="review-search">검토 항목 검색<br><input id="review-search" type="search" placeholder="전체 항목에서 파일명, ID, 사유 검색"></label>
<label for="review-size">페이지당 <select id="review-size"><option>25</option><option>50</option><option>100</option></select>건</label>
</div>
"""

PAGER = """
<nav class="pager" aria-label="검토 목록 페이지">
<span class="range" id="review-range" role="status" aria-live="polite"></span>
<button type="button" id="review-first" aria-label="첫 페이지">처음</button>
<button type="button" id="review-prev" aria-label="이전 페이지">이전</button>
<label>페이지 <input id="review-page" type="number" min="1" value="1" aria-label="이동할 페이지"></label>
<span id="review-pages"></span>
<button type="button" id="review-next" aria-label="다음 페이지">다음</button>
<button type="button" id="review-last" aria-label="마지막 페이지">마지막</button>
</nav><noscript>페이지 이동과 전체 검색을 사용하려면 JavaScript를 켜세요. 전체 항목은 위의 CSV·JSONL 파일에서도 확인할 수 있습니다.</noscript>
"""

SCRIPT = r"""
(() => {
  const all = JSON.parse(document.getElementById('review-data').textContent);
  const dashboard = !!document.getElementById('report-categories');
  const byId = id => document.getElementById('review-' + id);
  const search = byId('search'), size = byId('size'), input = byId('page');
  const indexed = all.map(row => ({row, text: [row['상태'], row['원본'], row['보완'], row['키'], row['위치'], row['사유']].join(' ').toLowerCase()}));
  const category = dashboard ? document.getElementById('report-category') : null;
  const status = dashboard ? document.getElementById('report-status') : null;
  const field = dashboard ? document.getElementById('report-field') : null;
  // Reuse the recorded changes. Keys alone are not unique across files/records.
  const pathsByRecord = new Map(), fieldCounts = new Map();
  if (field) {
    for (const row of all) {
      if (!row.groups.includes('changes') || typeof row['위치'] !== 'string') continue;
      const path = row['위치'];
      if (!row.link || !pathsByRecord.get(row.link)?.has(path))
        fieldCounts.set(path, (fieldCounts.get(path) || 0) + 1);
      if (row.link) {
        if (!pathsByRecord.has(row.link)) pathsByRecord.set(row.link, new Set());
        pathsByRecord.get(row.link).add(path);
      }
    }
    const fragment = document.createDocumentFragment(), any = document.createElement('option');
    any.value = ''; any.textContent = fieldCounts.size ? '모든 변경 필드' : '변경된 필드 없음';
    fragment.append(any);
    for (const path of [...fieldCounts.keys()].sort((a, b) => a.localeCompare(b))) {
      const option = document.createElement('option');
      // @root cannot collide with a non-empty JSON Pointer (which starts with /).
      option.value = path || '@root';
      option.textContent = `${path || '레코드 전체'} · ${fieldCounts.get(path).toLocaleString()}건`;
      fragment.append(option);
    }
    field.replaceChildren(fragment); field.disabled = !fieldCounts.size;
  }
  let filtered = all, page = 1, timer;
  function state() {
    return new URLSearchParams({q: search.value, size: size.value, page: String(page), ...(dashboard ? {group: category.value, status: status.value} : {}), ...(field && field.value ? {field: field.value} : {})});
  }
  function render() {
    const amount = Number(size.value), pages = Math.max(1, Math.ceil(filtered.length / amount));
    page = Math.max(1, Math.min(pages, Math.trunc(Number(page)) || 1));
    const start = (page - 1) * amount, end = Math.min(start + amount, filtered.length);
    const fragment = document.createDocumentFragment();
    const hash = '#' + state().toString();
    for (const row of filtered.slice(start, end)) {
      const tr = document.createElement('tr');
      for (const key of (dashboard ? ['상태', '원본', '보완', '키', '위치', '사유'] : ['상태', '원본', '키', '사유'])) {
        const td = document.createElement('td');
        td.textContent = row[key] == null ? '' : String(row[key]);
        if (dashboard && key === '보완') { td.title = td.textContent; td.textContent = td.textContent.split(/[\\/]/).pop(); }
        if (key === '사유') td.style.whiteSpace = 'pre-wrap';
        tr.append(td);
      }
      const td = document.createElement('td'), link = document.createElement('a');
      link.href = row.link + hash;
      link.textContent = dashboard ? '상세 비교' + (row.issues ? ' · ' + row.issues + '개 사유' : '') : '비교 보기 · ' + row.issues + '개 사유';
      td.append(link); tr.append(td); fragment.append(tr);
    }
    if (!filtered.length) {
      const tr = document.createElement('tr'), td = document.createElement('td');
      td.colSpan = dashboard ? 7 : 5; td.textContent = all.length ? '검색 결과가 없습니다.' : dashboard ? '표시할 처리 기록이 없습니다.' : '라벨 검토 항목이 없습니다.';
      tr.append(td); fragment.append(tr);
    }
    byId('list').replaceChildren(fragment);
    byId('range').textContent = filtered.length
      ? `${(start + 1).toLocaleString()}–${end.toLocaleString()} / ${filtered.length.toLocaleString()}건` +
        (search.value.trim() ? ` · 전체 ${all.length.toLocaleString()}건` : '')
      : `0건 · 전체 ${all.length.toLocaleString()}건`;
    input.value = String(page); input.max = String(pages);
    byId('pages').textContent = '/ ' + pages.toLocaleString();
    byId('first').disabled = byId('prev').disabled = page === 1;
    byId('next').disabled = byId('last').disabled = page === pages;
    try { history.replaceState(null, '', hash); } catch (_) {}
  }
  function filter() {
    const query = search.value.trim().toLowerCase();
    const path = field && field.value === '@root' ? '' : field && field.value;
    filtered = indexed.filter(item => (!dashboard || item.row.groups.includes(category.value)) &&
      (!field || !field.value || (item.row.groups.includes('changes')
        ? item.row['위치'] === path : pathsByRecord.get(item.row.link)?.has(path))) &&
      (!dashboard || !status.value || item.row.status === status.value) && (!query || item.text.includes(query))).map(item => item.row);
    if (dashboard) {
      document.getElementById('list-title').textContent = category.options[category.selectedIndex].text;
      document.querySelectorAll('[data-group]').forEach(node => node.setAttribute('aria-current', node.dataset.group === category.value ? 'true' : 'false'));
    }
  }
  function restore() {
    const params = new URLSearchParams(location.hash.slice(1));
    search.value = params.get('q') || '';
    size.value = ['25','50','100'].includes(params.get('size')) ? params.get('size') : '25';
    if (dashboard) {
      category.value = Array.from(category.options).some(o => o.value === params.get('group')) ? params.get('group') : 'output';
      status.value = Array.from(status.options).some(o => o.value === params.get('status')) ? params.get('status') : '';
      if (field) field.value = Array.from(field.options).some(o => o.value === params.get('field')) ? params.get('field') : '';
    }
    page = Number(params.get('page')) || 1; filter(); render();
  }
  if (dashboard) {
    if (field) field.addEventListener('change', () => { page = 1; filter(); render(); });
    category.addEventListener('change', () => { page = 1; status.value = ''; filter(); render(); });
    status.addEventListener('change', () => { page = 1; filter(); render(); });
    document.querySelectorAll('[data-group]').forEach(node => node.addEventListener('click', event => {
      event.preventDefault(); category.value = node.dataset.group; page = 1; search.value = ''; status.value = ''; filter(); render();
      document.getElementById('records').scrollIntoView({behavior: 'smooth'});
    }));
  }
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => { page = 1; filter(); render(); }, 120);
  });
  size.addEventListener('change', () => { page = 1; render(); });
  input.addEventListener('change', () => { page = Number(input.value); render(); });
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter') { page = Number(input.value); render(); }
  });
  byId('first').addEventListener('click', () => { page = 1; render(); });
  byId('prev').addEventListener('click', () => { page--; render(); });
  byId('next').addEventListener('click', () => { page++; render(); });
  byId('last').addEventListener('click', () => { page = Math.ceil(filtered.length / Number(size.value)); render(); });
  window.addEventListener('hashchange', restore);
  restore();
})();
"""
