from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "Novel Translator"
APP_VERSION = "1.1.0"

# Single source of truth for the default chunk size (#23). The preset menu is
# the user-facing control; this constant is only the fallback used when no
# preset is selected (and the default value of split_text's ``limit``).
DEFAULT_CHUNK_LIMIT = 275_000
CHUNK_PRESETS = {
    "Ultra Safe (60k)": 60_000,
    "Reliable (125k)": 125_000,
    "Balanced (275k)": DEFAULT_CHUNK_LIMIT,
    "Fast (450k)": 450_000,
}
DEFAULT_CHUNK_PRESET = "Balanced (275k)"

# ---------------------------------------------------------------------------
# Tunable timing / retry / validation knobs (#13). Centralised here so the
# worker, chrome driver, and settings all agree on the same named values.
# ---------------------------------------------------------------------------

# Chrome page navigation + settle timeouts (milliseconds unless noted).
CHROME_GOTO_TIMEOUT_MS = 30_000
CHROME_SETTLE_MS = 2_000

# Per-chunk attempt budget and the pause between attempts (seconds).
CHUNK_MAX_ATTEMPTS = 3
CHUNK_ATTEMPT_BACKOFF_S = 1.0

# Backoff (seconds) between automatic "retry Chrome Translate" nudges while a
# chunk has not yet started translating (chrome.py wait loop).
TRANSLATE_RETRY_DELAYS_S = (12, 30, 60)

# Fraction of the original Chinese-character count that may remain before a
# chunk is considered translated (validation + the wait-loop target).
TRANSLATION_SWEEP_TARGET_RATIO = 0.05

# Fraction of the source length the translated text must reach, otherwise the
# chunk is treated as suspiciously short (validation).
TRANSLATION_MIN_LENGTH_RATIO = 0.20
TRANSLATION_MIN_ABSOLUTE_CHARS = 100

# Paragraph-preservation guard: when the source has at least this many
# paragraphs, the result must keep at least TRANSLATION_MIN_PARAGRAPH_RATIO.
TRANSLATION_PARAGRAPH_FLOOR = 10
TRANSLATION_MIN_PARAGRAPH_RATIO = 0.75

# Scroll-speed slider bounds and required-sweeps stepper bounds.
SCROLL_INTERVAL_MIN = 0.02
SCROLL_INTERVAL_MAX = 2.0
REQUIRED_SWEEPS_MIN = 1
REQUIRED_SWEEPS_MAX = 10

# When the wait loop jumps straight to an untranslated paragraph (targeted
# scroll), don't scroll faster than this floor so Chrome has time to translate.
TARGETED_SCROLL_MIN_INTERVAL = 0.15

# Debounce window (milliseconds) for persisting settings after a control is
# touched, so a crash mid-edit still saves but we don't rewrite the file on
# every slider tick (#20).
SETTINGS_SAVE_DEBOUNCE_MS = 750
SETTINGS_PATH = Path.home() / "AppData" / "Local" / "NovelTranslator" / "settings.json"

# Characters Windows forbids in file/directory names, plus trailing dots and
# spaces (which Explorer silently strips). Used to sanitize output filenames
# derived from novel titles, e.g. "Blissful Skill: Golden Spear".
INVALID_FILENAME_CHARS_RE = re.compile(r"[:<>\x22/\\|?*\x00-\x1f]")
WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str) -> str:
    """Return ``name`` with Windows-illegal characters stripped and trailing
    dots/spaces removed, so it can be used as a file or directory name."""
    cleaned = INVALID_FILENAME_CHARS_RE.sub("", name).strip().rstrip(". ").strip()
    if not cleaned:
        cleaned = "untitled"
    if cleaned.lower() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


CHINESE_RE = re.compile(r"[\u3007\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ee5f\U0002f800-\U0002fa1f\U00030000-\U0003347f]")

# ---------------------------------------------------------------------------
# Chapter detection patterns (multi-tiered fallback system)
# ---------------------------------------------------------------------------

# Spelled-out English ordinals up to one hundred (used by Tier 1 regex and by
# the Tier 3 number parser in epub_builder).
CHAPTER_NUMBER_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|"
    "eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    "hundred"
)
_CHAPTER_NUM = (
    rf"(?:\d+|[ivxlcdm]+|{CHAPTER_NUMBER_WORDS}"
    rf"(?:[\s-]+(?:{CHAPTER_NUMBER_WORDS}))*)"
)

# Tier 1 - high-confidence, line-anchored chapter heading pattern.
# Covers standard/abbreviated/spelled-out chapter headings, alternative
# section terms, decorative wrappers, and bare sequential numbers.
# Group 1 captures the clean title (without decorative wrappers).
CHAPTER_RE = re.compile(
    r"(?im)^[ \t]*"
    # -- Decorative openers (optional, not captured)
    r"[【\[#*=~\-\s]*"
    # -- Capture group 1: the actual heading text
    r"("
    # -- Chapter / Episode / Volume with a number (digits, roman, or words)
    rf"(?:chapter|ch\.?|c(?!\w)|ep(?:isode)?\.?|volume|part|book)\s*{_CHAPTER_NUM}\b[^\n】\]#*=~]*?(?=\s*[】\]#*=~\-]*\s*$)"
    # -- Alternative section terms (Prologue, Epilogue, Extra, Side Story, ...)
    #    These must be standalone (end-of-line) or followed by a number/colon,
    #    NOT by arbitrary body text like "Bonus content.".
    r"|(?:prologue|epilogue|side[\s-]*stor(?:y|ies)|extra(?:\s+chapter)?"
    r"|interlude|intermission|bonus(?:\s+chapter)?|afterword|appendix"
    r"|author'?s\s+notes?|postscript|foreword|introduction|prelude"
    r"|final\s+chapter|last\s+chapter|ending|finale|sequel"
    r")(?=\s*$|\s*[:.\-]\s*\S)"
    # -- Alternative section terms with optional number/title after colon/dash
    r"|(?:prologue|epilogue|side[\s-]*stor(?:y|ies)|extra(?:\s+chapter)?"
    r"|interlude|intermission|bonus(?:\s+chapter)?|afterword|appendix"
    r"|author'?s\s+notes?|postscript|foreword|introduction|prelude"
    r"|final\s+chapter|last\s+chapter|ending|finale|sequel"
    r")\s*[:.\-]\s*\S[^\n】\]#*=~]*"
    # -- Chinese chapter markers
    r"|第[零〇一二三四五六七八九十百千万两\d]+章[^\n】\]#*=~]*"
    # -- Chinese volume markers
    r"|(?:卷|部|篇|冊|册)\s*[零〇一二三四五六七八九十百千万两\d]+[^\n】\]#*=~]*"
    # -- Chinese special-section markers
    r"|(?:序章|序言|楔子|引子|前言|后记|後記|番外|尾声|尾聲|终章|終章)[^\n】\]#*=~]*"
    # -- Bare sequential numbers: "001 - Title", "1. Title", "[1]", "1 -"
    r"|\d{1,4}\s*[.\-]\s+\S[^\n】\]#*=~]*"
    r"|\[\d{1,4}\][^\n】\]#*=~]*"
    r")"
    # -- Decorative closers (optional, not captured)
    r"\s*[】\]#*=~\-]*"
    r"[ \t]*$"
)

# Tier 2 - heuristic patterns for lines that look like structural headers.
# Numeric headings must look like "001 - Title" / "12. The Journey": the
# number is followed by a separator, or by a Title-Case word.  This avoids
# false positives on prose lines such as "20 minutes later".
HEADER_NUMERIC_RE = re.compile(
    r"^\s*(?:"
    r"\[?\d{1,4}\]?[.\-]\s+\S"          # "001 - Title" / "12. Title"
    r"|\[\d{1,4}\]\s+\S"                # "[12] Title"
    r"|\(\d{1,4}\)"                     # "(12)"
    r"|\d{1,4}\s+(?!(?:AM|PM|A\.M\.|P\.M\.)\b)[A-Z\"'“‘]"  # "12 The Journey", not "3 PM"
    r")"
)
HEADER_MARKDOWN_RE = re.compile(r"^\s*#{1,4}\s+\S")
HEADER_SYMBOL_WRAP_RE = re.compile(r"^\s*(?:\*{2,}|={2,}|-{2,})\s*\S.*\S\s*(?:\*{2,}|={2,}|-{2,})\s*$")
HEADER_ALLCAPS_RE = re.compile(r"^[A-Z][A-Z\s\d:;,.!?'\-&()]+$")

# Tier 3 - relaxed pattern used to locate a missing chapter number inside a gap.
# The number must stand alone or be followed by a separator, so "3 PM." is rejected.
GAP_NUMBER_RE = re.compile(r"(?im)^[ \t]*\[?(\d{1,4})\]?[ \t]*(?:$|[.\-:][ \t]+\S[^\n]*$)")

CHROME_ERROR_MARKERS = (
    "this site can't be reached",
    "this page isn't working",
    "aw, snap!",
    "err_connection_",
)
VOLUME_HEADING_RE = re.compile(r"(?i)^(?:volume|part|book)\s+(?:\d+|[ivxlcdm]+)\b|^(?:卷|部|篇|冊|册)\s*[零〇一二三四五六七八九十百千万两\d]+")

COLORS = {
    "window": "#07110D",
    "surface": "#12211B",
    "surface_alt": "#1B2D26",
    "input": "#101B17",
    "border": "#315247",
    "text": "#F3F7F5",
    "muted": "#93A69E",
    "accent": "#33D39B",
    "accent_hover": "#27B985",
    "danger": "#713631",
    "danger_hover": "#8D443D",
    "warm": "#73E0B5",
}

# Package files live one level below the project root, so walk up past the package directory.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def resource_path(relative_path: str) -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", PACKAGE_ROOT))
    return bundle_root / relative_path


@dataclass
class UiEvent:
    kind: str
    value: object
