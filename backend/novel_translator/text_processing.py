from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path

from .constants import CHINESE_RE, DEFAULT_CHUNK_LIMIT


def read_text_file(path: Path, encoding: str = "auto", expected_digest: str | None = None) -> str:
    from .encoding import decode_source
    return decode_source(path, encoding, expected_digest)[0]


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
