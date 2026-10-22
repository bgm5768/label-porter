const assert = require('node:assert/strict');
const vm = require('node:vm');
const script = require('node:fs').readFileSync(0, 'utf8');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.listeners = {}; this.value = ''; this.dataset = {}; this.options = []; }
  get selectedIndex() { return this.options.findIndex(o => o.value === this.value); }
  append(child) { this.children.push(child); }
  replaceChildren(fragment) { this.children = fragment.children; if (this.tag === 'select') this.options = this.children; }
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
  e['report-field'] = new Element('select');
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

// Field discovery uses real change rows, not paths mentioned in text or proposed changes.
const fieldRows = [], oddPath = '/태그~1이름/<img onerror=bad()>', listPath = '/Segmentation/eojeols';
for (let i = 0; i < 61; i++) {
  const link = `detail.html?chunk=000001&id=${i}`, paths = ['/label'];
  if (i % 2 === 0) paths.push(listPath);
  const base = {'상태':'changed',status:'updated','원본':`source-${i}.json`,'보완':'result.json',
    '키':'same-key','위치':'doc:0','사유':'changed',link,issues:0};
  fieldRows.push({...base,groups:['source','output','completed']});
  fieldRows.push({...base,groups:['result']});
  for (const path of paths) fieldRows.push({...base,groups:['changes'],'위치':path});
}
fieldRows.push({'상태':'review',status:'review_required','원본':'held.json','키':'same-key',
  '위치':'doc:0','사유':'proposed /label /unapplied',link:'review/held.html',groups:['source','output','review']});
for (const path of ['', oddPath]) {
  fieldRows.push({'상태':'changed',status:'updated','원본':'special.json','키':'special',
    '위치':path,'사유':'changed',link:'detail.html?chunk=000001&id=special',groups:['changes']});
}
const filtered = setup(fieldRows), f = filtered.e, dropdown = f['report-field'];
assert.equal(dropdown.disabled, false);
assert.equal(dropdown.options.length, 5);
assert.equal(dropdown.options.find(o => o.value === '/label').textContent, '/label · 61건');
assert.equal(dropdown.options.find(o => o.value === listPath).textContent, listPath + ' · 31건');
assert.equal(dropdown.options.find(o => o.value === oddPath).children.length, 0);
assert.equal(dropdown.options.some(o => o.value === '/unapplied'), false);
const duplicateChange = fieldRows.find(row => row.groups.includes('changes') && row['위치'] === '/label');
assert.equal(setup([...fieldRows, duplicateChange]).e['report-field'].options.find(o => o.value === '/label').textContent, '/label · 61건');
f['review-last'].fire('click');
dropdown.value = '/label'; dropdown.fire('change');
assert.equal(f['review-page'].value, '1');
assert.equal(f['review-list'].children.length, 25);
assert.equal(f['review-range'].textContent, '1–25 / 61건');
f['review-last'].fire('click');
assert.equal(f['review-list'].children.length, 11);
const back = setup(fieldRows, filtered.location.hash);
assert.equal(back.e['report-field'].value, '/label');
assert.equal(back.e['review-page'].value, '3');
assert.equal(back.e['review-list'].children.length, 11);
assert.ok(back.e['review-list'].children[0].children[6].children[0].href.includes('field=%2Flabel'));
filtered.buttons.find(b => b.dataset.group === 'changes').fire('click');
assert.equal(dropdown.value, '/label');
assert.equal(f['review-range'].textContent, '1–25 / 61건');
assert.ok(f['review-list'].children.every(row => row.children[4].textContent === '/label'));
filtered.buttons.find(b => b.dataset.group === 'result').fire('click');
assert.equal(f['review-range'].textContent, '1–25 / 61건');
dropdown.value = listPath; dropdown.fire('change');
f['review-search'].value = 'source-60.json'; f['review-search'].fire('input');
assert.equal(f['review-list'].children.length, 1);
f['report-status'].value = 'unchanged'; f['report-status'].fire('change');
assert.equal(f['review-list'].children[0].children[0].textContent, '검색 결과가 없습니다.');
filtered.buttons.find(b => b.dataset.group === 'review').fire('click');
assert.equal(f['review-list'].children[0].children[0].textContent, '검색 결과가 없습니다.');
dropdown.value = ''; dropdown.fire('change');
assert.equal(f['review-list'].children[0].children[1].textContent, 'held.json');
filtered.buttons.find(b => b.dataset.group === 'changes').fire('click');
dropdown.value = '@root'; dropdown.fire('change');
assert.equal(f['review-list'].children.length, 1);
assert.equal(f['review-list'].children[0].children[4].textContent, '');
assert.equal(setup(fieldRows, filtered.location.hash).e['report-field'].value, '@root');
assert.equal(setup(fieldRows, '#field=%2Funknown').e['report-field'].value, '');
assert.equal(setup([]).e['report-field'].disabled, true);
assert.equal(setup(rows).e['report-field'].options[0].textContent, '변경된 필드 없음');
console.log('changed fields: discovery, counts, same-key isolation, category/status/search intersection, paging, return state, root and safe text passed');
