from __future__ import annotations

import json
import logging
import queue
import sys
import threading
from pathlib import Path

from novel_translator.pipeline import PipelineContext, QueueHandler, TranslationRunner


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
        if kind == "settings":
            try:
                interval = float(message["scrollInterval"])
                if not 0.1 <= interval <= 5:
                    raise ValueError("Scroll interval must be between 0.1 and 5 seconds.")
            except (KeyError, TypeError, ValueError) as error:
                self.emit("settings_error", str(error))
                return
            self.scroll_interval = interval
        elif kind == "start":
            if self.thread and self.thread.is_alive():
                raise ValueError("A translation is already running.")
            source = Path(message["input"])
            output = Path(message["output"])
            if not source.is_file():
                raise ValueError(f"Source file not found: {source}")
            if source.suffix.lower() != ".txt":
                raise ValueError(f"Source must be a TXT file: {source}")
            if output.suffix.lower() != ".epub" or not output.parent.is_dir():
                raise ValueError("Choose an EPUB destination in an existing folder.")
            limit = int(message.get("chunkLimit", 275000))
            if limit not in (125000, 275000, 450000):
                raise ValueError("Unknown chunk preset.")
            interval = float(message.get("scrollInterval", 0.5))
            remaining_limit = message.get("remainingChineseLimit", 50)
            if type(remaining_limit) is not int or remaining_limit < 0:
                raise ValueError("Chinese character tolerance must be a nonnegative whole number.")
            sweeps = message.get("sweeps", 2)
            if not 0.1 <= interval <= 5:
                raise ValueError("Scroll interval must be between 0.1 and 5 seconds.")
            if type(sweeps) is not int or not 1 <= sweeps <= 10:
                raise ValueError("Scroll passes must be a whole number between 1 and 10.")
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
            )
            self.thread = threading.Thread(target=self.run, args=(context, source, output))
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

    def forward(self):
        while True:
            event = self.events.get()
            if event is None:
                if self.thread:
                    self.thread.join()
                self.emit("idle", None)
            else:
                self.emit(event.kind, event.value)


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
    bridge.command({"kind": "cancel"})
    if bridge.thread:
        bridge.thread.join()


if __name__ == "__main__":
    main()