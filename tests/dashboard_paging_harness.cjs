const assert = require('node:assert/strict');
const vm = require('node:vm');
const script = require('node:fs').readFileSync(0, 'utf8');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.listeners = {}; this.value = ''; this.dataset = {}; this.options = []; }
  get selectedIndex() { return this.options.findIndex(o => o.value === this.value); }
  append(child) { this.children.push(child); }
  replaceChildren(fragment) { this.children = fragment.children; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  fire(type, event = {preventDefault() {}}) { this.listeners[type](event); }
  setAttribute() {}
  scrollIntoView() {}
}
function setup(rows, hash = '') {
  const e = {};
  for (const name of ['data','search','size','page','list','range','pages','first','prev','next','last'])
    e['review-' + name] = new Element('div');
  for (const name of ['report-categories','report-category','report-status','list-title','records'])
    e[name] = new Element('div');
  const groups = ['source','result','output','completed','review','unmatched','changes','excluded','files'];
  e['report-category'].options = groups.map(value => ({value, text: value}));
  e['report-status'].options = ['', 'updated', 'unchanged'].map(value => ({value, text: value}));
  e['review-data'].textContent = JSON.stringify(rows);
  const buttons = groups.map(group => { const b = new Element('a'); b.dataset.group = group; return b; });
  const location = {hash};
  vm.runInNewContext(script, {document: {
    getElementById: id => e[id], querySelectorAll: () => buttons,
    createElement: tag => new Element(tag), createDocumentFragment: () => new Element('fragment')
  }, location, URLSearchParams, window: {addEventListener() {}},
    history: {replaceState: (_, __, hash) => location.hash = hash},
    setTimeout: fn => { fn(); return 1; }, clearTimeout() {}});
  return {e, buttons, location};
}
const rows = Array.from({length: 20001}, (_, i) => ({
  '상태': i % 2 ? 'changed' : 'same', status: i % 2 ? 'updated' : 'unchanged',
  '원본': 'original-' + i, '보완': 'C:\\private\\corrected.json', '키': 'id-' + i,
  '위치': '/label', '사유': i === 19999 ? '</script><img onerror=bad()>' : 'reason',
  groups: ['source','output','completed'], link: 'detail.html?chunk=000001&id=00000001', issues: 0
}));
rows.push({'상태':'orphan','원본':'','키':'missing','사유':'no source',groups:['result','unmatched'],status:'orphan_result',link:'review/000001.html'});
const {e, buttons, location} = setup(rows);
assert.equal(e['review-list'].children.length, 25);
buttons.find(b => b.dataset.group === 'completed').fire('click');
e['review-last'].fire('click');
assert.equal(e['review-list'].children.length, 1);
e['report-status'].value = 'updated'; e['report-status'].fire('change');
assert.equal(e['review-page'].value, '1');
assert.equal(e['review-list'].children[0].children[1].textContent, 'original-1');
assert.equal(e['review-list'].children[0].children[2].textContent, 'corrected.json');
e['review-search'].value = 'id-19999'; e['review-search'].fire('input');
assert.equal(e['review-list'].children.length, 1);
assert.equal(e['review-list'].children[0].children[5].textContent, '</script><img onerror=bad()>');
assert.equal(e['review-list'].children[0].children[5].children.length, 0);
assert.ok(e['review-list'].children[0].children[6].children[0].href.includes('group=completed'));
const restored = setup(rows, location.hash);
assert.equal(restored.e['report-category'].value, 'completed');
assert.equal(restored.e['report-status'].value, 'updated');
assert.equal(restored.e['review-list'].children.length, 1);
buttons.find(b => b.dataset.group === 'unmatched').fire('click');
assert.equal(e['review-search'].value, '');
assert.equal(e['review-list'].children.length, 1);
assert.equal(e['review-list'].children[0].children[3].textContent, 'missing');
console.log('20k rows: categories, status, search, pagination, safe text, detail return state passed');
