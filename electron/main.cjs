const { app, BrowserWindow, ipcMain, dialog, shell, Notification } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const readline = require('node:readline');
const { setTimeout, clearTimeout } = require('node:timers');
let window;
let worker;
let busy = false;
let result;
let folders = {};
let allowedPaths = {};
const encodings = new Set(['auto', 'utf-8', 'utf-16-le', 'utf-16-be', 'utf-32-le', 'utf-32-be', 'gb2312', 'gbk', 'gb18030', 'big5', 'big5hkscs']);
let previewSequence = 0;
let pendingPreview;
let validatedPreview;
let titleSequence = 0;
let pendingTitle;
function cancelTitle(reason = 'Title suggestion cancelled.') {
  if (!pendingTitle) return;
  pendingTitle.cancelReason = reason;
  if (worker?.stdin.writable) worker.stdin.write(JSON.stringify({ kind: 'cancel_title' }) + '\n');
  // Retain the lock until Python confirms subprocess/browser cleanup.
}
function finishTitle(error, value) {
  if (!pendingTitle) return;
  clearTimeout(pendingTitle.timer);
  if (error || pendingTitle.cancelReason) pendingTitle.reject(new Error(pendingTitle.cancelReason || error));
  else pendingTitle.resolve(value);
  pendingTitle = null;
}
function invalidatePreview(reason = 'Preview superseded. Preview and confirm the source again.') {
  validatedPreview = null;
  if (pendingPreview) {
    clearTimeout(pendingPreview.timer);
    pendingPreview.reject(new Error(reason));
    pendingPreview = null;
  }
  if (worker?.stdin.writable) worker.stdin.write(JSON.stringify({ kind: 'cancel_preview' }) + '\n');
}
function selectInput(file) {
  cancelTitle('Source selection changed.');
  invalidatePreview();
  allowedPaths.input = file;
}
function requirePreview(message) {
  if (!validatedPreview || validatedPreview.input !== message.input || validatedPreview.encoding !== message.encoding || validatedPreview.digest !== message.digest) {
    throw new Error('Preview and confirm the selected text encoding first.');
  }
}
function rememberFolder(kind, selected) {
  folders[kind] = path.dirname(selected);
  try {
    fs.writeFileSync(path.join(app.getPath('userData'), 'folders.json'), JSON.stringify(folders));
  } catch (error) {
    send('log', `Could not save folder preference: ${error.message}`);
  }
}
const send = (kind, value) => { if (window && !window.isDestroyed()) window.webContents.send('event', { kind, value }); };

function startWorker() {
  const backend = app.isPackaged ? path.join(process.resourcesPath, 'backend') : path.join(__dirname, '../backend');
  const local = path.join(__dirname, '../.venv/Scripts/python.exe');
  const parent = path.join(__dirname, '../../.venv/Scripts/python.exe');
  const python = process.env.NOVELTL_PYTHON || (fs.existsSync(local) ? local : fs.existsSync(parent) ? parent : 'python');
  const child = spawn(python, ['-u', path.join(backend, 'bridge.py')], { cwd: backend, windowsHide: true });
  worker = child;
  readline.createInterface({ input: child.stdout }).on('line', line => {
    if (worker !== child) return;
    try {
      const event = JSON.parse(line);
      if (event.kind === 'title_result') {
        if (pendingTitle?.id !== event.value.requestId) return;
        finishTitle(event.value.error, event.value.title);
        send('title_idle', null);
        return;
      }
      if (event.kind === 'preview_result') {
        const pending = pendingPreview;
        if (!pending || pending.id !== event.value.requestId) return;
        clearTimeout(pending.timer);
        pendingPreview = null;
        if (event.value.error) pending.reject(new Error(event.value.error));
        else {
          validatedPreview = { ...event.value, input: pending.input };
          pending.resolve(event.value);
        }
        return;
      }
      if (event.kind === 'idle' || event.kind === 'command_error') busy = false;
      if (event.kind === 'done') {
        result = String(event.value);
        window?.setProgressBar(-1);
        if (!window?.isFocused() && Notification.isSupported()) {
          new Notification({ title: 'NovelTL', body: 'Translation complete' }).show();
        }
      }
      if (event.kind === 'incomplete' || event.kind === 'error') window?.setProgressBar(-1);
      if (event.kind === 'validation') {
        window?.setProgressBar(2);
        if (!window?.isFocused() && Notification.isSupported()) {
          new Notification({ title: 'NovelTL', body: 'Translation needs review' }).show();
        }
      }
      if (event.kind === 'progress' && typeof event.value === 'number') window?.setProgressBar(event.value);
      send(event.kind, event.value);
    } catch { send('log', line); }
  });
  readline.createInterface({ input: child.stderr }).on('line', line => { if (worker === child) send('log', line); });
  const failed = message => {
    if (worker !== child) return;
    worker = null;
    finishTitle(message);
    invalidatePreview(message);
    busy = false;
    send('offline', message);
  };
  child.stdin.on('error', error => failed(error.message));
  child.on('error', error => failed(`Python could not start: ${error.message}`));
  child.on('close', code => failed(`Backend exited (${code}). Check Python dependencies.`));
}

app.whenReady().then(() => {
  try { folders = JSON.parse(fs.readFileSync(path.join(app.getPath('userData'), 'folders.json'), 'utf8')); } catch { folders = {}; }
  window = new BrowserWindow({ width: 1180, height: 820, minWidth: 620, minHeight: 600, backgroundColor: '#f4f6f5', webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true } });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.loadFile(path.join(__dirname, '../src/index.html'));
  window.webContents.once('did-finish-load', startWorker);
  window.on('close', event => {
    if (busy || pendingTitle) {
      const answer = dialog.showMessageBoxSync(window, { type: 'question', message: 'Cancel the active translation and close?', buttons: ['Keep translating', 'Cancel and close'], defaultId: 0, cancelId: 0 });
      if (answer === 0) { event.preventDefault(); return; }
    }
    worker?.stdin.end();
  });
});

ipcMain.handle('pick', async (_, kind) => {
  if (kind === 'input') {
    const selected = await dialog.showOpenDialog(window, { defaultPath: folders.input, properties: ['openFile'], filters: [{ name: 'Novel text', extensions: ['txt'] }] });
    if (!selected.canceled) {
      rememberFolder('input', selected.filePaths[0]);
      selectInput(selected.filePaths[0]);
    }
    return selected.canceled ? null : selected.filePaths[0];
  }
  if (kind === 'epub') {
    const selected = await dialog.showOpenDialog(window, { defaultPath: folders.epub || folders.output, properties: ['openFile'], filters: [{ name: 'EPUB book', extensions: ['epub'] }] });
    if (!selected.canceled) {
      rememberFolder('epub', selected.filePaths[0]);
      selectInput(selected.filePaths[0]);
    }
    return selected.canceled ? null : selected.filePaths[0];
  }
  if (kind === 'image') {
    const selected = await dialog.showOpenDialog(window, { defaultPath: folders.cover, properties: ['openFile'], filters: [{ name: 'Images', extensions: ['png', 'jpg', 'jpeg', 'webp'] }] });
    if (!selected.canceled) {
      rememberFolder('cover', selected.filePaths[0]);
      allowedPaths.cover = selected.filePaths[0];
    }
    return selected.canceled ? null : selected.filePaths[0];
  }
  if (kind !== 'output') throw new Error('Invalid picker');
  const selected = await dialog.showOpenDialog(window, { defaultPath: folders.output || app.getPath('documents'), properties: ['openDirectory'] });
  if (!selected.canceled) {
    folders.output = selected.filePaths[0];
    try {
      fs.writeFileSync(path.join(app.getPath('userData'), 'folders.json'), JSON.stringify(folders));
    } catch (error) {
      send('log', `Could not save folder preference: ${error.message}`);
    }
    allowedPaths.output = selected.filePaths[0];
  }
  return selected.canceled ? null : selected.filePaths[0];
});
ipcMain.handle('default-output', () => {
  const folder = folders.output;
  if (!folder || !fs.statSync(folder, { throwIfNoEntry: false })?.isDirectory()) return null;
  allowedPaths.output = folder;
  return folder;
});
ipcMain.handle('drop-input', (_, filePath) => {
  const ext = typeof filePath === 'string' ? path.extname(filePath).toLowerCase() : '';
  if (typeof filePath !== 'string' || !path.isAbsolute(filePath) || !['.txt', '.epub'].includes(ext)) throw new Error('Only .txt or .epub files can be dropped');
  if (!fs.statSync(filePath, { throwIfNoEntry: false })?.isFile()) throw new Error('Dropped file is not accessible');
  rememberFolder(ext === '.epub' ? 'epub' : 'input', filePath);
  selectInput(filePath);
  return filePath;
});
ipcMain.handle('cover-preview', () => {
  const file = allowedPaths.cover;
  if (!file || !fs.statSync(file, { throwIfNoEntry: false })?.isFile()) throw new Error('No cover image selected');
  const ext = path.extname(file).toLowerCase();
  const mime = ext === '.png' ? 'image/png' : ext === '.webp' ? 'image/webp' : 'image/jpeg';
  return `data:${mime};base64,${fs.readFileSync(file).toString('base64')}`;
});
ipcMain.handle('cancel-title', () => cancelTitle());
ipcMain.handle('suggest-title', () => {
  if (busy || pendingTitle) throw new Error('Wait for the active browser operation to finish.');
  if (!worker?.stdin.writable) throw new Error('Python backend is unavailable.');
  const input = allowedPaths.input;
  if (!input || path.extname(input).toLowerCase() !== '.txt') throw new Error('Select a TXT file first.');
  const id = ++titleSequence;
  return new Promise((resolve, reject) => {
    // Python owns the 90s operation budget plus up to 20s process-tree cleanup.
    const timer = setTimeout(() => cancelTitle('Title suggestion timed out; waiting for browser cleanup.'), 115000);
    pendingTitle = { id, resolve, reject, timer };
    worker.stdin.write(JSON.stringify({ kind: 'suggest_title', input, requestId: id }) + '\n');
  });
});
ipcMain.handle('cancel-text-preview', () => invalidatePreview());
ipcMain.handle('text-preview', (_, encoding) => {
  if (busy) throw new Error('Wait for the active run to finish.');
  if (!encodings.has(encoding)) throw new Error('Unsupported encoding');
  if (!worker?.stdin.writable) throw new Error('Python backend is unavailable. Restart it and select the source again.');
  const input = allowedPaths.input;
  if (!input || path.extname(input).toLowerCase() !== '.txt') throw new Error('Select a TXT file first.');
  invalidatePreview();
  const id = ++previewSequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => invalidatePreview('Preview timed out. Retry or select a smaller source.'), 120000);
    pendingPreview = { id, input, resolve, reject, timer };
    worker.stdin.write(JSON.stringify({ kind: 'preview', requestId: id, input, encoding }) + '\n');
  });
});
ipcMain.handle('command', (_, message) => {
  if (!worker || !worker.stdin.writable) throw new Error('Python backend is unavailable. Restart after installing dependencies.');
  if (!message || !['start', 'fix_epub', 'pause', 'resume', 'cancel', 'decision', 'settings', 'checkpoint_status'].includes(message.kind)) throw new Error('Invalid command');
  if (message.kind === 'start' || message.kind === 'checkpoint_status') requirePreview(message);
  if (message.kind === 'start' || message.kind === 'fix_epub' || message.kind === 'checkpoint_status') {
    if (busy || pendingTitle) throw new Error('A browser operation is already active');
    if (message.input !== allowedPaths.input) throw new Error('Source path does not match the file picker selection');
    if (typeof message.output !== 'string' || !allowedPaths.output || path.dirname(message.output) !== allowedPaths.output || path.extname(message.output).toLowerCase() !== '.epub') throw new Error('Destination must be an .epub file in the selected folder');
    if (path.resolve(message.output).toLowerCase() === path.resolve(message.input).toLowerCase()) throw new Error('Destination must differ from the source file');
    if (message.cover?.mode === 'import' && message.cover.imagePath !== allowedPaths.cover) throw new Error('Cover image does not match the file picker selection');
    if (message.kind !== 'checkpoint_status') {
      busy = true;
      result = null;
    }
  }
  worker.stdin.write(JSON.stringify(message) + '\n');
});
ipcMain.handle('open-result', async (_, folder) => {
  if (!result || !fs.existsSync(result)) throw new Error('No completed EPUB is available');
  if (folder) shell.showItemInFolder(result);
  else { const error = await shell.openPath(result); if (error) throw new Error(error); }
});
ipcMain.handle('restart-backend', () => {
  if (busy || pendingTitle) throw new Error('Cancel the active operation and wait for cleanup before restarting.');
  invalidatePreview('Backend restarted. Select and preview the source again.');
  if (worker) {
    worker.stdin.end();
    worker = null;
  }
  busy = false;
  result = null;
  allowedPaths.input = null;
  allowedPaths.output = folders.output && fs.statSync(folders.output, { throwIfNoEntry: false })?.isDirectory() ? folders.output : null;
  startWorker();
});
app.on('window-all-closed', () => app.quit());