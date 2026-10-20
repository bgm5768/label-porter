const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const folder = process.argv[2];
const html = fs.readFileSync(path.join(folder, 'detail.html'), 'utf8');
const code = html.match(/<script>([\s\S]*?)<\/script>/)[1];
function load(query, missing = false) {
  let appended = false, parsedBody;
  const loading = {textContent: ''}, back = {};
  const document = {
    getElementById: id => id === 'loading' ? loading : back,
    createElement: () => ({}),
    head: {replaceChildren(){}, append(script) {
      appended = true;
      if (missing) return script.onerror();
      const data = fs.readFileSync(path.join(folder, script.src), 'utf8');
      vm.runInContext(data, context);
      script.onload();
    }},
    body: {replaceChildren(){}},
  };
  const context = vm.createContext({URLSearchParams, location: {search: query, hash:'#group=output&page=2'},
    document, window: {}, DOMParser: class { parseFromString(body) {
      parsedBody = body; return {head:{childNodes:[]},body:{childNodes:[]}};
    }}});
  vm.runInContext(code, context);
  return {appended, parsedBody, loading, back};
}
const good = load('?chunk=000001&id=00000001');
assert.equal(good.appended, true);
assert.ok(good.parsedBody.includes('원본'));
assert.equal(good.back.href, 'report.html#group=output&page=2');
for (const query of ['', '?chunk=../bad&id=00000001', '?chunk=000001&id=x',
                     '?chunk=000001&id=000000010', '?chunk=00001&id=00000001']) {
  const bad = load(query); assert.equal(bad.appended, false);
  assert.ok(bad.loading.textContent.includes('유효하지'));
}
assert.ok(load('?chunk=000001&id=00000001', true).loading.textContent.includes('상세 파일'));
assert.ok(load('?chunk=000001&id=99999999').loading.textContent.includes('해당 레코드'));
console.log('Detail loader: valid, invalid, missing chunk/record, and return state passed.');
