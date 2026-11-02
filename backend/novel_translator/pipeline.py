"""Display-independent translation pipeline.

This module owns the chunk loop that used to live inline in
``TranslatorApp._translate_worker`` (#11). It knows nothing about Tkinter or
customtkinter — it only emits ``UiEvent`` objects onto a queue and reads a
couple of thread-safe callables — so the whole orchestration can be unit-tested
without a display, and the GUI simply forwards events.

Chunk HTML is served from an in-memory map over HTTP (#10) instead of writing a
file to disk per chunk, so a run touches the filesystem only for checkpoints
and the final EPUB.
"""
from __future__ import annotations

import logging
import queue
import shutil
import tempfile
import uuid
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from .checkpoint import (
    checkpoint_dir_for,
    initialize_checkpoint,
    load_checkpoint,
    save_checkpoint_chunk,
)
from .chrome import (
    _ActiveClock,
    interruptible_lock,
    automate_chrome_translation,
    seed_chrome_profile,
    wait_for_auto_translation,
    wait_for_translation,
)
from .constants import (
    CHROME_GOTO_TIMEOUT_MS,
    CHROME_SETTLE_MS,
    CHUNK_ATTEMPT_BACKOFF_S,
    CHUNK_MAX_ATTEMPTS,
    CHUNK_PRESETS,
    UiEvent,
    sanitize_filename,
)
from .epub_builder import make_epub
from .text_processing import (
    chinese_count,
    chunk_html,
    read_text_file,
    source_fingerprint,
    split_text,
)
from .translation import validate_translated_chunk
from .worker_cleanup import BrowserCleanup

log = logging.getLogger("novel_translator.pipeline")

# Marker used for the Chrome Translate host mapping (must stay in sync with the
# ``--host-resolver-rules`` arg below).
CHUNK_HOST = "novel-translator.test"


class QueueHandler(logging.Handler):
    """Forward log records onto the GUI event queue as ``("log", message)`` (#12).

    The worker thread logs through the standard :mod:`logging` module; this
    handler is what ferries those records to the GUI thread's textbox.
    """

    def __init__(self, events: "queue.Queue[UiEvent]") -> None:
        super().__init__()
        self._events = events

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._events.put(UiEvent("log", self.format(record)))
        except Exception:  # never let logging kill the worker
            self.handleError(record)


class InMemoryChunkHandler(BaseHTTPRequestHandler):
    """Serve chunk HTML straight from a dict instead of disk (#10).

    ``server.chunks`` maps URL name -> raw HTML bytes. Unknown names 404.
    """

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        name = self.path.lstrip("/")
        body = self.server.chunks.get(name)  # type: ignore[attr-defined]
        if body is None:
            self.send_error(404, "Not Found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_chunk_server() -> "tuple[ThreadingHTTPServer, dict[str, bytes], int]":
    """Create a loopback HTTP server whose ``chunks`` dict is served from memory.

    Returns ``(server, chunks_map, port)``. The caller fills ``chunks_map`` with
    ``{filename: html_bytes}`` and navigates Chrome to the matching URL.
    """
    # Chrome preconnects can leave sockets idle; they must not block GETs or shutdown.
    server = ThreadingHTTPServer(("127.0.0.1", 0), InMemoryChunkHandler)
    chunks: dict[str, bytes] = {}
    server.chunks = chunks  # type: ignore[attr-defined]
    return server, chunks, server.server_address[1]


def chrome_pid_for(browser) -> "int | None":
    """Best-effort PID of the launched browser via a browser-level CDP session.

    ``SystemInfo.getProcessInfo`` is only supported on the browser target, so
    this must use ``browser.new_browser_cdp_session()`` rather than a page
    session (which always raises). ``browser`` may be ``None`` (#6).
    """
    if browser is None:
        return None
    try:
        session = browser.new_browser_cdp_session()
        try:
            info = session.send("SystemInfo.getProcessInfo")
        finally:
            session.detach()
    except Exception:
        return None
    for proc in info.get("processInfo", []):
        if proc.get("type") == "browser":
            pid = proc.get("id")
            return int(pid) if isinstance(pid, int) else None
    return None


@dataclass
class PipelineContext:
    """Everything the runner needs, injected by the caller (GUI or test)."""

    events: "queue.Queue[UiEvent]"
    cancel_event: threading.Event
    pause_event: threading.Event
    get_scroll_interval: Callable[[], float]
    get_required_sweeps: Callable[[], int]
    worker_count: int = field(default=1, kw_only=True)
    source_encoding: str = field(default="auto", kw_only=True)
    source_digest: str | None = field(default=None, kw_only=True)
    remaining_chinese_limit: int = field(default=0, kw_only=True)
    request_validation: Callable[[int, str], str]
    chunk_limit: int = field(default_factory=lambda: CHUNK_PRESETS["Balanced (275k)"])
    cover_options: dict = field(default_factory=lambda: {"mode": "auto", "textColor": "#ffffff", "bgColor": "#0f131c"})


class TranslationRunner:
    """Runs the full read -> chunk -> translate -> EPUB pipeline (#11)."""

    def __init__(self, ctx: PipelineContext) -> None:
        if type(ctx.worker_count) is not int or not 1 <= ctx.worker_count <= 3:
            raise ValueError("Worker count must be an integer between 1 and 3.")
        self.ctx = ctx
        self.validation_lock = threading.Lock()

    def _emit(self, kind: str, value: object) -> None:
        self.ctx.events.put(UiEvent(kind, value))

    # One place that turns a log message into a GUI event (#14).
    def _report(self, message: str) -> None:
        self._emit("log", message)

    def run(self, input_path: Path, output_path: Path) -> None:
        work_dir = Path(tempfile.mkdtemp(prefix="novel-translator-"))
        workers = []
        stop = threading.Event()
        server: HTTPServer | None = None
        server_thread: threading.Thread | None = None
        try:
            # Defense-in-depth: the output filename may carry characters that
            # are illegal on Windows (a novel title like "Blissful Skill: ...")
            # if it arrived via saved settings or the Browse dialog rather than
            # the sanitizing GUI path. Rebuild the final path from a sanitized
            # stem so file/checkpoint operations can't fail with WinError 267.
            safe_output = output_path.with_name(
                f"{sanitize_filename(output_path.stem)}{output_path.suffix}"
            )
            if safe_output != output_path:
                log.info("Sanitized output filename to %s", safe_output.name)
                output_path = safe_output
            if output_path.exists():
                raise FileExistsError(f"EPUB already exists; choose another name: {output_path}")
            log.info("Reading %s", input_path.name)
            source_text = read_text_file(input_path, self.ctx.source_encoding, self.ctx.source_digest)
            chunk_limit = self.ctx.chunk_limit
            chunks = split_text(source_text, chunk_limit)
            if not chunks:
                raise ValueError("The input file is empty.")
            log.info("Prepared %d chunk(s), up to %d characters each.", len(chunks), chunk_limit)
            self._report("Scroll settings can be changed while this run is active.")

            progress_dir = checkpoint_dir_for(output_path)
            fingerprint = source_fingerprint(f"{chunk_limit}\0{source_text}")
            completed = load_checkpoint(progress_dir, fingerprint, len(chunks))
            initialize_checkpoint(progress_dir, fingerprint, len(chunks))
            statuses = [
                "Complete" if index in completed else "Pending"
                for index in range(1, len(chunks) + 1)
            ]
            self._emit("chunk_statuses", statuses)
            self._emit("run_info", (len(completed), len(chunks), 0))
            if completed:
                log.info("Resuming with %d/%d saved chunks.", len(completed), len(chunks))
                self._emit("progress", len(completed) / len(chunks))

            server, chunk_map, port = make_chunk_server()
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            jobs = queue.Queue()
            results = queue.Queue()
            for index, chunk in enumerate(chunks, 1):
                if index not in completed:
                    jobs.put((index, chunk))
            pending = jobs.qsize()
            count = min(self.ctx.worker_count, pending)
            for worker_id in range(1, count + 1):
                worker = threading.Thread(
                    target=self._worker,
                    args=(worker_id, work_dir, jobs, results, chunk_map, port, len(chunks), stop),
                    name=f"translation-{worker_id}", daemon=True,
                )
                workers.append(worker)
                worker.start()
            finished = 0
            while finished < count:
                _ActiveClock(self.ctx.pause_event, self.ctx.cancel_event).now()
                try:
                    kind, worker_id, index, value = results.get(timeout=0.1)
                except queue.Empty:
                    continue
                if kind == "finished":
                    finished += 1
                    self._worker_status(worker_id, 0, "Idle", 0.0)
                    continue
                if kind == "fatal":
                    raise RuntimeError(f"Worker {worker_id} failed: {value}")
                if kind == "started":
                    statuses[index - 1] = "Translating"
                    self._worker_status(worker_id, index, "Translating", 0.0)
                    self._emit("current_chunk", (index, 0.0))
                    self._emit("status", f"Translating {index}/{len(chunks)}")
                elif kind == "validation":
                    self._worker_status(worker_id, index, "Awaiting validation", 0.0)
                elif kind == "progress":
                    self._worker_status(worker_id, index, "Translating", value)
                    self._emit("drop_rate", (index, value))
                elif kind == "result":
                    result, elapsed = value
                    if result:
                        save_checkpoint_chunk(progress_dir, index, result)
                        completed[index] = result
                        statuses[index - 1] = "Complete"
                        self._worker_status(worker_id, index, "Complete", 1.0)
                        self._emit("stats", (len(result), elapsed, index, 100.0, False))
                        self._emit("progress", len(completed) / len(chunks))
                    else:
                        statuses[index - 1] = "Failed"
                        self._worker_status(worker_id, index, "Failed", 0.0)
                        self._emit("stats", (0, 0.0, index, 0.0, True))
                    self._emit("run_info", (len(completed), len(chunks), statuses.count("Failed")))
                self._emit("chunk_statuses", statuses.copy())

            if self.ctx.cancel_event.is_set():
                raise InterruptedError("Translation cancelled.")
            failed_count = len(chunks) - len(completed)
            if failed_count:
                self._emit("incomplete", failed_count)
                return
            self._emit("status", "Building EPUB")
            make_epub(
                "\n\n".join(completed[index] for index in range(1, len(chunks) + 1)),
                output_path, output_path.stem, cover_options=self.ctx.cover_options,
            )
            shutil.rmtree(progress_dir, ignore_errors=True)
            self._emit("done", str(output_path))
        except (Exception, PlaywrightTimeoutError) as exc:
            self._emit("error", str(exc) or exc.__class__.__name__)
        finally:
            stop.set()
            deadline = time.monotonic() + 10.0
            for worker in workers:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
            if server is not None:
                try:
                    # Only shut down while serve_forever() is actually running;
                    # calling shutdown() otherwise would block forever.
                    if server_thread is not None and server_thread.is_alive():
                        server.shutdown()
                        server_thread.join(timeout=2)
                    server.server_close()
                except Exception:
                    pass
            if any(worker.is_alive() for worker in workers):
                self._report(f"Browser cleanup exceeded 10 seconds; isolated profiles retained at {work_dir}.")
            else:
                shutil.rmtree(work_dir, ignore_errors=True)

    def _worker_status(self, worker_id, index, status, progress):
        self._emit("worker_status", {"workerId": worker_id, "index": index,
                                     "status": status, "progress": progress})

    def _worker(self, worker_id, work_dir, jobs, results, chunk_map, port, total, stop):
        cancel = _RunCancellation(self.ctx.cancel_event, stop)
        profile_dir = work_dir / f"chrome-profile-{worker_id}"
        index = 0
        cleanup = None
        try:
            seed_chrome_profile(profile_dir)
            with sync_playwright() as playwright:
                cleanup = BrowserCleanup(playwright, cancel)
                try:
                    self._consume_jobs(worker_id, playwright, profile_dir, jobs, results,
                                       chunk_map, port, total, cancel, cleanup)
                finally:
                    cleanup.closing.set()
        except InterruptedError:
            pass
        except Exception as exc:
            results.put(("fatal", worker_id, index, str(exc)))
        finally:
            if cleanup is not None:
                cleanup.finish()
            results.put(("finished", worker_id, 0, None))

    def _consume_jobs(self, worker_id, playwright, profile_dir, jobs, results,
                      chunk_map, port, total, cancel, cleanup):
        context = None
        page = None
        try:
            while True:
                _ActiveClock(self.ctx.pause_event, cancel).now()
                try:
                    index, chunk = jobs.get_nowait()
                except queue.Empty:
                    return
                chunk_name = f"chunk_{index:04d}.html"
                chunk_map[chunk_name] = chunk_html(chunk, f"Chunk {index}").encode("utf-8")
                results.put(("started", worker_id, index, None))
                try:
                    result = ""
                    chunk_started = time.monotonic()
                    last_error: Exception | None = None
                    attempt_clock = _ActiveClock(self.ctx.pause_event, cancel)
                    for attempt in range(1, CHUNK_MAX_ATTEMPTS + 1):
                        try:
                            attempt_clock.now()
                            log.info(
                                "Translating chunk %d/%d (attempt %d/%d)...",
                                index, total, attempt, CHUNK_MAX_ATTEMPTS,
                            )
                            if page is None or page.is_closed():
                                if context is not None:
                                    try:
                                        context.close()
                                    except Exception:
                                        pass
                                    context = None
                                context = playwright.chromium.launch_persistent_context(
                                    str(profile_dir), channel="chrome", headless=False,
                                    locale="en-US", timeout=20000,
                                    args=[
                                        "--lang=en-US",
                                        "--enable-features=Translate,TranslateUI,AutoTranslate",
                                        "--disable-features=TranslateLanguageByULP",
                                        f"--host-resolver-rules=MAP {CHUNK_HOST} 127.0.0.1",
                                        "--window-position=100,100",
                                    ],
                                )
                                page = context.pages[0] if context.pages else context.new_page()
                                # Persistent contexts still expose the Browser, so
                                # resolve its PID once at launch and cache it (#6).
                                page._novel_chrome_pid = chrome_pid_for(context.browser)
                                cleanup.track_browser(page._novel_chrome_pid)
                                log.info("Chrome ready; reusing this window for all chunks.")
                            page.goto(
                                f"http://{CHUNK_HOST}:{port}/{chunk_name}",
                                wait_until="domcontentloaded", timeout=CHROME_GOTO_TIMEOUT_MS,
                            )
                            page.wait_for_timeout(CHROME_SETTLE_MS)
                            # The browser PID was resolved once at launch and cached
                            # on the page; passing it lets chrome.py match by PID (#6).
                            chrome_pid = getattr(page, "_novel_chrome_pid", None)

                            def retry_translation() -> bool:
                                if cancel.is_set():
                                    raise InterruptedError("Translation cancelled.")
                                return automate_chrome_translation(page, self._report, chrome_pid,
                                                                   cancel=cancel, pause_event=self.ctx.pause_event)

                            original_count = chinese_count(chunk)
                            log.info("Page ready; waiting for Chrome auto-translate...")
                            if wait_for_auto_translation(
                                page, original_count, cancel,
                                pause_event=self.ctx.pause_event,
                            ):
                                log.info("Chrome auto-translate detected; no UI fallback needed.")
                            else:
                                log.info(
                                    "Chrome auto-translate did not start; using pywinauto fallback."
                                )
                                retry_translation()
                            candidate = wait_for_translation(
                                page, original_count, cancel,
                                self._report, retry_translation,
                                self.ctx.get_scroll_interval, self.ctx.get_required_sweeps,
                                pause_event=self.ctx.pause_event,
                                report_progress=lambda ratio: results.put(("progress", worker_id, index,
                                    max(0.0, min(1.0, 1.0 - ratio)))),
                                remaining_chinese_limit=self.ctx.remaining_chinese_limit,
                            )
                            attempt_clock.now()
                            valid, reason = validate_translated_chunk(
                                chunk, candidate, self.ctx.remaining_chinese_limit
                            )
                            if not valid:
                                with interruptible_lock(self.validation_lock, cancel, self.ctx.pause_event):
                                    results.put(("validation", worker_id, index, None))
                                    decision = self.ctx.request_validation(index, reason)
                                    attempt_clock.now()
                                    results.put(("started", worker_id, index, None))
                                if decision == "retry":
                                    raise ValueError(f"Chunk validation failed: {reason}.")
                                if decision == "skip":
                                    candidate = (
                                        "[Chunk skipped after failed translation validation.]"
                                    )
                                elif decision == "accept":
                                    log.info(
                                        "Chunk %d accepted despite validation warning.", index
                                    )
                                else:
                                    raise ValueError("Unknown validation decision.")
                            result = candidate
                            break
                        except InterruptedError:
                            raise
                        except Exception as exc:
                            last_error = exc
                            log.warning("Chunk %d attempt %d failed: %s", index, attempt, exc)
                            if attempt < CHUNK_MAX_ATTEMPTS:
                                log.info("Retrying the current chunk with a fresh navigation...")
                                retry_at = attempt_clock.now() + CHUNK_ATTEMPT_BACKOFF_S
                                while attempt_clock.now() < retry_at:
                                    time.sleep(0.1)
                    attempt_clock.now()
                    results.put(("result", worker_id, index, (result, time.monotonic() - chunk_started)))
                finally:
                    chunk_map.pop(chunk_name, None)
        finally:
            cleanup.closing.set()
            if context is not None:
                try:
                    context.close()
                except Exception:
                    log.exception("Worker %d Chrome close failed", worker_id)


class _RunCancellation:
    def __init__(self, external, internal):
        self.external = external
        self.internal = internal

    def is_set(self):
        return self.external.is_set() or self.internal.is_set()
