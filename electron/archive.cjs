const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');

const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const sameFile = (a, b) => a.dev === b.dev && a.ino === b.ino && a.size === b.size && a.mtimeMs === b.mtimeMs && a.ctimeMs === b.ctimeMs;

function captureOriginal(source, folder, expectedDigest) {
  if (!folder) throw new Error('Choose a Completed Originals folder first.');
  if (!fs.lstatSync(source).isFile()) throw new Error('Original must be a regular file, not a symbolic link.');
  const input = fs.realpathSync(source);
  const destination = fs.realpathSync(folder);
  if (!fs.statSync(destination).isDirectory()) throw new Error('Choose an accessible Completed Originals folder.');
  if (path.dirname(input).toLowerCase() === destination.toLowerCase()) throw new Error('Completed Originals must differ from the source folder.');
  const stat = fs.lstatSync(input);
  if (!stat.isFile()) throw new Error('Original must be a regular file.');
  const hash = digest(fs.readFileSync(input));
  if (hash !== expectedDigest || !sameFile(stat, fs.lstatSync(input))) throw new Error('Source changed since preview. Refresh and confirm the source again.');
  return { source: input, folder: destination, stat, hash };
}

async function archiveOriginal(job, io = fs.promises) {
  const unchanged = async () => sameFile(job.stat, await io.lstat(job.source)) && digest(await io.readFile(job.source)) === job.hash;
  if (!await unchanged()) throw new Error('Original changed during translation; it was not moved.');
  const parsed = path.parse(job.source);
  let target;
  for (let number = 0; ; number++) {
    target = path.join(job.folder, `${parsed.name}${number ? ` (${number})` : ''}${parsed.ext}`);
    try {
      await io.copyFile(job.source, target, fs.constants.COPYFILE_EXCL);
      break;
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
    }
  }
  try {
    if (digest(await io.readFile(target)) !== job.hash || !await unchanged()) throw new Error('Original or archive copy changed; original retained.');
    const handle = await io.open(target, 'r+');
    try { await handle.sync(); } finally { await handle.close(); }
    if (!await unchanged()) throw new Error('Original changed while archiving; original retained.');
  } catch (error) {
    await io.unlink(target).catch(() => {});
    throw error;
  }
  try {
    await io.unlink(job.source);
  } catch (error) {
    throw new Error(`Verified copy saved at ${target}, but original could not be removed: ${error.message}`);
  }
  return target;
}

module.exports = { captureOriginal, archiveOriginal };
