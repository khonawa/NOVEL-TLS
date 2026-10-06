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
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
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


def make_chunk_server() -> "tuple[HTTPServer, dict[str, bytes], int]":
    """Create a loopback HTTP server whose ``chunks`` dict is served from memory.

    Returns ``(server, chunks_map, port)``. The caller fills ``chunks_map`` with
    ``{filename: html_bytes}`` and navigates Chrome to the matching URL.
    """
    server = HTTPServer(("127.0.0.1", 0), InMemoryChunkHandler)
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
    remaining_chinese_limit: int = field(default=0, kw_only=True)
    # Returns "retry" | "accept" | "skip" for a chunk that failed validation.
    request_validation: Callable[[int, str], str]
    chunk_limit: int = field(default_factory=lambda: CHUNK_PRESETS["Balanced (275k)"])


class TranslationRunner:
    """Runs the full read -> chunk -> translate -> EPUB pipeline (#11)."""

    def __init__(self, ctx: PipelineContext) -> None:
        self.ctx = ctx

    def _emit(self, kind: str, value: object) -> None:
        self.ctx.events.put(UiEvent(kind, value))

    # One place that turns a log message into a GUI event (#14).
    def _report(self, message: str) -> None:
        self._emit("log", message)

    def run(self, input_path: Path, output_path: Path) -> None:
        work_dir = Path(tempfile.mkdtemp(prefix="novel-translator-"))
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
            log.info("Reading %s", input_path.name)
            source_text = read_text_file(input_path)
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

            profile_dir = work_dir / "chrome-profile"
            seed_chrome_profile(profile_dir)

            server, chunk_map, port = make_chunk_server()
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()

            def _clear_profile_locks() -> None:
                # A crashed Chrome can leave a SingletonLock that makes the next
                # launch fail with "profile in use"; clear it first (#17).
                for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                    try:
                        (profile_dir / lock_name).unlink(missing_ok=True)
                    except OSError:
                        pass

            with sync_playwright() as playwright:
                # Launch Chrome once and reuse it across chunks (#7); each chunk
                # just navigates the same page, so a run no longer pays a browser
                # relaunch (profile spin-up + window + CDP) per chunk.
                context = None
                page = None
                for index, chunk in enumerate(chunks, start=1):
                    if self.ctx.cancel_event.is_set():
                        raise InterruptedError("Translation cancelled.")
                    if index in completed:
                        log.info("Chunk %d/%d restored from saved progress.", index, len(chunks))
                        continue
                    statuses[index - 1] = "Translating"
                    self._emit("chunk_statuses", statuses.copy())
                    self._emit("current_chunk", (index, 0.0))
                    # Serve this chunk from memory; no per-chunk file on disk (#10).
                    chunk_name = f"chunk_{index:04d}.html"
                    chunk_map[chunk_name] = chunk_html(chunk, f"Chunk {index}").encode("utf-8")
                    self._emit("status", f"Translating {index}/{len(chunks)}")

                    result = ""
                    chunk_started = time.monotonic()
                    last_error: Exception | None = None
                    for attempt in range(1, CHUNK_MAX_ATTEMPTS + 1):
                        try:
                            log.info(
                                "Translating chunk %d/%d (attempt %d/%d)...",
                                index, len(chunks), attempt, CHUNK_MAX_ATTEMPTS,
                            )
                            if page is None or page.is_closed():
                                if context is not None:
                                    try:
                                        context.close()
                                    except Exception:
                                        pass
                                    context = None
                                _clear_profile_locks()
                                context = playwright.chromium.launch_persistent_context(
                                    str(profile_dir), channel="chrome", headless=False,
                                    locale="en-US",
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
                                return automate_chrome_translation(page, self._report, chrome_pid)

                            original_count = chinese_count(chunk)
                            log.info("Page ready; waiting for Chrome auto-translate...")
                            if wait_for_auto_translation(
                                page, original_count, self.ctx.cancel_event,
                                pause_event=self.ctx.pause_event,
                            ):
                                log.info("Chrome auto-translate detected; no UI fallback needed.")
                            else:
                                log.info(
                                    "Chrome auto-translate did not start; using pywinauto fallback."
                                )
                                retry_translation()
                            candidate = wait_for_translation(
                                page, original_count, self.ctx.cancel_event,
                                self._report, retry_translation,
                                self.ctx.get_scroll_interval, self.ctx.get_required_sweeps,
                                pause_event=self.ctx.pause_event,
                                report_progress=lambda ratio: self._emit(
                                    "drop_rate", (index, max(0.0, min(1.0, 1.0 - ratio)))
                                ),
                                remaining_chinese_limit=self.ctx.remaining_chinese_limit,
                            )
                            valid, reason = validate_translated_chunk(
                                chunk, candidate, self.ctx.remaining_chinese_limit
                            )
                            if not valid:
                                decision = self.ctx.request_validation(index, reason)
                                if decision == "retry":
                                    raise ValueError(f"Chunk validation failed: {reason}.")
                                if decision == "skip":
                                    candidate = (
                                        "[Chunk skipped after failed translation validation.]"
                                    )
                                else:
                                    log.info(
                                        "Chunk %d accepted despite validation warning.", index
                                    )
                            result = candidate
                            break
                        except InterruptedError:
                            raise
                        except Exception as exc:
                            last_error = exc
                            log.warning("Chunk %d attempt %d failed: %s", index, attempt, exc)
                            if attempt < CHUNK_MAX_ATTEMPTS:
                                log.info("Restarting Chrome and retrying this chunk...")
                        finally:
                            if attempt < CHUNK_MAX_ATTEMPTS and not result:
                                time.sleep(CHUNK_ATTEMPT_BACKOFF_S)
                    if not result:
                        statuses[index - 1] = "Failed"
                        self._emit("chunk_statuses", statuses.copy())
                        log.warning(
                            "Chunk %d marked failed after %d attempts: %s",
                            index, CHUNK_MAX_ATTEMPTS, last_error,
                        )
                        self._emit("run_info", (len(completed), len(chunks), statuses.count("Failed")))
                        self._emit("stats", (0, 0.0, index, 0.0, True))
                        continue
                    save_checkpoint_chunk(progress_dir, index, result)
                    completed[index] = result
                    statuses[index - 1] = "Complete"
                    self._emit("chunk_statuses", statuses.copy())
                    chunk_map.pop(chunk_name, None)
                    self._emit("stats", (len(result), time.monotonic() - chunk_started, index, 100.0, False))
                    self._emit("progress", len(completed) / len(chunks))
                    self._emit("run_info", (len(completed), len(chunks), statuses.count("Failed")))
                    log.info(
                        "Chunk %d validated and saved (%d translated characters).",
                        index, len(result),
                    )
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass

            if self.ctx.cancel_event.is_set():
                raise InterruptedError("Translation cancelled.")
            failed_count = len(chunks) - len(completed)
            if failed_count:
                self._emit("incomplete", failed_count)
                return
            self._emit("status", "Building EPUB")
            make_epub(
                "\n\n".join(completed[index] for index in range(1, len(chunks) + 1)),
                output_path, sanitize_filename(input_path.stem),
            )
            shutil.rmtree(progress_dir, ignore_errors=True)
            self._emit("done", str(output_path))
        except (Exception, PlaywrightTimeoutError) as exc:
            self._emit("error", str(exc) or exc.__class__.__name__)
        finally:
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
            shutil.rmtree(work_dir, ignore_errors=True)
