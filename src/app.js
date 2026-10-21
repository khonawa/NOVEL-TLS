const element = id => document.getElementById(id);
const api = window.desktop;
let ready = false;
let busy = false;
let paused = false;
let started = 0;
let logs = [];
let currentStats = { speed: null, eta: null, currentPct: 0 };
let outputFolder = '';

loadSettings();
applyTheme();
element('night').onchange = () => { applyTheme(); localStorage.setItem('theme', document.documentElement.dataset.theme); };
element('original-name').onchange = () => {
  localStorage.setItem('original-name', element('original-name').checked);
  if (element('original-name').checked && sourceName()) element('name').value = sourceName() + ' EN';
  syncOutput();
};
element('name').oninput = () => { element('original-name').checked = false; localStorage.setItem('original-name', 'false'); syncOutput(); };
element('preset').onchange = () => { localStorage.setItem('preset', element('preset').value); checkForCheckpoint(); };
for (const id of ['scroll-interval', 'scroll-slider']) element(id).oninput = syncScroll;
element('chinese-limit').oninput = () => {
  if (element('chinese-limit').checkValidity()) localStorage.setItem('chinese-limit', element('chinese-limit').value);
  controls();
};
for (const kind of ['input', 'output']) element(`pick-${kind}`).onclick = () => pickFile(kind);
element('start').onclick = start;
element('pause').onclick = togglePause;
element('cancel').onclick = cancel;
element('retry').onclick = retry;
element('open').onclick = () => openResult(false);
element('folder').onclick = () => openResult(true);
element('clear').onclick = () => { logs = []; element('log').textContent = ''; };
element('restart-backend').onclick = restartBackend;
element('export-diagnostics').onclick = exportDiagnostics;
element('validation').addEventListener('cancel', event => event.preventDefault());
for (const button of document.querySelectorAll('[data-decision]')) button.onclick = () => sendDecision(button.dataset.decision);
setInterval(updateElapsed, 1000);
setupDragDrop();
if (api) {
  api.onEvent(handleEvent);
  api.defaultOutput().then(folder => { if (folder) { outputFolder = folder; syncOutput(); } });
} else {
  element('connection').textContent = 'Browser preview - translation unavailable';
  element('notice').textContent = 'No desktop backend connection. Open the NovelTL desktop window launched by npm start.';
}
controls();

function loadSettings() {
  element('preset').value = localStorage.getItem('preset') || '275000';
  element('night').checked = localStorage.getItem('theme') === 'night';
  element('original-name').checked = localStorage.getItem('original-name') !== 'false';
  element('scroll-interval').value = localStorage.getItem('scroll-interval') || '0.5';
  element('scroll-slider').value = element('scroll-interval').value;
  element('chinese-limit').value = localStorage.getItem('chinese-limit') || '50';
}

function applyTheme() {
  document.documentElement.dataset.theme = element('night').checked ? 'night' : 'day';
}

function sourceName() {
  return element('input').value.split(/[\\/]/).pop().replace(/\.txt$/i, '');
}

function outputName() {
  return element('name').value.trim().replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').replace(/[. ]+$/, '').replace(/\.epub$/i, '');
}

function syncOutput() {
  element('output').value = outputFolder && outputName() ? outputFolder + '\\' + outputName() + '.epub' : outputFolder;
  controls();
}

function syncScroll() {
  const input = element(this.id === 'scroll-interval' ? 'scroll-interval' : 'scroll-slider');
  if (input.value && input.checkValidity()) {
    element('scroll-interval').value = input.value;
    element('scroll-slider').value = input.value;
    localStorage.setItem('scroll-interval', input.value);
    if (ready && busy) void command({ kind: 'settings', scrollInterval: Number(input.value) });
  }
  controls();
}

function log(message) {
  logs.push(String(message));
  logs = logs.slice(-500);
  element('log').textContent = logs.join('\n');
  element('log').scrollTop = element('log').scrollHeight;
}

function setNotice(message, color) {
  element('notice').textContent = message;
  element('notice').style.color = color || '';
}

function controls() {
  element('start').disabled = !ready || busy || !element('input').value || !element('output').value || !outputName() || !element('scroll-interval').value || !element('scroll-interval').checkValidity() || !element('chinese-limit').checkValidity();
  for (const id of ['pick-input', 'pick-output', 'preset', 'name', 'original-name', 'chinese-limit']) element(id).disabled = !ready || busy;
  for (const id of ['scroll-interval', 'scroll-slider']) element(id).disabled = !ready;
  element('pause').disabled = !busy;
  element('cancel').disabled = !busy;
  element('retry').disabled = !ready || busy;
  element('pause').textContent = paused ? 'Resume' : 'Pause';
  element('restart-backend').disabled = ready && busy;
  element('export-diagnostics').disabled = busy;
}

async function command(message) {
  try { await api.command(message); return true; }
  catch (error) { setNotice(error.message); log(error.message); return false; }
}

async function pickFile(kind) {
  try {
    const selected = await api.pick(kind);
    if (selected) {
      if (kind === 'input') {
        element('input').value = selected;
        if (element('original-name').checked || !element('name').value) element('name').value = sourceName() + ' EN';
        syncOutput();
        checkForCheckpoint();
      } else {
        outputFolder = selected;
        syncOutput();
        checkForCheckpoint();
      }
    }
    controls();
  } catch (error) { setNotice(error.message); }
}

async function checkForCheckpoint() {
  if (!element('input').value || !outputFolder || !outputName()) {
    element('checkpoint-status').textContent = '';
    return;
  }
  try {
    await command({ kind: 'checkpoint_status', input: element('input').value, output: element('output').value, chunkLimit: Number(element('preset').value) });
  } catch {}
}

async function start() {
  resetRun();
  busy = true;
  paused = false;
  controls();
  if (await command({ kind: 'start', input: element('input').value, output: element('output').value, chunkLimit: Number(element('preset').value), scrollInterval: Number(element('scroll-interval').value), remainingChineseLimit: Number(element('chinese-limit').value) })) {
    started = Date.now();
    element('status').textContent = 'Running';
    setNotice('');
  } else {
    busy = false;
    controls();
  }
}

async function togglePause() {
  if (await command({ kind: paused ? 'resume' : 'pause' })) {
    paused = !paused;
    element('status').textContent = paused ? 'Paused' : 'Running';
    controls();
  }
}

async function cancel() {
  if (await command({ kind: 'cancel' })) {
    element('status').textContent = 'Cancelling';
    element('validation').close();
  }
}

async function retry() {
  start();
}

async function openResult(folder) {
  try { await api.openResult(folder); }
  catch (error) { setNotice(error.message); }
}

async function sendDecision(decision) {
  if (await command({ kind: 'decision', value: decision })) element('validation').close();
}

async function restartBackend() {
  try {
    await api.restartBackend();
    ready = false;
    busy = false;
    element('connection').textContent = 'Restarting backend...';
    setNotice('Backend is restarting');
  } catch (error) { setNotice(error.message); }
}

async function exportDiagnostics() {
  const lines = [
    'NovelTL Desktop',
    `Input selected: ${Boolean(element('input').value)}`,
    `Output selected: ${Boolean(element('output').value)}`,
    `Preset: ${element('preset').options[element('preset').selectedIndex]?.text || 'unknown'}`,
    `Scroll interval: ${element('scroll-interval').value}s`,
    `Chinese limit: ${element('chinese-limit').value}`,
    `Status: ${element('status').textContent}`,
    `Complete: ${element('complete').textContent}`,
    `Failed: ${element('failed').textContent}`,
    '',
    'Log:',
    element('log').textContent
  ].join('\n');
  const blob = new Blob([lines], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `noveltl-diagnostics-${Date.now()}.txt`;
  a.click();
  URL.revokeObjectURL(a.href);
}

function resetRun() {
  element('open').disabled = true;
  element('folder').disabled = true;
  element('retry').disabled = true;
  element('progress').value = 0;
  element('complete').textContent = '0 / 0';
  element('failed').textContent = '0';
  element('elapsed').textContent = '00:00';
  element('stats').textContent = '';
  element('chunks').innerHTML = '<p class="empty">No active translation</p>';
  currentStats = { speed: null, eta: null, currentPct: 0 };
}

function updateElapsed() {
  if (busy && started) {
    const seconds = Math.floor((Date.now() - started) / 1000);
    element('elapsed').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  }
}

function renderStats() {
  const parts = [];
  if (currentStats.speed != null) parts.push(`${currentStats.speed.toLocaleString()} char/s`);
  if (currentStats.currentPct > 0) parts.push(`Chunk ${currentStats.currentPct.toFixed(0)}% translated`);
  if (currentStats.eta != null) parts.push(`ETA ${currentStats.eta}`);
  element('stats').textContent = parts.join(' | ');
}

function setupDragDrop() {
  const target = document.body;
  target.addEventListener('dragover', event => {
    event.preventDefault();
    event.dataTransfer.dropEffect = ready && !busy ? 'copy' : 'none';
  });
  target.addEventListener('drop', async event => {
    event.preventDefault();
    if (!ready || busy) return;
    const file = [...event.dataTransfer.files].find(f => f.name.toLowerCase().endsWith('.txt'));
    if (!file) { setNotice('Drop a .txt file'); return; }
    try {
      element('input').value = await api.dropInput(file);
    } catch (error) {
      setNotice(error.message);
      return;
    }
    if (element('original-name').checked || !element('name').value) element('name').value = sourceName() + ' EN';
    syncOutput();
    await checkForCheckpoint();
    controls();
  });
}

function handleEvent({ kind, value }) {
  if (kind === 'ready') {
    ready = true;
    element('connection').textContent = 'Python connected';
    setNotice('');
    controls();
  }
  if (kind === 'log') log(value);
  if (kind === 'settings_error') { setNotice(String(value)); log(value); }
  if (kind === 'status') element('status').textContent = String(value);
  if (kind === 'progress') element('progress').value = value;
  if (kind === 'run_info') {
    const [complete, total, failed] = value;
    element('complete').textContent = `${complete} / ${total}`;
    element('failed').textContent = failed;
  }
  if (kind === 'stats') {
    const [chars, duration, , pct] = value;
    if (chars > 0 && duration > 0) currentStats.speed = Math.round(chars / duration);
    currentStats.currentPct = pct;
    renderStats();
  }
  if (kind === 'current_chunk') {
    const [, pct] = value;
    currentStats.currentPct = pct;
    renderStats();
  }
  if (kind === 'chunk_statuses') {
    element('chunks').replaceChildren(...value.map((status, index) => {
      const row = document.createElement('div');
      row.className = 'chunk';
      const title = document.createElement('strong');
      title.textContent = `Chunk ${String(index + 1).padStart(3, '0')}`;
      const label = document.createElement('span');
      label.textContent = status;
      row.append(title, label);
      return row;
    }));
  }
  if (kind === 'validation') {
    element('reason').textContent = `Chunk ${value.index}: ${value.reason}`;
    element('validation').showModal();
  }
  if (kind === 'checkpoint_status') {
    if (value.exists) element('checkpoint-status').textContent = `Resume: ${value.completed}/${value.total} chunks saved`;
    else element('checkpoint-status').textContent = '';
  }
  if (kind === 'done') {
    element('status').textContent = 'Complete';
    setNotice(`Saved: ${value}`);
    element('progress').value = 1;
    element('open').disabled = false;
    element('folder').disabled = false;
  }
  if (kind === 'incomplete') {
    element('status').textContent = 'Incomplete';
    setNotice(`${value} chunk(s) failed. Retry to process saved progress.`);
    element('retry').disabled = false;
  }
  if (['error', 'command_error', 'offline'].includes(kind)) {
    element('status').textContent = 'Stopped';
    setNotice(String(value));
    log(value);
    element('validation').close();
  }
  if (['idle', 'command_error', 'offline'].includes(kind)) {
    busy = false;
    paused = false;
  }
  if (kind === 'offline') {
    ready = false;
    element('connection').textContent = 'Backend offline';
  }
  controls();
}
