# NovelTL Desktop

- [x] Verify workspace instructions exist.
- [x] Clarify requirements: Electron desktop port of khonawa/novelTL, retaining Python translation logic.
- [x] Scaffold project: plain renderer, Electron main/preload, Python JSON-lines bridge.
- [x] Customize project: native file pickers, run controls, validation decisions, progress and results.
- [x] Install required extensions: none required.
- [x] Compile and test project: JavaScript syntax checks and three bridge tests pass.
- [x] Create and run the desktop task: NovelTL: Desktop.
- [x] Verify launch: native NovelTL window observed; real translation remains untested.
- [x] Document setup, packaging limitations, and deferred parity features.

Keep the renderer sandboxed with context isolation and no Node integration.
Use narrow preload APIs; never expose arbitrary shell execution or filesystem access.
Preserve the upstream engine unless a focused test supports changing it.
Use `npm run check` and `python -m unittest discover -s backend -p test_bridge.py` for focused checks.
Windows and installed Google Chrome remain runtime requirements.