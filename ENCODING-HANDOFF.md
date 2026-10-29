# Handoff: text encoding detection and preview

## Request and status
The user wants built-in handling for text files that fail to decode or appear garbled, and requested this handoff to continue with a different agent. This is a summary of the conversation and proposed plan, not a verbatim transcript.

No implementation changes have been made. Only the existing reader was inspected. No tests have been run for this feature. The original affected text file has not been supplied, so its actual encoding and whether its bytes are intact remain unknown.

## Observed problem
- The app displays: `Could not decode the file as UTF-8, GB18030, or Big5.`
- Screenshots show garbled characters such as `Ã`, `¼`, `Ö`, and repeated `¡`-like symbols. An ASCII URL remains readable.
- VS Code's status bar shows `ANSI`, `Windows (CRLF)`, and `Plain Text`. One screenshot shows about 6.65 million characters.
- The editor's ANSI label does not establish the original encoding. GBK/GB18030 is a candidate, not a verified diagnosis.
- The user clarified that they wanted to understand what happens to this garbled text, not merely document-format import.

## Existing implementation inspected
File: [backend/novel_translator/text_processing.py](backend/novel_translator/text_processing.py)

`read_text_file(path: Path) -> str` currently:
1. Reads the original bytes with `path.read_bytes()`.
2. Tries strict `utf-8-sig` and `utf-8`.
3. Tries strict `gb18030` and `big5`.
4. Rejects replacement characters and applies a heuristic to the first 20,000 decoded characters.
5. Accepts a legacy candidate if the sample is shorter than 100 characters, has more than 5% characters matching `CHINESE_RE`, or has fewer than 1% characters in U+0080–U+04FF.
6. Otherwise raises the error shown above.

Reinspect the current files before editing; they may have changed since this conversation.

## Explanation agreed during discussion
A text file contains bytes. An encoding maps those bytes to characters. Using the wrong encoding can produce mojibake without changing the original bytes.

Example: `中` is UTF-8 bytes `E4 B8 AD`, or GBK bytes `D6 D0`. Interpreting `D6 D0` as Windows-1252 produces `ÖÐ`.

ASCII letters and URLs can remain readable across encodings even while Chinese text is garbled. Line endings and indentation should be retained during decoding. Repeated strange symbols may represent misread punctuation or spaces; do not simply delete them.

Distinguish these cases:
- Original bytes intact, wrong display encoding: correct decoding can restore readable text.
- Ambiguous encoding: show candidates and a preview; require user selection/confirmation.
- Garbled text already resaved: ordinary detection may not repair it. Reversing a mistaken conversion is sometimes possible, but should be a separate explicit repair workflow.
- Information replaced with `�` or `?`: exact recovery may be impossible without the original. Such characters are clues, not proof that every occurrence represents corruption.

## Proposed scope
Implement plain-text encoding detection and a preview/manual override first. EPUB/PDF/Word import is a separate feature, not part of this plan. Automatic repair of already-resaved mojibake is not part of the initial scope.

The user requested a plan and explanation, then asked to save this handoff. Do not claim the feature has already been approved in every implementation detail or implemented.

## Proposed behavior
1. Read original file bytes without modifying the source file.
2. Check for UTF-8, UTF-16, and UTF-32 byte-order marks. Match longer markers first because UTF-32 LE shares a prefix with UTF-16 LE.
3. Without a marker, evaluate likely encodings: strict UTF-8, plausible UTF-16 byte patterns, GB18030, Big5, and any explicitly supported legacy choices.
4. Treat successful decoding as necessary but not sufficient. Several codecs accept the same bytes. Evaluate unexpected controls, mojibake patterns, and language plausibility; Chinese-character count alone is insufficient.
5. Show a short decoded preview, the detected/selected encoding, and an uncertainty warning where appropriate. Do not invent precise confidence percentages without a justified detector.
6. Offer an encoding selector: Auto, UTF-8, UTF-16 LE/BE, GB18030, Big5, and relevant legacy choices. Account for UTF-32 BOM handling as well.
7. Every selection change decodes the original bytes again, not the previous preview text.
8. Translation uses the exact encoding confirmed in the preview for the whole file. Do not redetect independently and choose a different encoding.
9. Validate the full file strictly before translation. A valid preview is not proof that the rest of the file decodes. Stop with an actionable error rather than silently replacing/discarding bytes or proceeding with known-invalid text.
10. Preserve existing behavior for correctly supported inputs, and keep the source file unchanged.

For large files, keep the UI responsive and bound the preview. If decoding samples, avoid false failures from cutting multibyte characters at sample boundaries. Detection/preview/full-file validation details should be decided after inspecting the current architecture.

## Integration plan
- Python backend: detection, strict decoding, preview metadata, and translation encoding selection.
- Electron main/preload: narrow APIs for authorized selected-file previews and encoding choices; no arbitrary filesystem or shell access.
- Renderer: preview, encoding dropdown, clear errors/warnings, and confirmation for uncertain results.
- Trace the existing file-selection and translation request paths before choosing exact interfaces.
- Preserve renderer sandboxing, context isolation, and disabled Node integration.
- Preserve the upstream translation engine unless a focused regression test supports changing it.

Relevant project files to inspect:
- [backend/bridge.py](backend/bridge.py)
- [backend/novel_translator/text_processing.py](backend/novel_translator/text_processing.py)
- [backend/novel_translator/pipeline.py](backend/novel_translator/pipeline.py)
- [backend/test_bridge.py](backend/test_bridge.py)
- [electron/main.cjs](electron/main.cjs)
- [electron/preload.cjs](electron/preload.cjs)
- [src/app.js](src/app.js)
- [src/index.html](src/index.html)
- [README.md](README.md)

## Validation plan
- Add focused tests with known original bytes for UTF-8 (with/without BOM), UTF-16 LE/BE, UTF-32 BOMs, GB18030, and Big5.
- Cover empty files, ASCII-only/ambiguous input, invalid/truncated data, non-text input, and sample-boundary cases.
- Verify manual overrides, invalid encoding selections, preview/translation consistency, and source-file immutability.
- Verify that a successful preview cannot hide a full-file decoding error.
- Check existing valid-file behavior and UI responsiveness for large inputs.
- Run `npm run check` and `python -m unittest discover -s backend -p test_bridge.py`, using the configured Python interpreter. Run any added focused decoding tests too.
- Manually validate the UI and, if available, the user's original affected file.
- Update directly related README documentation with supported encodings and recovery limitations.

## Suggested next steps
1. Inspect repository instructions, current changes, and the existing bridge/file-selection flow.
2. Obtain the original affected `.txt` file or its local path if possible. Do not ask the user to resave the garbled display first. Never upload its contents to third-party services for detection.
3. Implement and test byte-level detection and strict explicit decoding before wiring the preview UI.
4. Integrate encoding selection through the bridge into translation and validate end to end.

## Prompt to give the next agent
Read ENCODING-HANDOFF.md, inspect the current implementation, and continue the built-in text-encoding detection/preview feature. Preserve original files, use strict decoding, keep Electron APIs narrow and sandboxing intact, and ensure translation uses the same confirmed encoding as the preview. Verify uncertain implementation decisions rather than assuming the screenshot proves a particular encoding. Report what is implemented and tested versus what still needs the original affected file.
