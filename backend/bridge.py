from __future__ import annotations

import json
import logging
import queue
import re
import sys
import threading
from pathlib import Path


try:
    from novel_translator.pipeline import PipelineContext, QueueHandler, TranslationRunner
    from novel_translator.checkpoint import checkpoint_dir_for, load_checkpoint
    from novel_translator.text_processing import source_fingerprint, read_text_file
    from novel_translator.encoding import decode_source, ENCODINGS
    from novel_translator.constants import CHUNK_PRESETS, sanitize_filename
except ImportError as error:
    print(json.dumps({"kind": "error", "value": f"Missing dependency: {error.name}. Install with: pip install -r requirements.txt"}, ensure_ascii=True), flush=True)
    sys.exit(1)


class Bridge:
    def __init__(self, emit, runner_factory=TranslationRunner):
        self.emit = emit
        self.runner_factory = runner_factory
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.pause = threading.Event()
        self.pause.set()
        self.decisions = queue.Queue()
        self.thread = None
        self.scroll_interval = 0.5
        self.preview_cancel = threading.Event()
        self.preview_thread = None
        self.title_cancel = threading.Event()
        self.title_thread = None

    def decide(self, index, reason):
        self.emit("validation", {"index": index, "reason": reason})
        while not self.cancel.is_set():
            try:
                return self.decisions.get(timeout=0.1)
            except queue.Empty:
                pass
        raise InterruptedError("Translation cancelled.")

    def command(self, message):
        kind = message.get("kind")
        if kind == "cancel_title":
            self.title_cancel.set()
        elif kind == "suggest_title":
            if (self.thread and self.thread.is_alive()) or (self.title_thread and self.title_thread.is_alive()):
                raise ValueError("A browser operation is already running.")
            self.title_cancel = threading.Event()
            self.title_thread = threading.Thread(target=self.run_title, args=(message, self.title_cancel), daemon=True)
            self.title_thread.start()
        elif kind in ("preview", "cancel_preview"):
            self.preview_cancel.set()
            if kind == "cancel_preview":
                return
            self.preview_cancel = threading.Event()
            previous = self.preview_thread
            self.preview_thread = threading.Thread(target=self.preview, args=(message, self.preview_cancel, previous), daemon=True)
            self.preview_thread.start()
        elif kind == "settings":
            try:
                interval = float(message["scrollInterval"])
                if not 0.1 <= interval <= 5:
                    raise ValueError("Scroll interval must be between 0.1 and 5 seconds.")
            except (KeyError, TypeError, ValueError) as error:
                self.emit("settings_error", str(error))
                return
            self.scroll_interval = interval
        elif kind == "checkpoint_status":
            source = Path(message["input"])
            output = Path(message["output"])
            limit = int(message.get("chunkLimit", 275000))
            if not source.is_file():
                self.emit("checkpoint_status", {"exists": False, "requestId": message.get("requestId")})
                return
            try:
                source_text = read_text_file(source, message.get("encoding", "auto"), message.get("digest"))
                from novel_translator.text_processing import split_text
                chunks = split_text(source_text, limit)
                progress_dir = checkpoint_dir_for(output)
                fingerprint = source_fingerprint(f"{limit}\0{source_text}")
                completed = load_checkpoint(progress_dir, fingerprint, len(chunks))
                self.emit("checkpoint_status", {"exists": bool(completed), "completed": len(completed), "total": len(chunks), "requestId": message.get("requestId")})
            except Exception as error:
                self.emit("checkpoint_status", {"exists": False, "error": str(error), "requestId": message.get("requestId")})
        elif kind == "start":
            if (self.title_thread and self.title_thread.is_alive()) or (self.thread and self.thread.is_alive()):
                raise ValueError("A translation is already running.")
            errors = _validate_start_command(message)
            if errors:
                raise ValueError(errors[0])
            source = Path(message["input"])
            output = Path(message["output"])
            limit = int(message.get("chunkLimit", 275000))
            interval = float(message.get("scrollInterval", 0.5))
            remaining_limit = message.get("remainingChineseLimit", 50)
            sweeps = message.get("sweeps", 2)
            cover_options = message.get("cover", {"mode": "auto", "textColor": "#ffffff", "bgColor": "#0f131c"})
            self.cancel.clear()
            self.scroll_interval = interval
            self.pause.set()
            self.decisions = queue.Queue()
            context = PipelineContext(
                events=self.events,
                cancel_event=self.cancel,
                pause_event=self.pause,
                get_scroll_interval=lambda: self.scroll_interval,
                get_required_sweeps=lambda: sweeps,
                request_validation=self.decide,
                chunk_limit=limit,
                remaining_chinese_limit=remaining_limit,
                cover_options=cover_options,
                source_encoding=message.get("encoding", "auto"),
                source_digest=message.get("digest"),
            )
            self.thread = threading.Thread(target=self.run, args=(context, source, output))
            self.thread.start()
        elif kind == "fix_epub":
            if (self.title_thread and self.title_thread.is_alive()) or (self.thread and self.thread.is_alive()):
                raise ValueError("A run is already active.")
            source = Path(message["input"])
            output = Path(message["output"])
            if not source.is_file() or source.suffix.lower() != ".epub":
                raise ValueError(f"Source must be an existing EPUB file: {source}")
            if output.suffix.lower() != ".epub" or not output.parent.is_dir():
                raise ValueError("Choose an EPUB destination in an existing folder.")
            output = output.with_name(sanitize_filename(output.stem) + '.epub')
            if output.exists():
                raise FileExistsError("EPUB already exists; choose another name.")
            if output.resolve() == source.resolve():
                raise ValueError("Destination must differ from the source EPUB.")
            cover_options = message.get("cover") or {"mode": "auto", "textColor": "#ffffff", "bgColor": "#0f131c"}
            self.cancel.clear()
            self.thread = threading.Thread(target=self.run_fix, args=(source, output, cover_options))
            self.thread.start()
        elif kind == "pause":
            self.pause.clear()
        elif kind == "resume":
            self.pause.set()
        elif kind == "cancel":
            self.cancel.set()
            self.pause.set()
        elif kind == "decision":
            decision = message.get("value")
            if decision not in ("retry", "accept", "skip"):
                raise ValueError("Unknown validation decision.")
            self.decisions.put(decision)
        else:
            raise ValueError("Unknown command.")

    def run_title(self, message, cancel):
        reply = {"requestId": message.get("requestId")}
        try:
            from novel_translator.title import suggest_title
            reply["title"] = suggest_title(Path(message["input"]), cancel)
        except Exception as error:
            reply["error"] = str(error)
        self.title_thread = None
        self.emit("title_result", reply)

    def preview(self, message, cancel, previous):
        if previous:
            previous.join()
        if cancel.is_set():
            return
        reply = {"requestId": message.get("requestId")}
        try:
            source = Path(message["input"])
            if source.suffix.lower() != ".txt":
                raise ValueError("Choose a plain-text TXT file.")
            _, metadata = decode_source(source, message.get("encoding", "auto"), cancel=cancel)
            reply.update(metadata)
        except Exception as error:
            reply["error"] = str(error)
        if not cancel.is_set():
            self.emit("preview_result", reply)

    def run(self, context, source, output):
        handler = QueueHandler(self.events)
        logger = logging.getLogger("novel_translator.pipeline")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            self.runner_factory(context).run(source, output)
        except Exception as error:
            self.emit("error", str(error))
        finally:
            logger.removeHandler(handler)
            self.events.put(None)

    def run_fix(self, source, output, cover_options):
        try:
            from novel_translator.epub_builder import extract_epub_text, make_epub, read_epub_title
            self.emit("log", f"Reading {source.name}")
            self.emit("progress", 0.2)
            text = extract_epub_text(source)
            # The name chosen in the app wins over the old EPUB metadata, matching translate mode.
            title = re.sub(r"\s+fixed$", "", output.stem, flags=re.IGNORECASE).strip()
            title = title or read_epub_title(source) or source.stem
            self.emit("log", f"Rebuilding chapters and cover for \"{title}\"")
            self.emit("progress", 0.6)
            make_epub(text, output, title, cover_options)
            self.emit("progress", 1.0)
            self.emit("log", f"Saved {output}")
            self.emit("done", str(output))
        except Exception as error:
            self.emit("error", str(error))
        finally:
            self.events.put(None)

    def forward(self):
        while True:
            event = self.events.get()
            if event is None:
                if self.thread:
                    self.thread.join()
                self.emit("idle", None)
            else:
                self.emit(event.kind, event.value)


def _validate_start_command(message):
    errors = []
    source = Path(message["input"])
    output = Path(message["output"])
    if not source.is_file():
        errors.append(f"Source file not found: {source}")
    elif source.suffix.lower() != ".txt":
        errors.append(f"Source must be a TXT file: {source}")
    if output.suffix.lower() != ".epub" or not output.parent.is_dir():
        errors.append("Choose an EPUB destination in an existing folder.")
    if output.exists():
        errors.append("EPUB already exists; choose another name.")
    limit = int(message.get("chunkLimit", 275000))
    if limit not in CHUNK_PRESETS.values():
        errors.append("Unknown chunk preset.")
    if message.get("encoding", "auto") not in ("auto", *ENCODINGS):
        errors.append("Unsupported source encoding.")
    remaining_limit = message.get("remainingChineseLimit", 50)
    if type(remaining_limit) is not int or remaining_limit < 0:
        errors.append("Chinese character tolerance must be a nonnegative whole number.")
    sweeps = message.get("sweeps", 2)
    interval = float(message.get("scrollInterval", 0.5))
    if not 0.1 <= interval <= 5:
        errors.append("Scroll interval must be between 0.1 and 5 seconds.")
    if type(sweeps) is not int or not 1 <= sweeps <= 10:
        errors.append("Scroll passes must be a whole number between 1 and 10.")
    return errors


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    lock = threading.Lock()

    def emit(kind, value):
        with lock:
            print(json.dumps({"kind": kind, "value": value}, ensure_ascii=True), flush=True)

    bridge = Bridge(emit)
    threading.Thread(target=bridge.forward, daemon=True).start()
    emit("ready", None)
    for line in sys.stdin:
        try:
            bridge.command(json.loads(line))
        except Exception as error:
            emit("command_error", str(error))
    bridge.command({"kind": "cancel_title"})
    if bridge.title_thread:
        bridge.title_thread.join()
    bridge.command({"kind": "cancel_preview"})
    bridge.command({"kind": "cancel"})
    if bridge.thread:
        bridge.thread.join()


if __name__ == "__main__":
    main()