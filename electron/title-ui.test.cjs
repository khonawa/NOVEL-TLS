const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
  const source = fs.readFileSync(path.join(__dirname, '../src/app.js'), 'utf8');
  const fields = { name: { value: 'Original EN' }, input: { value: 'synthetic.txt' }, 'suggest-title': {}, 'title-status': {}, 'original-name': {} };
  let resolve, reject;
  const context = vm.createContext({
    element: id => fields[id],
    api: { suggestTitle: () => new Promise((yes, no) => { resolve = yes; reject = no; }), cancelTitle: () => Promise.resolve() },
    localStorage: { setItem() {} }, controls() {}, checkForCheckpoint() {}, syncOutput() {}, isFixMode: () => false,
  });
  vm.runInContext('let titleGeneration = 0; let titleWorking = false;\n' + source.slice(source.indexOf('function invalidateTitle()'), source.indexOf('\nloadSettings();')) + '\n' + source.slice(source.indexOf('function outputName()'), source.indexOf('\nfunction getChunkSize()')), context);
  return { fields, context, run: () => vm.runInContext('suggestTitle()', context), resolve: value => resolve(value), reject: value => reject(new Error(value)), invalidate: () => vm.runInContext('invalidateTitle()', context) };
}

test('title success is editable; failure preserves fallback and manual name', async () => {
  const h = harness();
  let pending = h.run();
  h.resolve('Synthetic English Title');
  await pending;
  assert.equal(h.fields.name.value, 'Synthetic English Title');
  h.fields.name.value = 'Manual choice';
  pending = h.run();
  h.reject('Synthetic failure');
  await pending;
  assert.equal(h.fields.name.value, 'Manual choice');
  h.fields.name.value = 'Original EN';
  pending = h.run();
  h.reject('Synthetic failure');
  await pending;
  assert.equal(h.fields.name.value, 'Original EN');
});

test('pending name edits and source/mode invalidation discard stale successes and failures', async () => {
  for (const failure of [false, true]) {
    for (const change of ['name', 'source', 'mode']) {
      const h = harness();
      const pending = h.run();
      if (change === 'name') h.fields.name.value = 'Manual while pending';
      if (change === 'source') h.fields.input.value = 'another.txt';
      h.invalidate();
      if (failure) h.reject('Late failure'); else h.resolve('Late title');
      await pending;
      assert.equal(h.fields.name.value, change === 'name' ? 'Manual while pending' : 'Original EN');
      assert.equal(h.fields['title-status'].textContent, '');
    }
  }
});

test('renderer sanitizes Windows device names, extension and long titles', () => {
  const h = harness();
  for (const [name, expected] of [['CON.txt', '_CON.txt'], ['LPT¹.foo', '_LPT¹.foo'], ['A:B?.epub', 'A_B_'], ['Book... ', 'Book']]) {
    h.fields.name.value = name;
    assert.equal(vm.runInContext('outputName()', h.context), expected);
  }
  h.fields.name.value = '😀'.repeat(200);
  assert.equal(vm.runInContext('outputName()', h.context).length, 180);
});
