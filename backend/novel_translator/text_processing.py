from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path

from .constants import CHINESE_RE, DEFAULT_CHUNK_LIMIT


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    # UTF-8 variants first: strict, so a genuinely UTF-8 file never falls through.
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # Legacy CJK codecs will happily "decode" almost any byte sequence into
    # mojibake, so only accept the result when it actually looks like Chinese.
    for encoding in ("gb18030", "big5"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\ufffd" in text:
            continue
        # Confidence check: expect a meaningful share of CJK characters and no
        # large runs of rare/symbol codepoints that indicate a wrong codec.
        sample = text[:20000]
        cjk = len(CHINESE_RE.findall(sample))
        suspicious = sum(1 for ch in sample if "\u0080" <= ch <= "\u04ff")
        if len(sample) < 100 or cjk / len(sample) > 0.05 or suspicious < len(sample) * 0.01:
            return text
    raise ValueError("Could not decode the file as UTF-8, GB18030, or Big5.")


def split_text(text: str, limit: int = DEFAULT_CHUNK_LIMIT) -> list[str]:
    paragraphs = re.split(r"\r?\n", text)
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0

    def flush() -> None:
        nonlocal current, current_size
        if current:
            chunks.append("\n".join(current).strip())
            current = []
            current_size = 0

    for paragraph in paragraphs:
        pieces = [paragraph[index : index + limit] for index in range(0, len(paragraph), limit)] or [""]
        for piece in pieces:
            # Decide whether to flush based on what this piece would cost in the
            # current chunk (its length + one joining newline when not first).
            if current and current_size + len(piece) + 1 > limit:
                flush()
            # Now account for the piece against the chunk it actually lands in.
            current_size += len(piece) + (1 if current else 0)
            current.append(piece)
    flush()
    return [chunk for chunk in chunks if chunk]


def chunk_html(text: str, title: str) -> str:
    paragraphs = "\n".join(
        f"<p>{html.escape(line)}</p>" if line.strip() else "<p><br></p>"
        for line in text.splitlines()
    )
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title></head><body>{paragraphs}</body></html>"
    )


def chinese_count(text: str) -> int:
    # subn counts matches in C without building a list of one-char strings
    # (findall) or paying per-match generator overhead (finditer) (#8).
    return CHINESE_RE.subn("", text)[1]

def source_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
