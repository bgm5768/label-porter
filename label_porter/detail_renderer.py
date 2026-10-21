"""Render a single offline detail only when opened, rather than every record."""

RENDERER = r"""
// Keep number lexemes intact (IDs beyond 2^53 and decimal/exponent values).
// Input is generated strict JSON; no eval and no HTML from input is executed.
class DetailNumber { constructor(text) { this.text = text; } toString() { return this.text; } }
const detailKeyOrder = new WeakMap();
function parseDetail(text) {
  const tokens = text.match(/"(?:\\[\s\S]|[^"\\])*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null|[{}\[\]:,]/g);
  let position = 0;
  const take = expected => { if (tokens[position++] !== expected) throw Error('Invalid detail'); };
  function value() {
    const token = tokens[position++];
    if (token === '{') {
      const result = Object.create(null), order = [];
      detailKeyOrder.set(result, order);
      if (tokens[position] !== '}') do { const key = JSON.parse(tokens[position++]); take(':'); order.push(key); result[key] = value(); if (tokens[position] !== ',') break; position++; } while (true);
      take('}'); return result;
    }
    if (token === '[') {
      const result = [];
      if (tokens[position] !== ']') do { result.push(value()); if (tokens[position] !== ',') break; position++; } while (true);
      take(']'); return result;
    }
    return /^-?\d/.test(token) ? new DetailNumber(token) : JSON.parse(token);
  }
  return value();
}
function formatDetail(value, depth=0) {
  if (value instanceof DetailNumber) return value.text;
  if (value === null || typeof value !== 'object') return JSON.stringify(value ?? null);
  const array = Array.isArray(value), entries = array ? value : (detailKeyOrder.get(value) || Object.keys(value)).map(key => [key, value[key]]);
  const parts = entries.map(entry => array ? formatDetail(entry, depth+1) : JSON.stringify(entry[0])+': '+formatDetail(entry[1], depth+1));
  return (array ? '[' : '{') + (parts.length ? '\n'+'  '.repeat(depth+1)+parts.join(',\n'+'  '.repeat(depth+1))+'\n'+'  '.repeat(depth) : '') + (array ? ']' : '}');
}
function renderDetail(id, d) {
  const e = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#x27;'}[c]));
  const dump = v => e(formatDetail(v));
  const pre = v => '<pre>' + dump(v) + '</pre>';
  const issues = d.issues || [], changes = d.changes || [];
  const title = issues.length ? '검토 항목' : '레코드 처리 상세';
  let body = '<main><a id="back-to-list" href="report.html">← 처리 결과서 목록</a><h1>' + title + ' ' + e(id) + '</h1>';
  body += '<div class="meta"><b>처리 상태: ' + e(detailStatuses[d.status] || d.status) + '</b><br>원본: ' + e(d.source_file) + ' · ' + e(d.record_path) +
    '<br>연결키: ' + e(d.key) + '<br>결과: ' + e(d.result_file) + ' · 레코드 ' + e(d.result_row) +
    '<br>전체 원문은 보고서의 레코드 상세 JSONL에서도 확인할 수 있습니다.</div><p>' + e(d.reason) + '</p>';
  if (changes.length) body += '<h2>변경 필드 ' + changes.length.toLocaleString('ko-KR') + '개</h2><table><thead><tr><th>필드 경로</th><th>변경 전</th><th>변경 후</th></tr></thead><tbody>' +
    changes.map(c => '<tr><td><code>'+e(c.path)+'</code></td><td>'+pre(c.before)+'</td><td>'+pre(c.after)+'</td></tr>').join('') + '</tbody></table>';
  if (issues.length) body += '<h2>검토 사유 ' + issues.length.toLocaleString('ko-KR') + '개</h2>';
  issues.forEach((issue, i) => {
    body += '<article><div class="issue-head"><b>' + (i+1) + '. ' + e(issue.message) + '</b><code>' + e(issue.source_path || '레코드 전체') + '</code></div>';
    let rows = '';
    for (const [key,label] of [['source','기준값 / 원본'],['platform','플랫폼 최종값'],['expected','반입 당시 값'],['comparison','조건 검사값']]) {
      const v = issue[key];
      if (v && Object.hasOwn(v, 'raw_present')) rows += '<tr><th>'+label+'</th><td>'+(v.raw_present ? pre(v.raw) : '필드 없음 / 읽기 실패')+'</td><td>'+(v.evaluated_present ? pre(v.evaluated) : e(v.evaluated_error || '계산 실패'))+'</td></tr>';
    }
    if (rows) body += '<table><thead><tr><th>비교 대상</th><th>원래 값</th><th>변환 후 검사값</th></tr></thead><tbody>'+rows+'</tbody></table>';
    for (const [key,label] of [['item_id','연결 ID'],['source_item','원본 항목'],['result_item','플랫폼 최종 항목'],['previous_item','이전 구간 후보'],['candidate_item','현재 항목 후보'],['overlap','겹치는 시간'],['duration','전체 길이']]) {
      if (Object.hasOwn(issue,key)) body += '<div class="value"><b>'+label+'</b>'+pre(issue[key])+'</div>';
    }
    body += '<details><summary>검사식·전체 근거 펼치기</summary>'+pre(Object.fromEntries(Object.entries(issue).filter(([k]) => !['message','code','source_path'].includes(k))))+'</details></article>';
  });
  body += '<h2>원본 · 보완 데이터 · 처리 결과</h2><div class="comparison">';
  for (const [key,title,caption] of [
    ['source_record','원본 JSON','보완 전 원본 레코드'],
    ['platform_record','보완 데이터 JSON','플랫폼 또는 별도로 보완한 레코드 · 수정값 출처'],
    ['final_record','실제 출력 JSON',d.preview ? '미리보기에서는 데이터 파일을 생성하지 않습니다.' : '실제 출력값입니다. 전체 반영을 보류한 레코드는 원본 그대로입니다.'],
    ['candidate_record','반영 예정 / 후보 JSON',d.candidate_note || '실제 저장값이 아닙니다.']]) {
    if (Object.hasOwn(d,key)) body += '<section><h2>'+title+'</h2><p class="muted">'+e(caption)+'</p>'+pre(d[key])+'</section>';
  }
  if (Object.hasOwn(d,'proposed_changes')) body += '<section><h2>계산 가능한 변경 후보 · 저장되지 않음</h2>'+pre(d.proposed_changes)+'</section>';
  const omitted = ['issues','source_record','platform_record','final_record','candidate_record','candidate_note','changes','proposed_changes','source_file','record_path','key','result_file','result_row','status','review_id'];
  const extra = Object.fromEntries(Object.entries(d).filter(([k]) => !omitted.includes(k)));
  if (Object.keys(extra).length) body += '<section><h2>추가 연결·입력 정보</h2>'+pre(extra)+'</section>';
  return detailHeader.replace('<title></title>', '<title>'+title+' '+e(id)+'</title>') + body + '</div></main></html>';
}
"""
