const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const timers = require('node:timers');

async function harness(timerApi, archiveApi) {
  const handlers = {};
  const children = [];
  const events = [];
  let win;
  class Window extends EventEmitter {
    constructor(options) {
      super();
      win = this;
      this.options = options;
      this.webContents = new EventEmitter();
      this.webContents.setWindowOpenHandler = () => {};
      this.webContents.send = (_, event) => events.push(event);
    }
    loadFile() {}
    setProgressBar() {}
    isFocused() { return true; }
    isDestroyed() { return false; }
  }
  const electron = {
    app: { whenReady: () => Promise.resolve(), on: () => {}, getPath: () => 'C:\\books' },
    BrowserWindow: Window,
    ipcMain: { handle: (name, fn) => { handlers[name] = fn; } },
    dialog: { showOpenDialog: async () => ({ canceled: false, filePaths: ['C:\\books'] }) },
    shell: {}, Notification: {}
  };
  const sandbox = {
    __dirname,
    process: { env: {} },
    require: name => {
      if (name === 'electron') return electron;
      if (name === './archive.cjs') return archiveApi || require('./archive.cjs');
      if (name === 'node:timers' && timerApi) return timerApi;
      if (name === 'node:fs') return { existsSync: () => true, statSync: () => ({ isFile: () => true, isDirectory: () => true }), readFileSync: () => '{}', writeFileSync: () => {} };
      if (name === 'node:child_process') return { spawn: () => {
        const child = new EventEmitter();
        child.stdin = new PassThrough();
        child.stdout = new PassThrough();
        child.stderr = new PassThrough();
        child.messages = [];
        child.stdin.on('data', data => child.messages.push(JSON.parse(data.toString())));
        children.push(child);
        return child;
      } };
      return require(name);
    }
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8'), sandbox);
  await Promise.resolve();
  win.webContents.emit('did-finish-load');
  const call = (name, value) => handlers[name]({}, value);
  const respond = (child, value) => child.stdout.write(JSON.stringify({ kind: 'preview_result', value }) + '\n');
  return { call, children, win, respond, events };
}

test('preview authorization, correlation, selection invalidation and sandbox', async () => {
  const { call, children, win, respond } = await harness();
  const child = children[0];
  assert.equal(win.options.webPreferences.sandbox, true);
  assert.equal(win.options.webPreferences.nodeIntegration, false);
  assert.equal(win.options.webPreferences.contextIsolation, true);
  assert.throws(() => call('text-preview', 'auto'), /Select a TXT/);
  call('drop-input', 'C:\\books\\novel.txt');
  assert.throws(() => call('text-preview', 'latin1'), /Unsupported/);
  const old = call('text-preview', 'auto');
  const oldRejected = assert.rejects(old, /superseded/);
  const staleId = child.messages.at(-1).requestId;
  const latest = call('text-preview', 'gbk');
  const id = child.messages.at(-1).requestId;
  await oldRejected;
  respond(child, { requestId: staleId, encoding: 'utf-8', digest: 'old' });
  respond(child, { requestId: id, encoding: 'gbk', digest: 'new', preview: '中文' });
  assert.equal((await latest).encoding, 'gbk');
  await call('pick', 'output');
  const message = { kind: 'checkpoint_status', input: 'C:\\books\\novel.txt', output: 'C:\\books\\novel.epub', encoding: 'gbk', digest: 'new' };
  call('command', message);
  assert.equal(child.messages.at(-1).encoding, 'gbk');
  assert.throws(() => call('command', { ...message, encoding: 'utf-8' }), /Preview and confirm/);
  call('drop-input', 'C:\\books\\another.txt');
  assert.throws(() => call('command', message), /Preview and confirm/);
});

test('parallel worker bounds and validation correlation are checked before forwarding', async () => {
  const { call, children, respond } = await harness();
  const child = children[0];
  for (const workerCount of [0, 4, 1.5, '2', true, null]) {
    assert.throws(() => call('command', { kind: 'start', workerCount }), /Worker count/);
  }
  assert.throws(() => call('command', { kind: 'decision', value: 'accept' }), /request ID/);
  call('command', { kind: 'decision', value: 'retry', requestId: 'synthetic-id' });
  assert.equal(child.messages.at(-1).requestId, 'synthetic-id');
  call('drop-input', 'C:\\books\\novel.txt');
  const preview = call('text-preview', 'utf-8');
  respond(child, { requestId: child.messages.at(-1).requestId, encoding: 'utf-8', digest: 'new' });
  await preview;
  await call('pick', 'output');
  call('command', { kind: 'start', input: 'C:\\books\\novel.txt', output: 'C:\\books\\novel.epub', encoding: 'utf-8', digest: 'new', workerCount: 3 });
  assert.equal(child.messages.at(-1).workerCount, 3);
});

test('preview errors, cancellation and backend failures settle requests', async () => {
  const { call, children, respond } = await harness();
  call('drop-input', 'C:\\books\\novel.txt');
  const child = children[0];
  const bad = call('text-preview', 'utf-8');
  const rejected = assert.rejects(bad, /Invalid bytes/);
  respond(child, { requestId: child.messages.at(-1).requestId, error: 'Invalid bytes' });
  await rejected;
  const cancelled = call('text-preview', 'auto');
  const cancellation = assert.rejects(cancelled, /superseded/);
  call('cancel-text-preview');
  await cancellation;
  const pending = call('text-preview', 'auto');
  const failed = assert.rejects(pending, /Backend exited/);
  child.emit('close', 1);
  await failed;
  assert.throws(() => call('text-preview', 'auto'), /unavailable/);
});

test('archive completion holds the run lock through idle and preserves EPUB on failure', async () => {
  let finish;
  const calls = [];
  const h = await harness(null, {
    captureOriginal: (...args) => { calls.push(args); return { source: args[0] }; },
    archiveOriginal: () => new Promise((_, reject) => { finish = reject; })
  });
  const child = h.children[0];
  h.call('drop-input', 'C:\\books\\novel.txt');
  const preview = h.call('text-preview', 'utf-8');
  h.respond(child, { requestId: child.messages.at(-1).requestId, encoding: 'utf-8', digest: 'hash' });
  await preview;
  await h.call('pick', 'output');
  await h.call('pick', 'archive');
  h.call('command', { kind: 'start', input: 'C:\\books\\novel.txt', output: 'C:\\books\\novel.epub', encoding: 'utf-8', digest: 'hash', moveOriginal: true });
  assert.equal(calls.length, 1);
  const emit = kind => child.stdout.write(JSON.stringify({ kind, value: 'C:\\books\\novel.epub' }) + '\n');
  emit('done');
  emit('idle');
  assert.equal(h.events.some(event => event.kind === 'idle'), false);
  assert.throws(() => h.call('restart-backend'), /active operation/);
  let prevented = false;
  h.win.emit('close', { preventDefault: () => { prevented = true; } });
  assert.equal(prevented, true);
  finish(new Error('locked'));
  await new Promise(resolve => timers.setImmediate(resolve));
  assert.ok(h.events.some(event => event.kind === 'done'));
  assert.ok(h.events.some(event => event.kind === 'archive_warning' && event.value.includes('locked')));
  assert.equal(h.events.at(-1).kind, 'idle');
});

test('successful archive reports destination once, invalidates source and waits for backend idle', async () => {
  let finish;
  let moves = 0;
  const h = await harness(null, { captureOriginal: () => ({}), archiveOriginal: () => { moves++; return new Promise(resolve => { finish = resolve; }); } });
  const child = h.children[0];
  h.call('drop-input', 'C:\\books\\novel.txt');
  const preview = h.call('text-preview', 'utf-8');
  h.respond(child, { requestId: child.messages.at(-1).requestId, encoding: 'utf-8', digest: 'hash' });
  await preview;
  await h.call('pick', 'output');
  h.call('command', { kind: 'start', input: 'C:\\books\\novel.txt', output: 'C:\\books\\novel.epub', encoding: 'utf-8', digest: 'hash', moveOriginal: true });
  for (let i = 0; i < 2; i++) child.stdout.write(JSON.stringify({ kind: 'done', value: 'C:\\books\\novel.epub' }) + '\n');
  assert.equal(moves, 1);
  finish('C:\\completed\\novel.txt');
  await new Promise(resolve => timers.setImmediate(resolve));
  assert.ok(h.events.some(event => event.kind === 'original_archived' && event.value.destination === 'C:\\completed\\novel.txt'));
  assert.throws(() => h.call('restart-backend'), /active operation/);
  child.stdout.write(JSON.stringify({ kind: 'idle' }) + '\n');
  assert.throws(() => h.call('text-preview', 'utf-8'), /Select a TXT/);
});

test('cancelled, incomplete, failed, disabled and EPUB-fix jobs never archive', async () => {
  for (const outcome of ['cancel', 'incomplete', 'error', 'disabled', 'fix']) {
    let moved = false;
    const h = await harness(null, { captureOriginal: () => ({}), archiveOriginal: async () => { moved = true; } });
    const child = h.children[0];
    h.call('drop-input', 'C:\\books\\novel.txt');
    const preview = h.call('text-preview', 'utf-8');
    h.respond(child, { requestId: child.messages.at(-1).requestId, encoding: 'utf-8', digest: 'hash' });
    await preview;
    await h.call('pick', 'output');
    h.call('command', { kind: outcome === 'fix' ? 'fix_epub' : 'start', input: 'C:\\books\\novel.txt', output: 'C:\\books\\novel.epub', encoding: 'utf-8', digest: 'hash', moveOriginal: outcome !== 'disabled' });
    if (outcome === 'cancel') h.call('command', { kind: 'cancel' });
    if (['error', 'incomplete'].includes(outcome)) child.stdout.write(JSON.stringify({ kind: outcome, value: 1 }) + '\n');
    child.stdout.write(JSON.stringify({ kind: 'done', value: 'C:\\books\\novel.epub' }) + '\n');
    await new Promise(resolve => timers.setImmediate(resolve));
    assert.equal(moved, false, outcome);
  }
});

test('old backend close cannot disconnect restarted worker', async () => {
  const { call, children, respond } = await harness();
  call('restart-backend');
  children[0].emit('close', 0);
  call('drop-input', 'C:\\books\\novel.txt');
  const pending = call('text-preview', 'auto');
  const child = children[1];
  respond(child, { requestId: child.messages.at(-1).requestId, encoding: 'utf-8', digest: 'ok' });
  assert.equal((await pending).digest, 'ok');
  await new Promise(resolve => timers.setImmediate(resolve));
});

test('title transport watchdog permits Python timeout cleanup and retains the lock', async () => {
  const scheduled = [];
  const { call, children } = await harness({
    setTimeout: (callback, delay) => { const timer = { callback, delay }; scheduled.push(timer); return timer; },
    clearTimeout: () => {}
  });
  call('drop-input', 'C:\\books\\synthetic.txt');
  const pending = call('suggest-title');
  const rejected = assert.rejects(pending, /during closing Chrome/);
  const child = children[0];
  const id = child.messages.at(-1).requestId;
  assert.ok(scheduled[0].delay > (90 + 10 + 10) * 1000);
  child.stdout.write(JSON.stringify({ kind: 'title_result', value: { requestId: id, error: 'Title translation timed out during closing Chrome.' } }) + '\n');
  await rejected;
  const next = call('suggest-title');
  const cancelled = assert.rejects(next, /waiting for browser cleanup/);
  const nextId = child.messages.at(-1).requestId;
  scheduled[1].callback();
  assert.equal(child.messages.at(-1).kind, 'cancel_title');
  assert.throws(() => call('suggest-title'), /active browser/);
  child.stdout.write(JSON.stringify({ kind: 'title_result', value: { requestId: nextId, error: 'cancelled' } }) + '\n');
  await cancelled;
});

test('title selection authorization, cancellation lock, correlation, offline and restart', async () => {
  const { call, children } = await harness();
  assert.throws(() => call('suggest-title'), /Select a TXT/);
  call('drop-input', 'C:\\books\\synthetic.txt');
  const child = children[0];
  const pending = call('suggest-title');
  const message = child.messages.at(-1);
  assert.equal(message.input, 'C:\\books\\synthetic.txt');
  assert.throws(() => call('suggest-title'), /active browser/);
  assert.throws(() => call('restart-backend'), /cleanup/);
  const rejected = assert.rejects(pending, /cancelled/);
  call('cancel-title');
  assert.throws(() => call('suggest-title'), /active browser/);
  child.stdout.write(JSON.stringify({ kind: 'title_result', value: { requestId: message.requestId - 1, title: 'Stale' } }) + '\n');
  assert.throws(() => call('suggest-title'), /active browser/);
  child.stdout.write(JSON.stringify({ kind: 'title_result', value: { requestId: message.requestId, title: 'Synthetic Title' } }) + '\n');
  await rejected;
  const success = call('suggest-title');
  child.stdout.write(JSON.stringify({ kind: 'title_result', value: { requestId: child.messages.at(-1).requestId, title: 'Synthetic Title' } }) + '\n');
  assert.equal(await success, 'Synthetic Title');
  const offline = call('suggest-title');
  const failed = assert.rejects(offline, /Backend exited/);
  child.emit('close', 1);
  await failed;
  call('restart-backend');
  assert.throws(() => call('suggest-title'), /Select a TXT/);
});
