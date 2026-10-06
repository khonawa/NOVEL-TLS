const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const readline = require('node:readline');
let window;
let worker;
let busy = false;
let result;
let folders = {};
function rememberFolder(kind, selected) {
  folders[kind] = path.dirname(selected);
  fs.writeFileSync(path.join(app.getPath('userData'), 'folders.json'), JSON.stringify(folders));
}
const send = (kind, value) => { if (window && !window.isDestroyed()) window.webContents.send('event', { kind, value }); };

function startWorker() {
  const backend = app.isPackaged ? path.join(process.resourcesPath, 'backend') : path.join(__dirname, '../backend');
  const local = path.join(__dirname, '../.venv/Scripts/python.exe');
  const parent = path.join(__dirname, '../../.venv/Scripts/python.exe');
  const python = process.env.NOVELTL_PYTHON || (fs.existsSync(local) ? local : fs.existsSync(parent) ? parent : 'python');
  worker = spawn(python, ['-u', path.join(backend, 'bridge.py')], { cwd: backend, windowsHide: true });
  readline.createInterface({ input: worker.stdout }).on('line', line => {
    try {
      const event = JSON.parse(line);
      if (event.kind === 'idle' || event.kind === 'command_error') busy = false;
      if (event.kind === 'done') result = String(event.value);
      send(event.kind, event.value);
    } catch { send('log', line); }
  });
  worker.stderr.on('data', data => send('log', data.toString()));
  worker.stdin.on('error', error => send('error', error.message));
  worker.on('error', error => send('error', `Python could not start: ${error.message}`));
  worker.on('close', code => { worker = null; busy = false; send('offline', `Backend exited (${code}). Check Python dependencies.`); });
}

app.whenReady().then(() => {
  try { folders = JSON.parse(fs.readFileSync(path.join(app.getPath('userData'), 'folders.json'), 'utf8')); } catch { folders = {}; }
  window = new BrowserWindow({ width: 1180, height: 820, minWidth: 620, minHeight: 600, backgroundColor: '#f4f6f5', webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true } });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.loadFile(path.join(__dirname, '../src/index.html'));
  window.webContents.once('did-finish-load', startWorker);
  window.on('close', event => {
    if (busy) {
      const answer = dialog.showMessageBoxSync(window, { type: 'question', message: 'Cancel the active translation and close?', buttons: ['Keep translating', 'Cancel and close'], defaultId: 0, cancelId: 0 });
      if (answer === 0) { event.preventDefault(); return; }
    }
    worker?.stdin.end();
  });
});

ipcMain.handle('pick', async (_, kind, name) => {
  if (kind === 'input') {
    const selected = await dialog.showOpenDialog(window, { defaultPath: folders.input, properties: ['openFile'], filters: [{ name: 'Novel text', extensions: ['txt'] }] });
    if (!selected.canceled) rememberFolder('input', selected.filePaths[0]);
    return selected.canceled ? null : selected.filePaths[0];
  }
  if (kind !== 'output') throw new Error('Invalid picker');
  const filename = typeof name === 'string' ? path.basename(name).replace(/[<>:"/\\|?*\x00-\x1f]/g, '_') : 'Translation.epub';
  const selected = await dialog.showSaveDialog(window, { defaultPath: path.join(folders.output || app.getPath('documents'), filename), filters: [{ name: 'EPUB book', extensions: ['epub'] }] });
  if (!selected.canceled) rememberFolder('output', selected.filePath);
  return selected.canceled ? null : selected.filePath;
});
ipcMain.handle('command', (_, message) => {
  if (!worker || !worker.stdin.writable) throw new Error('Python backend is unavailable. Restart after installing dependencies.');
  if (!message || !['start', 'pause', 'resume', 'cancel', 'decision', 'settings'].includes(message.kind)) throw new Error('Invalid command');
  if (message.kind === 'start') {
    if (busy) throw new Error('A run is already active');
    busy = true;
    result = null;
  }
  worker.stdin.write(JSON.stringify(message) + '\n');
});
ipcMain.handle('open-result', async (_, folder) => {
  if (!result || !fs.existsSync(result)) throw new Error('No completed EPUB is available');
  if (folder) shell.showItemInFolder(result);
  else { const error = await shell.openPath(result); if (error) throw new Error(error); }
});
app.on('window-all-closed', () => app.quit());