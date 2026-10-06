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
