const assert = require('node:assert/strict');
const vm = require('node:vm');
const script = require('node:fs').readFileSync(0, 'utf8');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.listeners = {}; this.value = ''; }
  append(child) { this.children.push(child); }
  replaceChildren(fragment) { this.children = fragment.children; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  fire(type, event = {}) { this.listeners[type](event); }
}
function setup(rows, hash = '') {
  const elements = {};
  for (const name of ['data', 'search', 'size', 'page', 'list', 'range', 'pages', 'first', 'prev', 'next', 'last'])
    elements[name] = new Element('div');
  elements.data.textContent = JSON.stringify(rows);
  const location = {hash};
  const document = {
    getElementById: id => elements[id.slice(7)],
    createElement: tag => new Element(tag),
    createDocumentFragment: () => new Element('fragment')
  };
  const context = {document, location, URLSearchParams, window: {addEventListener() {}},
    history: {replaceState: (_, __, hash) => location.hash = hash},
    setTimeout: fn => { fn(); return 1; }, clearTimeout() {}};
  vm.runInNewContext(script, context);
  return {e: elements, location};
}
const rows = Array.from({length: 105}, (_, i) => ({
  '상태': '검토', '원본': `file-${i}`, '키': `key-${i}`, '사유': i === 104 ? '</script><img onerror=bad()>' : 'reason',
  link: `review/${i}.html`, issues: 1
}));
const {e, location} = setup(rows);
assert.equal(e.list.children.length, 25);
assert.equal(e.first.disabled, true);
e.next.fire('click');
assert.equal(e.list.children[0].children[1].textContent, 'file-25');
e.last.fire('click');
assert.equal(e.list.children.length, 5);
assert.equal(e.next.disabled, true);
e.search.value = 'key-104'; e.search.fire('input');
assert.equal(e.list.children.length, 1);
assert.equal(e.page.value, '1');
assert.equal(e.list.children[0].children[3].children.length, 0);
assert.equal(e.list.children[0].children[3].textContent, '</script><img onerror=bad()>');
assert.ok(e.list.children[0].children[4].children[0].href.includes('q=key-104'));
e.search.value = 'not-found'; e.search.fire('input');
assert.equal(e.next.disabled, true); assert.equal(e.prev.disabled, true);
assert.equal(e.list.children[0].children[0].textContent, '검색 결과가 없습니다.');
e.search.value = ''; e.search.fire('input');
e.size.value = '100'; e.size.fire('change');
assert.equal(e.list.children.length, 100);
e.page.value = '999'; e.page.fire('keydown', {key: 'Enter'});
assert.equal(e.page.value, '2'); assert.equal(e.list.children.length, 5);
e.page.value = '-5'; e.page.fire('change'); assert.equal(e.page.value, '1');
e.size.value = '25'; e.size.fire('change'); e.next.fire('click'); e.next.fire('click');
const restored = setup(rows, location.hash);
assert.equal(restored.e.page.value, '3');
assert.equal(restored.e.list.children[0].children[1].textContent, 'file-50');
const empty = setup([]);
assert.equal(empty.e.next.disabled, true);
assert.equal(empty.e.list.children[0].children[0].textContent, '라벨 검토 항목이 없습니다.');
console.log('pagination: boundaries, full search, page size, escaping, restoration and empty state passed');
