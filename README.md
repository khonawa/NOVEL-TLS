# NovelTL Desktop

An Electron desktop port of [khonawa/novelTL](https://github.com/khonawa/novelTL).
The original display-independent Python pipeline is retained under `backend/novel_translator`.
The frontend does not load the legacy Tkinter interface.

## Requirements

- Windows 10 or 11, installed Google Chrome, and internet access.
- Node.js 22+ and Python 3.10+.
- Chrome translation must be enabled. Electron's own Chromium is not the translation provider.

## Run

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
npm install
npm start
```

The development launcher searches `.venv`, then the parent folder's `.venv`, then `python` on PATH.
Set `$env:NOVELTL_PYTHON` to an absolute interpreter path to override detection.

Select a TXT source and EPUB output. Start launches the installed Chrome.
Pause/resume and cancellation use the original pipeline's thread-safe controls.
Validation failures offer retry, accept, or skip. Starting again with the same files and preset resumes checkpoints.
Changing the preset changes checkpoint compatibility. Completed books can be opened or revealed in Explorer.

## Improvements

- Responsive desktop interface with native pickers, chunk status, progress, elapsed time, and bounded logs.
- Isolated, sandboxed renderer with a narrow IPC API and no Node access.
- Separate backend process and explicit backend connection/error state.
- Close confirmation during active work; cancellation releases paused workers.
- Preset persistence and headless bridge regression tests.
- Separate remembered import/export folders and persistent night mode.
- Editable translation filename, with an Original name + EN option.
- Persistent scroll interval (0.1-5 seconds, default 0.5) with synchronized slider and number input, adjustable during translation or pause. Changes apply when scheduling the next scroll; existing waits finish normally and targeted cleanup retains its minimum dwell time.
- Automatic cleanup: one initial sweep, then direct jumps to paragraphs containing Chinese. After three cleanup rounds without improvement, Chrome Translate is retried once; three further stalled rounds send the result to review.
- Extended CJK character detection. Nonempty pages within the configured tolerance finish after two stable checks; output validation still applies.
- Saved per-chunk Chinese-character tolerance, default 50. Set a nonnegative whole number before starting; 0 requires zero remaining Chinese. Accepted leftovers remain in the EPUB.

## Check

```powershell
npm run check
.\.venv\Scripts\python.exe -m unittest discover -s backend -p test_bridge.py
```

## Packaging

`npm run pack` builds an unpacked Electron application; `npm run dist` creates a Windows NSIS installer.
These bundle the Python source, **not a Python runtime or its dependencies**.
Packaged users still need Python dependencies and Chrome, with `NOVELTL_PYTHON` or Python on PATH.
A fully standalone installer needs a frozen Python sidecar in a subsequent packaging step.
The initial dependency audit reports eight moderate vulnerabilities after updating Electron and
electron-builder to current releases. Review `npm audit` before distributing builds.

## Port Scope

This initial port preserves the upstream translation engine, decoding, chapter detection, EPUB generation,
validation, and checkpoints. It does not yet expose drag-and-drop, throughput/ETA,
or diagnostics export from the old GUI. Scroll interval is adjustable live; cleanup replaces configurable pass counts.
Remaining Chinese above the configured tolerance requires cleanup or review; automatic completion requires stable text within tolerance and normal output validation. Existing checkpointed chunks are not revalidated when the tolerance changes.
Real Chrome translation requires an interactive user session and has not been validated end to end here.
The upstream project is described as provided as-is for personal use; review its terms before redistribution.