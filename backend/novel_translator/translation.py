from __future__ import annotations

from .constants import (
    CHROME_ERROR_MARKERS,
    TRANSLATION_MIN_ABSOLUTE_CHARS,
    TRANSLATION_MIN_LENGTH_RATIO,
    TRANSLATION_MIN_PARAGRAPH_RATIO,
    TRANSLATION_PARAGRAPH_FLOOR,
    TRANSLATION_SWEEP_TARGET_RATIO,
)
from .text_processing import chinese_count


class TranslationWatchdog:
    """Count-based recovery budget for one chunk attempt; times are active seconds."""

    STARTUP_GRACE = 60.0
    STALL_SECONDS = 60.0
    RECOVERY_WAIT = 20.0
    ROLLBACK_SAMPLES = 3

    def __init__(self, original_count: int, now: float) -> None:
        self.original_count = original_count
        self.reactivated = False
        self.reloaded = False
        self.cooldown_until = now
        self.recovery_until: float | None = None
        self.reset_page(now)

    def reset_page(self, now: float) -> None:
        self.page_started = now
        self.last_improvement = now
        self.best = self.original_count
        self.progress_anchor = self.original_count
        self.rollback_samples = 0
        self.next_observation = now
        self.recovery_until = None

    def observe(self, remaining: int, now: float, *, has_text: bool = True) -> tuple[str | None, str]:
        if now < self.next_observation:
            return None, ""
        self.next_observation = now + 1.0
        # Empty/error DOMs must not masquerade as translation progress.
        if has_text:
            self.best = min(self.best, remaining)
            meaningful = max(1, int(self.original_count * 0.005))
            if self.progress_anchor - remaining >= meaningful:
                self.progress_anchor = remaining
                self.last_improvement = now
                self.recovery_until = None
            rollback = (
                self.original_count > 0
                and self.best <= self.original_count * 0.8
                and remaining >= self.original_count * 0.95
            )
        else:
            rollback = False
        self.rollback_samples = self.rollback_samples + 1 if rollback else 0
        reason = "confirmed rollback" if self.rollback_samples >= self.ROLLBACK_SAMPLES else "no meaningful count improvement"
        if now < self.cooldown_until:
            return None, ""
        if self.recovery_until is not None:
            if now < self.recovery_until:
                return None, ""
        elif self.rollback_samples < self.ROLLBACK_SAMPLES and (
            now - self.page_started < self.STARTUP_GRACE
            or now - self.last_improvement < self.STALL_SECONDS
        ):
            return None, ""
        if not self.reactivated:
            self.reactivated = True
            self.recovery_until = now + self.RECOVERY_WAIT
            return "reactivate", reason
        if not self.reloaded:
            self.reloaded = True
            return "reload", reason
        return "exhausted", reason


def validate_translated_chunk(source: str, result: str, remaining_chinese_limit: int | None = None) -> tuple[bool, str]:
    source_count = chinese_count(source)
    remaining = chinese_count(result)
    lowered = result.lower()
    if not result.strip():
        return False, "Chrome returned an empty page"
    if any(marker in lowered for marker in CHROME_ERROR_MARKERS):
        return False, "Chrome returned an error page"
    limit = max(3, int(source_count * TRANSLATION_SWEEP_TARGET_RATIO)) if remaining_chinese_limit is None else remaining_chinese_limit
    if remaining > limit:
        return False, f"{remaining:,} Chinese characters remain (allowed per chunk: {limit:,})"
    if len(result.strip()) < max(
        TRANSLATION_MIN_ABSOLUTE_CHARS, int(len(source.strip()) * TRANSLATION_MIN_LENGTH_RATIO)
    ):
        return False, "translated output is unexpectedly short"
    source_paragraphs = sum(1 for line in source.splitlines() if line.strip())
    result_paragraphs = sum(1 for line in result.splitlines() if line.strip())
    if source_paragraphs >= TRANSLATION_PARAGRAPH_FLOOR and result_paragraphs < int(
        source_paragraphs * TRANSLATION_MIN_PARAGRAPH_RATIO
    ):
        return False, f"only {result_paragraphs}/{source_paragraphs} paragraphs were preserved"
    return True, ""
