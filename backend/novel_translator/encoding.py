"""Strict, local-only source decoding. Detection is a suggestion, never a repair."""
from __future__ import annotations

import codecs
import hashlib
import re
from pathlib import Path
from threading import Event
from typing import TypedDict

ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be",
             "gb2312", "gbk", "gb18030", "big5", "big5hkscs")
BOMS = ((codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"))
BINARY_SIGNATURES = (b"PK\x03\x04", b"%PDF-", b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
LEGACY_CANDIDATES = ("gb18030", "big5", "big5hkscs")
PREVIEW_LIMIT = 4000
BLOCK = 65536
SAMPLE_SIZE = 20000
# A BOM-less UTF-16/32 lane counts as "mostly NUL" above this ratio (typical of ASCII-heavy text).
NUL_LANE_RATIO = 0.6
NUL_LANE_BONUS = 1.0
# Without NUL-lane evidence, a UTF-16 guess must look almost entirely like real text.
MIN_UNICODE_GUESS_PLAUSIBILITY = 0.9
MIN_UNICODE_COMMON_HANZI_RATIO = 0.05
COMMON_HANZI_WEIGHT = 2.0
MOJIBAKE_MIN_WEAK_HITS = 3

CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffe\uffff]")
MOJIBAKE_STRONG = re.compile(r"\ufffd|â€|ï»¿")
MOJIBAKE_WEAK = re.compile(r"[ÃÂÐÖ¼]|\?{4,}")
EXTRA_PUNCTUATION = frozenset("“”‘’…—–·")
# Frequent characters in both Simplified and Traditional forms; a wrong legacy decoding rarely produces them.
COMMON_HANZI = frozenset(
    "的一是不了人我在有他她它也就要你那到大小上下中出看去好道么麼什"
    "这這个個们們说說时時会會来來对對过過还還没沒发發经經问問为為"
    "见見进進动動开開关關长長门門车車马馬话話国國学學里裡")


class SourceInfo(TypedDict):
    encoding: str
    digest: str
    preview: str
    truncated: bool
    characters: int
    warnings: list[str]
    candidates: list[str]


def check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Preview cancelled.")


def read_source_bytes(path: Path, cancel: Event | None = None) -> bytes:
    check_cancel(cancel)
    raw = path.read_bytes()
    check_cancel(cancel)
    return raw


def detect_bom(raw: bytes) -> tuple[bytes, str | None]:
    return next(((bom, codec) for bom, codec in BOMS if raw.startswith(bom)), (b"", None))


def decode_strict(payload: bytes, codec: str, cancel: Event | None = None) -> str:
    decoder = codecs.getincrementaldecoder(codec)(errors="strict")
    chunks = []
    try:
        for offset in range(0, len(payload), BLOCK):
            check_cancel(cancel)
            text = decoder.decode(payload[offset:offset + BLOCK])
            if CONTROLS.search(text):
                raise ValueError("Unexpected control characters: binary data or wrong encoding.")
            chunks.append(text)
        chunks.append(decoder.decode(b"", final=True))
    except UnicodeDecodeError as error:
        raise ValueError(f"Cannot decode the entire file as {codec}. Select another encoding or obtain the original file.") from error
    text = "".join(chunks)
    if CONTROLS.search(text) or not text.strip():
        raise ValueError("The file contains no usable text or contains invalid control characters.")
    return text


def looks_like_mojibake(text: str) -> bool:
    if MOJIBAKE_STRONG.search(text):
        return True
    return len(MOJIBAKE_WEAK.findall(text)) >= MOJIBAKE_MIN_WEAK_HITS


def is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf"


def is_plausible(ch: str) -> bool:
    return (ch in "\t\n\r " or "!" <= ch <= "~" or is_cjk(ch) or "\u3000" <= ch <= "\u303f"
            or "\uff00" <= ch <= "\uffef" or ch in EXTRA_PUNCTUATION)


def plausibility(sample: str) -> float:
    return sum(map(is_plausible, sample)) / max(1, len(sample))


def score_text(sample: str) -> float:
    cjk = sum(map(is_cjk, sample))
    common = sum(ch in COMMON_HANZI for ch in sample)
    mojibake = len(MOJIBAKE_STRONG.findall(sample)) + len(MOJIBAKE_WEAK.findall(sample))
    return (plausibility(sample) + COMMON_HANZI_WEIGHT * common / max(1, cjk)
            - mojibake / max(1, len(sample)))


def nul_lane_codecs(payload: bytes) -> list[str]:
    sample = payload[:SAMPLE_SIZE]
    found = []
    for width in (4, 2):
        if len(payload) % width:
            continue
        lanes = [sample[i::width] for i in range(width)]
        for endian, lane in (("le", 0), ("be", width - 1)):
            if all(lanes[i] and lanes[i].count(0) / len(lanes[i]) > NUL_LANE_RATIO
                   for i in range(width) if i != lane):
                found.append(f"utf-{width * 8}-{endian}")
    return found


def guess_encoding(payload: bytes, cancel: Event | None = None) -> tuple[str, str, list[str]]:
    """Return (codec, text, candidates) for BOM-less non-UTF-8 input."""
    laned = nul_lane_codecs(payload)
    unicode_codecs = list(laned)
    if len(payload) % 4 == 0:
        unicode_codecs += [c for c in ("utf-32-le", "utf-32-be") if c not in unicode_codecs]
    if len(payload) % 2 == 0:
        unicode_codecs += [c for c in ("utf-16-le", "utf-16-be") if c not in unicode_codecs]
    best: tuple[float, str, str] | None = None
    candidates = []
    for codec in (*unicode_codecs, *LEGACY_CANDIDATES):
        try:
            text = decode_strict(payload, codec, cancel)
        except ValueError:
            continue
        sample = text[:SAMPLE_SIZE]
        if codec in unicode_codecs and codec not in laned:
            common_ratio = sum(ch in COMMON_HANZI for ch in sample) / max(1, len(sample))
            if (plausibility(sample) < MIN_UNICODE_GUESS_PLAUSIBILITY
                    or common_ratio < MIN_UNICODE_COMMON_HANZI_RATIO):
                continue
        candidates.append(codec)
        score = score_text(sample) + (NUL_LANE_BONUS if codec in laned else 0)
        if best is None or score > best[0]:
            best = (score, codec, text)
    if best is None:
        raise ValueError("No supported encoding yields valid plain text. Try a manual encoding or obtain the original text file.")
    return best[1], best[2], candidates


def decode_source(path: Path, encoding: str = "auto", expected_digest: str | None = None,
                  cancel: Event | None = None) -> tuple[str, SourceInfo]:
    if encoding not in ("auto", *ENCODINGS):
        raise ValueError("Unsupported encoding. Choose an encoding from the list.")
    raw = read_source_bytes(path, cancel)
    digest = hashlib.sha256(raw).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        raise ValueError("Source changed since preview. Preview and confirm it again.")
    if not raw:
        raise ValueError("The input file is empty. Choose a nonempty text file.")
    if raw.startswith(BINARY_SIGNATURES):
        raise ValueError("This appears to be a binary/document file, not plain text.")
    bom, marked = detect_bom(raw)
    if marked and encoding not in ("auto", marked):
        raise ValueError(f"The byte-order mark identifies {marked}; select Auto or {marked}.")
    payload = raw[len(bom):]

    warnings = []
    candidates: list[str] = []
    if encoding != "auto" or marked:
        chosen = marked or encoding
        text = decode_strict(payload, chosen, cancel)
    else:
        try:
            text = decode_strict(payload, "utf-8", cancel)
            chosen = "utf-8"
            if text.isascii():
                warnings.append("ASCII-only text: multiple encodings are indistinguishable; UTF-8 is used.")
        except ValueError:
            chosen, text, candidates = guess_encoding(payload, cancel)
            warnings.append("Encoding is uncertain. Compare the preview with the original and choose manually if needed. Valid decoding does not prove the encoding is correct.")
    check_cancel(cancel)
    if looks_like_mojibake(text):
        warnings.append("Possible mojibake or previously replaced characters. Changing encoding cannot reliably repair already-resaved text; obtain the original if this preview is garbled.")
    return text, {"encoding": chosen, "digest": digest, "preview": text[:PREVIEW_LIMIT],
                  "truncated": len(text) > PREVIEW_LIMIT, "characters": len(text),
                  "warnings": warnings, "candidates": candidates or [chosen]}
