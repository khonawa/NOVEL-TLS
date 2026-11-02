const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { captureOriginal, archiveOriginal } = require('./archive.cjs');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noveltl-archive-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, 'novel.txt');
  const folder = path.join(root, 'completed');
  fs.mkdirSync(folder);
  fs.writeFileSync(source, 'Original 中文');
  const hash = createHash('sha256').update(fs.readFileSync(source)).digest('hex');
  return { root, source, folder, hash, job: captureOriginal(source, folder, hash) };
}

test('verified copy moves the original without overwriting collisions', async t => {
  const f = fixture(t);
  fs.writeFileSync(path.join(f.folder, 'novel.txt'), 'existing');
  const target = await archiveOriginal(f.job);
  assert.equal(target, path.join(f.folder, 'novel (1).txt'));
  assert.equal(fs.readFileSync(target, 'utf8'), 'Original 中文');
  assert.equal(fs.readFileSync(path.join(f.folder, 'novel.txt'), 'utf8'), 'existing');
  assert.equal(fs.existsSync(f.source), false);
});

test('missing destination, same folder and changed preview are rejected', t => {
  const f = fixture(t);
  assert.throws(() => captureOriginal(f.source, '', f.hash), /Choose/);
  assert.throws(() => captureOriginal(f.source, f.root, f.hash), /differ/);
  assert.throws(() => captureOriginal(f.source, f.folder, 'wrong'), /changed/);
});

test('source changes after start preserve the original', async t => {
  const f = fixture(t);
  fs.writeFileSync(f.source, 'new content');
  await assert.rejects(archiveOriginal(f.job), /changed/);
  assert.equal(fs.readFileSync(f.source, 'utf8'), 'new content');
  assert.deepEqual(fs.readdirSync(f.folder), []);
});

test('copy failure never deletes the source', async t => {
  const f = fixture(t);
  await assert.rejects(archiveOriginal(f.job, { ...fs.promises, copyFile: async () => { throw new Error('disk full'); } }), /disk full/);
  assert.ok(fs.existsSync(f.source));
});

test('verification failure removes the bad copy, not the source', async t => {
  const f = fixture(t);
  await assert.rejects(archiveOriginal(f.job, { ...fs.promises, copyFile: async (_, target) => fs.promises.writeFile(target, 'corrupt', { flag: 'wx' }) }), /changed/);
  assert.ok(fs.existsSync(f.source));
  assert.deepEqual(fs.readdirSync(f.folder), []);
});

test('delete failure retains both verified copy and source with actionable warning', async t => {
  const f = fixture(t);
  await assert.rejects(archiveOriginal(f.job, { ...fs.promises, unlink: async () => { throw new Error('locked'); } }), /Verified copy saved.*locked/);
  assert.ok(fs.existsSync(f.source));
  assert.equal(fs.readFileSync(path.join(f.folder, 'novel.txt'), 'utf8'), 'Original 中文');
});
