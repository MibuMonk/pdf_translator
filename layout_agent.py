#!/usr/bin/env python3
"""
Layout Agent — redact original text and re-render translated text into PDF.

Usage:
    python layout_agent.py --input doc.pdf --json translated.json [--output doc.ja.pdf]
                           [--font /path/to/font.ttf] [--pages "1,3,5-8"]
"""

import argparse
import bisect
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LINE_HEIGHT_FACTOR = 1.4
_MARGIN = 1.0
_Y_GAP_MERGE = 6.0       # pixels — adjacent block merge threshold
_X_OVERLAP_RATIO = 0.30  # 30% x-overlap for adjacent merge


# ---------------------------------------------------------------------------
# Unicode / text helpers
# ---------------------------------------------------------------------------

def _has_cjk(text: str) -> bool:
    """Return True if *text* contains CJK or kana characters."""
    for ch in text:
        cp = ord(ch)
        if (
            0x3000 <= cp <= 0x9FFF   # CJK unified + hiragana/katakana
            or 0xAC00 <= cp <= 0xD7AF  # Hangul syllables
            or 0xF900 <= cp <= 0xFAFF  # CJK compatibility
            or 0x20000 <= cp <= 0x2FA1F  # CJK extensions B-F
        ):
            return True
    return False


def estimate_em_width(text: str) -> float:
    """Estimate text width in em units.  CJK chars = 1.0 em, ASCII = 0.55 em."""
    total = 0.0
    for ch in text:
        cp = ord(ch)
        if (
            0x3000 <= cp <= 0x9FFF
            or 0xAC00 <= cp <= 0xD7AF
            or 0xF900 <= cp <= 0xFAFF
            or 0x20000 <= cp <= 0x2FA1F
        ):
            total += 1.0
        else:
            total += 0.55
    return total


def _truncate_to_em_width(text: str, max_em: float) -> str:
    """Truncate *text* so that its em-width does not exceed *max_em*, appending '…'."""
    total = 0.0
    for i, ch in enumerate(text):
        cp = ord(ch)
        w = 1.0 if (
            0x3000 <= cp <= 0x9FFF
            or 0xAC00 <= cp <= 0xD7AF
            or 0xF900 <= cp <= 0xFAFF
            or 0x20000 <= cp <= 0x2FA1F
        ) else 0.55
        if total + w > max_em:
            return text[:i] + "…"
        total += w
    return text


# ---------------------------------------------------------------------------
# Special-character pre-processing
# ---------------------------------------------------------------------------

_SPECIAL_REPL = [
    (re.compile(r"[▸►→▶]"), "▶"),
    (re.compile(r"[✅]"), "✓"),
    (re.compile(r"[Δδ]"), "△"),
]

_BULLET_RE = re.compile(r"([\u2022\u25cf\u25cb\u25a0\u25a1\u2023\u25e6\u2043•])\s+")
_EN_CJK_RE = re.compile(r"([A-Za-z0-9])\s+([\u3000-\u9fff\uac00-\ud7af])")


def preprocess(text: str) -> str:
    """Apply pre-processing rules to translated text before rendering."""
    # 1. Special char replacements
    for pattern, repl in _SPECIAL_REPL:
        text = pattern.sub(repl, text)

    # 2. bullet + whitespace → bullet + \xa0
    text = _BULLET_RE.sub(lambda m: m.group(1) + "\xa0", text)

    # 3. English word + space + CJK → \xa0 (non-breaking space to prevent mid-word wrap)
    text = _EN_CJK_RE.sub(lambda m: m.group(1) + "\xa0" + m.group(2), text)

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

    # ASCII pre-check at base_size
    if not _has_cjk(text):
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
        fontname=fn, min_size=min_size,
    )

    # Commit via Shape; fall back to page.insert_textbox when fontfile is needed
    try:
        shape = page.new_shape()
        rc = shape.insert_textbox(
            bbox, text,
            fontsize=fit_size,
            fontname=fn,
            color=color,
            align=align,
            lineheight=_LINE_HEIGHT_FACTOR,
        )
        if rc >= 0:
            shape.commit()
            return
        shape.commit()  # commit anyway (partial render)
    except Exception:
        pass

    # Fallback: page.insert_textbox with fontfile
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
# Clustering helper
# ---------------------------------------------------------------------------

def _cluster(vals: list, tol: float = 3.0, min_count: int = 2) -> dict:
    """Group floats that are within *tol* of each other.

    Returns {representative_value: [original_values]} for groups with
    at least *min_count* members.
    """
    if not vals:
        return {}
    sorted_vals = sorted(vals)
    groups: list[list[float]] = [[sorted_vals[0]]]
    for v in sorted_vals[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    result = {}
    for grp in groups:
        if len(grp) >= min_count:
            rep = sum(grp) / len(grp)
            result[rep] = grp
    return result


# ---------------------------------------------------------------------------
# Voronoi cell tree (axis-parallel L∞)
# ---------------------------------------------------------------------------

def _build_cell_tree(
    bboxes: list,
    page_rect: fitz.Rect,
    obstacles: Optional[list] = None,
) -> list:
    """Compute axis-parallel Voronoi cells for each bbox.

    Each cell is a fitz.Rect that represents the maximum space available to
    that bbox, bounded by the page and any obstacles (image regions).

    Row-aware x-boundaries: only obstacles with y-overlap (±b.height tolerance).
    Column-aware y-boundaries: only obstacles with x-overlap (±b.width tolerance).
    """
    if obstacles is None:
        obstacles = []

    all_rects = list(bboxes) + list(obstacles)
    cells = []

    for i, b in enumerate(bboxes):
        cx = (b.x0 + b.x1) / 2.0
        cy = (b.y0 + b.y1) / 2.0
        bw = b.width
        bh = b.height

        # Y-tolerance for x-boundary search (row-aware)
        Y_TOL = bh
        # X-tolerance for y-boundary search (column-aware)
        X_TOL = bw

        # Start with full page
        cell_x0 = page_rect.x0
        cell_x1 = page_rect.x1
        cell_y0 = page_rect.y0
        cell_y1 = page_rect.y1

        for j, obs in enumerate(all_rects):
            if j == i:
                continue

            obs_cx = (obs.x0 + obs.x1) / 2.0
            obs_cy = (obs.y0 + obs.y1) / 2.0

            # X-boundary (left/right): only if y-overlap within tolerance
            y_overlap = not (obs.y1 < b.y0 - Y_TOL or obs.y0 > b.y1 + Y_TOL)
            if y_overlap:
                if obs_cx < cx and obs.x1 > cell_x0:
                    cell_x0 = max(cell_x0, obs.x1)
                elif obs_cx > cx and obs.x0 < cell_x1:
                    cell_x1 = min(cell_x1, obs.x0)

            # Y-boundary (top/bottom): only if x-overlap within tolerance
            x_overlap = not (obs.x1 < b.x0 - X_TOL or obs.x0 > b.x1 + X_TOL)
            if x_overlap:
                if obs_cy < cy and obs.y1 > cell_y0:
                    cell_y0 = max(cell_y0, obs.y1)
                elif obs_cy > cy and obs.y0 < cell_y1:
                    cell_y1 = min(cell_y1, obs.y0)

        # Guarantee cell covers at least the original bbox
        cell_x0 = min(cell_x0, b.x0)
        cell_x1 = max(cell_x1, b.x1)
        cell_y0 = min(cell_y0, b.y0)
        cell_y1 = max(cell_y1, b.y1)

        cells.append(fitz.Rect(cell_x0, cell_y0, cell_x1, cell_y1))

    return cells


# ---------------------------------------------------------------------------
# Insert-bbox computation from Voronoi cell
# ---------------------------------------------------------------------------

def _cell_insert_bbox(bbox: fitz.Rect, cell: fitz.Rect, align: int) -> fitz.Rect:
    """Compute the actual insertion rect for a block within its Voronoi cell.

    align==2 (RIGHT): anchor right edge to bbox.x1, extend left to cell.x0+MARGIN
    else (LEFT/CENTER): anchor left edge to bbox.x0, extend right to cell.x1-MARGIN
    y0 = bbox.y0, y1 = cell.y1 - MARGIN
    """
    if align == 2:  # RIGHT
        x0 = cell.x0 + _MARGIN
        x1 = bbox.x1
    else:  # LEFT or CENTER
        x0 = bbox.x0
        x1 = cell.x1 - _MARGIN

    y0 = bbox.y0
    y1 = cell.y1 - _MARGIN

    # Guarantee >= original bbox
    x0 = min(x0, bbox.x0)
    x1 = max(x1, bbox.x1)
    y0 = min(y0, bbox.y0)
    y1 = max(y1, bbox.y1)

    return fitz.Rect(x0, y0, x1, y1)


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


def _consistency_pass(fitting_sizes: list, source_sizes: list) -> list:
    """Cap fitting sizes per source_size group at the 80th percentile.

    For each group, sort descending, take the value at index = int(len*0.2)
    as the 80th-percentile cap.
    """
    # Build groups
    groups: dict[float, list] = {}
    for fs, ss in zip(fitting_sizes, source_sizes):
        groups.setdefault(ss, []).append(fs)

    caps: dict[float, float] = {}
    for ss, sizes in groups.items():
        sorted_desc = sorted(sizes, reverse=True)
        idx = int(len(sorted_desc) * 0.2)
        idx = min(idx, len(sorted_desc) - 1)
        caps[ss] = sorted_desc[idx]

    render_sizes = []
    for fs, ss in zip(fitting_sizes, source_sizes):
        render_sizes.append(min(fs, caps[ss]))
    return render_sizes


def render_page(
    page: fitz.Page,
    page_data: dict,
    font_name: Optional[str],
    fontfile: Optional[str],
    cjk_font: Optional[str],
    page_rect: fitz.Rect,
) -> None:
    """Redact and re-render one page."""
    blocks = page_data.get("blocks", [])
    if not blocks:
        return

    image_obstacles = [fitz.Rect(b) for b in page_data.get("image_obstacles", [])]

    # ------------------------------------------------------------------
    # Step 1: Redact
    # ------------------------------------------------------------------
    for block in blocks:
        for rb in block.get("redact_bboxes", []):
            page.add_redact_annot(fitz.Rect(rb))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    # ------------------------------------------------------------------
    # Step 2: Pre-process translated texts
    # ------------------------------------------------------------------
    translated_texts = [preprocess(b.get("translated", "")) for b in blocks]
    bboxes = [fitz.Rect(b["bbox"]) for b in blocks]
    source_sizes = [float(b.get("font_size", 10.0)) for b in blocks]
    aligns = [int(b.get("align", 0)) for b in blocks]  # 0=left,1=center,2=right

    # ------------------------------------------------------------------
    # Step 3: Adjacent merge
    # ------------------------------------------------------------------
    translated_texts, bboxes, source_sizes = _merge_adjacent_blocks(
        translated_texts, bboxes, source_sizes
    )
    # Re-derive aligns after merge (use first block's align; indices may shift)
    # We rebuild from scratch using the merged bboxes
    new_aligns = []
    for i, bbox in enumerate(bboxes):
        # Find original block with closest matching bbox
        best_align = 0
        best_dist = float("inf")
        for orig_b, orig_a in zip([fitz.Rect(b["bbox"]) for b in blocks], aligns if len(aligns) == len(blocks) else [0]*len(blocks)):
            d = abs(orig_b.x0 - bbox.x0) + abs(orig_b.y0 - bbox.y0)
            if d < best_dist:
                best_dist = d
                best_align = orig_a
        new_aligns.append(best_align)
    aligns = new_aligns

    # ------------------------------------------------------------------
    # Step 4: Title detection
    # ------------------------------------------------------------------
    max_fs = max(source_sizes) if source_sizes else 10.0
    title_threshold = max_fs * 0.85
    page_h = page_rect.height
    title_indices: set[int] = set()
    for idx, (fs, bbox) in enumerate(zip(source_sizes, bboxes)):
        is_large = fs >= title_threshold and fs >= 16.0
        in_top = bbox.y0 < page_h * 0.25
        very_large = fs >= 40.0
        if is_large and (in_top or very_large):
            title_indices.add(idx)

    # ------------------------------------------------------------------
    # Step 5: Snap y0 by clustering
    # ------------------------------------------------------------------
    y0_vals = [b.y0 for b in bboxes]
    clusters = _cluster(y0_vals, tol=3.0, min_count=2)
    snap_map: dict[float, float] = {}
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
    # Step 7: Voronoi cells
    # ------------------------------------------------------------------
    cells = _build_cell_tree(bboxes, page_rect, obstacles=image_obstacles)

    # ------------------------------------------------------------------
    # Step 8: Compute insert_bboxes, clipped against image obstacles
    # ------------------------------------------------------------------
    insert_bboxes = []
    for bbox, cell, align in zip(bboxes, cells, aligns):
        ibbox = _cell_insert_bbox(bbox, cell, align)

        # Clip against each image obstacle
        for obs in image_obstacles:
            # If obstacle overlaps ibbox from the right, shrink x1
            if obs.x0 < ibbox.x1 and obs.y0 < ibbox.y1 and obs.y1 > ibbox.y0:
                if obs.x0 > ibbox.x0:
                    ibbox = fitz.Rect(ibbox.x0, ibbox.y0, min(ibbox.x1, obs.x0), ibbox.y1)
            # If obstacle overlaps from the left, shrink x0
            if obs.x1 > ibbox.x0 and obs.y0 < ibbox.y1 and obs.y1 > ibbox.y0:
                if obs.x1 < ibbox.x1:
                    ibbox = fitz.Rect(max(ibbox.x0, obs.x1), ibbox.y0, ibbox.x1, ibbox.y1)
            # If obstacle overlaps from the bottom, shrink y1
            if obs.y0 > ibbox.y0 and obs.x0 < ibbox.x1 and obs.x1 > ibbox.x0:
                if obs.y0 < ibbox.y1:
                    ibbox = fitz.Rect(ibbox.x0, ibbox.y0, ibbox.x1, min(ibbox.y1, obs.y0))

        # Guarantee at least original bbox
        ibbox = fitz.Rect(
            min(ibbox.x0, bbox.x0),
            min(ibbox.y0, bbox.y0),
            max(ibbox.x1, bbox.x1),
            max(ibbox.y1, bbox.y1),
        )
        insert_bboxes.append(ibbox)

    # ------------------------------------------------------------------
    # Step 9: Phase 2 — compute fitting_sizes
    # ------------------------------------------------------------------
    fitting_sizes = []
    for idx, (ibbox, text, ss, align) in enumerate(
        zip(insert_bboxes, translated_texts, source_sizes, aligns)
    ):
        base = ss if idx not in title_indices else ss
        fs = _find_fitting_size(
            page, ibbox, text, base_size=base,
            color=(0, 0, 0), align=align,
            fontname=fn, min_size=4.0,
        )
        fitting_sizes.append(fs)

    # ------------------------------------------------------------------
    # Step 10: Consistency pass
    # ------------------------------------------------------------------
    render_sizes = _consistency_pass(fitting_sizes, source_sizes)

    # ------------------------------------------------------------------
    # Step 11: Phase 3 — render
    # ------------------------------------------------------------------
    for idx, (ibbox, text, rs, align) in enumerate(
        zip(insert_bboxes, translated_texts, render_sizes, aligns)
    ):
        color = (0, 0, 0)
        insert_text_fitting(
            page, ibbox, text,
            base_size=rs,
            color=color,
            align=align,
            fontname=fn,
            fontfile=cjk_font,
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

    # Load JSON
    with open(json_path, encoding="utf-8") as f:
        translated_data = json.load(f)

    # translated_data is expected to be a dict: {page_num_str: page_data}
    # or a list indexed by page number.
    if isinstance(translated_data, list):
        page_map = {str(i + 1): d for i, d in enumerate(translated_data)}
    else:
        page_map = {str(k): v for k, v in translated_data.items()}

    # Determine which pages to process
    if args.pages:
        requested_pages = set(parse_pages(args.pages))
    else:
        requested_pages = {int(k) for k in page_map.keys()}

    # Discover CJK font
    cjk_font = find_cjk_font("ja", hint=args.font)
    if cjk_font:
        print(f"[INFO] Using CJK font: {cjk_font}", file=sys.stderr)
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
        )

    doc.save(str(output_path), garbage=4, deflate=True)
    doc.close()
    print(f"[INFO] Saved: {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
