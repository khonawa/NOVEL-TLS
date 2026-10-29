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

Select a TXT source, review its decoded preview, confirm the encoding, and choose an EPUB output. Start launches the installed Chrome.

### Text encoding and preview

Auto recognizes UTF-8/16/32 byte-order marks (UTF-32 first), strict UTF-8, plausible BOM-less Unicode byte patterns, and legacy Chinese candidates. BOM-less Chinese UTF-16 is also considered without ASCII/NUL lane patterns, with text-plausibility and frequent-character checks to avoid mistaking arbitrary bytes for Chinese. Legacy guesses use frequent Simplified/Traditional characters as additional evidence. Isolated weak mojibake indicators do not trigger warnings; replacement characters and strong mojibake sequences still do. Plain text starting with `MZ` is not rejected solely for those two letters. Manual choices are UTF-8, UTF-16 LE/BE, UTF-32 LE/BE, GB2312, GBK, GB18030, Big5, and Big5-HKSCS. Detection is heuristic, not a guarantee; ambiguous and possible mojibake results show warnings. Compare the preview and explicitly confirm it before translating.

Every choice rereads the original bytes locally, validates the **whole file strictly**, and displays at most 4,000 characters. No replacement/ignored-byte decoding or source rewriting occurs. Invalid text, unexpected controls, and recognized binary/document signatures are rejected. A matching BOM is removed from decoded text; a conflicting manual selection is rejected. Line endings and whitespace are preserved in the preview (translation chunking retains its existing behavior).

Changing files, modes, encodings, or refreshing clears confirmation. Refresh after editing a source externally: translation checks the source-byte digest and stops if it changed. Translation and checkpoint lookup use the same resolved encoding and digest, not a second detection. Preview runs off the command thread, supports superseding/cancellation, and times out after two minutes. The preview is bounded, but whole-file validation still uses memory proportional to source size.

Readable ASCII URLs do not establish an encoding. Already-resaved mojibake or replaced characters may require the original file; this feature does not automatically repair corrupted text. EPUB fixing remains a separate workflow and does not use text encoding selection.
Pause/resume and cancellation use the original pipeline's thread-safe controls.
Validation failures offer retry, accept, or skip. Starting again with the same files and preset resumes checkpoints.
Changing the preset changes checkpoint compatibility. Completed books can be opened or revealed in Explorer.

### Optional English filename suggestion

After selecting a TXT, click **Translate filename to English** to opt in. This opens installed Chrome and displays only the source filename stem as visible content in a short-lived, loopback-served HTML page. **Chrome’s online translation service is used; this is not offline translation.** No extra API key, dependency, or service is added. Nothing is submitted just by choosing a file.

Review/edit the suggestion in **Translation name** before starting. That name determines the EPUB filename, metadata title, and generated cover. Failure keeps the current name (normally original + EN); edits, source/mode changes, and cancelled/stale responses cannot replace it. Source files are unchanged. Title work runs separately, is cancellable and bounded to 90 seconds plus process cleanup, and cannot overlap a translation. Start/restart wait for browser cleanup. Offline failures settle pending requests. Short titles (including one character) are checked for actual English text, not page-wide translation markers; the native fallback explicitly selects English. Chrome's native failure bubble is reported as a translation/network failure instead of waiting for an unchanged title to time out. Activation failure and stalled translation are separate errors. The 25-second title-reading budget includes native activation (it is not reset by retries); Electron's transport watchdog allows Python's 90-second worker budget and up to 20 seconds of process cleanup before intervening. The title subprocess now emits a versioned JSON-lines readiness handshake after imports and Playwright startup, before receiving the filename over stdin or launching Chrome. EOF at readiness shuts it down without submitting a title; malformed or missing readiness fails explicitly. Startup stage reporting distinguishes dependency imports from Playwright startup (the old “starting title worker” fallback covered both and did not prove an IPC failure). Run `python -m unittest discover -s backend -p test_title_startup.py` to check the actual production launch/readiness/EOF path locally without remote translation.

Windows-illegal characters become underscores; reserved device names are prefixed and names are limited to 180 UTF-16 units with trailing dots/spaces removed. Existing EPUBs are never replaced, including files created during translation: final publication refuses collisions. Choose another name or move the existing EPUB. Checkpoint folders are not collisions; compatible unfinished runs still resume at the same fixed output path. A final collision retains checkpoints. Fix EPUB remains separate and also refuses overwriting an existing destination.

## Improvements

- Responsive desktop interface with native pickers, chunk status, progress, elapsed time, and bounded logs.
- Isolated, sandboxed renderer with a narrow IPC API and no Node access.
- Separate backend process and explicit backend connection/error state.
- Close confirmation during active work; cancellation releases paused workers.
- Preset persistence and headless bridge regression tests.
- Separate remembered import/export folders and persistent night mode.
- Editable translation filename, with an Original name + EN option.
- Persistent scroll interval (0.1-5 seconds, default 0.5) with synchronized slider and number input, adjustable during translation or pause. Changes apply when scheduling the next scroll; existing waits finish normally and targeted cleanup retains its minimum dwell time.
- Automatic cleanup: one initial sweep, then direct jumps to paragraphs containing Chinese. A watchdog tracks meaningful count improvement (0.5% of the original count, minimum one character), not scrolling. After 60 active seconds without improvement (including a 60-second startup grace), it reactivates Chrome Translate once. A rollback to at least 95% of the original count after at least 20% progress triggers earlier recovery after three observations at least one second apart.
- Recovery is sequential: scrolling stops for a 20-second cooldown after reactivation. Without meaningful improvement, the current chunk reloads once and restarts its scroll/count/stability state. Exception: if targeted cleanup is already active and at most 5% of the original Chinese characters remain, a stall after the reactivation sends the page to the validation dialog (Accept/Retry/Skip) instead of reloading it; confirmed rollbacks still reload. Budgets remain one reactivation and one reload per chunk attempt; exhaustion enters the existing three-attempt retry path, never auto-splits chunks, and never overwrites completed checkpoints. Reload gets a fresh startup grace, not a fresh recovery budget. Pause time is excluded; cancellation is checked between bounded browser/UI operations (an in-flight native operation cannot be interrupted immediately). Diagnostics report active elapsed time, counts, recovery actions, and observed browser errors—not an inferred RAM cause. Stable output still undergoes normal completeness validation; explicit accept/skip decisions retain their existing behavior.
- Extended CJK character detection. Nonempty pages within the configured tolerance finish after two stable checks; output validation still applies.
- Saved per-chunk Chinese-character tolerance, default 50. Set a nonnegative whole number before starting; 0 requires zero remaining Chinese. Accepted leftovers remain in the EPUB.

## Check

```powershell
npm run check
.\.venv\Scripts\python.exe -m unittest discover -s backend -p test_watchdog.py
.\.venv\Scripts\python.exe -m unittest discover -s backend -p test_bridge.py
.\.venv\Scripts\python.exe -m unittest discover -s backend -p test_encoding.py
.\.venv\Scripts\python.exe -m unittest discover -s backend -p test_title.py
node --test electron\*.test.cjs
```

## Packaging

`npm run pack` builds an unpacked Electron application; `npm run dist` creates a Windows NSIS installer.
These bundle the Python source, **not a Python runtime or its dependencies**.
Packaged users still need Python dependencies and Chrome, with `NOVELTL_PYTHON` or Python on PATH.
A fully standalone installer needs a frozen Python sidecar in a subsequent packaging step.
The initial dependency audit reports eight moderate vulnerabilities after updating Electron and
electron-builder to current releases. Review `npm audit` before distributing builds.

## Port Scope

This initial port preserves the upstream translation engine, chapter detection, EPUB generation,
validation, and checkpoints. It does not yet expose drag-and-drop, throughput/ETA,
or diagnostics export from the old GUI. Scroll interval is adjustable live; cleanup replaces configurable pass counts.
Remaining Chinese above the configured tolerance requires cleanup or review; automatic completion requires stable text within tolerance and normal output validation. Existing checkpointed chunks are not revalidated when the tolerance changes.
Real Chrome translation requires an interactive user session and has not been validated end to end here.
The upstream project is described as provided as-is for personal use; review its terms before redistribution.