const element = id => document.getElementById(id);
const api = window.desktop;
let ready = false;
let busy = false;
let paused = false;
let started = 0;
let logs = [];
element('preset').value = localStorage.getItem('preset') || '275000';
element('night').checked = localStorage.getItem('theme') === 'night';
function theme() { document.documentElement.dataset.theme = element('night').checked ? 'night' : 'day'; localStorage.setItem('theme', document.documentElement.dataset.theme); }
element('night').onchange = theme;
theme();
element('original-name').checked = localStorage.getItem('original-name') !== 'false';
element('scroll-interval').value = localStorage.getItem('scroll-interval') || '0.5';
element('chinese-limit').value = localStorage.getItem('chinese-limit') || '50';
element('chinese-limit').oninput = () => {
  if (element('chinese-limit').checkValidity()) localStorage.setItem('chinese-limit', element('chinese-limit').value);
  controls();
};
function sourceName() { return element('input').value.split(/[\\/]/).pop().replace(/\.txt$/i, ''); }
function outputName() { return element('name').value.trim().replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').replace(/[. ]+$/, '').replace(/\.epub$/i, ''); }
function syncOutput() {
  const output = element('output').value;
  if (output && outputName()) element('output').value = output.slice(0, Math.max(output.lastIndexOf('\\'), output.lastIndexOf('/')) + 1) + outputName() + '.epub';
  controls();
}
element('original-name').onchange = () => {
  localStorage.setItem('original-name', element('original-name').checked);
  if (element('original-name').checked && sourceName()) element('name').value = sourceName() + ' EN';
  syncOutput();
};
element('name').oninput = () => { element('original-name').checked = false; localStorage.setItem('original-name', 'false'); syncOutput(); };
element('scroll-slider').value = element('scroll-interval').value;
for (const id of ['scroll-interval', 'scroll-slider']) element(id).oninput = () => {
  const input = element(id);
  if (input.value && input.checkValidity()) {
    element('scroll-interval').value = input.value;
    element('scroll-slider').value = input.value;
    localStorage.setItem('scroll-interval', input.value);
    if (ready && busy) void command({ kind: 'settings', scrollInterval: Number(input.value) });
  }
  controls();
};
function log(message) { logs.push(String(message)); logs = logs.slice(-500); element('log').textContent = logs.join('\n'); element('log').scrollTop = element('log').scrollHeight; }
function controls() {
  element('start').disabled = !ready || busy || !element('input').value || !element('output').value || !outputName() || !element('scroll-interval').value || !element('scroll-interval').checkValidity();
  element('start').disabled ||= !element('chinese-limit').checkValidity();
  for (const id of ['pick-input', 'pick-output', 'preset', 'name', 'original-name', 'chinese-limit']) element(id).disabled = !ready || busy;
  for (const id of ['scroll-interval', 'scroll-slider']) element(id).disabled = !ready;
  element('pause').disabled = !busy;
  element('cancel').disabled = !busy;
  element('pause').textContent = paused ? 'Resume' : 'Pause';
}
async function command(message) {
  try { await api.command(message); return true; }
  catch (error) { element('notice').textContent = error.message; log(error.message); return false; }
}
for (const kind of ['input', 'output']) element(`pick-${kind}`).onclick = async () => {
  try {
    const selected = await api.pick(kind, (outputName() || 'Translation') + '.epub');
    if (selected) {
      element(kind).value = selected;
      if (kind === 'input') {
        if (element('original-name').checked || !element('name').value) element('name').value = sourceName() + ' EN';
        syncOutput();
      } else {
        element('name').value = selected.split(/[\\/]/).pop().replace(/\.epub$/i, '');
        element('original-name').checked = element('name').value === sourceName() + ' EN';
        localStorage.setItem('original-name', element('original-name').checked);
      }
    }
    controls();
  }
  catch (error) { element('notice').textContent = error.message; }
};
element('preset').onchange = () => localStorage.setItem('preset', element('preset').value);
element('start').onclick = async () => {
  busy = true; paused = false; controls();
  element('open').disabled = true; element('folder').disabled = true;
  element('progress').value = 0;
  if (await command({ kind: 'start', input: element('input').value, output: element('output').value, chunkLimit: Number(element('preset').value), scrollInterval: Number(element('scroll-interval').value), remainingChineseLimit: Number(element('chinese-limit').value) })) {
    started = Date.now(); element('status').textContent = 'Running'; element('notice').textContent = '';
  } else { busy = false; controls(); }
};
element('pause').onclick = async () => { if (await command({ kind: paused ? 'resume' : 'pause' })) { paused = !paused; element('status').textContent = paused ? 'Paused' : 'Running'; controls(); } };
element('cancel').onclick = async () => { if (await command({ kind: 'cancel' })) { element('status').textContent = 'Cancelling'; element('validation').close(); } };
for (const id of ['open', 'folder']) element(id).onclick = async () => { try { await api.openResult(id === 'folder'); } catch (error) { element('notice').textContent = error.message; } };
element('clear').onclick = () => { logs = []; element('log').textContent = ''; };
element('validation').addEventListener('cancel', event => event.preventDefault());
for (const button of document.querySelectorAll('[data-decision]')) button.onclick = async () => { if (await command({ kind: 'decision', value: button.dataset.decision })) element('validation').close(); };
setInterval(() => { if (busy && started) { const seconds = Math.floor((Date.now() - started) / 1000); element('elapsed').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`; } }, 1000);
if (api) api.onEvent(({ kind, value }) => {
  if (kind === 'ready') { ready = true; element('connection').textContent = 'Python connected'; element('notice').textContent = ''; }
  if (kind === 'log') log(value);
  if (kind === 'settings_error') { element('notice').textContent = String(value); log(value); }
  if (kind === 'status') element('status').textContent = String(value);
  if (kind === 'progress') element('progress').value = value;
  if (kind === 'run_info') { element('complete').textContent = `${value[0]} / ${value[1]}`; element('failed').textContent = value[2]; }
  if (kind === 'chunk_statuses') {
    element('chunks').replaceChildren(...value.map((status, index) => { const row = document.createElement('div'); row.className = 'chunk'; const title = document.createElement('strong'); title.textContent = `Chunk ${String(index + 1).padStart(3, '0')}`; const label = document.createElement('span'); label.textContent = status; row.append(title, label); return row; }));
  }
  if (kind === 'validation') { element('reason').textContent = `Chunk ${value.index}: ${value.reason}`; element('validation').showModal(); }
  if (kind === 'done') { element('status').textContent = 'Complete'; element('notice').textContent = `Saved: ${value}`; element('progress').value = 1; element('open').disabled = false; element('folder').disabled = false; }
  if (kind === 'incomplete') { element('status').textContent = 'Incomplete'; element('notice').textContent = `${value} chunk(s) failed. Start again with the same files to retry saved progress.`; }
  if (['error', 'command_error', 'offline'].includes(kind)) { element('status').textContent = 'Stopped'; element('notice').textContent = String(value); log(value); element('validation').close(); }
  if (['idle', 'command_error', 'offline'].includes(kind)) { busy = false; paused = false; }
  if (kind === 'offline') { ready = false; element('connection').textContent = 'Backend offline'; }
  controls();
});
else { element('connection').textContent = 'Browser preview - translation unavailable'; element('notice').textContent = 'No desktop backend connection. Open the NovelTL desktop window launched by npm start.'; }
controls();