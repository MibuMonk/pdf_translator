"""
qa_readability.py — Rendering quality checks.
Covers: text_too_small, content_truncated, bbox_overlap, content_drift, glyph_dropout.
Imported by: test_agent
"""
import math
import re
import sys
from pathlib import Path

try:
    import fitz
except ImportError:
    print("ERROR: PyMuPDF not installed.", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from qa_utils import (  # noqa: E402
    extract_pdf_spans_by_page,
    extract_pdf_text_block_bboxes_by_page,
    find_best_span_match,
    _collect_spans_in_bbox,
    _text_similarity,
    _weighted_len,
)


def _check_bbox_overlaps(page_bboxes: dict, source_overlap_keys: set = None) -> list[dict]:
    """
    For each page, check all pairs of text block bboxes for overlap.
    Returns a list of bbox_overlap issues.
    An overlap is reported when the intersection area exceeds 30% of the
    smaller bbox's area.  Max 5 overlap issues per page.
    Note: 10% was too sensitive — CJK paragraph line bboxes from PyMuPDF
    share y-range edges (~10% overlap), causing false positives.

    source_overlap_keys: optional set of (page, bbox_a_rounded, bbox_b_rounded)
    tuples from the source PDF — overlaps matching a baseline entry are skipped
    (they pre-exist and were not introduced by the pipeline).
    """
    def _rnd5(bbox):
        return tuple(round(v / 5) * 5 for v in bbox)

    issues = []
    for page_num, bboxes in page_bboxes.items():
        page_issues = []
        n = len(bboxes)
        for i in range(n):
            if len(page_issues) >= 5:
                break
            ax0, ay0, ax1, ay1 = bboxes[i]
            a_area = (ax1 - ax0) * (ay1 - ay0)
            for j in range(i + 1, n):
                if len(page_issues) >= 5:
                    break
                bx0, by0, bx1, by1 = bboxes[j]
                b_area = (bx1 - bx0) * (by1 - by0)
                # Intersection
                ix0 = max(ax0, bx0)
                iy0 = max(ay0, by0)
                ix1 = min(ax1, bx1)
                iy1 = min(ay1, by1)
                if ix0 >= ix1 or iy0 >= iy1:
                    continue
                inter_area = (ix1 - ix0) * (iy1 - iy0)
                min_area = min(a_area, b_area)
                if min_area <= 0:
                    continue
                if inter_area > min_area * 0.30:
                    if source_overlap_keys:
                        key = (page_num, _rnd5(bboxes[i]), _rnd5(bboxes[j]))
                        if key in source_overlap_keys:
                            continue
                    page_issues.append({
                        "page": page_num,
                        "type": "bbox_overlap",
                        "severity": "warning",
                        "bbox_a": [round(v, 1) for v in bboxes[i]],
                        "bbox_b": [round(v, 1) for v in bboxes[j]],
                        "intersection_area": round(inter_area, 1),
                        "smaller_bbox_area": round(min_area, 1),
                        "overlap_pct": round(inter_area / min_area * 100, 1),
                    })
        issues.extend(page_issues)
    return issues


def _check_content_drift(pages: list, pdf_spans: dict, drift_tolerance: float = 60.0, min_area: float = 300.0) -> list[dict]:
    """
    Detect L3 content drift: translated block bbox has no rendered text nearby.
    For each block, expand the planned bbox by drift_tolerance on all sides and check
    if any PDF span falls within that zone. If not, compute distance to nearest span
    center; if > drift_tolerance, report a content_drift issue.
    """
    issues = []
    for page_entry in pages:
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks = page_entry.get("blocks", [])
        spans = pdf_spans.get(page_num, [])
        for idx, block in enumerate(blocks):
            # Skip blocks with no translated text
            translated = block.get("translated_text") or block.get("translated", "")
            if not translated or not translated.strip():
                continue
            bbox = block.get("bbox")
            if not bbox or len(bbox) < 4:
                continue
            x0, y0, x1, y1 = bbox
            area = (x1 - x0) * (y1 - y0)
            if area < min_area:
                continue

            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))

            # Expand bbox by drift_tolerance on all sides
            ex0 = x0 - drift_tolerance
            ey0 = y0 - drift_tolerance
            ex1 = x1 + drift_tolerance
            ey1 = y1 + drift_tolerance

            # Check if any span overlaps with expanded bbox
            found_in_zone = False
            for span in spans:
                sx0, sy0, sx1, sy1 = span["bbox"]
                if sx1 > ex0 and sx0 < ex1 and sy1 > ey0 and sy0 < ey1:
                    found_in_zone = True
                    break

            if found_in_zone:
                continue

            # No span found — compute distance from planned bbox center to nearest span center
            cx = (x0 + x1) / 2
            cy = (y0 + y1) / 2
            min_dist = float("inf")
            for span in spans:
                sx0, sy0, sx1, sy1 = span["bbox"]
                scx = (sx0 + sx1) / 2
                scy = (sy0 + sy1) / 2
                dist = math.hypot(scx - cx, scy - cy)
                if dist < min_dist:
                    min_dist = dist

            if min_dist > drift_tolerance:
                issues.append({
                    "page": page_num,
                    "block_id": block_id,
                    "type": "content_drift",
                    "severity": "warning",
                    "planned_bbox": [round(v, 1) for v in bbox],
                    "nearest_span_distance": round(min_dist, 1),
                })
    return issues


def readability_check(translated_json_path: str, pdf_path: str, source_pdf_path: str = None) -> dict:
    """
    Check for readability issues in the rendered output.
    - text_too_small: rendered font size < 8pt
    - content_truncated: translated text far exceeds bbox capacity (>2x)
    - multicolor_fallback: color_spans block with mismatched translated_spans char count
    - structure_collapse_suspect: single block dominating >50% page area with >200 chars
    - inconsistent_sizing: same-content pages with >30% font size difference
    - word_split: English word broken across \\n in translated text (e.g. "Sc\\nenarios")
    - number_unit_split: abbr/number token wrapped across visual lines in PDF (e.g. "UNP\\n1000", "8,000\\nkm")
    - bbox_overlap: overlapping text block bboxes in rendered PDF (intersection > 10% of smaller)
    - content_drift: planned block bbox has no rendered text nearby (L3)
    """
    import json
    from collections import defaultdict

    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
        target_lang = ""
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
        target_lang = data.get("target_lang", "")
    else:
        return {"check_result": "fail", "details": [{"error": "Unexpected JSON structure"}]}

    # Latin-target translations (en, de, fr, es, ...) use narrower characters (~0.5em wide).
    # The capacity formula assumes CJK square chars; for Latin targets, multiply by 2.
    _latin_targets = {"english", "en", "german", "french", "spanish", "portuguese",
                      "italian", "dutch", "russian", "polish", "arabic"}
    _capacity_factor = 2.0 if target_lang.lower() in _latin_targets else 1.0

    # Extract actual rendered spans from PDF
    pdf_spans = extract_pdf_spans_by_page(Path(pdf_path))

    issues: list[dict] = []

    # Collect first-block info per page for inconsistent_sizing check
    page_first_blocks: list[dict] = []

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks = page_entry.get("blocks", [])
        spans = pdf_spans.get(page_num, [])

        first_block_recorded = False

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            translated = (block.get("translated") or "").strip()
            bbox = block.get("bbox", [0, 0, 0, 0])
            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))
            font_size = block.get("font_size", 0)

            if not translated:
                continue

            # Record first text block per page for similarity check
            if not first_block_recorded:
                page_first_blocks.append({
                    "page": page_num,
                    "text": translated,
                    "font_size": font_size,
                })
                first_block_recorded = True

            # --- text_too_small: check actual rendered size ---
            # Use 7.5pt threshold (not 8.0) to allow for floating-point
            # imprecision: layout_agent renders at 8pt but PyMuPDF may
            # store/read back as 7.99x.
            matched_span = find_best_span_match(bbox, spans, tolerance=10.0)
            if matched_span is not None and matched_span["size"] < 7.5:
                issues.append({
                    "page": page_num,
                    "block_id": block_id,
                    "type": "text_too_small",
                    "severity": "warning",
                    "rendered_size": round(matched_span["size"], 2),
                })

            # --- content_truncated: compare text length vs bbox capacity ---
            # Use the actual rendered font size from PDF (if available) instead
            # of the source font_size, because layout_agent may have resized.
            # Also use the rendered span's bbox to account for overflow_bbox
            # expansion that layout_agent performs but doesn't write back to
            # translated.json.
            effective_size = font_size
            effective_bbox = bbox
            if matched_span is not None and matched_span["size"] > 0:
                effective_size = matched_span["size"]
            if len(effective_bbox) >= 4 and effective_size and effective_size > 0:
                bbox_w = effective_bbox[2] - effective_bbox[0]
                bbox_h = effective_bbox[3] - effective_bbox[1]
                if bbox_w > 0 and bbox_h > 0:
                    # Estimate capacity: area / font_size^2 gives rough char count
                    # CJK chars are roughly square (1:1 aspect), Latin ~0.5:1
                    area = bbox_w * bbox_h
                    # Skip extremely small blocks (< 500 sq px) — typically
                    # chart annotations where truncation is unavoidable.
                    if area < 500:
                        continue
                    estimated_capacity = area / (effective_size * effective_size) * _capacity_factor
                    # Use weighted length for fair comparison
                    text_weight = _weighted_len(translated)
                    if estimated_capacity > 0:
                        ratio = text_weight / estimated_capacity
                        if ratio > 2.0:
                            # Determine severity based on ratio and bbox size.
                            # Small bboxes (< 2000 sq px) have inherent space
                            # constraints from the source layout — downgrade to
                            # warning.  For normal bboxes, ratio > 3.0 is error,
                            # 2.0–3.0 is warning.
                            if ratio > 3.0 and area >= 2000:
                                severity = "error"
                            else:
                                severity = "warning"
                            issues.append({
                                "page": page_num,
                                "block_id": block_id,
                                "type": "content_truncated",
                                "severity": severity,
                                "text_weighted_len": round(text_weight, 1),
                                "estimated_capacity": round(estimated_capacity, 1),
                                "ratio": round(ratio, 2),
                            })

    # --- multicolor_fallback_check: detect color degradation ---
    # When a block has color_spans (>=2 distinct colors) but translated_spans
    # total char count != translated char count, layout_agent will fall back to
    # single-color rendering, losing all color information.
    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        for idx, block in enumerate(page_entry.get("blocks", [])):
            if not isinstance(block, dict):
                continue
            color_spans = block.get("color_spans")
            if not color_spans or not isinstance(color_spans, list):
                continue
            # Check for >=2 distinct colors
            distinct_colors = set()
            for cs in color_spans:
                if isinstance(cs, dict) and "color" in cs:
                    c = cs["color"]
                    if isinstance(c, (list, tuple)):
                        distinct_colors.add(tuple(c))
            if len(distinct_colors) < 2:
                continue
            translated_spans = block.get("translated_spans")
            translated = block.get("translated", "")
            if not translated_spans or not isinstance(translated_spans, list):
                continue
            span_chars = sum(len((s.get("text", "") if isinstance(s, dict) else "").replace("\n", "")) for s in translated_spans)
            text_chars = len(translated.replace("\n", ""))
            if span_chars != text_chars:
                block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))
                issues.append({
                    "page": page_num,
                    "block_id": block_id,
                    "type": "multicolor_fallback",
                    "severity": "warning",
                    "span_chars": span_chars,
                    "text_chars": text_chars,
                })

    # --- block_density_check: detect structure collapse ---
    # A single block occupying >50% of total text area on a page with >200 chars
    # is a strong signal that L2 structure collapsed into one block.
    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks = page_entry.get("blocks", [])
        # Compute total text block area on this page
        block_areas = []
        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                block_areas.append(0.0)
                continue
            translated = (block.get("translated") or "").strip()
            if not translated:
                block_areas.append(0.0)
                continue
            bbox = block.get("bbox", [0, 0, 0, 0])
            if len(bbox) >= 4:
                w = bbox[2] - bbox[0]
                h = bbox[3] - bbox[1]
                block_areas.append(max(w, 0) * max(h, 0))
            else:
                block_areas.append(0.0)
        total_area = sum(block_areas)
        if total_area <= 0:
            continue
        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            translated = block.get("translated") or ""
            char_count = len(translated)
            if char_count <= 200:
                continue
            area_pct = block_areas[idx] / total_area
            if area_pct > 0.50:
                block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))
                issues.append({
                    "page": page_num,
                    "block_id": block_id,
                    "type": "structure_collapse_suspect",
                    "severity": "warning",
                    "char_count": char_count,
                    "bbox_area_pct": round(area_pct * 100, 1),
                })

    # --- word_split: detect English words broken across \n in translated text ---
    # When a bbox is too narrow, layout may force-break English words across lines
    # (e.g. "Sc\nenarios", "Li\nDAR").  We detect this by checking \n boundaries
    # in the translated text: if the characters immediately before and after \n
    # are both ASCII letters and they form a continuous letter sequence >= 4 chars,
    # it's likely a broken word.
    _TRAILING_ALPHA = re.compile(r'([a-zA-Z]+)$')
    _LEADING_ALPHA = re.compile(r'^([a-zA-Z]+)')
    word_split_per_page: dict[int, int] = defaultdict(int)
    WORD_SPLIT_PAGE_LIMIT = 5

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        for idx, block in enumerate(page_entry.get("blocks", [])):
            if not isinstance(block, dict):
                continue
            translated = block.get("translated") or ""
            if "\n" not in translated:
                continue
            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))
            lines = translated.split("\n")
            for li in range(len(lines) - 1):
                if word_split_per_page[page_num] >= WORD_SPLIT_PAGE_LIMIT:
                    break
                prev_line = lines[li]
                next_line = lines[li + 1]
                m_tail = _TRAILING_ALPHA.search(prev_line)
                m_head = _LEADING_ALPHA.match(next_line)
                if not m_tail or not m_head:
                    continue
                tail_frag = m_tail.group(1)
                head_frag = m_head.group(1)
                combined = tail_frag + head_frag
                if len(combined) < 4:
                    continue
                # Heuristic: if tail fragment is a very short piece (1-2 chars)
                # it's almost certainly a broken word, not a standalone word.
                # For longer tail fragments (>=3), require that at least one
                # fragment is short (<=2) — otherwise both could be real words.
                if len(tail_frag) >= 3 and len(head_frag) >= 3:
                    continue
                issues.append({
                    "page": page_num,
                    "block_id": block_id,
                    "type": "word_split",
                    "severity": "warning",
                    "broken_word": combined,
                    "split_at": f"...{tail_frag}\\n{head_frag}...",
                })
                word_split_per_page[page_num] += 1

    # --- number_unit_split: detect abbr/number tokens wrapped across visual lines in PDF ---
    # Catches "UNP\n1000", "MPI\n100", "8,000\nkm" patterns that preprocess() should have
    # protected with \xa0. Reads visual lines from the rendered PDF (not translated.json),
    # because the wrap only appears after layout_agent renders the text into the bbox.
    _ABBR_TAIL = re.compile(r'[A-Z]{2,}$')       # line ends with all-caps abbr (≥2 chars)
    _NUM_TAIL  = re.compile(r'[\d,]+\d$')          # line ends with a number (incl. "8,000")
    _NUM_HEAD  = re.compile(r'^\d')                # next line starts with digit
    _UNIT_HEAD = re.compile(r'^[A-Za-z]{1,5}\b')  # next line starts with short word (unit)
    num_split_per_page: dict[int, int] = defaultdict(int)
    NUM_SPLIT_PAGE_LIMIT = 5
    try:
        pdf_doc = fitz.open(str(pdf_path))
        for page_idx in range(len(pdf_doc)):
            page_num = page_idx + 1
            page = pdf_doc[page_idx]
            for blk in page.get_text("dict")["blocks"]:
                if blk.get("type") != 0:
                    continue
                blk_id = blk.get("number")
                pdf_lines = blk.get("lines", [])
                for li in range(len(pdf_lines) - 1):
                    if num_split_per_page[page_num] >= NUM_SPLIT_PAGE_LIMIT:
                        break
                    line_a = "".join(s["text"] for s in pdf_lines[li].get("spans", [])).rstrip()
                    line_b = "".join(s["text"] for s in pdf_lines[li + 1].get("spans", [])).lstrip()
                    if not line_a or not line_b:
                        continue
                    # abbr→num: "UNP" / "1000"
                    if _ABBR_TAIL.search(line_a) and _NUM_HEAD.match(line_b):
                        issues.append({
                            "page": page_num,
                            "block_id": blk_id,
                            "type": "number_unit_split",
                            "severity": "warning",
                            "split_at": f"...{line_a[-8:]}\\n{line_b[:8]}...",
                            "pattern": "abbr_num",
                        })
                        num_split_per_page[page_num] += 1
                    # num→unit: "8,000" / "km"
                    elif _NUM_TAIL.search(line_a) and _UNIT_HEAD.match(line_b):
                        head_word = _UNIT_HEAD.match(line_b).group(0)
                        # Skip all-caps words — those are acronyms (e.g. "DDLD", "CCB"),
                        # not units (km, kph, GHz). Units are lower/mixed case.
                        if len(head_word) <= 5 and not head_word.isupper():
                            issues.append({
                                "page": page_num,
                                "block_id": blk_id,
                                "type": "number_unit_split",
                                "severity": "warning",
                                "split_at": f"...{line_a[-8:]}\\n{line_b[:8]}...",
                                "pattern": "num_unit",
                            })
                            num_split_per_page[page_num] += 1
        pdf_doc.close()
    except Exception:
        pass

    # --- inconsistent_sizing: pages with similar first-block text but different font_size ---
    for i in range(len(page_first_blocks)):
        for j in range(i + 1, len(page_first_blocks)):
            a = page_first_blocks[i]
            b = page_first_blocks[j]
            if a["font_size"] and b["font_size"] and a["font_size"] > 0 and b["font_size"] > 0:
                sim = _text_similarity(a["text"], b["text"])
                if sim > 0.80:
                    size_diff = abs(a["font_size"] - b["font_size"]) / max(a["font_size"], b["font_size"])
                    if size_diff > 0.30:
                        issues.append({
                            "page_a": a["page"],
                            "page_b": b["page"],
                            "type": "inconsistent_sizing",
                            "severity": "warning",
                            "font_size_a": round(a["font_size"], 2),
                            "font_size_b": round(b["font_size"], 2),
                            "similarity": round(sim, 3),
                            "size_diff_pct": round(size_diff * 100, 1),
                        })

    # --- bbox_overlap: detect overlapping text blocks in rendered PDF ---
    # If source_pdf_path is provided, compute baseline overlaps from the source
    # and filter them out — only NEW overlaps introduced by the pipeline are flagged.
    pdf_block_bboxes = extract_pdf_text_block_bboxes_by_page(Path(pdf_path))
    source_overlap_keys: set = set()
    if source_pdf_path:
        src_bboxes = extract_pdf_text_block_bboxes_by_page(Path(source_pdf_path))
        def _rnd5(bbox):
            return tuple(round(v / 5) * 5 for v in bbox)
        for pg, bboxes in src_bboxes.items():
            n = len(bboxes)
            for i in range(n):
                ax0, ay0, ax1, ay1 = bboxes[i]
                for j in range(i + 1, n):
                    bx0, by0, bx1, by1 = bboxes[j]
                    ix0 = max(ax0, bx0); iy0 = max(ay0, by0)
                    ix1 = min(ax1, bx1); iy1 = min(ay1, by1)
                    if ix0 >= ix1 or iy0 >= iy1:
                        continue
                    inter_area = (ix1 - ix0) * (iy1 - iy0)
                    min_area = min((ax1-ax0)*(ay1-ay0), (bx1-bx0)*(by1-by0))
                    if min_area > 0 and inter_area > min_area * 0.10:
                        # Key: (page, rounded bbox_a, rounded bbox_b)
                        # Use 5pt rounding so minor float drift doesn't miss the match
                        source_overlap_keys.add((pg, _rnd5(bboxes[i]), _rnd5(bboxes[j])))
    overlap_issues = _check_bbox_overlaps(pdf_block_bboxes, source_overlap_keys)
    issues.extend(overlap_issues)

    # --- content_drift: detect text rendered far from its planned position ---
    drift_issues = _check_content_drift(pages, pdf_spans)
    issues.extend(drift_issues)

    has_errors = any(i.get("severity") == "error" for i in issues)
    return {
        "check_result": "fail" if has_errors else "pass",
        "details": {
            "issues": issues,
            "text_too_small_count": sum(1 for i in issues if i["type"] == "text_too_small"),
            "content_truncated_count": sum(1 for i in issues if i["type"] == "content_truncated"),
            "inconsistent_sizing_count": sum(1 for i in issues if i["type"] == "inconsistent_sizing"),
            "multicolor_fallback_count": sum(1 for i in issues if i["type"] == "multicolor_fallback"),
            "structure_collapse_suspect_count": sum(1 for i in issues if i["type"] == "structure_collapse_suspect"),
            "word_split_count": sum(1 for i in issues if i["type"] == "word_split"),
            "number_unit_split_count": sum(1 for i in issues if i["type"] == "number_unit_split"),
            "bbox_overlap_count": sum(1 for i in issues if i["type"] == "bbox_overlap"),
            "content_drift_count": sum(1 for i in issues if i["type"] == "content_drift"),
        },
    }


def glyph_dropout_check(translated_json_path: str, pdf_path: str) -> dict:
    """
    Check for glyph dropout (L7): characters present in translated.json but
    missing from the rendered PDF.  Compares per-block translated text against
    the concatenated text of overlapping PDF spans.

    Returns dict with "check_result" ("pass"/"fail") and "details" containing
    "issues" list.  Each issue has page, block_index, severity, code, message.
    """
    import json
    import unicodedata

    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
    else:
        return {"check_result": "fail", "details": {"issues": [], "error": "Unexpected JSON structure"}}

    pdf_spans = extract_pdf_spans_by_page(Path(pdf_path))
    issues: list[dict] = []

    # Load layout_plan.json to identify reflow-group blocks (their render
    # positions differ from original bbox — don't report dropout there).
    # Key: 0-based page index (matches translated.json pages array order).
    _reflow_blocks: dict[int, set[int]] = {}  # page_index → set of block indices in multi-block groups
    layout_plan_path = Path(translated_json_path).parent / "layout_plan.json"
    if layout_plan_path.exists():
        try:
            with open(layout_plan_path, encoding="utf-8") as _f:
                _plan = json.load(_f)
            for _pi, _pp in enumerate(_plan.get("pages", [])):
                for _grp in _pp.get("groups", []):
                    if len(_grp.get("block_indices", [])) > 1:
                        _reflow_blocks.setdefault(_pi, set()).update(_grp["block_indices"])
        except Exception:
            pass

    for page_idx, page_entry in enumerate(pages):
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", page_idx + 1))
        blocks = page_entry.get("blocks", [])
        spans = pdf_spans.get(page_num, [])
        reflow_indices = _reflow_blocks.get(page_idx, set())

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            translated = (block.get("translated") or "").strip()
            if not translated:
                continue

            bbox = block.get("bbox", [0, 0, 0, 0])
            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))

            # Reflow-group blocks are rendered at a shifted y position;
            # use all page spans so the character-presence check isn't
            # confused by the position change.
            if idx in reflow_indices:
                matched_spans = spans
            else:
                matched_spans = _collect_spans_in_bbox(spans, bbox, tolerance=10.0)

            rendered_text = "".join(s["text"] for s in matched_spans)

            # Normalize: strip whitespace, normalize unicode for both strings
            def _normalize(s):
                s = unicodedata.normalize("NFC", s)
                # Remove all whitespace
                s = re.sub(r'\s+', '', s)
                # Strip invisible/zero-width Unicode chars that PDF rendering won't extract
                # (WORD JOINER U+2060, ZERO WIDTH SPACE U+200B, ZERO WIDTH JOINER U+200D,
                #  ZERO WIDTH NON-JOINER U+200C, BOM U+FEFF, soft hyphen U+00AD)
                s = re.sub(r'[\u00ad\u200b-\u200d\u2060\ufeff]', '', s)
                return s

            norm_translated = _normalize(translated)
            norm_rendered = _normalize(rendered_text)

            if not norm_translated:
                continue

            # Skip very short blocks (single char) — too noisy
            if len(norm_translated) <= 1:
                continue

            # Count how many unique chars from translated are missing in rendered
            rendered_chars = set(norm_rendered)
            missing_chars = []
            for ch in norm_translated:
                if ch not in rendered_chars:
                    missing_chars.append(ch)

            # Deduplicate for reporting
            unique_missing = list(dict.fromkeys(missing_chars))
            if not unique_missing:
                continue

            # Calculate dropout ratio (missing char occurrences / total chars)
            dropout_ratio = len(missing_chars) / len(norm_translated)

            # Tolerance: allow up to 10% dropout or <= 1 missing unique char
            # for short texts (< 20 chars)
            if len(norm_translated) < 20 and len(unique_missing) <= 1:
                continue
            if dropout_ratio <= 0.10:
                continue

            severity = "error" if dropout_ratio > 0.25 else "warning"
            sample = "".join(unique_missing[:10])
            issues.append({
                "page": page_num,
                "block_index": idx,
                "block_id": block_id,
                "severity": severity,
                "code": "L7",
                "type": "glyph_dropout",
                "message": (
                    f"Glyph dropout: {len(unique_missing)} unique char(s) missing "
                    f"({dropout_ratio:.0%} of text). "
                    f"Missing sample: '{sample}'"
                ),
                "dropout_ratio": round(dropout_ratio, 3),
                "missing_count": len(missing_chars),
                "translated_length": len(norm_translated),
            })

    has_errors = any(i.get("severity") == "error" for i in issues)
    return {
        "check_result": "fail" if has_errors else "pass",
        "details": {
            "issues": issues,
            "glyph_dropout_count": len(issues),
        },
    }
