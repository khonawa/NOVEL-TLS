const element = id => document.getElementById(id);
const api = window.desktop;
let ready = false;
let busy = false;
let paused = false;
let started = 0;
let logs = [];
let currentStats = { speed: null, eta: null, currentPct: 0 };
let outputFolder = '';
let coverImage = null;
let cropState = null;
let previewGeneration = 0;
let sourcePreview = null;
let checkpointRequest = 0;
let titleGeneration = 0;
let titleWorking = false;

function invalidateTitle() {
  titleGeneration++;
  element('title-status').textContent = '';
  if (titleWorking && api) void api.cancelTitle().catch(() => {});
}

async function suggestTitle() {
  if (element('suggest-title').disabled) return;
  const generation = ++titleGeneration;
  const original = element('name').value;
  const input = element('input').value;
  titleWorking = true;
  element('title-status').textContent = 'Translating filename with Chrome…';
  controls();
  try {
    const title = await api.suggestTitle();
    if (generation !== titleGeneration || input !== element('input').value || isFixMode() || original !== element('name').value) return;
    element('name').value = title;
    element('original-name').checked = false;
    localStorage.setItem('original-name', 'false');
    element('title-status').textContent = 'Suggestion ready. Review or edit the translation name.';
    syncOutput();
    checkForCheckpoint();
  } catch (error) {
    if (generation !== titleGeneration) return;
    element('title-status').textContent = `${error.message} Your current name was kept.`;
  } finally {
    titleWorking = false;
    controls();
    checkForCheckpoint();
  }
}


loadSettings();
element('encoding').onchange = refreshPreview;
element('refresh-preview').onclick = refreshPreview;
element('confirm-encoding').onchange = () => { controls(); checkForCheckpoint(); };
applyTheme();
element('night').onchange = () => { applyTheme(); localStorage.setItem('theme', document.documentElement.dataset.theme); };
for (const radio of document.querySelectorAll('[name="mode"]')) radio.onchange = toggleMode;
element('suggest-title').onclick = suggestTitle;
element('cancel-title').onclick = () => { invalidateTitle(); element('title-status').textContent = 'Cancelling suggestion…'; };
element('original-name').onchange = () => {
  invalidateTitle();
  localStorage.setItem('original-name', element('original-name').checked);
  if (element('original-name').checked && sourceName()) element('name').value = sourceName() + nameSuffix();
  syncOutput();
};
element('name').oninput = () => { invalidateTitle(); element('original-name').checked = false; localStorage.setItem('original-name', 'false'); syncOutput(); checkForCheckpoint(); };
element('preset').onchange = () => {
  const isCustom = element('preset').value === 'custom';
  element('custom-chunk-wrapper').style.display = isCustom ? 'block' : 'none';
  if (isCustom) {
    const saved = localStorage.getItem('custom-chunk');
    if (saved && !isNaN(saved)) element('custom-chunk').value = saved;
    element('custom-chunk').focus();
  }
  localStorage.setItem('preset', element('preset').value);
  checkForCheckpoint();
  controls();
};
element('custom-chunk').oninput = () => {
  if (element('custom-chunk').checkValidity()) {
    localStorage.setItem('custom-chunk', element('custom-chunk').value);
  }
  controls();
};
for (const id of ['scroll-interval', 'scroll-slider']) element(id).oninput = syncScroll;
element('chinese-limit').oninput = () => {
  if (element('chinese-limit').checkValidity()) localStorage.setItem('chinese-limit', element('chinese-limit').value);
  controls();
};
for (const radio of document.querySelectorAll('[name="cover-mode"]')) radio.onchange = toggleCoverMode;
element('cover-text-color').oninput = () => localStorage.setItem('cover-text-color', element('cover-text-color').value);
element('cover-bg-color').oninput = () => localStorage.setItem('cover-bg-color', element('cover-bg-color').value);
element('pick-cover').onclick = pickCoverImage;
element('crop-reset').onclick = resetCrop;
element('crop-full').onclick = useWholeImage;
setupCropCanvas();
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
  const savedPreset = localStorage.getItem('preset');
  const validPresets = ['60000', '125000', '275000', '450000', 'custom'];
  element('preset').value = validPresets.includes(savedPreset) ? savedPreset : '275000';
  if (savedPreset === 'custom') {
    element('custom-chunk-wrapper').style.display = 'block';
    const savedCustom = localStorage.getItem('custom-chunk');
    if (savedCustom) element('custom-chunk').value = savedCustom;
  }
  element('night').checked = localStorage.getItem('theme') === 'night';
  element('original-name').checked = localStorage.getItem('original-name') !== 'false';
  element('scroll-interval').value = localStorage.getItem('scroll-interval') || '0.5';
  element('scroll-slider').value = element('scroll-interval').value;
  element('chinese-limit').value = localStorage.getItem('chinese-limit') || '50';
  const savedMode = localStorage.getItem('mode') || 'translate';
  document.querySelector(`[name="mode"][value="${savedMode}"]`).checked = true;
  toggleMode();
  const savedCoverMode = localStorage.getItem('cover-mode') || 'auto';
  document.querySelector(`[name="cover-mode"][value="${savedCoverMode}"]`).checked = true;
  toggleCoverMode();
  element('cover-text-color').value = localStorage.getItem('cover-text-color') || '#ffffff';
  element('cover-bg-color').value = localStorage.getItem('cover-bg-color') || '#0f131c';
}

function applyTheme() {
  document.documentElement.dataset.theme = element('night').checked ? 'night' : 'day';
}

function sourceName() {
  return element('input').value.split(/[\\/]/).pop().replace(/\.(txt|epub)$/i, '');
}

function isFixMode() {
  return document.querySelector('[name="mode"]:checked')?.value === 'fix';
}

function toggleMode() {
  const fix = isFixMode();
  localStorage.setItem('mode', fix ? 'fix' : 'translate');
  element('input-label').textContent = fix ? 'Translated EPUB to fix' : 'Source novel';
  element('original-name').parentElement.lastChild.textContent = fix ? 'Original name + fixed' : 'Original name + EN';
  for (const section of document.querySelectorAll('.translate-only')) section.hidden = fix;
  element('start').textContent = fix ? 'Fix EPUB' : 'Start translation';
  element('retry').hidden = fix;
  element('pause').hidden = fix;
  element('input').value = '';
  resetPreview();
  element('encoding').value = 'auto';
  element('checkpoint-status').textContent = '';
  if (element('original-name').checked) element('name').value = '';
  syncOutput();
}

function nameSuffix() {
  return isFixMode() ? ' fixed' : ' EN';
}

function outputName() {
  let name = element('name').value.trim().replace(/\.epub$/i, '').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').slice(0, 180).replace(/[. ]+$/, '');
  if (/[\uD800-\uDBFF]$/.test(name)) name = name.slice(0, -1);
  if (/^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?: *\.|$)/i.test(name)) name = '_' + name;
  return name.slice(0, 180).replace(/[. ]+$/, '');
}

function getChunkSize() {
  const preset = element('preset').value;
  if (preset === 'custom') {
    return Number(element('custom-chunk').value) || 275000;
  }
  return Number(preset);
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
  const fix = isFixMode();
  const customChunkValid = fix || element('preset').value !== 'custom' || (element('custom-chunk').value && element('custom-chunk').checkValidity());
  const translateValid = fix || (element('scroll-interval').value && element('scroll-interval').checkValidity() && element('chinese-limit').checkValidity());
  const coverValid = document.querySelector('[name="cover-mode"]:checked')?.value !== 'import' || Boolean(coverImage);
  const encodingValid = fix || Boolean(sourcePreview && element('confirm-encoding').checked);
  element('suggest-title').disabled = !ready || busy || titleWorking || fix || !element('input').value;
  element('cancel-title').disabled = !titleWorking;
  element('start').disabled = !ready || busy || titleWorking || !element('input').value || !element('output').value || !outputName() || !translateValid || !customChunkValid || !coverValid || !encodingValid;
  element('encoding').disabled = !ready || busy;
  element('refresh-preview').disabled = !ready || busy || !element('input').value;
  element('confirm-encoding').disabled = !ready || busy || !sourcePreview;
  for (const id of ['pick-input', 'pick-output', 'preset', 'name', 'original-name', 'chinese-limit', 'custom-chunk', 'pick-cover']) element(id).disabled = !ready || busy;
  for (const radio of document.querySelectorAll('[name="mode"], [name="cover-mode"]')) radio.disabled = busy;
  for (const id of ['scroll-interval', 'scroll-slider']) element(id).disabled = !ready;
  element('pause').disabled = !busy;
  element('cancel').disabled = !busy;
  element('retry').disabled = element('start').disabled;
  element('pause').textContent = paused ? 'Resume' : 'Pause';
  element('restart-backend').disabled = ready && (busy || titleWorking);
  element('export-diagnostics').disabled = busy;
}

async function command(message) {
  try { await api.command(message); return true; }
  catch (error) { setNotice(error.message); log(error.message); return false; }
}

function resetPreview() {
  invalidateTitle();
  previewGeneration++;
  checkpointRequest++;
  sourcePreview = null;
  element('confirm-encoding').checked = false;
  element('confirm-encoding').disabled = true;
  element('text-preview').textContent = '';
  element('encoding-status').textContent = 'Select a TXT file to preview.';
  element('checkpoint-status').textContent = '';
  if (api) void api.cancelPreview().catch(() => {});
}

async function refreshPreview() {
  resetPreview();
  const generation = previewGeneration;
  controls();
  if (isFixMode() || !ready || !element('input').value) return;
  element('encoding-status').textContent = 'Validating the entire file…';
  try {
    const preview = await api.previewText(element('encoding').value);
    if (generation !== previewGeneration) return;
    sourcePreview = preview;
    element('text-preview').textContent = preview.preview;
    element('encoding-status').textContent = `${preview.encoding} · ${preview.characters.toLocaleString()} characters · Whole file validated. ${preview.truncated ? 'Showing the first 4,000 characters. ' : ''}${preview.warnings.join(' ')} Review and confirm before translating.`;
  } catch (error) {
    if (generation !== previewGeneration) return;
    element('encoding-status').textContent = error.message;
  }
  controls();
}

async function pickFile(kind) {
  try {
    const selected = await api.pick(kind === 'input' && isFixMode() ? 'epub' : kind);
    if (selected) {
      if (kind === 'input') {
        element('input').value = selected;
        element('encoding').value = 'auto';
        void refreshPreview();
        if (element('original-name').checked || !element('name').value) element('name').value = sourceName() + nameSuffix();
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
  const requestId = ++checkpointRequest;
  if (busy || titleWorking || isFixMode() || !sourcePreview || !element('confirm-encoding').checked || !element('input').value || !outputFolder || !outputName()) {
    element('checkpoint-status').textContent = '';
    return;
  }
  try {
    await command({ kind: 'checkpoint_status', requestId, input: element('input').value, output: element('output').value, chunkLimit: getChunkSize(), encoding: sourcePreview.encoding, digest: sourcePreview.digest });
  } catch {}
}

async function start() {
  if (element('start').disabled) return;
  resetRun();
  busy = true;
  paused = false;
  controls();
  const coverMode = document.querySelector('[name="cover-mode"]:checked').value;
  const coverOptions = coverMode === 'auto'
    ? { mode: 'auto', textColor: element('cover-text-color').value, bgColor: element('cover-bg-color').value }
    : { mode: 'import', imagePath: coverImage || null, crop: cropRegion() };
  const message = isFixMode()
    ? { kind: 'fix_epub', input: element('input').value, output: element('output').value, cover: coverOptions }
    : { kind: 'start', input: element('input').value, output: element('output').value, encoding: sourcePreview.encoding, digest: sourcePreview.digest, chunkLimit: getChunkSize(), scrollInterval: Number(element('scroll-interval').value), remainingChineseLimit: Number(element('chinese-limit').value), cover: coverOptions };
  if (await command(message)) {
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
    resetPreview();
    element('input').value = '';
    ready = false;
    busy = false;
    element('connection').textContent = 'Restarting backend...';
    setNotice('Backend is restarting');
    controls();
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
    const wanted = isFixMode() ? '.epub' : '.txt';
    const file = [...event.dataTransfer.files].find(f => f.name.toLowerCase().endsWith(wanted));
    if (!file) { setNotice(`Drop a ${wanted} file`); return; }
    try {
      element('input').value = await api.dropInput(file);
      element('encoding').value = 'auto';
      void refreshPreview();
    } catch (error) {
      setNotice(error.message);
      return;
    }
    if (element('original-name').checked || !element('name').value) element('name').value = sourceName() + nameSuffix();
    syncOutput();
    await checkForCheckpoint();
    controls();
  });
}

function handleEvent({ kind, value }) {
  if (kind === 'title_idle') { titleWorking = false; checkForCheckpoint(); }
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
  if (kind === 'checkpoint_status' && value.requestId === checkpointRequest) {
    if (value.error?.includes('Source changed since preview')) {
      resetPreview();
      element('encoding-status').textContent = value.error;
    }
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
    if (String(value).includes('Source changed since preview')) resetPreview();
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
    titleWorking = false;
    resetPreview();
    ready = false;
    element('connection').textContent = 'Backend offline';
  }
  controls();
}

function toggleCoverMode() {
  const mode = document.querySelector('[name="cover-mode"]:checked').value;
  localStorage.setItem('cover-mode', mode);
  element('cover-auto').style.display = mode === 'auto' ? 'flex' : 'none';
  element('cover-import').style.display = mode === 'import' ? 'flex' : 'none';
  coverImage = null;
  cropState = null;
  element('cover-file-name').textContent = '';
  element('crop-controls').style.display = 'none';
  element('crop-hint').style.display = 'none';
  const canvas = element('cover-canvas');
  canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
  controls();
}

async function pickCoverImage() {
  if (!api) return;
  try {
    const path = await api.pick('image');
    if (!path) return;
    const dataUrl = await api.coverPreview();
    const img = new Image();
    img.onload = () => {
      coverImage = path;
      element('cover-file-name').textContent = path.split(/[/\\]/).pop();
      const canvas = element('cover-canvas');
      const scale = Math.min(400 / img.width, 600 / img.height, 1);
      canvas.width = Math.round(img.width * scale);
      canvas.height = Math.round(img.height * scale);
      cropState = { img, scale, crop: null, drag: null };
      resetCrop();
      element('crop-controls').style.display = 'flex';
      element('crop-hint').style.display = 'block';
      controls();
    };
    img.onerror = () => setNotice('Could not load the selected image');
    img.src = dataUrl;
  } catch (error) { setNotice(error.message); }
}

// Largest centered 2:3 rectangle (canvas pixels) that fits the image.
function defaultCrop() {
  const canvas = element('cover-canvas');
  let w = canvas.width;
  let h = w * 1.5;
  if (h > canvas.height) { h = canvas.height; w = h / 1.5; }
  return { x: (canvas.width - w) / 2, y: (canvas.height - h) / 2, w, h };
}

function drawCrop() {
  if (!cropState) return;
  const canvas = element('cover-canvas');
  const ctx = canvas.getContext('2d');
  const { img, crop } = cropState;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  if (!crop) return;
  ctx.fillStyle = 'rgba(0, 0, 0, 0.55)';
  ctx.fillRect(0, 0, canvas.width, crop.y);
  ctx.fillRect(0, crop.y + crop.h, canvas.width, canvas.height - crop.y - crop.h);
  ctx.fillRect(0, crop.y, crop.x, crop.h);
  ctx.fillRect(crop.x + crop.w, crop.y, canvas.width - crop.x - crop.w, crop.h);
  ctx.strokeStyle = '#ffffff';
  ctx.lineWidth = 2;
  ctx.strokeRect(crop.x + 1, crop.y + 1, crop.w - 2, crop.h - 2);
}

function canvasPoint(event) {
  const canvas = element('cover-canvas');
  const rect = canvas.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(canvas.width, (event.clientX - rect.left) * canvas.width / rect.width)),
    y: Math.max(0, Math.min(canvas.height, (event.clientY - rect.top) * canvas.height / rect.height))
  };
}

function setupCropCanvas() {
  const canvas = element('cover-canvas');
  canvas.addEventListener('pointerdown', event => {
    if (!cropState || busy) return;
    const p = canvasPoint(event);
    const c = cropState.crop;
    const inside = c && p.x >= c.x && p.x <= c.x + c.w && p.y >= c.y && p.y <= c.y + c.h;
    cropState.drag = inside ? { move: true, dx: p.x - c.x, dy: p.y - c.y } : { move: false, ox: p.x, oy: p.y };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener('pointermove', event => {
    if (!cropState?.drag) return;
    const p = canvasPoint(event);
    const d = cropState.drag;
    if (d.move) {
      const c = cropState.crop;
      c.x = Math.max(0, Math.min(canvas.width - c.w, p.x - d.dx));
      c.y = Math.max(0, Math.min(canvas.height - c.h, p.y - d.dy));
    } else {
      const sx = p.x >= d.ox ? 1 : -1;
      const sy = p.y >= d.oy ? 1 : -1;
      const maxW = sx > 0 ? canvas.width - d.ox : d.ox;
      const maxH = sy > 0 ? canvas.height - d.oy : d.oy;
      let w = Math.min(Math.max(Math.abs(p.x - d.ox), Math.abs(p.y - d.oy) / 1.5), maxW, maxH / 1.5);
      if (w < 10) return;
      const h = w * 1.5;
      cropState.crop = { x: sx > 0 ? d.ox : d.ox - w, y: sy > 0 ? d.oy : d.oy - h, w, h };
    }
    drawCrop();
  });
  const end = () => { if (cropState) cropState.drag = null; };
  canvas.addEventListener('pointerup', end);
  canvas.addEventListener('pointercancel', end);
}

// Crop rectangle in original image pixels, or null for the whole image.
function cropRegion() {
  if (!cropState?.crop) return null;
  const { crop, scale, img } = cropState;
  const x = Math.round(crop.x / scale);
  const y = Math.round(crop.y / scale);
  return { x, y, width: Math.min(img.width - x, Math.round(crop.w / scale)), height: Math.min(img.height - y, Math.round(crop.h / scale)) };
}

function resetCrop() {
  if (!cropState) return;
  cropState.crop = defaultCrop();
  drawCrop();
}

function useWholeImage() {
  if (!cropState) return;
  cropState.crop = null;
  drawCrop();
}