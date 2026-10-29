from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

from playwright.sync_api import Page

from .constants import (
    CHROME_GOTO_TIMEOUT_MS,
    CHROME_ERROR_MARKERS,
    CLEANUP_REVIEW_MAX_RATIO,
    TARGETED_SCROLL_MIN_INTERVAL,
)
from .text_processing import chinese_count
from .translation import TranslationWatchdog


class _ActiveClock:
    def __init__(self, pause_event: threading.Event | None, cancel: threading.Event) -> None:
        self.pause_event = pause_event
        self.cancel = cancel
        self.paused_seconds = 0.0

    def now(self) -> float:
        if self.cancel.is_set():
            raise InterruptedError("Translation cancelled.")
        if self.pause_event is not None and not self.pause_event.is_set():
            started = time.monotonic()
            _pace(self.pause_event, self.cancel)
            self.paused_seconds += time.monotonic() - started
        if self.cancel.is_set():
            raise InterruptedError("Translation cancelled.")
        return time.monotonic() - self.paused_seconds


def _get_desktop_class():
    """Import pywinauto's Desktop lazily.

    pywinauto initializes COM at import time (forcing a specific threading
    apartment), which breaks tkinterdnd2's OLE2 drag-and-drop if it happens
    before the GUI starts. Since UI automation is only a translation fallback,
    we import it on first use instead of at module load. Returns None when
    pywinauto isn't installed.
    """
    try:
        from pywinauto import Desktop
        return Desktop
    except ImportError:
        return None


def seed_chrome_profile(profile_dir: Path) -> None:
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    preferences = {
        "intl": {"accept_languages": "en-US,en"},
        "translate_accepted_count": {"zh": 100, "zh-CN": 100},
        "translate_denied_count_for_language": {},
        "translate_allowlanguages": ["en"],
        "translate_allowlists": {"zh": "en", "zh-CN": "en"},
        "translate_blocked_languages": [],
        "translate_recent_target": "en",
        "translate": {
            "enabled": True,
            "translate_whitelists": {"zh": "en", "zh-CN": "en"},
            "blocked_languages": [],
            "site_blacklist": [],
            "too_often_denied": False,
        },
    }
    (default_dir / "Preferences").write_text(json.dumps(preferences), encoding="utf-8")


def automate_chrome_translation(
    page: Page,
    report: Callable[[str], None],
    pid: "int | None" = None,
    *,
    ensure_english: bool = False,
) -> bool:
    """Invoke Chrome's native page-translation menu through Windows UI Automation."""
    Desktop = _get_desktop_class()
    if Desktop is None:
        report("Windows UI automation is unavailable; use the Chrome context menu manually.")
        return False
    try:
        page.bring_to_front()
        page.wait_for_timeout(500)
        page_title = page.title()
        # Prefer the launched browser's own process so we never drive an
        # unrelated Chrome window whose title happens to match (#6).
        desktop = Desktop(backend="uia")
        if pid:
            try:
                chrome = desktop.window(process=pid, title_re=rf".*{re.escape(page_title)}.*")
                chrome.wait("visible", timeout=3)
            except Exception:
                if ensure_english:
                    raise
                chrome = desktop.window(title_re=rf".*{re.escape(page_title)}.*Chrome.*")
                chrome.wait("visible", timeout=10)
        else:
            chrome = desktop.window(title_re=rf".*{re.escape(page_title)}.*Chrome.*")
            chrome.wait("visible", timeout=10)
        chrome.restore()
        chrome.set_focus()
        try_again = chrome.child_window(
            title_re=r"(?i)^Try again$", control_type="Button"
        )
        if not ensure_english and try_again.exists(timeout=1):
            try_again.wait("visible enabled", timeout=3)
            try:
                try_again.invoke()
            except Exception:
                try_again.click_input()
            report("Chrome translation failed; pywinauto clicked Try again.")
            return True
        # Use content coordinates, not screen coordinates interpreted as window-relative.
        viewport = page.viewport_size or page.evaluate("() => ({width: innerWidth, height: innerHeight})")
        page.mouse.click(viewport["width"] // 2, viewport["height"] // 2, button="right")
        menu = Desktop(backend="uia").window(class_name="#32768")
        menu.wait("visible", timeout=5)
        translation_item = menu.child_window(title_re=r"(?i).*translate.*", control_type="MenuItem")
        translation_item.wait("visible enabled", timeout=8)
        try:
            translation_item.invoke()
        except Exception:
            translation_item.click_input()
        page.wait_for_timeout(750)
        english_control = chrome.child_window(
            title_re=r"(?i)^English$", control_type="TabItem"
        )
        if not english_control.exists(timeout=2):
            english_control = chrome.child_window(
                title_re=r"(?i)^English$", control_type="Button"
            )
        if not english_control.exists(timeout=2):
            english_control = chrome.child_window(title_re=r"(?i)^English$")
        english_control.wait("visible enabled", timeout=5)
        try:
            english_control.invoke()
        except Exception:
            english_control.click_input()
        report("pywinauto fallback selected Translate and clicked English.")
        return True
    except Exception as exc:
        report(f"Automatic Chrome menu selection failed ({exc}); choose Translate to English manually.")
        try:
            Desktop(backend="uia").window(class_name="#32768").press_escape()
        except Exception:
            pass
        return False


def _pace(pause_event: threading.Event | None, cancel: threading.Event) -> None:
    """Block while paused (used by F2 pause/resume); bail out if cancelled."""
    if pause_event is None:
        return
    while not pause_event.is_set():
        if cancel.is_set():
            raise InterruptedError("Translation cancelled.")
        time.sleep(0.1)


def wait_for_auto_translation(
    page: Page,
    original_count: int,
    cancel: threading.Event,
    timeout_seconds: float = 5.0,
    pause_event: threading.Event | None = None,
) -> bool:
    """Give Chrome's profile-driven translation a chance before using UI automation."""
    clock = _ActiveClock(pause_event, cancel)
    deadline = clock.now() + timeout_seconds
    while clock.now() < deadline:
        translated_dom = page.evaluate(
            """() => document.documentElement.classList.contains('translated-ltr') ||
                document.documentElement.classList.contains('translated-rtl')"""
        )
        text = page.locator("body").inner_text(timeout=5_000).strip()
        count_dropped = bool(text) and original_count > 0 and chinese_count(text) < original_count * 0.99
        if translated_dom or count_dropped:
            return True
        page.wait_for_timeout(250)
    return False


def wait_for_translation(
    page: Page,
    original_count: int,
    cancel: threading.Event,
    report: Callable[[str], None],
    retry_translation: Callable[[], bool],
    get_scroll_interval: Callable[[], float],
    get_required_sweeps: Callable[[], int],
    pause_event: threading.Event | None = None,
    report_progress: Callable[[float], None] | None = None,
    remaining_chinese_limit: int = 0,
) -> str:
    clock = _ActiveClock(pause_event, cancel)
    started = clock.now()
    watchdog = TranslationWatchdog(original_count, started)
    next_report = started + 5
    next_stats = started
    next_scroll = started
    translation_started = False
    pass_number = 1
    cleanup = False

    while True:
        if cancel.is_set():
            raise InterruptedError("Translation cancelled.")
        clock.now()
        try:
            text = page.locator("body").inner_text(timeout=5_000).strip()
        except Exception as exc:
            report(f"Chrome body read failed ({type(exc).__name__}): {exc}")
            raise
        remaining = chinese_count(text)
        error_marker = next((marker for marker in CHROME_ERROR_MARKERS if marker in text.lower()), None)
        if error_marker:
            report(f"Chrome error-page evidence: {error_marker}")
            raise RuntimeError(f"Chrome returned an error page: {error_marker}")
        if text and remaining <= remaining_chinese_limit:
            stable = True
            for sample in range(2):
                clock.now()
                if cancel.is_set():
                    raise InterruptedError("Translation cancelled.")
                page.wait_for_timeout(750)
                clock.now()
                latest = page.locator("body").inner_text(timeout=5_000).strip()
                if latest != text or chinese_count(latest) > remaining_chinese_limit:
                    stable = False
                text = latest
            if cancel.is_set():
                raise InterruptedError("Translation cancelled.")
            if stable:
                report(f"Stable page within tolerance: {chinese_count(text):,} Chinese characters remain (allowed: {remaining_chinese_limit:,}).")
                if report_progress is not None:
                    report_progress(chinese_count(text) / original_count if original_count else 0.0)
                return text
            remaining = chinese_count(text)
        now = clock.now()
        action, reason = watchdog.observe(remaining, now, has_text=bool(text))
        if action:
            report(
                f"Translation watchdog: {reason}; action={action}, elapsed={now - started:.1f}s active, "
                f"remaining={remaining:,}/{original_count:,}, best={watchdog.best:,}, "
                f"no improvement={now - watchdog.last_improvement:.1f}s."
            )
            clock.now()
            # A mostly translated page that stops improving needs review, not a reload that discards it.
            if (
                cleanup
                and action != "reactivate"
                and reason != "confirmed rollback"
                and bool(text)
                and remaining <= original_count * CLEANUP_REVIEW_MAX_RATIO
            ):
                report(
                    f"Targeted cleanup stalled at {remaining:,} Chinese characters; "
                    "sending result for review."
                )
                return text
            if action == "exhausted":
                raise TimeoutError("Translation recovery exhausted for this chunk attempt.")
            if action == "reactivate":
                activated = retry_translation()
                finished = clock.now()
                watchdog.cooldown_until = finished + watchdog.RECOVERY_WAIT
                watchdog.recovery_until = watchdog.cooldown_until
                report(f"Chrome Translate reactivation returned {activated}; waiting {watchdog.RECOVERY_WAIT:g}s before further recovery.")
            else:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=CHROME_GOTO_TIMEOUT_MS)
                    clock.now()
                    page.evaluate("() => { delete document.documentElement.dataset.noveltlTarget; window.scrollTo(0, 0); }")
                except Exception as exc:
                    report(f"Chrome current-chunk reload failed ({type(exc).__name__}): {exc}")
                    raise
                now = clock.now()
                watchdog.reset_page(now)
                translation_started = False
                cleanup = False
                pass_number = 1
                next_scroll = next_stats = now
                report("Reloaded current chunk; reset count history, stability checks and scroll position (recovery budget retained).")
            continue
        # Observe counts during recovery, but never compete with Translate by scrolling.
        if now < watchdog.cooldown_until or watchdog.recovery_until is not None:
            time.sleep(0.05)
            continue
        if report_progress is not None and original_count > 0 and now >= next_stats:
            report_progress(remaining / original_count)
            next_stats = now + 1.0
        if not translation_started:
            translated_dom = page.evaluate(
                """() => document.documentElement.classList.contains('translated-ltr') ||
                    document.documentElement.classList.contains('translated-rtl') ||
                    Boolean(document.querySelector('font[style*="vertical-align"]'))"""
            )
            if translated_dom or remaining < int(original_count * 0.99):
                translation_started = True
                report("Chrome translation detected; starting uninterrupted scrolling.")
                next_scroll = now

        if translation_started and now >= next_scroll:
            scroll_state = page.evaluate(
                """({cleanup}) => {
                    const viewport = Math.max(window.innerHeight, 400);
                    const pageBottom = document.documentElement.scrollHeight - viewport;
                    if (cleanup) {
                        const chinese = /[\\u3007\\u3400-\\u4dbf\\u4e00-\\u9fff\\uf900-\\ufaff\\u{20000}-\\u{2ee5f}\\u{2f800}-\\u{2fa1f}\\u{30000}-\\u{3347f}]/u;
                        const paragraphs = [...document.querySelectorAll('p')];
                        const lastIndex = Number(document.documentElement.dataset.noveltlTarget || -1);
                        const nextIndex = paragraphs.findIndex((element, index) => {
                            const bounds = element.getBoundingClientRect();
                            return index > lastIndex && bounds.width > 0 && bounds.height > 0 && chinese.test(element.innerText || '');
                        });
                        if (nextIndex >= 0) {
                            document.documentElement.dataset.noveltlTarget = String(nextIndex);
                            paragraphs[nextIndex].scrollIntoView({block: 'center', behavior: 'instant'});
                            return {bottom: false, targeted: true};
                        }
                        delete document.documentElement.dataset.noveltlTarget;
                        return {bottom: true, targeted: false};
                    }
                    const bottom = window.scrollY + viewport >= document.documentElement.scrollHeight - 10;
                    if (bottom) {
                        return {bottom: true, targeted: false};
                    }
                    window.scrollBy({top: Math.floor(viewport * 0.8), behavior: 'instant'});
                    return {bottom: false, targeted: false};
                }""",
                {"cleanup": cleanup},
            )
            if scroll_state["bottom"]:
                report(
                    f"Finished scan {pass_number}: {remaining:,}/{original_count:,} "
                    "Chinese characters remain."
                )
                if not cleanup:
                    cleanup = True
                    report("Initial sweep complete; targeting remaining Chinese paragraphs.")
                pass_number += 1
            interval = get_scroll_interval()
            if cleanup:
                interval = max(interval, TARGETED_SCROLL_MIN_INTERVAL)
            next_scroll = now + interval
        if now >= next_report:
            report(
                f"Waiting for Chrome: {remaining:,}/{original_count:,} Chinese characters remain "
                f"({int(now - started)}s, pass {pass_number})."
            )
            next_report = now + 10
        time.sleep(min(0.05, max(0.01, get_scroll_interval())))
