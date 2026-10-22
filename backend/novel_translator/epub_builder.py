from __future__ import annotations

import html
import io
import re
import time
from pathlib import Path

from ebooklib import epub
from PIL import Image, ImageDraw, ImageFont

from .constants import (
    CHAPTER_NUMBER_WORDS,
    CHAPTER_RE,
    GAP_NUMBER_RE,
    HEADER_ALLCAPS_RE,
    HEADER_MARKDOWN_RE,
    HEADER_NUMERIC_RE,
    HEADER_SYMBOL_WRAP_RE,
    VOLUME_HEADING_RE,
)

# ---------------------------------------------------------------------------
# Lookup tables for parsing chapter numbers out of headings (Tier 3).
# ---------------------------------------------------------------------------

_WORD_TO_NUM: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "hundred": 100,
}

_ROMAN_TO_NUM: dict[str, int] = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7,
    "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13,
    "xiv": 14, "xv": 15, "xvi": 16, "xvii": 17, "xviii": 18,
    "xix": 19, "xx": 20, "xxi": 21, "xxii": 22, "xxiii": 23,
    "xxiv": 24, "xxv": 25, "xxvi": 26, "xxvii": 27, "xxviii": 28,
    "xxix": 29, "xxx": 30, "xl": 40, "l": 50, "lx": 60, "xc": 90,
    "c": 100,
}

_CN_NUM_MAP: dict[str, int] = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

# Pre-built regex for extracting a number token from a heading line.
_NUM_TOKEN_RE = re.compile(
    rf"(\d+|[ivxlcdm]+|{CHAPTER_NUMBER_WORDS}(?:[\s-]+(?:{CHAPTER_NUMBER_WORDS}))*)",
    re.IGNORECASE,
)


def _chapter_number(title: str) -> int | None:
    """Best-effort extraction of a numeric chapter index from a heading."""
    text = title.strip()
    # Chinese-style: 第N章
    m = re.search(r"第([零〇一二三四五六七八九十百千万两\d]+)章", text)
    if m:
        return _parse_cn_number(m.group(1))
    # Look for an explicit numeric/word token after a keyword.
    m = re.search(
        r"(?:chapter|ch\.?|ep(?:isode)?\.?|volume|part|book)\s+",
        text,
        re.IGNORECASE,
    )
    if m:
        token = _NUM_TOKEN_RE.search(text, m.end())
        if token:
            return _parse_token(token.group(0))
    # Bare sequential number at start: "001 - Title", "1. Title", "[1]"
    m = re.match(r"\s*\[?(\d{1,4})\]?\s*[.\s\-]", text)
    if m:
        return int(m.group(1))
    return None


def _parse_token(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    if token in _ROMAN_TO_NUM:
        return _ROMAN_TO_NUM[token]
    # Spelled-out: "twenty-three", "one hundred", "five"
    total = 0
    for part in re.split(r"[\s-]+", token):
        value = _WORD_TO_NUM.get(part)
        if value is None:
            return None
        total += value
    return total if total > 0 else None


def _parse_cn_number(cn: str) -> int | None:
    """Parse a Chinese numeral string (e.g. 二十三, 一百零五) into an int.

    A digit before 十/百/千 multiplies that unit; 万 folds the running section
    into the ten-thousands total. Bare 十 counts as ten. Returns ``None`` for an
    empty or unparseable string (the main caller never passes empty input thanks
    to its ``+`` regex, but the parser still guards against it).
    """
    if not cn:
        return None
    if cn.isdigit():
        return int(cn)
    total = 0
    section = 0
    digit = 0
    for char in cn:
        if char in _CN_NUM_MAP:
            digit = _CN_NUM_MAP[char]
        elif char == "十":
            section += (digit or 1) * 10
            digit = 0
        elif char == "百":
            section += (digit or 1) * 100
            digit = 0
        elif char == "千":
            section += (digit or 1) * 1000
            digit = 0
        elif char == "万":
            total = (total + section + digit) * 10000
            section = 0
            digit = 0
        else:
            return None
    return total + section + digit


# ---------------------------------------------------------------------------
# Tier 1 – high-confidence regex detection
# ---------------------------------------------------------------------------


_BARE_NUMBER_TITLE_RE = re.compile(r"^\[?\d")
_HAS_WORD_RE = re.compile(r"[^\W\d_]")


def _has_list_neighbor(block: str, line_start: int, target: int) -> bool:
    """True when ``target-1`` or ``target+1`` also starts a nearby line (a numbered list)."""
    window = block[max(0, line_start - 600): line_start + 600]
    pattern = rf"(?m)^[ \t]*\[?(?:{target - 1}|{target + 1})\]?[ \t]*[.)\-:][ \t]+\S"
    return bool(re.search(pattern, window))


def _drop_numbered_lists(entries: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Remove bare-number headings that restart numbering mid-book.

    In "1. Start, 2. Middle, 1. Skirt, 2. Gown, 3. Dress, 3. End" the run
    1-2-3 restarts below the last chapter number, so it is a list in prose.
    """
    result: list[tuple[int, int, str]] = []
    last = 0
    run_prev: int | None = None
    for entry in entries:
        num = _chapter_number(entry[2]) if _BARE_NUMBER_TITLE_RE.match(entry[2]) else None
        if num is None:
            run_prev = None
            keyword_num = _chapter_number(entry[2])
            if keyword_num is not None:
                last = keyword_num
            result.append(entry)
            continue
        if run_prev is not None and num == run_prev + 1:
            run_prev = num
            continue
        if last and num <= last:
            run_prev = num
            continue
        run_prev = None
        last = num
        result.append(entry)
    return result


def _tier1_find(text: str) -> list[tuple[int, int, str]]:
    """Return ``(start, end, title)`` for every Tier-1 regex match.

    The title is extracted from capture group 1 (the clean heading without
    decorative wrappers like ``=====`` or ``【】``).
    """
    results: list[tuple[int, int, str]] = []
    for m in CHAPTER_RE.finditer(text):
        # Use group 1 (clean title) if available, otherwise fall back to full match.
        raw_title = m.group(1) if m.lastindex else m.group(0)
        title = re.sub(r"\s+", " ", raw_title.strip())
        results.append((m.start(), m.end(), title))
    # Numbered list items ("2. Some point") are not chapters when keyword headings exist.
    if any(not _BARE_NUMBER_TITLE_RE.match(title) for _, _, title in results):
        results = [r for r in results if not _BARE_NUMBER_TITLE_RE.match(r[2])]
    return results


# ---------------------------------------------------------------------------
# Tier 2 – heuristic line analysis
# ---------------------------------------------------------------------------


def _is_isolated(lines: list[str], idx: int) -> bool:
    """True when *lines[idx]* is surrounded by blank lines."""
    prev_blank = idx == 0 or not lines[idx - 1].strip()
    next_blank = idx == len(lines) - 1 or not lines[idx + 1].strip()
    return prev_blank and next_blank


def _tier2_find(text: str, existing: set[int], strict: bool = False) -> list[tuple[int, int, str]]:
    """Find structural headers that Tier 1 may have missed.

    *existing* is a set of character offsets already claimed by Tier 1 so we
    don't double-detect the same line. *strict* accepts only markdown or
    symbol-wrapped headers.
    """
    lines = text.splitlines(keepends=True)
    results: list[tuple[int, int, str]] = []
    offset = 0
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        line_start = offset
        offset += len(raw)

        if not stripped or len(stripped) >= 80:
            continue
        if line_start in existing:
            continue
        if not _is_isolated(lines, idx):
            continue
        # Scene breaks like "*** *** ***" or "=====" carry no words.
        if not _HAS_WORD_RE.search(stripped):
            continue

        # A line qualifies when at least one structural indicator fires.
        is_numeric = bool(HEADER_NUMERIC_RE.match(stripped))
        is_markdown = bool(HEADER_MARKDOWN_RE.match(stripped))
        is_wrapped = bool(HEADER_SYMBOL_WRAP_RE.match(stripped))
        is_allcaps = (
            bool(HEADER_ALLCAPS_RE.match(stripped))
            and len(stripped) < 40
            and len(_HAS_WORD_RE.findall(stripped)) >= 4
            and not stripped.endswith((".", "!", "?"))
        )

        if not (is_numeric or is_markdown or is_wrapped or is_allcaps):
            continue
        # With real keyword headings present, only explicit markup counts;
        # numbered or shouted prose lines are not missing chapters.
        if strict and not (is_markdown or is_wrapped):
            continue

        title = re.sub(r"\s+", " ", stripped)
        results.append((line_start, line_start + len(raw), title))

    return results


# ---------------------------------------------------------------------------
# Tier 3 – sequence-gap repair
# ---------------------------------------------------------------------------


def _tier3_repair(
    text: str,
    chapters: list[tuple[int, int, str]],
) -> list[tuple[int, int, str]]:
    """Fill gaps in the detected chapter-number sequence.

    If chapters 1,2,3,5,6 are found, we search the text block between
    chapter 3 and chapter 5 for a candidate line containing the digit ``4``
    or the shortest isolated line.
    """
    if len(chapters) < 2:
        return chapters

    numbered: list[tuple[int, tuple[int, int, str]]] = []
    for entry in chapters:
        num = _chapter_number(entry[2])
        if num is not None:
            numbered.append((num, entry))

    if not numbered:
        return chapters

    # Build a quick lookup from chapter-number -> position in chapters list.
    pos_by_num: dict[int, int] = {}
    for i, (num, _) in enumerate(numbered):
        pos_by_num[num] = i

    known_nums = sorted(pos_by_num)
    full_range = range(known_nums[0], known_nums[-1] + 1)
    missing = [n for n in full_range if n not in pos_by_num]
    if not missing:
        return chapters

    # Cap repair: only attempt to fill gaps of at most 5 missing numbers in
    # a single stretch.  Larger gaps almost certainly reflect a genuinely
    # different numbering scheme (e.g. spelled-out chapter titles where the
    # parser can only extract a partial sequence).
    if len(missing) > 5:
        return chapters

    # For every missing number, locate the gap boundaries in *chapters*.
    result = list(chapters)  # mutable copy
    used_positions: set[int] = set()  # avoid inserting the same line twice
    for gap_num in missing:
        # Find the position in the (possibly already extended) result list.
        prev_idx = next(
            (i for i in range(len(result) - 1, -1, -1)
             if (n := _chapter_number(result[i][2])) is not None and n < gap_num),
            None,
        )
        next_idx = next(
            (i for i in range(len(result))
             if (n := _chapter_number(result[i][2])) is not None and n > gap_num),
            None,
        )
        if prev_idx is None or next_idx is None:
            continue

        search_start = result[prev_idx][1]  # end of previous chapter heading
        search_end = result[next_idx][0]    # start of next chapter heading
        block = text[search_start:search_end]

        candidate = _find_gap_candidate(block, gap_num)
        if candidate is not None:
            abs_start, abs_end, title = candidate
            # Skip if we already inserted this exact position.
            if (search_start + abs_start) in used_positions:
                continue
            used_positions.add(search_start + abs_start)
            result.insert(
                prev_idx + 1,
                (search_start + abs_start, search_start + abs_end, title),
            )

    return result


def _find_gap_candidate(
    block: str, target: int,
) -> tuple[int, int, str] | None:
    """Search *block* for a line that explicitly carries chapter number *target*."""
    lines = block.splitlines(keepends=True)
    offset = 0

    for raw in lines:
        stripped = raw.strip()
        line_start = offset
        offset += len(raw)

        if not stripped or len(stripped) >= 80:
            continue

        m = GAP_NUMBER_RE.match(stripped)
        if m and int(m.group(1)) == target and not _has_list_neighbor(block, line_start, target):
            title = re.sub(r"\s+", " ", stripped)
            return (line_start, line_start + len(raw), title)

    return None


# ---------------------------------------------------------------------------
# Public API – multi-tiered pipeline
# ---------------------------------------------------------------------------


def split_chapters(text: str) -> list[tuple[str, str]]:
    """Split *text* into ``(title, body)`` chapter pairs.

    Uses a three-tiered fallback:
      1. High-confidence regex matching (``CHAPTER_RE``).
      2. Heuristic line analysis for structural headers.
      3. Sequence-gap repair to fill missing numbers.
    """
    # -- Tier 1 -----------------------------------------------------------
    tier1 = _tier1_find(text)

    # -- Tier 2 (fallback) -------------------------------------------------
    # Run Tier 2 when Tier 1 found nothing, or when results are suspiciously
    # sparse relative to the overall text length.
    tier2: list[tuple[int, int, str]] = []
    if not tier1:
        tier2 = _tier2_find(text, set())
    elif len(text) > 10_000:
        # Heuristic: fewer than one chapter per 20 000 characters is sparse.
        avg_chars = len(text) / len(tier1)
        if avg_chars > 20_000:
            has_keyword = any(not _BARE_NUMBER_TITLE_RE.match(t) for _, _, t in tier1)
            tier2 = _tier2_find(text, {start for start, _, _ in tier1}, strict=has_keyword)

    # Merge tiers and sort by position.
    combined = _drop_numbered_lists(sorted(tier1 + tier2, key=lambda t: t[0]))

    # -- Tier 3 (repair) ----------------------------------------------------
    combined = _tier3_repair(text, combined)

    if not combined:
        return [("Translated Novel", text)]

    # -- Assemble final chapter list ---------------------------------------
    chapters: list[tuple[str, str]] = []
    preface = text[: combined[0][0]].strip()
    if preface:
        chapters.append(("Preface", preface))
    for index, (start, end, title) in enumerate(combined):
        body_end = combined[index + 1][0] if index + 1 < len(combined) else len(text)
        body = text[end:body_end].strip()
        chapters.append((title, body))
    return chapters


def make_epub(text: str, output: Path, book_title: str, cover_options: dict | None = None) -> None:
    if cover_options is None:
        cover_options = {"mode": "auto", "textColor": "#ffffff", "bgColor": "#0f131c"}
    
    book = epub.EpubBook()
    book.set_identifier(f"local-translation-{int(time.time())}")
    book.set_title(book_title)
    book.set_language("en")
    book.add_author("Translated locally with Chrome")
    
    # Generate or import cover image
    if cover_options["mode"] == "auto":
        cover_bytes = _generate_text_cover(book_title, cover_options.get("textColor", "#ffffff"), cover_options.get("bgColor", "#0f131c"))
        book.set_cover("cover.jpg", cover_bytes)
    elif cover_options["mode"] == "import" and cover_options.get("imagePath"):
        try:
            cover_bytes = Path(cover_options["imagePath"]).read_bytes()
            book.set_cover("cover.jpg", cover_bytes)
        except Exception:
            pass

    items: list[epub.EpubHtml] = []
    toc: list[object] = []
    current_volume: epub.EpubHtml | None = None
    current_volume_children: list[epub.EpubHtml] = []

    def flush_volume() -> None:
        nonlocal current_volume, current_volume_children
        if current_volume is not None:
            toc.append((current_volume, tuple(current_volume_children)))
            current_volume = None
            current_volume_children = []

    def clean_chapter_title(title: str) -> str:
        """Remove book title prefix from chapter titles.
        Handles patterns like 'Book Title: 1.' or 'Book Title 1-140: 2.'"""
        colon_pos = title.find(': ')
        if colon_pos != -1:
            after_colon = title[colon_pos + 2:].strip()
            if after_colon and (after_colon[0].isdigit() or after_colon.lower().startswith(('chapter', 'prologue', 'epilogue'))):
                return after_colon
        return title

    for index, (title, body) in enumerate(split_chapters(text), start=1):
        title = clean_chapter_title(title)
        chapter = epub.EpubHtml(title=title, file_name=f"chapter_{index:04d}.xhtml", lang="en")
        
        # Build paragraphs with special handling for numbered lists
        # Group consecutive numbered lines into a single <p> with <br> to prevent TOC detection
        para_list = []
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        
        i = 0
        while i < len(lines):
            line = lines[i]
            
            # Check if this line starts with digit + period
            is_numbered = False
            if len(line) > 1 and line[0].isdigit():
                period_pos = line.find('.')
                if 0 < period_pos <= 2 and line[:period_pos].isdigit():
                    line_number = int(line[:period_pos])
                    if line_number <= 9:
                        is_numbered = True
            
            if is_numbered:
                # Collect all consecutive numbered lines
                numbered_group = []
                while i < len(lines):
                    curr_line = lines[i]
                    if len(curr_line) > 1 and curr_line[0].isdigit():
                        p_pos = curr_line.find('.')
                        if 0 < p_pos <= 2 and curr_line[:p_pos].isdigit():
                            num = int(curr_line[:p_pos])
                            if num <= 9:
                                numbered_group.append(html.escape(curr_line))
                                i += 1
                                continue
                    break
                # Put all numbered items in ONE paragraph separated by <br>
                if numbered_group:
                    para_list.append(f"<p>{'<br/>'.join(numbered_group)}</p>")
            else:
                para_list.append(f"<p>{html.escape(line)}</p>")
                i += 1
        
        paragraphs = "".join(para_list)
        
        chapter.content = f"<h1>{html.escape(title)}</h1><div class='content'>{paragraphs}</div>"
        book.add_item(chapter)
        items.append(chapter)
        if VOLUME_HEADING_RE.match(title):
            flush_volume()
            current_volume = chapter
        elif current_volume is not None:
            current_volume_children.append(chapter)
        else:
            toc.append(chapter)

    flush_volume()
    book.toc = tuple(toc)
    book.spine = ["nav", *items]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    style = epub.EpubItem(
        uid="style", file_name="style/main.css", media_type="text/css",
        content=b"body{font-family:serif;line-height:1.6;margin:5%;}.content{display:block;}.content p{text-indent:1.5em;margin:.4em 0;display:block;}h1{text-align:center;}p{text-indent:1.5em;margin:.4em 0;}"
    )
    book.add_item(style)
    for item in items:
        item.add_item(style)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file then atomically replace, so a crash mid-write can
    # never leave a corrupt half-written EPUB at the destination.
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        epub.write_epub(str(temporary), book, {'epub3_landmark': False, 'epub3_pages': False})
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def _generate_text_cover(title: str, text_color: str, bg_color: str) -> bytes:
    """Generate a simple text-only cover image with title centered on colored background."""
    width, height = 800, 1200
    img = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)
    
    # Try to load a large font; fallback to default if not found
    try:
        font = ImageFont.truetype("arial.ttf", 60)
    except Exception:
        font = ImageFont.load_default()
    
    # Word wrap the title if it's too long
    words = title.split()
    lines = []
    current_line = []
    
    for word in words:
        test_line = " ".join(current_line + [word])
        bbox = draw.textbbox((0, 0), test_line, font=font)
        if bbox[2] - bbox[0] <= width - 100:
            current_line.append(word)
        else:
            if current_line:
                lines.append(" ".join(current_line))
            current_line = [word]
    
    if current_line:
        lines.append(" ".join(current_line))
    
    # Calculate total height and vertical centering
    total_height = 0
    line_heights = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_height = bbox[3] - bbox[1]
        line_heights.append(line_height)
        total_height += line_height
    
    line_spacing = 20
    total_height += line_spacing * (len(lines) - 1)
    
    y = (height - total_height) // 2
    
    # Draw each line centered
    for line, line_height in zip(lines, line_heights):
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]
        x = (width - text_width) // 2
        draw.text((x, y), line, fill=text_color, font=font)
        y += line_height + line_spacing
    
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()
