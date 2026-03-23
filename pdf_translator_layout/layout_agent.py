#!/usr/bin/env python3
"""
Layout Agent — redact original text and re-render translated text into PDF.

Usage:
    python layout_agent.py --input doc.pdf --json translated.json [--output doc.ja.pdf]
                           [--font /path/to/font.ttf] [--pages "1,3,5-8"]
"""

import argparse
import json
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

_BULLET_RE  = re.compile(r"([\u2022\u25cf\u25cb\u25a0\u25a1\u2023\u25e6\u2043•])\s+")
_EN_CJK_RE  = re.compile(r"([A-Za-z0-9])\s+([\u3000-\u9fff\uac00-\ud7af])")
_CJK_EN_RE  = re.compile(r"([\u3000-\u9fff\uac00-\ud7af])\s+([A-Za-z0-9])")


def preprocess(text: str) -> str:
    """Apply pre-processing rules to translated text before rendering."""
    # 1. Special char replacements
    for pattern, repl in _SPECIAL_REPL:
        text = pattern.sub(repl, text)

    # 2. bullet + whitespace → bullet + \xa0
    text = _BULLET_RE.sub(lambda m: m.group(1) + "\xa0", text)

    # 3. Non-breaking spaces around mixed CJK/ASCII boundaries to prevent mid-phrase wrap
    text = _EN_CJK_RE.sub(lambda m: m.group(1) + "\xa0" + m.group(2), text)  # ASCII→CJK
    text = _CJK_EN_RE.sub(lambda m: m.group(1) + "\xa0" + m.group(2), text)  # CJK→ASCII

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



def render_page(
    page: fitz.Page,
    page_data: dict,
    font_name: Optional[str],
    fontfile: Optional[str],
    cjk_font: Optional[str],
    page_rect: fitz.Rect,
    plan_page: Optional[dict] = None,
) -> None:
    """Redact and re-render one page."""
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
    # ------------------------------------------------------------------
    for block in blocks:
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
        clusters = _cluster(y0_vals, tol=3.0, min_count=2)
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

    # Cap insert_bbox to original block bbox for table cells
    capped_insert_bboxes = []
    for i, ibbox in enumerate(insert_bboxes):
        if table_cell_mask[i] and i < len(orig_bboxes):
            capped_insert_bboxes.append(orig_bboxes[i])
        else:
            capped_insert_bboxes.append(ibbox)
    insert_bboxes = capped_insert_bboxes

    # ------------------------------------------------------------------
    # Step 9: Phase 2 — compute fitting_sizes via VisualOptimizer
    # ------------------------------------------------------------------
    visual = VisualOptimizer(page, fontname=fn, fontfile=cjk_font)
    fitting_sizes = [
        visual.fitting_size(ibbox, text, ss, color=(0, 0, 0), align=align)
        for ibbox, text, ss, align in zip(insert_bboxes, translated_texts, source_sizes, aligns)
    ]

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
        """Set all render_sizes in each group to the group's minimum."""
        for group in groups_of_indices:
            if len(group) >= 2:
                min_size = min(render_sizes[i] for i in group)
                for i in group:
                    render_sizes[i] = min_size

    # Build column sibling groups (shared x0 ±15px)
    col_visited = [False] * n_blocks
    col_groups = []
    for i in range(n_blocks):
        if col_visited[i]:
            continue
        group = [i]
        xi = insert_bboxes[i].x0
        for j in range(i + 1, n_blocks):
            if not col_visited[j] and abs(insert_bboxes[j].x0 - xi) <= _X0_TOL:
                group.append(j)
        if len(group) >= 2:
            col_groups.append(group)
            for idx in group:
                col_visited[idx] = True

    # Build row sibling groups (shared y0 ±6px)
    row_visited = [False] * n_blocks
    row_groups = []
    for i in range(n_blocks):
        if row_visited[i]:
            continue
        group = [i]
        yi = insert_bboxes[i].y0
        for j in range(i + 1, n_blocks):
            if not row_visited[j] and abs(insert_bboxes[j].y0 - yi) <= _Y0_TOL:
                group.append(j)
        if len(group) >= 2:
            row_groups.append(group)
            for idx in group:
                row_visited[idx] = True

    render_sizes = list(render_sizes)  # ensure mutable list
    _apply_sibling_min(col_groups)
    _apply_sibling_min(row_groups)

    # ------------------------------------------------------------------
    # Step 11: Phase 3 — render
    # ------------------------------------------------------------------
    for idx, (ibbox, text, rs, align) in enumerate(
        zip(insert_bboxes, translated_texts, render_sizes, aligns)
    ):
        # REQ-2: fall back to original block text if preprocessed text is blank
        if not text.strip() and idx < len(blocks):
            text = blocks[idx].get("text", text)
        src_color = source_colors[idx] if idx < len(source_colors) else (0.0, 0.0, 0.0)
        color = visual.adjust_color(src_color)
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
    parser.add_argument("--plan", default=None, help="layout_plan.json from space_planner (optional)")
    parser.add_argument("--tgt", default="ja", help="Target language code (default: ja)")
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
        )

    doc.save(str(output_path), garbage=4, deflate=True)
    doc.close()
    print(f"[INFO] Saved: {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
