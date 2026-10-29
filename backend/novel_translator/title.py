"""Opt-in Chrome title translation, isolated so cancellation has a hard bound."""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .constants import CHROME_ERROR_MARKERS, sanitize_filename
from .text_processing import chinese_count


def validate_title(source: str, candidate: str) -> str:
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError("Chrome returned an empty title.")
    candidate = candidate.strip()
    if len(candidate) > 500 or any(ord(c) < 32 for c in candidate) or chinese_count(candidate) or not re.search(r"[A-Za-z]", candidate):
        raise ValueError("Chrome did not return a usable English title.")
    if any(marker in candidate.lower() for marker in CHROME_ERROR_MARKERS) or '\ufffd' in candidate:
        raise ValueError("Chrome returned an error instead of a title.")
    if chinese_count(source) and candidate == source:
        raise ValueError("Chrome left the title untranslated.")
    return sanitize_filename(candidate)


ERRORS = {
    'activation': 'Chrome could not activate filename translation to English. Keep the original name or edit it manually.',
    'failed': 'Chrome reported that this page could not be translated. Check Chrome translation/network availability and try again.',
    'stalled': 'Chrome filename translation stalled: no usable English title appeared.',
}


class TitleTranslationError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(ERRORS[code])


def translation_failed(page):
    # The failure bubble is browser UI, not part of the translated document.
    from .chrome import _get_desktop_class
    Desktop = _get_desktop_class()
    pid = getattr(page, '_novel_chrome_pid', None)
    if Desktop is None or not isinstance(pid, int):
        return False
    try:
        chrome = Desktop(backend='uia').window(process=pid, title_re=r'.*NovelTL title suggestion.*')
        return chrome.child_window(title_re=r"(?i)^This page couldn.t be translated$", control_type='Text').exists(timeout=0.1)
    except Exception:
        return False


def translate_page(page, source, cancel, retry):
    previous = None
    stable = 0
    started = time.monotonic()
    deadline = started + 25
    retried = False
    while time.monotonic() < deadline:
        if cancel.is_set():
            raise InterruptedError("Title translation cancelled.")
        candidate = page.locator("#novel-title").inner_text(timeout=2000).strip()
        try:
            title = validate_title(source, candidate)
        except ValueError:
            stable = 0
        else:
            stable = stable + 1 if candidate == previous else 0
            if stable >= 2:
                return title
        previous = candidate
        # Page-wide translated CSS is not proof that this short title changed.
        if translation_failed(page):
            raise TitleTranslationError('failed')
        if not retried and time.monotonic() - started >= 5:
            activated = retry()
            if cancel.is_set():
                raise InterruptedError("Title translation cancelled.")
            if translation_failed(page):
                raise TitleTranslationError('failed')
            if not activated:
                raise TitleTranslationError('activation')
            retried = True
            # UI automation consumes the same budget; never restart the deadline.
        page.wait_for_timeout(500)
    raise TitleTranslationError('stalled')


def report_stage(work: Path, stage: str) -> None:
    # Windows communicate() cannot expose partial stderr while its reader is blocked.
    (work / 'stage').write_text(stage, encoding='utf-8')


def last_stage(work: Path, fallback: str) -> str:
    try:
        stage = (work / 'stage').read_text(encoding='utf-8')
    except OSError:
        return fallback
    if stage in {'importing title dependencies', 'starting Playwright', 'waiting for title request', 'starting Chrome', 'loading title page', 'activating translation', 'reading translated title', 'closing Chrome', 'stopping local server'}:
        return stage
    return fallback


def chrome_title(source: str | None, work: Path) -> str | None:
    report_stage(work, 'importing title dependencies')
    from playwright.sync_api import sync_playwright
    from .chrome import automate_chrome_translation, seed_chrome_profile
    from .pipeline import CHUNK_HOST, chrome_pid_for, make_chunk_server
    import html

    profile = work / "profile"
    seed_chrome_profile(profile)
    server, pages, port = make_chunk_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        report_stage(work, 'starting Playwright')
        with sync_playwright() as playwright:
            if source is None:
                report_stage(work, 'waiting for title request')
                print(json.dumps({'kind': 'ready', 'version': 1}), flush=True)
                line = sys.stdin.readline()
                if not line:
                    return None
                request = json.loads(line)
                if not isinstance(request, dict) or request.get('kind') != 'translate' or not isinstance(request.get('title'), str):
                    raise ValueError('Invalid title worker request.')
                source = request['title']
            pages["title.html"] = ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>NovelTL title suggestion</title></head><body><p id='novel-title'>" + html.escape(source) + "</p></body></html>").encode("utf-8")
            report_stage(work, 'starting Chrome')
            context = playwright.chromium.launch_persistent_context(
                str(profile), channel="chrome", headless=False, locale="en-US", timeout=20000,
                args=["--lang=en-US", "--enable-features=Translate,TranslateUI,AutoTranslate", "--disable-features=TranslateLanguageByULP", f"--host-resolver-rules=MAP {CHUNK_HOST} 127.0.0.1"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                report_stage(work, 'loading title page')
                page.goto(f"http://{CHUNK_HOST}:{port}/title.html", wait_until="domcontentloaded", timeout=10000)
                pid = chrome_pid_for(context.browser)
                page._novel_chrome_pid = pid
                def retry():
                    report_stage(work, 'activating translation')
                    activated = automate_chrome_translation(page, lambda _: None, pid, ensure_english=True)
                    report_stage(work, 'reading translated title')
                    return activated
                report_stage(work, 'reading translated title')
                return translate_page(page, source, threading.Event(), retry)
            finally:
                report_stage(work, 'closing Chrome')
                context.close()
    finally:
        report_stage(work, 'stopping local server')
        server.shutdown()
        thread.join()
        server.server_close()


def launch_title_worker(work: Path):
    return subprocess.Popen(
        [sys.executable, '-u', '-m', 'novel_translator.title', '--worker', str(work)],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, 'TEMP': str(work), 'TMP': str(work)},
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )


def await_ready(process, cancel, deadline, work):
    lines = queue.Queue()

    def read_ready():
        try:
            lines.put(process.stdout.readline(4097))
        except OSError:
            lines.put(b'')

    reader = threading.Thread(target=read_ready, daemon=True)
    reader.start()
    while True:
        if cancel.is_set():
            raise InterruptedError('Title translation cancelled.')
        if time.monotonic() >= deadline:
            stage = last_stage(work, 'starting title worker')
            raise TimeoutError(f'Title translation timed out during {stage}. Keep the original name or edit it manually.')
        try:
            line = lines.get(timeout=0.1)
            break
        except queue.Empty:
            continue
    reader.join()
    try:
        event = json.loads(line)
    except (ValueError, UnicodeError):
        raise ValueError('Title worker exited before readiness.' if not line else 'Invalid title worker readiness response.') from None
    if event != {'kind': 'ready', 'version': 1}:
        raise ValueError('Invalid title worker readiness response.')
    if cancel.is_set():
        raise InterruptedError('Title translation cancelled.')


def suggest_title(source: Path, cancel: threading.Event, timeout: float = 90) -> str:
    if not source.is_file() or source.suffix.lower() != ".txt":
        raise ValueError("Select an existing TXT source first.")
    work = Path(__file__).resolve().parents[1] / (".title-" + uuid.uuid4().hex)
    work.mkdir()
    process = None
    try:
        process = launch_title_worker(work)
        deadline = time.monotonic() + timeout
        await_ready(process, cancel, deadline, work)
        request = json.dumps({'kind': 'translate', 'title': source.stem}, ensure_ascii=True).encode('utf-8') + b'\n'
        stage = 'waiting for title request'
        while True:
            if cancel.is_set():
                raise InterruptedError("Title translation cancelled.")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Title translation timed out during {stage}. Keep the original name or edit it manually.")
            try:
                stdout, stderr = process.communicate(input=request, timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                request = None
                stage = last_stage(work, stage)
        if process.returncode:
            # Only structured, allowlisted errors cross the worker boundary.
            try:
                code = json.loads(stdout.decode('utf-8')).get('error')
            except (ValueError, AttributeError):
                code = None
            if code in ERRORS:
                raise TitleTranslationError(code)
            stage = last_stage(work, stage)
            raise ValueError(f"Chrome could not translate the filename during {stage}. Keep the original name or edit it manually.")
        return validate_title(source.stem, json.loads(stdout.decode("utf-8")))
    finally:
        if process is not None and process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10)
            else:
                process.kill()
            process.communicate(timeout=10)
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        result = chrome_title(None if sys.argv[1] == '--worker' else sys.argv[1], Path(sys.argv[2]))
    except TitleTranslationError as error:
        print(json.dumps({'error': error.code}), flush=True)
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=True), flush=True)
