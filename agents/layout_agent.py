#!/usr/bin/env python3
"""
Layout Agent — redact original text and re-render translated text into PDF.

Usage:
    python layout_agent.py --input doc.pdf --json translated.json [--output doc.ja.pdf]
                           [--font /path/to/font.ttf] [--pages "1,3,5-8"]
"""

import argparse
import json
import logging
import math
import os
import re
import sys
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

# Sub-agent helpers (same directory)
sys.path.insert(0, str(Path(__file__).parent))
from visual_agent import VisualOptimizer       # noqa: E402
from topology_agent import TopologyAnalyzer    # noqa: E402
from shared_utils import has_cjk, cluster      # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LINE_HEIGHT_FACTOR = 1.2
_MARGIN = 1.0
_Y_GAP_MERGE = 6.0       # pixels — adjacent block merge threshold
_X_OVERLAP_RATIO = 0.30  # 30% x-overlap for adjacent merge


# ---------------------------------------------------------------------------
# Unicode / text helpers
# ---------------------------------------------------------------------------


def _is_fullwidth(cp: int) -> bool:
    """Return True if codepoint renders at full (1 em) width in CJK fonts."""
    return (
        0x3000 <= cp <= 0x9FFF
        or 0xAC00 <= cp <= 0xD7AF
        or 0xF900 <= cp <= 0xFAFF
        or 0x20000 <= cp <= 0x2FA1F
        or 0xFF01 <= cp <= 0xFF60  # fullwidth ASCII variants
        or 0xFFE0 <= cp <= 0xFFE6  # fullwidth symbol variants
    )


def estimate_em_width(text: str) -> float:
    """Estimate text width in em units.  CJK/fullwidth chars = 1.0 em, ASCII = 0.55 em.

    Fullwidth punctuation (U+FF01-U+FF60, U+FFE0-U+FFE6) is treated as 1.0 em
    because CJK fonts render these at full character width.
    """
    total = 0.0
    for ch in text:
        if _is_fullwidth(ord(ch)):
            total += 1.0
        else:
            total += 0.55
    return total


def _estimate_lines_needed(text: str, font_size: float, bbox_width: float) -> int:
    """Estimate visual lines needed, respecting explicit newlines.

    Splits text on ``\\n`` and computes wrapped lines for each segment,
    then sums.  This avoids the bug where ``estimate_em_width`` on the
    full text treats ``\\n`` as a 0.55-em character instead of a forced
    line break.
    """
    if font_size <= 0 or bbox_width <= 0:
        return 999
    chars_per_line = bbox_width / font_size
    if chars_per_line <= 0:
        return 999
    total_lines = 0
    for segment in text.split("\n"):
        em_w = estimate_em_width(segment)
        seg_lines = math.ceil(em_w / chars_per_line) if em_w > 0 else 1
        # An empty segment (blank line) still occupies one visual line
        total_lines += max(seg_lines, 1)
    return total_lines



# ---------------------------------------------------------------------------
# Special-character pre-processing
# ---------------------------------------------------------------------------

_SPECIAL_REPL = [
    (re.compile(r"[▸►→▶]"), "▶"),
    (re.compile(r"[✅]"), "✓"),
    (re.compile(r"[Δδ]"), "△"),
]

_BULLET_RE  = re.compile(r"([\u2022\u25cf\u25cb\u25a0\u25a1\u2023\u25e6\u2043•])\s+")
_EN_CJK_RE  = re.compile(r"([A-Za-z0-9])[ \t]+([\u3000-\u9fff\uac00-\ud7af\uff01-\uff60])")
_CJK_EN_RE  = re.compile(r"([\u3000-\u9fff\uac00-\ud7af\uff01-\uff60])[ \t]+([A-Za-z0-9])")
_NUM_UNIT_RE = re.compile(r"(\d)[ \t]+([A-Za-z])")  # digit + ASCII unit, e.g. "8,000 km"
_UNIT_NUM_RE = re.compile(r"([A-Za-z])[ \t]+(\d)")  # ASCII abbr + digit, e.g. "UNP 1000", "MPI 100"


def preprocess(text: str) -> str:
    """Apply pre-processing rules to translated text before rendering."""
    # 1. Special char replacements
    for pattern, repl in _SPECIAL_REPL:
        text = pattern.sub(repl, text)

    # 2. bullet + whitespace → bullet + \xa0
    text = _BULLET_RE.sub(lambda m: m.group(1) + "\xa0", text)

    # 3. Non-breaking spaces around mixed CJK/ASCII boundaries to prevent mid-phrase wrap
    text = _EN_CJK_RE.sub(lambda m: m.group(1) + "\xa0" + m.group(2), text)  # ASCII→CJK/fullwidth
    text = _CJK_EN_RE.sub(lambda m: m.group(1) + "\xa0" + m.group(2), text)  # CJK/fullwidth→ASCII
    text = _NUM_UNIT_RE.sub(lambda m: m.group(1) + "\xa0" + m.group(2), text)  # digit→ASCII unit
    text = _UNIT_NUM_RE.sub(lambda m: m.group(1) + "\xa0" + m.group(2), text)  # ASCII abbr→digit

    # 4. Strip leading spaces per line
    lines = text.split("\n")
    lines = [ln.lstrip(" ") for ln in lines]
    text = "\n".join(lines)

    return text


# ---------------------------------------------------------------------------
# Font discovery
# ---------------------------------------------------------------------------

_FONT_SEARCH_PATHS = {
    "ja": [
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        os.path.expanduser("~/Library/Fonts/NotoSansCJKjp-Regular.otf"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    ],
    "zh": [
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        os.path.expanduser("~/Library/Fonts/NotoSansCJKsc-Regular.otf"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ],
    "ko": [
        os.path.expanduser("~/Library/Fonts/NotoSansCJKkr-Regular.otf"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ],
}


def find_cjk_font(tgt_lang: str = "ja", hint: Optional[str] = None) -> Optional[str]:
    """Return an absolute path to a suitable CJK font file, or None."""
    if hint and os.path.isfile(hint):
        return hint

    candidates = _FONT_SEARCH_PATHS.get(tgt_lang, []) + _FONT_SEARCH_PATHS.get("ja", [])
    for path in candidates:
        if os.path.isfile(path):
            return path

    # Last-resort: scan common font directories
    for root in ["/System/Library/Fonts", "/Library/Fonts",
                 os.path.expanduser("~/Library/Fonts"),
                 "/usr/share/fonts"]:
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                lower = fn.lower()
                if any(k in lower for k in ("noto", "cjk", "hiragino", "gothic", "mincho")):
                    full = os.path.join(dirpath, fn)
                    if os.path.isfile(full):
                        return full
    return None


# ---------------------------------------------------------------------------
# Fitting-size helpers
# ---------------------------------------------------------------------------

def _find_fitting_size(
    page: fitz.Page,
    bbox: fitz.Rect,
    text: str,
    base_size: float,
    color: tuple,
    align: int,
    fontname: Optional[str] = None,
    min_size: float = 4.0,
    fontfile: Optional[str] = None,
) -> float:
    """Binary-search for the largest font size that fits *text* inside *bbox*.

    Uses a Shape dry-run (returns value >= 0 when text fits).
    """
    if not text.strip():
        return base_size

    # Small bbox shortcut
    if bbox.width < 2 or bbox.height < 2:
        return min_size

    fn = fontname or "helv"

    if has_cjk(text):
        if fontfile:
            shape = page.new_shape()
            rc = shape.insert_textbox(
                bbox, text,
                fontsize=base_size,
                fontname=fn,
                fontfile=fontfile,
                align=align,
                lineheight=_LINE_HEIGHT_FACTOR,
            )
            if rc >= 0:
                return base_size

            lo, hi = min_size, base_size
            result = min_size
            for _ in range(10):
                mid = (lo + hi) / 2.0
                shape = page.new_shape()
                rc = shape.insert_textbox(
                    bbox, text,
                    fontsize=mid,
                    fontname=fn,
                    fontfile=fontfile,
                    align=align,
                    lineheight=_LINE_HEIGHT_FACTOR,
                )
                if rc >= 0:
                    result = mid
                    lo = mid
                else:
                    hi = mid
            return result
        else:
            lo, hi = min_size, base_size
            result = min_size
            for _ in range(10):
                mid = (lo + hi) / 2.0
                if mid < 0.5:
                    break
                lines_needed = _estimate_lines_needed(text, mid, bbox.width)
                if lines_needed == 1:
                    height_needed = mid
                else:
                    height_needed = lines_needed * mid * _LINE_HEIGHT_FACTOR
                if height_needed <= bbox.height and bbox.width >= mid:
                    result = mid
                    lo = mid
                else:
                    hi = mid
            return result

    # ASCII pre-check at base_size
    shape = page.new_shape()
    rc = shape.insert_textbox(
        bbox, text,
        fontsize=base_size,
        fontname=fn,
        align=align,
        lineheight=_LINE_HEIGHT_FACTOR,
    )
    if rc >= 0:
        return base_size

    lo, hi = min_size, base_size
    result = min_size

    for _ in range(8):
        mid = (lo + hi) / 2.0
        shape = page.new_shape()
        rc = shape.insert_textbox(
            bbox, text,
            fontsize=mid,
            fontname=fn,
            align=align,
            lineheight=_LINE_HEIGHT_FACTOR,
        )
        if rc >= 0:
            result = mid
            lo = mid
        else:
            hi = mid

    return result


def _estimate_text_width(text: str, font_size: float) -> float:
    """Estimate pixel width of text. CJK/fullwidth = font_size, ASCII = font_size * 0.55."""
    w = 0.0
    for ch in text:
        if _is_fullwidth(ord(ch)):
            w += font_size
        else:
            w += font_size * 0.55
    return w


def _wrap_char_colors(char_colors: list, max_width: float, font_size: float) -> list:
    """Word-wrap a list of (char, color) tuples into visual lines.

    Returns a list of lines, where each line is a list of (char, color).
    Wraps at *max_width* pixels using em-width estimation at *font_size*.
    Explicit newlines are honoured.
    """
    lines: list = []
    current_line: list = []
    current_width = 0.0

    for ch, color in char_colors:
        if ch == '\n':
            lines.append(current_line)
            current_line = []
            current_width = 0.0
            continue
        if _is_fullwidth(ord(ch)):
            ch_w = font_size
        else:
            ch_w = font_size * 0.55

        if current_width + ch_w > max_width and current_line:
            lines.append(current_line)
            current_line = []
            current_width = 0.0
        current_line.append((ch, color))
        current_width += ch_w

    if current_line:
        lines.append(current_line)
    return lines


def insert_text_multicolor(
    page: fitz.Page,
    bbox: fitz.Rect,
    text: str,
    base_size: float,
    color_spans: list,
    align: int,
    fontname: Optional[str] = None,
    fontfile: Optional[str] = None,
    line_height: float = _LINE_HEIGHT_FACTOR,
) -> None:
    """Render *text* inside *bbox* with per-span colors using insert_text.

    *color_spans* is a list of {"text": str, "color": [R,G,B]} dicts.
    Each span's text is rendered with its own color.

    The font size is scaled down (to a minimum of 4pt) so that all text
    fits inside *bbox* without truncation.
    """
    if not text.strip():
        return

    # --- Guard: span-text character count consistency ---
    span_count = sum(len(sp["text"].replace("\n", "")) for sp in color_spans)
    text_count = len(text.replace("\n", ""))
    if span_count != text_count:
        fallback_color = tuple(float(c) for c in color_spans[0]["color"]) if color_spans else (0, 0, 0)
        logger.warning(
            "multicolor fallback: span chars (%d) != text chars (%d), block bbox %s",
            span_count, text_count, tuple(bbox),
        )
        insert_text_fitting(
            page, bbox, text, base_size, fallback_color, align,
            fontname=fontname, fontfile=fontfile,
        )
        return

    text = text.replace("\u3000", "\xa0")
    if bbox.width < 2 or bbox.height < 2:
        return

    fn = fontname or "helv"

    # Build segments from color_spans (text-based format)
    segments = []  # list of (segment_text, color_tuple)
    for span in color_spans:
        seg_text = span["text"].replace("\u3000", "\xa0")
        color = tuple(float(c) for c in span["color"])
        if seg_text:
            segments.append((seg_text, color))

    if not segments:
        return

    # Flatten into per-character colors, reconciling with `text` which
    # contains authoritative \n positions that color_spans omit.
    seg_chars = []  # flat list of (ch, color) from segments (skip \n)
    for seg_text, color in segments:
        for ch in seg_text:
            if ch != '\n':
                seg_chars.append((ch, color))

    char_colors = []
    si = 0  # index into seg_chars
    for ch in text:
        if ch == '\n':
            last_color = seg_chars[si - 1][1] if si > 0 else segments[0][1]
            char_colors.append(('\n', last_color))
        else:
            if si < len(seg_chars):
                char_colors.append(seg_chars[si])
                si += 1
            # else: text has more chars than segments — skip gracefully

    # --- Determine font size that fits all text in bbox ---
    # Use the full concatenated text for fitting (same logic as insert_text_fitting)
    full_text = "".join(ch for ch, _ in char_colors)
    dominant_color = segments[0][1] if segments else (0, 0, 0)
    fit_size = _find_fitting_size(
        page, bbox, full_text, base_size, dominant_color, align,
        fontname=fn, min_size=4.0, fontfile=fontfile,
    )
    render_size = fit_size

    # --- Wrap lines at bbox width using render_size ---
    wrapped_lines = _wrap_char_colors(char_colors, bbox.width, render_size)

    # Render each line
    y = bbox.y0 + render_size
    for line_chars in wrapped_lines:
        if not line_chars or all(ch.isspace() for ch, _ in line_chars):
            y += render_size * line_height
            continue
        if y > bbox.y1:
            break

        line_text = "".join(ch for ch, _ in line_chars)

        # Group consecutive same-color chars into runs
        runs = []  # list of (run_text, color)
        for ch, color in line_chars:
            if runs and runs[-1][1] == color:
                runs[-1] = (runs[-1][0] + ch, color)
            else:
                runs.append((ch, color))

        # Compute line width for alignment
        line_width = _estimate_text_width(line_text, render_size)
        if align == 1:  # CENTER
            x = bbox.x0 + (bbox.width - line_width) / 2.0
            x = max(x, bbox.x0)
        elif align == 2:  # RIGHT
            x = bbox.x1 - line_width
            x = max(x, bbox.x0)
        else:  # LEFT
            x = bbox.x0

        # Render each run
        for run_text, color in runs:
            try:
                kwargs = dict(
                    fontsize=render_size,
                    fontname=fn,
                    color=color,
                )
                if fontfile:
                    kwargs["fontfile"] = fontfile
                page.insert_text(fitz.Point(x, y), run_text, **kwargs)
            except Exception as exc:
                print(f"[WARN] insert_text failed: {exc}", file=sys.stderr)
            x += _estimate_text_width(run_text, render_size)

        y += render_size * line_height


def insert_text_fitting(
    page: fitz.Page,
    bbox: fitz.Rect,
    text: str,
    base_size: float,
    color: tuple,
    align: int,
    fontname: Optional[str] = None,
    fontfile: Optional[str] = None,
    min_factor: float = 0.4,
) -> None:
    """Render *text* inside *bbox* at the best fitting font size."""
    if not text.strip():
        return

    # Replace CJK ideographic space with non-breaking space for layout
    text = text.replace("\u3000", "\xa0")

    # Small bbox: skip
    if bbox.width < 2 or bbox.height < 2:
        return

    min_size = max(4.0, base_size * min_factor)
    fn = fontname or "helv"

    fit_size = _find_fitting_size(
        page, bbox, text, base_size, color, align,
        fontname=fn, min_size=min_size, fontfile=fontfile,
    )

    # Commit via Shape — if the em-width estimated fit_size doesn't actually
    # fit (rc < 0), binary-search with real insert_textbox to find a size that
    # works.  This fixes CJK blocks where em-width approximation overestimates
    # capacity, causing text to disappear entirely.
    _ABS_MIN = 3.0  # absolute minimum to prevent invisible text (Bug C fix: lowered from 4.0)

    def _try_commit(size: float) -> bool:
        """Try to render text at *size*; return True if successful."""
        try:
            s = page.new_shape()
            kw = dict(
                fontsize=size,
                fontname=fn,
                color=color,
                align=align,
                lineheight=_LINE_HEIGHT_FACTOR,
            )
            if fontfile:
                kw["fontfile"] = fontfile
            rc = s.insert_textbox(bbox, text, **kw)
            if rc >= 0:
                s.commit()
                return True
        except Exception:
            pass
        return False

    if _try_commit(fit_size):
        return

    # fit_size didn't work — binary search downward with real font metrics
    lo_s, hi_s = _ABS_MIN, fit_size
    best_s = None
    for _ in range(8):
        mid_s = (lo_s + hi_s) / 2.0
        try:
            s = page.new_shape()
            kw = dict(
                fontsize=mid_s,
                fontname=fn,
                color=color,
                align=align,
                lineheight=_LINE_HEIGHT_FACTOR,
            )
            if fontfile:
                kw["fontfile"] = fontfile
            rc = s.insert_textbox(bbox, text, **kw)
            if rc >= 0:
                best_s = mid_s
                lo_s = mid_s
            else:
                hi_s = mid_s
        except Exception:
            hi_s = mid_s

    if best_s is not None and _try_commit(best_s):
        return

    # Last resort: try at _ABS_MIN
    if _try_commit(_ABS_MIN):
        return

    # Fallback: page.insert_textbox with fontfile (different rendering path)
    try:
        kwargs: dict = dict(
            fontsize=fit_size,
            color=color,
            align=align,
            lineheight=_LINE_HEIGHT_FACTOR,
        )
        if fontfile:
            kwargs["fontfile"] = fontfile
            kwargs["fontname"] = fn
        else:
            kwargs["fontname"] = fn
        page.insert_textbox(bbox, text, **kwargs)
    except Exception as exc:
        print(f"[WARN] insert_textbox failed: {exc}", file=sys.stderr)






# ---------------------------------------------------------------------------
# Page-level rendering
# ---------------------------------------------------------------------------

def _merge_adjacent_blocks(
    translated_texts: list,
    bboxes: list,
    font_sizes: list,
) -> tuple:
    """Merge vertically adjacent blocks with x-overlap > 30% and y-gap < 6px.

    Returns new (translated_texts, bboxes, font_sizes).
    """
    if not bboxes:
        return translated_texts, bboxes, font_sizes

    merged = True
    while merged:
        merged = False
        new_texts, new_bboxes, new_sizes = [], [], []
        used = [False] * len(bboxes)

        for i in range(len(bboxes)):
            if used[i]:
                continue
            bi = bboxes[i]
            ti = translated_texts[i]
            si = font_sizes[i]
            for j in range(i + 1, len(bboxes)):
                if used[j]:
                    continue
                bj = bboxes[j]
                y_gap = abs(bj.y0 - bi.y1)
                if y_gap >= _Y_GAP_MERGE:
                    continue
                # x overlap ratio relative to the narrower block
                x_overlap = min(bi.x1, bj.x1) - max(bi.x0, bj.x0)
                min_width = min(bi.width, bj.width)
                if min_width <= 0:
                    continue
                if x_overlap / min_width > _X_OVERLAP_RATIO:
                    # Merge j into i
                    ti = ti + "\n" + translated_texts[j]
                    bi = fitz.Rect(
                        min(bi.x0, bj.x0),
                        min(bi.y0, bj.y0),
                        max(bi.x1, bj.x1),
                        max(bi.y1, bj.y1),
                    )
                    si = max(si, font_sizes[j])
                    used[j] = True
                    merged = True
            new_texts.append(ti)
            new_bboxes.append(bi)
            new_sizes.append(si)
            used[i] = True

        translated_texts = new_texts
        bboxes = new_bboxes
        font_sizes = new_sizes

    return translated_texts, bboxes, font_sizes


def _find_neighbor_y1_limit(
    block_idx: int,
    insert_bboxes: list,
    gap: float = 2.0,
) -> float:
    """Find the max y1 for downward expansion of block_idx without overlapping neighbors.

    Looks for blocks whose y0 is below the current block's y0 and whose
    x-range overlaps with the current block.  Returns the nearest such
    neighbor's y0 minus *gap*, or +inf if no neighbor is found below.
    """
    cur = insert_bboxes[block_idx]
    best_y0 = float("inf")
    for j, other in enumerate(insert_bboxes):
        if j == block_idx:
            continue
        # Only consider blocks whose top edge is below the current block's top edge
        if other.y0 <= cur.y0:
            continue
        # Check x-overlap: the two rects must share some horizontal range
        x_overlap = min(cur.x1, other.x1) - max(cur.x0, other.x0)
        if x_overlap <= 0:
            continue
        if other.y0 < best_y0:
            best_y0 = other.y0
    return best_y0 - gap


def _find_safe_expand_x_limits(
    block_idx: int,
    insert_bboxes: list,
    page_rect: "fitz.Rect",
    gap: float = 2.0,
) -> tuple:
    """Find safe horizontal expansion limits for block_idx.

    Returns (safe_x0, safe_x1) where expansion is constrained by neighboring
    blocks that y-overlap with the current block.  Mirrors the logic of
    _find_neighbor_y1_limit but for the horizontal axis.
    """
    cur = insert_bboxes[block_idx]
    safe_x0 = page_rect.x0
    safe_x1 = page_rect.x1

    for j, other in enumerate(insert_bboxes):
        if j == block_idx:
            continue
        # Check y-overlap with current block
        y_overlap = min(cur.y1, other.y1) - max(cur.y0, other.y0)
        if y_overlap <= 0:
            continue
        # Block is entirely to the LEFT → limit leftward expansion
        if other.x1 <= cur.x0:
            safe_x0 = max(safe_x0, other.x1 + gap)
        # Block is entirely to the RIGHT → limit rightward expansion
        elif other.x0 >= cur.x1:
            safe_x1 = min(safe_x1, other.x0 - gap)

    return safe_x0, safe_x1


def _estimate_text_height(text: str, font_size: float, bbox_width: float) -> float:
    """Estimate rendered height of *text* at *font_size* inside *bbox_width*.

    Uses newline-aware em-width estimation (same as _estimate_lines_needed).
    Returns height in points including line-height factor.
    """
    if font_size <= 0 or bbox_width <= 0:
        return font_size  # single-line fallback
    lines = _estimate_lines_needed(text, font_size, bbox_width)
    # Add 20% safety margin so reflow doesn't place the next block too close.
    # Em-width estimation underestimates actual rendered height (descenders,
    # inter-line spacing rounding), causing adjacent blocks to overlap.
    return lines * font_size * _LINE_HEIGHT_FACTOR * 1.2


def render_page(
    page: fitz.Page,
    page_data: dict,
    font_name: Optional[str],
    fontfile: Optional[str],
    cjk_font: Optional[str],
    page_rect: fitz.Rect,
    plan_page: Optional[dict] = None,
    no_reflow: bool = False,
) -> None:
    """Redact and re-render one page.

    Args:
        no_reflow: If True, skip all reflow logic (Phases 2/3/4).
                   Enabled via --no-reflow CLI flag for A/B comparison.
    """
    blocks = page_data.get("blocks", [])
    if not blocks:
        return

    image_obstacles = [fitz.Rect(b) for b in page_data.get("image_obstacles", [])]

    # Fetch drawings once — reused in Step 1 (REQ-1) and Step 7+8 (topology)
    drawings = page.get_drawings()

    # Pre-build list of filled-drawing rects for background overlap check (REQ-1)
    filled_drawing_rects = [
        fitz.Rect(d["rect"])
        for d in drawings
        if d.get("fill") is not None and d.get("rect") is not None
    ]

    def _has_background_overlap(rect: fitz.Rect, threshold: float = 0.30) -> bool:
        """Return True if rect overlaps > threshold fraction with background elements."""
        rect_area = rect.width * rect.height
        if rect_area <= 0:
            return False
        for bg_rect in filled_drawing_rects:
            inter = rect & bg_rect  # intersection
            if inter.is_empty:
                continue
            if (inter.width * inter.height) / rect_area > threshold:
                return True
        for obs in image_obstacles:
            inter = rect & obs
            if inter.is_empty:
                continue
            if (inter.width * inter.height) / rect_area > threshold:
                return True
        return False

    # ------------------------------------------------------------------
    # Step 1: Redact
    # Phase 3: for multi-block groups, redact the union bbox of all blocks
    # in the group (original positions) so the full reflow region is cleared.
    # Single-block groups and non-group blocks use per-block redact logic.
    # Skipped when --no-reflow is set.
    # ------------------------------------------------------------------
    groups_raw = (plan_page or {}).get("groups", [])
    # Build mapping: block_idx → union_bbox for blocks in multi-block groups
    _group_union_bbox: dict[int, fitz.Rect] = {}
    if groups_raw and not no_reflow:
        for grp in groups_raw:
            indices = grp.get("block_indices", [])
            if len(indices) < 2:
                continue
            union: Optional[fitz.Rect] = None
            for bi in indices:
                if bi < 0 or bi >= len(blocks):
                    continue
                for rb in blocks[bi].get("redact_bboxes", []):
                    r = fitz.Rect(rb)
                    union = r if union is None else union | r
            if union is not None and not union.is_empty:
                for bi in indices:
                    _group_union_bbox[bi] = union

    # Track already-added union rects to avoid redundant annotations
    _union_rects_added: set[tuple] = set()
    for blk_idx, block in enumerate(blocks):
        if blk_idx in _group_union_bbox:
            union_r = _group_union_bbox[blk_idx]
            key = (union_r.x0, union_r.y0, union_r.x1, union_r.y1)
            if key not in _union_rects_added:
                _union_rects_added.add(key)
                if _has_background_overlap(union_r):
                    page.add_redact_annot(union_r, fill=None)
                else:
                    page.add_redact_annot(union_r)
        else:
            for rb in block.get("redact_bboxes", []):
                r = fitz.Rect(rb)
                if _has_background_overlap(r):
                    # Transparent fill: removes text but preserves background (REQ-1)
                    page.add_redact_annot(r, fill=None)
                else:
                    page.add_redact_annot(r)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    # ------------------------------------------------------------------
    # Step 2: Pre-process translated texts
    # ------------------------------------------------------------------
    translated_texts = [preprocess(b.get("translated", "")) for b in blocks]
    bboxes = [fitz.Rect(b["bbox"]) for b in blocks]
    source_sizes = [float(b.get("font_size", 10.0)) for b in blocks]
    aligns = [int(b.get("align", 0)) for b in blocks]  # 0=left,1=center,2=right
    # Preserve original text color; normalize from [R,G,B] list to tuple
    source_colors = [
        tuple(float(c) for c in b.get("color", [0.0, 0.0, 0.0]))
        for b in blocks
    ]

    # ------------------------------------------------------------------
    # Step 3: Adjacent merge (skipped when plan is provided — consolidator
    # already handled fragmentation; plan's block count must stay in sync)
    # ------------------------------------------------------------------
    if plan_page is None:
        translated_texts, bboxes, source_sizes = _merge_adjacent_blocks(
            translated_texts, bboxes, source_sizes
        )
        # Re-derive aligns after merge
        new_aligns = []
        for i, bbox in enumerate(bboxes):
            best_align = 0
            best_dist = float("inf")
            for orig_b, orig_a in zip([fitz.Rect(b["bbox"]) for b in blocks],
                                      aligns if len(aligns) == len(blocks) else [0]*len(blocks)):
                d = abs(orig_b.x0 - bbox.x0) + abs(orig_b.y0 - bbox.y0)
                if d < best_dist:
                    best_dist = d
                    best_align = orig_a
            new_aligns.append(best_align)
        aligns = new_aligns

    # ------------------------------------------------------------------
    # Step 4: Title detection — use plan when available
    # ------------------------------------------------------------------
    if plan_page is not None:
        title_indices: set[int] = set(plan_page.get("title_indices", []))
    else:
        max_fs = max(source_sizes) if source_sizes else 10.0
        title_threshold = max_fs * 0.85
        page_h = page_rect.height
        title_indices = set()
        for idx, (fs, bbox) in enumerate(zip(source_sizes, bboxes)):
            is_large = fs >= title_threshold and fs >= 16.0
            in_top = bbox.y0 < page_h * 0.25
            very_large = fs >= 40.0
            if is_large and (in_top or very_large):
                title_indices.add(idx)

    # ------------------------------------------------------------------
    # Step 5: Snap y0 — use plan's precomputed snap_map if available
    # ------------------------------------------------------------------
    if plan_page is not None:
        raw_snap = plan_page.get("snap_map", {})
        snap_map: dict[float, float] = {float(k): float(v) for k, v in raw_snap.items()}
    else:
        y0_vals = [b.y0 for b in bboxes]
        clusters = cluster(y0_vals, tol=3.0, min_count=2)
        snap_map = {}
        for rep, members in clusters.items():
            for v in members:
                snap_map[v] = rep

    snapped_bboxes = []
    for b in bboxes:
        new_y0 = snap_map.get(b.y0, b.y0)
        snapped_bboxes.append(fitz.Rect(b.x0, new_y0, b.x1, b.y1 + (new_y0 - b.y0)))
    bboxes = snapped_bboxes

    # ------------------------------------------------------------------
    # Step 6: Insert CJK font
    # ------------------------------------------------------------------
    if cjk_font:
        try:
            page.insert_font(fontname="F0", fontfile=cjk_font)
            font_name = "F0"
        except Exception as exc:
            print(f"[WARN] Could not insert font: {exc}", file=sys.stderr)

    fn = font_name or "helv"

    # ------------------------------------------------------------------
    # Step 7+8: Topology analysis — use precomputed plan when available
    # ------------------------------------------------------------------
    if plan_page is not None:
        plan_cells = plan_page.get("cells", [])
        insert_bboxes = [fitz.Rect(c["insert_bbox"]) for c in plan_cells]
        # Extract container colors from plan (optional field, may be absent)
        container_colors = [
            tuple(c["container_color"]) if "container_color" in c else None
            for c in plan_cells
        ]
        # Align count: if plan and block lists diverge (shouldn't happen), fall back
        if len(insert_bboxes) != len(bboxes):
            print(
                f"[WARN] plan cell count ({len(insert_bboxes)}) != block count ({len(bboxes)}); "
                "falling back to live topology",
                file=sys.stderr,
            )
            plan_page = None  # trigger fallback below

    if plan_page is None:
        # `drawings` was already fetched before Step 1 (REQ-1)
        topo_result = TopologyAnalyzer(page_rect).analyze(
            bboxes, aligns, drawings, image_obstacles
        )
        insert_bboxes = topo_result.insert_bboxes
        container_colors = topo_result.container_colors

    # ------------------------------------------------------------------
    # REQ-2: Table cell detection — cap insert_bbox to original block bbox
    # ------------------------------------------------------------------
    # A block is a table cell if it has a horizontal neighbor:
    #   abs(other_y0 - this_y0) <= 8px  AND  abs(other_x0 - this_x0) > 60px
    orig_bboxes = [fitz.Rect(b["bbox"]) for b in blocks]
    table_cell_mask = [False] * len(bboxes)
    for i in range(len(bboxes)):
        bi = orig_bboxes[i] if i < len(orig_bboxes) else bboxes[i]
        for j in range(len(bboxes)):
            if i == j:
                continue
            bj = orig_bboxes[j] if j < len(orig_bboxes) else bboxes[j]
            if (
                abs(bj.y0 - bi.y0) <= 8
                and abs(bj.x0 - bi.x0) > 60
            ):
                table_cell_mask[i] = True
                break

    # REQ-2: For table cells, keep insert_bbox from space_planner
    # (already column-width-capped) instead of reverting to orig_bbox,
    # which may be too narrow for translated text (e.g. lidar→LiDAR).

    # ------------------------------------------------------------------
    # Step 9: Phase 2 — compute fitting_sizes via VisualOptimizer
    # ------------------------------------------------------------------
    _READABILITY_FLOOR = 8.0  # minimum readable font size (pt)
    _OVERFLOW_MIN_SIZE = 4.0  # absolute minimum for overflow fallback

    visual = VisualOptimizer(page, fontname=fn, fontfile=cjk_font)
    fitting_sizes = [
        visual.fitting_size(ibbox, text, ss, color=(0, 0, 0), align=align)
        for ibbox, text, ss, align in zip(insert_bboxes, translated_texts, source_sizes, aligns)
    ]

    # Step 9b: Readability overflow — when fitting_size < 8pt, expand bbox
    # instead of using tiny font.  This targets diagram/architecture pages
    # with many small annotation blocks.
    overflow_expanded = [False] * len(fitting_sizes)  # track which blocks were expanded
    for i in range(len(fitting_sizes)):
        if fitting_sizes[i] < _READABILITY_FLOOR and translated_texts[i].strip():
            safe_x0, safe_x1 = _find_safe_expand_x_limits(i, insert_bboxes, page_rect)
            constrained_rect = fitz.Rect(safe_x0, page_rect.y0, safe_x1, page_rect.y1)
            expanded = visual.overflow_bbox(
                insert_bboxes[i],
                translated_texts[i],
                _READABILITY_FLOOR,
                color=(0, 0, 0),
                align=aligns[i],
                page_rect=constrained_rect,
            )
            # Clamp downward expansion to avoid overlapping neighbor blocks.
            # Bug A fix: if the clamp would shrink the bbox below 50% of its
            # pre-expansion height (already-overlapping source bboxes), skip it —
            # clamping here would produce a bbox shorter than the original.
            orig_height = insert_bboxes[i].height
            neighbor_y1_limit = _find_neighbor_y1_limit(i, insert_bboxes)
            if expanded.y1 > neighbor_y1_limit:
                clamped_height = neighbor_y1_limit - expanded.y0
                if clamped_height < orig_height * 0.5:
                    pass  # skip clamp — it would shrink below original bbox height
                else:
                    expanded = fitz.Rect(expanded.x0, expanded.y0, expanded.x1, neighbor_y1_limit)
            insert_bboxes[i] = expanded
            # Verify text actually fits at 8pt in expanded bbox; if not,
            # re-fit with a lower floor so text renders (small > invisible).
            verify_size = visual.fitting_size(
                expanded, translated_texts[i], _READABILITY_FLOOR,
                color=(0, 0, 0), align=aligns[i], min_size=_OVERFLOW_MIN_SIZE,
            )
            if verify_size >= _READABILITY_FLOOR:
                fitting_sizes[i] = _READABILITY_FLOOR
                overflow_expanded[i] = True
            else:
                # Expansion was insufficient — use the best size that fits
                fitting_sizes[i] = verify_size

    # ------------------------------------------------------------------
    # Step 9c: Phase 2 — reflow position calculation
    # For each multi-block group from space_planner, restack blocks
    # vertically from the group anchor, preserving original inter-block
    # gaps. Only y-coordinates change; x stays fixed.
    # Skipped when --no-reflow is set or groups[] is absent in the plan.
    # ------------------------------------------------------------------
    # reflow_y[i] = (new_y0, new_y1) or None if this block is not reflowed
    reflow_y: list[Optional[tuple]] = [None] * len(fitting_sizes)

    if groups_raw and not no_reflow:
        _REFLOW_PAGE_MARGIN = 10.0  # px margin from page bottom
        _page_height = page_rect.height

        for grp in groups_raw:
            grp_indices = grp.get("block_indices", [])
            if len(grp_indices) < 2:
                continue  # single-block groups are never reflowed

            # Only include indices that are in range and have fitting sizes
            valid = [
                bi for bi in grp_indices
                if 0 <= bi < len(fitting_sizes) and fitting_sizes[bi] is not None
            ]
            if len(valid) < 2:
                continue

            # Sort by original y0 (top to bottom)
            try:
                sorted_grp = sorted(
                    valid,
                    key=lambda bi: bboxes[bi].y0 if bi < len(bboxes) else 0.0,
                )
            except Exception:
                continue  # skip malformed group

            cursor_y = float(grp["anchor"][1])  # y0 of first block in group

            for pos, bi in enumerate(sorted_grp):
                if bi >= len(bboxes) or bi >= len(fitting_sizes):
                    break
                blk_bbox = bboxes[bi]
                fs = fitting_sizes[bi]
                if fs is None or fs <= 0:
                    continue

                if pos == 0:
                    inter_gap = 0.0
                else:
                    prev_bi = sorted_grp[pos - 1]
                    prev_bbox = bboxes[prev_bi] if prev_bi < len(bboxes) else blk_bbox
                    # Preserve original gap; clamp negative (overlapping source blocks)
                    inter_gap = max(0.0, blk_bbox.y0 - prev_bbox.y1)

                cursor_y += inter_gap
                new_y0 = cursor_y

                # Estimate rendered height using fitting_size
                bw = blk_bbox.width if blk_bbox.width > 0 else (
                    insert_bboxes[bi].width if bi < len(insert_bboxes) else 1.0
                )
                txt = translated_texts[bi] if bi < len(translated_texts) else ""
                rh = _estimate_text_height(txt, fs, bw)
                new_y1 = new_y0 + rh

                # Stop reflow if we've run off the page
                if new_y0 >= _page_height - _REFLOW_PAGE_MARGIN:
                    logger.debug(
                        "reflow: block %d new_y0=%.1f exceeds page bottom; stopping group",
                        bi, new_y0,
                    )
                    break

                new_y1 = min(new_y1, _page_height - _REFLOW_PAGE_MARGIN)
                reflow_y[bi] = (new_y0, new_y1)
                cursor_y = new_y1

    # ------------------------------------------------------------------
    # Step 10: Consistency pass via VisualOptimizer
    # ------------------------------------------------------------------
    title_mask = [i in title_indices for i in range(len(fitting_sizes))]
    render_sizes = visual.consistency_map(fitting_sizes, source_sizes, title_mask)

    # ------------------------------------------------------------------
    # REQ-3: Parallel sibling font-size normalization
    # ------------------------------------------------------------------
    # Detect vertical column siblings (same x0 ±15px) and horizontal row
    # siblings (same y0 ±6px).  Within each group of 2+, set all render
    # sizes to min(render_sizes_in_group).  Font sizes only — no colors.
    _X0_TOL = 15.0  # tolerance for vertical column siblings
    _Y0_TOL = 6.0   # tolerance for horizontal row siblings
    n_blocks = len(render_sizes)

    def _apply_sibling_min(groups_of_indices: list) -> None:
        """Set all render_sizes in non-title members of each group to the group's minimum.

        Title blocks are excluded from normalization: they must not be capped
        down to the minimum of body-text siblings that happen to share an x0/y0.
        The result is floored at _READABILITY_FLOOR to prevent tiny text.
        """
        for group in groups_of_indices:
            # Only normalize non-title members among themselves
            non_title = [i for i in group if not title_mask[i]]
            if len(non_title) >= 2:
                group_min = max(
                    min(render_sizes[i] for i in non_title),
                    _READABILITY_FLOOR,
                )
                for i in non_title:
                    render_sizes[i] = min(render_sizes[i], group_min)

    # Build column sibling groups (shared x0 ±15px) — exclude title blocks
    col_visited = [False] * n_blocks
    col_groups = []
    for i in range(n_blocks):
        if col_visited[i] or title_mask[i]:
            continue
        group = [i]
        xi = insert_bboxes[i].x0
        for j in range(i + 1, n_blocks):
            if not col_visited[j] and not title_mask[j] and abs(insert_bboxes[j].x0 - xi) <= _X0_TOL:
                group.append(j)
        if len(group) >= 2:
            col_groups.append(group)
            for idx in group:
                col_visited[idx] = True

    # Build row sibling groups (shared y0 ±6px) — exclude title blocks
    row_visited = [False] * n_blocks
    row_groups = []
    for i in range(n_blocks):
        if row_visited[i] or title_mask[i]:
            continue
        group = [i]
        yi = insert_bboxes[i].y0
        for j in range(i + 1, n_blocks):
            if not row_visited[j] and not title_mask[j] and abs(insert_bboxes[j].y0 - yi) <= _Y0_TOL:
                group.append(j)
        if len(group) >= 2:
            row_groups.append(group)
            for idx in group:
                row_visited[idx] = True

    render_sizes = list(render_sizes)  # ensure mutable list
    _apply_sibling_min(col_groups)
    _apply_sibling_min(row_groups)

    # ------------------------------------------------------------------
    # Step 10b: Content overflow — expand bbox for blocks where text
    # still exceeds bbox capacity at render_size (prevents truncation).
    # If bbox expansion is insufficient (page bounds), fall back to
    # shrinking font size below the 8pt floor — truncated text is worse
    # than small text.
    # ------------------------------------------------------------------
    for i in range(len(render_sizes)):
        if not translated_texts[i].strip():
            continue
        rs = render_sizes[i]
        ibbox = insert_bboxes[i]
        if rs <= 0 or ibbox.width < 2 or ibbox.height < 2:
            continue
        # Estimate if text overflows: use em-width estimation (newline-aware)
        lines_needed = _estimate_lines_needed(translated_texts[i], rs, ibbox.width)
        height_needed = lines_needed * rs * _LINE_HEIGHT_FACTOR
        if height_needed > ibbox.height * 1.1:  # >10% overflow
            safe_x0, safe_x1 = _find_safe_expand_x_limits(i, insert_bboxes, page_rect)
            constrained_rect = fitz.Rect(safe_x0, page_rect.y0, safe_x1, page_rect.y1)
            expanded = visual.overflow_bbox(
                ibbox,
                translated_texts[i],
                rs,
                color=(0, 0, 0),
                align=aligns[i],
                page_rect=constrained_rect,
            )
            # Clamp downward expansion to avoid overlapping neighbor blocks
            neighbor_y1_limit = _find_neighbor_y1_limit(i, insert_bboxes)
            if expanded.y1 > neighbor_y1_limit:
                expanded = fitz.Rect(expanded.x0, expanded.y0, expanded.x1, neighbor_y1_limit)
            insert_bboxes[i] = expanded

            # Check if expansion was sufficient — re-estimate overflow
            exp_bbox = insert_bboxes[i]
            if rs > 0 and exp_bbox.width > 0:
                exp_lines = _estimate_lines_needed(translated_texts[i], rs, exp_bbox.width)
                exp_height = exp_lines * rs * _LINE_HEIGHT_FACTOR
                if exp_height > exp_bbox.height * 1.05:
                    # Bbox expansion insufficient — shrink font to fit
                    new_rs = _find_fitting_size(
                        page, exp_bbox, translated_texts[i],
                        base_size=rs,
                        color=(0, 0, 0),
                        align=aligns[i],
                        fontname=fn,
                        min_size=_OVERFLOW_MIN_SIZE,
                    )
                    render_sizes[i] = new_rs

    # ------------------------------------------------------------------
    # Step 11: Phase 3 — render
    # Phase 4: if a block has a reflow_y entry (from Step 9c), use the
    # reflow bbox [x0, new_y0, x1, new_y1] instead of insert_bbox.
    # ------------------------------------------------------------------
    for idx, (ibbox, text, rs, align) in enumerate(
        zip(insert_bboxes, translated_texts, render_sizes, aligns)
    ):
        # Phase 4: apply reflow position when available
        if idx < len(reflow_y) and reflow_y[idx] is not None:
            try:
                ry0, ry1 = reflow_y[idx]
                ibbox = fitz.Rect(ibbox.x0, ry0, ibbox.x1, ry1)
            except Exception:
                pass  # fall back to original insert_bbox on any error

        # REQ-2: fall back to original block text if preprocessed text is blank
        if not text.strip() and idx < len(blocks):
            text = blocks[idx].get("text", text)
        src_color = source_colors[idx] if idx < len(source_colors) else (0.0, 0.0, 0.0)
        bg_color = container_colors[idx] if idx < len(container_colors) else None
        color = visual.adjust_color(src_color, bg_color)

        block = blocks[idx] if idx < len(blocks) else {}

        # Prefer translated_spans (from span-aware translation) over color_spans
        translated_spans = block.get("translated_spans", [])
        if len(translated_spans) > 1:
            # Adjust each span color through visual.adjust_color
            adjusted_cs = [
                {"text": s["text"],
                 "color": list(visual.adjust_color(tuple(float(c) for c in s["color"]), bg_color))}
                for s in translated_spans
            ]
            insert_text_multicolor(
                page, ibbox, text,
                base_size=rs,
                color_spans=adjusted_cs,
                align=align,
                fontname=fn,
                fontfile=cjk_font,
            )
        else:
            # Fallback: check original color_spans (text format)
            cs = block.get("color_spans", [])
            has_multicolor = len(cs) > 1

            if has_multicolor:
                # Adjust each span color through visual.adjust_color
                adjusted_cs = [
                    {"text": s["text"],
                     "color": list(visual.adjust_color(tuple(float(c) for c in s["color"]), bg_color))}
                    for s in cs
                ]
                insert_text_multicolor(
                    page, ibbox, text,
                    base_size=rs,
                    color_spans=adjusted_cs,
                    align=align,
                    fontname=fn,
                    fontfile=cjk_font,
                )
            else:
                # If this block was overflow-expanded (Step 9b), prevent
                # re-shrinking below the readability floor by setting
                # min_factor=1.0 so min_size == base_size == render_size.
                mf = 1.0 if overflow_expanded[idx] else 0.4
                insert_text_fitting(
                    page, ibbox, text,
                    base_size=rs,
                    color=color,
                    align=align,
                    fontname=fn,
                    fontfile=cjk_font,
                    min_factor=mf,
                )


# ---------------------------------------------------------------------------
# Page-range parsing
# ---------------------------------------------------------------------------

def parse_pages(spec: str) -> list:
    """Parse a page specification like "1,3,5-8" into a sorted list of 1-based page numbers."""
    pages = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        else:
            pages.add(int(part))
    return sorted(pages)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Layout agent: redact and re-render translated PDF."
    )
    parser.add_argument("--input", required=True, help="Source PDF file path")
    parser.add_argument("--json", required=True, help="translated.json path")
    parser.add_argument("--output", default=None, help="Output PDF path (default: <stem>.ja.pdf)")
    parser.add_argument("--font", default=None, help="CJK font file path")
    parser.add_argument("--pages", default=None, help='Page spec, e.g. "1,3,5-8"')
    parser.add_argument("--plan", default=None, help="layout_plan.json from space_planner (optional)")
    parser.add_argument("--tgt", default="ja", help="Target language code (default: ja)")
    parser.add_argument(
        "--no-reflow", dest="no_reflow", action="store_true",
        help="Disable group reflow (Phases 2/3/4). Useful for A/B comparison.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[ERROR] Input PDF not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    json_path = Path(args.json)
    if not json_path.exists():
        print(f"[ERROR] JSON not found: {json_path}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output) if args.output else input_path.with_suffix("").with_name(
        input_path.stem + ".ja.pdf"
    )

    # Load layout plan (optional)
    plan_map: dict[str, dict] = {}
    if args.plan:
        plan_path = Path(args.plan)
        if plan_path.exists():
            with open(plan_path, encoding="utf-8") as f:
                plan_data = json.load(f)
            for pp in plan_data.get("pages", []):
                plan_map[str(pp["page_num"])] = pp
            print(f"[INFO] Loaded layout plan: {plan_path} ({len(plan_map)} pages)", file=sys.stderr)
        else:
            print(f"[WARN] --plan file not found: {plan_path}", file=sys.stderr)

    # Load JSON
    with open(json_path, encoding="utf-8") as f:
        translated_data = json.load(f)

    # Normalise JSON schema: {"version":..., "pages":[...]} or list
    if isinstance(translated_data, dict) and "pages" in translated_data:
        pages_list = translated_data["pages"]
    elif isinstance(translated_data, list):
        pages_list = translated_data
    else:
        print("[ERROR] Unrecognised translated.json schema", file=sys.stderr)
        sys.exit(1)
    page_map = {str(p["page_num"]): p for p in pages_list}

    # Determine which pages to process
    if args.pages:
        requested_pages = set(parse_pages(args.pages))
    else:
        requested_pages = {int(k) for k in page_map.keys()}

    # Discover CJK font
    cjk_font = find_cjk_font(args.tgt, hint=args.font)
    if cjk_font:
        print(f"[INFO] Using CJK font: {cjk_font}", file=sys.stderr)
        # Self-check: verify glyph coverage against actual translated text
        all_translated = " ".join(
            b.get("translated", "")
            for p in pages_list
            for b in p.get("blocks", [])
        )
        unique_chars = set(all_translated) - set(" \n\t")
        try:
            fnt = fitz.Font(fontfile=cjk_font)
            missing_chars = [ch for ch in unique_chars if not fnt.has_glyph(ord(ch))]
            if missing_chars:
                pct = len(missing_chars) / len(unique_chars) * 100
                print(
                    f"[WARN] Font missing {len(missing_chars)}/{len(unique_chars)} "
                    f"unique chars ({pct:.1f}%): {''.join(sorted(missing_chars)[:20])}",
                    file=sys.stderr,
                )
                # Re-scan system fonts for a better alternative.
                # Candidate must: (a) cover more of the missing chars,
                # AND (b) render the primary CJK script (exclude fallback/placeholder fonts).
                _CJK_PROBE = "日本語テスト한국어中文"  # hiragana/katakana/kanji/hangul/hanzi
                _SKIP_FONTS = {"lastresort", "applecoloremo", ".lastresort"}

                best_font, best_missing = cjk_font, len(missing_chars)
                for root in ["/System/Library/Fonts", "/Library/Fonts",
                             os.path.expanduser("~/Library/Fonts")]:
                    if not os.path.isdir(root):
                        continue
                    for dirpath, _, filenames in os.walk(root):
                        for fn in filenames:
                            if not fn.lower().endswith((".ttf", ".ttc", ".otf")):
                                continue
                            if any(skip in fn.lower() for skip in _SKIP_FONTS):
                                continue
                            fp = os.path.join(dirpath, fn)
                            try:
                                f2 = fitz.Font(fontfile=fp)
                                # Must render the primary script
                                if not all(f2.has_glyph(ord(ch)) for ch in _CJK_PROBE):
                                    continue
                                m = sum(1 for ch in unique_chars if not f2.has_glyph(ord(ch)))
                                if m < best_missing:
                                    best_missing = m
                                    best_font = fp
                            except Exception:
                                pass
                if best_font != cjk_font:
                    print(
                        f"[INFO] Switching to better font: {best_font} "
                        f"(missing {best_missing}/{len(unique_chars)})",
                        file=sys.stderr,
                    )
                    cjk_font = best_font
                else:
                    print(
                        f"[INFO] No better font found; keeping {os.path.basename(cjk_font)} "
                        f"(missing chars are non-critical symbols)",
                        file=sys.stderr,
                    )
            else:
                print(f"[INFO] Font coverage: all {len(unique_chars)} unique chars present.", file=sys.stderr)
        except Exception as exc:
            print(f"[WARN] Could not check font coverage: {exc}", file=sys.stderr)
    else:
        print("[WARN] No CJK font found; CJK text may not render correctly.", file=sys.stderr)

    # Open PDF
    doc = fitz.open(str(input_path))

    for page_num_1based in sorted(requested_pages):
        key = str(page_num_1based)
        if key not in page_map:
            continue
        page_data = page_map[key]
        if not page_data.get("blocks"):
            continue

        page_idx = page_num_1based - 1
        if page_idx < 0 or page_idx >= doc.page_count:
            print(f"[WARN] Page {page_num_1based} out of range, skipping.", file=sys.stderr)
            continue

        page = doc[page_idx]
        page_rect = page.rect
        print(f"[INFO] Rendering page {page_num_1based} ...", file=sys.stderr)

        render_page(
            page=page,
            page_data=page_data,
            font_name=None,
            fontfile=cjk_font,
            cjk_font=cjk_font,
            page_rect=page_rect,
            plan_page=plan_map.get(key),
            no_reflow=args.no_reflow,
        )

    doc.save(str(output_path), garbage=4, deflate=True)
    doc.close()
    print(f"[INFO] Saved: {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
