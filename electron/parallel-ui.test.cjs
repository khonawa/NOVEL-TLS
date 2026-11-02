const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/app.js'), 'utf8');

function harness() {
  const fields = {};
  const buttons = [{}, {}, {}];
  const node = () => ({ textContent: '', children: [], append(...children) { this.children.push(...children); }, replaceChildren(...children) { this.children = children; }, close() { this.open = false; }, showModal() { this.open = true; } });
  let resolve;
  let sent;
  const context = vm.createContext({
    element: id => fields[id] ||= node(),
    document: { createElement: node, querySelectorAll: () => buttons },
    command: message => { sent = message; return new Promise(done => { resolve = done; }); },
  });
  vm.runInContext('let validationRequestId = null; let decisionPending = false; const workerStates = new Map();\n' + source.slice(source.indexOf('function renderWorkers()'), source.indexOf('function invalidateTitle()')) + '\n' + source.slice(source.indexOf('async function sendDecision('), source.indexOf('async function restartBackend(')), context);
  return { fields, buttons, context, run: code => vm.runInContext(code, context), resolve: value => resolve(value), sent: () => sent };
}

test('validation decisions are correlated and double clicks suppressed', async () => {
  const h = harness();
  h.run("validationRequestId = 'first'");
  const pending = h.run("sendDecision('accept')");
  assert.equal(h.sent().requestId, 'first');
  assert.equal(h.sent().value, 'accept');
  await h.run("sendDecision('skip')");
  assert.equal(h.sent().value, 'accept');
  h.resolve(true);
  await pending;
  assert.equal(h.run('validationRequestId'), null);
});

test('late decision acknowledgement cannot close the next prompt', async () => {
  const h = harness();
  h.run("validationRequestId = 'first'");
  const pending = h.run("sendDecision('retry')");
  h.run("validationRequestId = 'second'; decisionPending = false; element('validation').showModal()");
  h.resolve(true);
  await pending;
  assert.equal(h.run('validationRequestId'), 'second');
  assert.equal(h.fields.validation.open, true);
});

test('failed decision enables retry and cancellation invalidates the prompt', async () => {
  const h = harness();
  h.run("validationRequestId = 'first'");
  const pending = h.run("sendDecision('retry')");
  h.resolve(false);
  await pending;
  assert.equal(h.run('decisionPending'), false);
  assert.ok(h.buttons.every(button => !button.disabled));
  h.run('clearValidation()');
  assert.equal(h.run('validationRequestId'), null);
});

test('worker display sorts workers and replaces independent chunk progress', () => {
  const h = harness();
  h.run("workerStates.set(2, {workerId: 2, index: 5, status: 'Translating', progress: 0.42}); workerStates.set(1, {workerId: 1, index: 4, status: 'Waiting for desktop'}); renderWorkers()");
  assert.equal(h.fields.workers.children.length, 2);
  assert.equal(h.fields.workers.children[0].children[0].textContent, 'Worker 1');
  assert.equal(h.fields.workers.children[1].children[1].textContent, 'Chunk 5 · Translating · 42%');
  h.run("workerStates.set(2, {workerId: 2, index: 6, status: 'Translating'}); renderWorkers()");
  assert.equal(h.fields.workers.children[1].children[1].textContent, 'Chunk 6 · Translating');
});
