#!/usr/bin/env python3
"""
Round-trip layout evaluation script.

Translates a PDF A→B→A and measures how well layout is preserved
by comparing original blocks to round-tripped blocks.

Usage:
  python3 scripts/roundtrip_eval.py <pdf_path> --lang-b <lang> [options]
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import fitz
from scipy.optimize import linear_sum_assignment

HIGH_COST = 2.0


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

def _detect_lang_from_filename(pdf_path: Path) -> str:
    """Guess source language from filename heuristics; fallback to 'en'."""
    name = pdf_path.stem.lower()
    for marker in ('ja', 'jpn', 'japanese', '\u65e5\u672c'):
        if marker in name:
            return 'ja'
    for marker in ('zh', 'chn', 'chinese', '\u4e2d\u6587'):
        if marker in name:
            return 'zh'
    for marker in ('ko', 'kor', 'korean'):
        if marker in name:
            return 'ko'
    return 'en'


# ---------------------------------------------------------------------------
# Block extraction
# ---------------------------------------------------------------------------

def extract_blocks(pdf_path, filter_covered=False):
    doc = fitz.open(str(pdf_path))
    all_blocks = []
    for page_num, page in enumerate(doc):
        page_w = page.rect.width
        page_h = page.rect.height
        raw = page.get_text('dict', flags=fitz.TEXT_PRESERVE_WHITESPACE)

        # When filter_covered=True (used for layout_agent output PDFs), build a
        # list of opaque fill rects and skip XObject ghost blocks that survived
        # apply_redactions() but are visually covered by white/bg cover rects.
        opaque_rects = []
        if filter_covered:
            for d in page.get_drawings():
                fill = d.get("fill")
                # Only white/light fills are layout_agent cover rects.
                # Dark fills are slide backgrounds — must NOT be treated as covers.
                if fill is not None and d.get("rect") is not None:
                    if all(c >= 0.7 for c in fill[:3]):
                        opaque_rects.append(fitz.Rect(d["rect"]))

        def _is_covered(bbox, threshold=0.80):
            """Return True if >= threshold fraction of bbox area is covered by opaque rects."""
            if not opaque_rects:
                return False
            r = fitz.Rect(bbox)
            area = r.width * r.height
            if area <= 0:
                return False
            covered = 0.0
            for or_ in opaque_rects:
                inter = r & or_
                if not inter.is_empty:
                    covered += inter.width * inter.height
            return (covered / area) >= threshold

        for block in raw['blocks']:
            if block['type'] != 0:
                continue
            # Skip blocks substantially covered by opaque drawing rects —
            # these are XObject ghosts that survived apply_redactions().
            if filter_covered and _is_covered(block['bbox']):
                continue
            lines = block['lines']
            line_count = len(lines)
            text_parts, colors, sizes = [], [], []
            for line in lines:
                for span in line['spans']:
                    t = span['text'].strip()
                    if t:
                        text_parts.append(t)
                    colors.append(span['color'])
                    sizes.append(span['size'])
            text = ' '.join(text_parts)
            if not text:
                continue
            dom_color = max(set(colors), key=colors.count) if colors else 0
            avg_size = sum(sizes) / len(sizes) if sizes else 0
            all_blocks.append({
                'page': page_num,
                'bbox': list(block['bbox']),
                'text': text,
                'line_count': line_count,
                'color': dom_color,
                'font_size': avg_size,
                'page_w': page_w,
                'page_h': page_h,
            })
    return all_blocks


# ---------------------------------------------------------------------------
# Text similarity
# ---------------------------------------------------------------------------

def bigram_jaccard(a, b):
    def bigrams(s):
        s = s.replace(' ', '')
        return set(s[i:i+2] for i in range(len(s) - 1)) if len(s) >= 2 else set(s)
    ba, bb = bigrams(a), bigrams(b)
    if not ba and not bb:
        return 1.0
    return len(ba & bb) / len(ba | bb)


def _sim_cache_key(a, b):
    return f'{a[:50]}|||{b[:50]}'


def _load_sim_cache(cache_path: Path) -> dict:
    if cache_path.exists():
        try:
            with open(cache_path, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_sim_cache(cache_path: Path, cache: dict):
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def text_sim_cached(a, b, cache):
    key = _sim_cache_key(a, b)
    if key not in cache:
        cache[key] = bigram_jaccard(a, b)
    return cache[key]


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def geo_dist(b_orig, b_rt):
    pw, ph = b_orig['page_w'], b_orig['page_h']
    cx_o = (b_orig['bbox'][0] + b_orig['bbox'][2]) / 2 / pw
    cy_o = (b_orig['bbox'][1] + b_orig['bbox'][3]) / 2 / ph
    cx_r = (b_rt['bbox'][0] + b_rt['bbox'][2]) / 2 / pw
    cy_r = (b_rt['bbox'][1] + b_rt['bbox'][3]) / 2 / ph
    return ((cx_o - cx_r) ** 2 + (cy_o - cy_r) ** 2) ** 0.5


def bbox_area(bbox):
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def color_to_hex(color_int: int) -> str:
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8) & 0xFF
    b = color_int & 0xFF
    return f'#{r:02X}{g:02X}{b:02X}'


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match_page(orig_blocks, rt_blocks, alpha, beta, sim_cache):
    """Hungarian matching for one page. Returns (matches, orphan_orig, orphan_rt)."""
    n, m = len(orig_blocks), len(rt_blocks)
    if n == 0 and m == 0:
        return [], 0, 0

    size = max(n, m)
    cost = [[HIGH_COST] * size for _ in range(size)]

    for i, bo in enumerate(orig_blocks):
        for j, br in enumerate(rt_blocks):
            ts = text_sim_cached(bo['text'], br['text'], sim_cache)
            gd = geo_dist(bo, br)
            cost[i][j] = alpha * (1 - ts) + beta * gd

    row_ind, col_ind = linear_sum_assignment(cost)

    matches = []
    assigned_orig = set()
    assigned_rt = set()

    for r, c in zip(row_ind, col_ind):
        is_real_orig = r < n
        is_real_rt = c < m
        if is_real_orig and is_real_rt:
            bo = orig_blocks[r]
            br = rt_blocks[c]
            ts = text_sim_cached(bo['text'], br['text'], sim_cache)
            gd = geo_dist(bo, br)
            mc = alpha * (1 - ts) + beta * gd
            fsd_pct = ((br['font_size'] - bo['font_size']) / bo['font_size'] * 100
                       if bo['font_size'] else 0.0)
            matches.append({
                'page': bo['page'],
                'orig_text': bo['text'],
                'rt_text': br['text'],
                'text_sim': ts,
                'match_cost': mc,
                'color_match': bo['color'] == br['color'],
                'color_orig': color_to_hex(bo['color']),
                'color_rt': color_to_hex(br['color']),
                'line_delta': br['line_count'] - bo['line_count'],
                'font_size_orig': bo['font_size'],
                'font_size_rt': br['font_size'],
                'font_size_delta_pct': fsd_pct,
                'bbox_area_orig': bbox_area(bo['bbox']),
                'bbox_area_rt': bbox_area(br['bbox']),
                'bbox_rt': br['bbox'],
            })
            assigned_orig.add(r)
            assigned_rt.add(c)

    orphan_orig = n - len(assigned_orig)
    orphan_rt = m - len(assigned_rt)
    return matches, orphan_orig, orphan_rt


# ---------------------------------------------------------------------------
# Identity eval helpers
# ---------------------------------------------------------------------------

def _create_identity_translated_json(parsed_json_path: Path, output_path: Path) -> None:
    """Create translated.json with translated = text for each block (no API needed)."""
    with open(parsed_json_path, encoding="utf-8") as f:
        data = json.load(f)
    for page in data.get("pages", []):
        for block in page.get("blocks", []):
            block["translated"] = block.get("text", "")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _run_identity(pdf_path: Path, work_dir: Path, pages=None) -> Path:
    """Run parse→consolidate→identity_translate→space_plan→layout without API calls.
    Returns path to the rendered output PDF."""
    work_dir.mkdir(parents=True, exist_ok=True)
    agents_dir = Path(__file__).resolve().parent.parent / 'agents'

    stem = pdf_path.stem
    parsed_json = work_dir / f"{stem}.parsed.json"
    translated_json = work_dir / f"{stem}.translated.json"
    layout_plan = work_dir / "layout_plan.json"
    output_pdf = work_dir / "identity_output.pdf"

    def _run(cmd):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"{cmd[0]} failed:\n{r.stderr[-500:]}")

    _run([sys.executable, str(agents_dir / "parse_agent.py"),
          "--input", str(pdf_path), "--output", str(parsed_json)]
         + (["--pages", pages] if pages else []))
    _run([sys.executable, str(agents_dir / "consolidator.py"), "--input", str(parsed_json)])
    _create_identity_translated_json(parsed_json, translated_json)
    _run([sys.executable, str(agents_dir / "space_planner.py"),
          "--input", str(pdf_path), "--parsed", str(parsed_json),
          "--output", str(layout_plan)])
    _run([sys.executable, str(agents_dir / "layout_agent.py"),
          "--input", str(pdf_path), "--json", str(translated_json),
          "--plan", str(layout_plan), "--output", str(output_pdf)]
         + (["--pages", pages] if pages else []))
    return output_pdf


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

def _run_layout_only(rt_b_pdf: Path, work_ba_dir: Path, lang_a: str, rt_a_pdf: Path):
    """Re-run only layout_agent using cached translated.json + layout_plan.json."""
    translated_json = work_ba_dir / f'{rt_b_pdf.stem}.translated.json'
    layout_plan_json = work_ba_dir / 'layout_plan.json'
    if not translated_json.exists():
        raise FileNotFoundError(f'Missing cached translated.json: {translated_json}')
    agents_dir = Path(__file__).resolve().parent.parent / 'agents'
    cmd = [
        sys.executable,
        str(agents_dir / 'layout_agent.py'),
        '--input', str(rt_b_pdf),
        '--json',  str(translated_json),
        '--output', str(rt_a_pdf),
        '--tgt',   lang_a,
    ]
    if layout_plan_json.exists():
        cmd += ['--plan', str(layout_plan_json)]
    print(f'  [layout-only] {" ".join(cmd)}')
    subprocess.run(cmd, check=True)


def _run_pipeline(input_pdf: Path, src: str, tgt: str, output_pdf: Path,
                  workdir: Path, cache: Path):
    pipeline_path = Path(__file__).resolve().parent.parent / 'run_pipeline.py'
    cmd = [
        sys.executable,
        str(pipeline_path),
        str(input_pdf),
        '--src', src,
        '--tgt', tgt,
        '--output', str(output_pdf),
        '--workdir', str(workdir),
        '--cache', str(cache),
        '--skip-qa',
    ]
    print(f'  Running: {" ".join(cmd)}')
    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------
# Orphan categorization
# ---------------------------------------------------------------------------

def _categorize_orphans(rt_blocks, matched_rt_texts, lang_a):
    """
    Categorize unmatched RT blocks into three types:
      untranslated - contains CJK when lang_a is non-CJK (translate_agent skipped it)
      fragment     - <=5 words and text is a substring of matched content (layout_agent split a block)
      expansion    - everything else (translation genuinely added content)
    """
    def _has_cjk(text):
        return any('\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff' for c in text)

    cjk_target = lang_a in ('ja', 'zh', 'ko')
    matched_lower = [t.lower() for t in matched_rt_texts]

    cats = {'untranslated': [], 'fragment': [], 'expansion': []}
    for b in rt_blocks:
        text = b['text']
        if not cjk_target and _has_cjk(text):
            cats['untranslated'].append(text)
        elif len(text.split()) <= 5 and any(text.lower().strip() in m for m in matched_lower):
            cats['fragment'].append(text)
        else:
            cats['expansion'].append(text)

    return {cat: {'count': len(v), 'examples': v[:3]} for cat, v in cats.items()}


# ---------------------------------------------------------------------------
# Main evaluation function (importable)
# ---------------------------------------------------------------------------

def run_eval(pdf_path, lang_a, lang_b, work_dir, alpha=0.4, beta=0.6, force=False, layout_only=False, identity=False):
    """
    Run full round-trip evaluation and return the report dict.
    Also writes <work_dir>/roundtrip_report.json.

    When identity=True, skips all API calls: parse→identity_translate→layout,
    then compares source PDF vs rendered PDF (layout quality only).
    """
    pdf_path = Path(pdf_path).resolve()

    # Identity mode: use a dedicated work dir if none specified
    if identity and work_dir is None:
        work_dir = pdf_path.parent / 'work_rt_identity'
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    # --- Identity eval branch ---
    if identity:
        print('[Identity] Running parse→identity_translate→layout (no API) ...')
        rendered_pdf = _run_identity(pdf_path, work_dir / 'work_id')

        print('[Identity] Extracting blocks ...')
        orig_blocks = extract_blocks(pdf_path)
        rt_blocks = extract_blocks(rendered_pdf)

        sim_cache_path = work_dir / 'text_sim_cache.json'
        sim_cache = _load_sim_cache(sim_cache_path)

        # For identity mode, alpha=0 (skip text similarity; text is identical)
        id_alpha = 0.0
        id_beta = 1.0

        orig_by_page = {}
        rt_by_page = {}
        for b in orig_blocks:
            orig_by_page.setdefault(b['page'], []).append(b)
        for b in rt_blocks:
            rt_by_page.setdefault(b['page'], []).append(b)

        all_pages = sorted(set(list(orig_by_page.keys()) + list(rt_by_page.keys())))

        all_matches = []
        total_orphan_orig = 0
        total_orphan_rt = 0
        total_ghost_rt = 0  # orphan_rt blocks that overlap matched rt blocks (XObject ghosts)

        for pg in all_pages:
            ob = orig_by_page.get(pg, [])
            rb = rt_by_page.get(pg, [])
            matches, oo, or_ = match_page(ob, rb, id_alpha, id_beta, sim_cache)
            all_matches.extend(matches)
            total_orphan_orig += oo
            total_orphan_rt += or_

            # Ghost detection: orphan_rt blocks that are PDF layout artifacts should
            # not count against the layout quality score.  Four rules are applied;
            # the first match wins (break after each rule).
            #
            # Rule 1 – XObject ghost (original): orphan whose bbox overlaps ≥50% of
            #   its own area with a single matched rt block.  These are XObject ghosts
            #   that survived apply_redactions() and are visually covered.
            #
            # Rule 2 – Merge artifact ghost (single block): orphan whose bbox is ≥80%
            #   contained within a single matched rt block's bbox.  These were absorbed
            #   by _merge_adjacent_blocks() in layout_agent.
            #
            # Rule 3a – Tiny fragment ghost (overlap): small block (< 300 px²) with
            #   ≥30% overlap with any matched block.
            # Rule 3b – Tiny fragment ghost (proximity): small block (< 300 px²)
            #   adjacent (within 10px vertically) to a matched block with ≥30% horizontal
            #   span overlap.  Handles XObject duplicates offset by a few pixels.
            #
            # Rule 4 – Union merge ghost: orphan whose area is ≥45% covered by the
            #   UNION of all matched rt block bboxes.  Catches multi-block merge artifacts
            #   where _merge_adjacent_blocks() merged several source blocks into one large
            #   orphan whose individual overlaps are each below Rule 1/2 thresholds.
            if or_ > 0:
                matched_rt_bboxes = [m['bbox_rt'] for m in matches]
                matched_rt_bbox_keys = {tuple(b) for b in matched_rt_bboxes}
                for orb in rb:
                    if tuple(orb['bbox']) in matched_rt_bbox_keys:
                        continue  # this block was actually matched
                    orb_area = bbox_area(orb['bbox'])
                    if orb_area <= 0:
                        continue
                    ox0, oy0, ox1, oy1 = orb['bbox']
                    orb_w = ox1 - ox0
                    is_ghost = False
                    for mb in matched_rt_bboxes:
                        ix0 = max(ox0, mb[0]); iy0 = max(oy0, mb[1])
                        ix1 = min(ox1, mb[2]); iy1 = min(oy1, mb[3])
                        if ix1 > ix0 and iy1 > iy0:
                            inter_area = (ix1 - ix0) * (iy1 - iy0)
                            overlap_frac = inter_area / orb_area
                            # Rule 1: XObject ghost – orphan ≥50% covered by matched block
                            if overlap_frac >= 0.5:
                                is_ghost = True
                                break
                            # Rule 2: Merge artifact ghost – orphan ≥80% contained within
                            # matched block (absorbed by _merge_adjacent_blocks())
                            mb_area = bbox_area(mb)
                            if mb_area > 0 and overlap_frac >= 0.8:
                                is_ghost = True
                                break
                            # Rule 3a: Tiny fragment ghost – small block (< 300 px²)
                            # with ≥30% overlap with any matched block
                            if orb_area < 300 and overlap_frac >= 0.3:
                                is_ghost = True
                                break
                        if not is_ghost and orb_area < 300:
                            # Rule 3b: Tiny fragment ghost – small block adjacent to a
                            # matched block (within 10px vertically) with ≥30% horizontal
                            # span overlap.  Handles XObject duplicates at slightly offset
                            # y-positions that don't geometrically overlap their counterpart.
                            v_gap = max(oy0 - mb[3], mb[1] - oy1, 0)
                            if v_gap <= 10 and orb_w > 0:
                                x_inter = max(0.0, min(ox1, mb[2]) - max(ox0, mb[0]))
                                if x_inter / orb_w >= 0.3:
                                    is_ghost = True
                                    break
                    if not is_ghost:
                        # Rule 4: Union merge ghost – compute union coverage of orphan
                        # area by all matched blocks using a coarse pixel grid.  Catches
                        # multi-block merge artifacts where no single matched block covers
                        # ≥50% of the orphan but collectively they cover ≥45%.
                        scale = 4  # grid cell size in px (coarse for speed)
                        covered = 0
                        total = 0
                        for gx in range(int(ox0), int(ox1), scale):
                            for gy in range(int(oy0), int(oy1), scale):
                                total += 1
                                for mb in matched_rt_bboxes:
                                    if mb[0] <= gx <= mb[2] and mb[1] <= gy <= mb[3]:
                                        covered += 1
                                        break
                        if total > 0 and covered / total >= 0.45:
                            is_ghost = True
                    if is_ghost:
                        total_ghost_rt += 1

        _save_sim_cache(sim_cache_path, sim_cache)

        matched = len(all_matches)

        # Identity score formula
        overflow_count = sum(1 for m in all_matches if m['line_delta'] > 0)
        font_deltas = [
            (m['font_size_delta_pct'], m['font_size_orig'])
            for m in all_matches
            if m.get('font_size_orig', 0) > 0
        ]
        overflow_rate = overflow_count / max(matched, 1)
        real_orphan_rt = total_orphan_rt - total_ghost_rt
        blank_rate = real_orphan_rt / max(matched + real_orphan_rt, 1)
        font_mse = (
            sum(((abs(d / 100.0 * e) / max(e, 1.0)) ** 2) for d, e in font_deltas)
            / max(len(font_deltas), 1)
        )
        score = 1.0 - (0.4 * overflow_rate + 0.3 * blank_rate + 0.3 * min(font_mse, 1.0))
        score = max(0.0, min(1.0, score))

        avg_line_delta = (sum(m['line_delta'] for m in all_matches) / matched) if matched else 0.0
        avg_fsd = (sum(m['font_size_delta_pct'] for m in all_matches) / matched) if matched else 0.0
        avg_cost = (sum(m['match_cost'] for m in all_matches) / matched) if matched else 0.0
        color_mismatch = sum(1 for m in all_matches if not m['color_match'])
        color_mismatch_pct = color_mismatch / matched * 100 if matched else 0.0
        line_overflow_pct = overflow_count / matched * 100 if matched else 0.0

        worst_blocks = sorted(all_matches, key=lambda m: m['match_cost'], reverse=True)[:20]

        report = {
            'pdf_path': str(pdf_path),
            'lang_a': lang_a,
            'lang_b': lang_b,
            'mode': 'identity',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'summary': {
                'total_blocks': len(orig_blocks),
                'matched_blocks': matched,
                'orphan_orig': total_orphan_orig,
                'orphan_rt': total_orphan_rt,
                'ghost_rt': total_ghost_rt,
                'color_mismatch_count': color_mismatch,
                'color_mismatch_pct': color_mismatch_pct,
                'line_overflow_count': overflow_count,
                'line_overflow_pct': line_overflow_pct,
                'avg_line_delta': avg_line_delta,
                'avg_font_size_delta_pct': avg_fsd,
                'orphan_rt_rate': blank_rate,
                'avg_match_cost': avg_cost,
                'score': score,
            },
            'worst_blocks': worst_blocks,
            'matches': all_matches,
        }

        report_path = work_dir / 'roundtrip_report.json'
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        s = report['summary']
        print()
        print('=== Identity Layout Evaluation ===')
        print(f'  PDF      : {pdf_path}')
        print(f'  Mode     : identity (no API)')
        print(f'  Timestamp: {report["timestamp"]}')
        print()
        print(f'  Blocks   : {s["total_blocks"]} original, {s["matched_blocks"]} matched')
        print(f'  Orphans  : {s["orphan_orig"]} orig-only, {s["orphan_rt"]} rt-only ({s["ghost_rt"]} ghosts)')
        print(f'  Line overflow   : {s["line_overflow_count"]} ({s["line_overflow_pct"]:.1f}%)')
        print(f'  Avg line delta  : {s["avg_line_delta"]:+.2f}')
        print(f'  Avg font \u0394      : {s["avg_font_size_delta_pct"]:+.1f}%')
        print(f'  Orphan RT rate  : {s["orphan_rt_rate"]:.1%}')
        print(f'  Avg match cost  : {s["avg_match_cost"]:.4f}')
        print(f'  SCORE           : {s["score"]:.4f}')
        print(f'  Report saved to : {report_path}')

        return report

    # --- Normal round-trip eval ---
    rt_b_pdf = work_dir / 'rt_B.pdf'
    rt_a_pdf = work_dir / 'rt_A.pdf'

    # Step 1 \u2014 A\u2192B
    if not layout_only:
        if force or not rt_b_pdf.exists():
            print(f'[Step 1] Translating {lang_a}\u2192{lang_b} ...')
            _run_pipeline(
                pdf_path, lang_a, lang_b,
                rt_b_pdf,
                work_dir / 'work_AB',
                work_dir / 'AB.transcache.json',
            )
        else:
            print(f'[Step 1] Skipping A\u2192B (rt_B.pdf exists, use --force to re-run)')

    # Step 2 \u2014 B\u2192A
    if layout_only:
        if not rt_b_pdf.exists():
            raise FileNotFoundError('rt_B.pdf not found \u2014 run full eval first before using layout_only=True')
        print(f'[Step 2] Re-running layout only ({lang_b}\u2192{lang_a}) ...')
        _run_layout_only(rt_b_pdf, work_dir / 'work_BA', lang_a, rt_a_pdf)
    elif force or not rt_a_pdf.exists():
        print(f'[Step 2] Translating {lang_b}\u2192{lang_a} ...')
        _run_pipeline(
            rt_b_pdf, lang_b, lang_a,
            rt_a_pdf,
            work_dir / 'work_BA',
            work_dir / 'BA.transcache.json',
        )
    else:
        print(f'[Step 2] Skipping B\u2192A (rt_A.pdf exists, use --force to re-run)')

    # Step 3 \u2014 Extract blocks
    print('[Step 3] Extracting blocks ...')
    orig_blocks = extract_blocks(pdf_path)
    # Don't use filter_covered for rt_A: layered translation accumulates cover
    # rects from both the A→B and B→A passes, which coincide with rendered text
    # positions and would falsely filter all translated blocks. XObject ghosts
    # in rt_A are handled downstream by the ghost detection rules.
    rt_blocks = extract_blocks(rt_a_pdf, filter_covered=False)

    # Step 4 \u2014 Load / init text similarity cache
    sim_cache_path = work_dir / 'text_sim_cache.json'
    sim_cache = _load_sim_cache(sim_cache_path)

    # Step 5 \u2014 Match per page
    print('[Step 5] Matching blocks ...')
    orig_by_page = {}
    rt_by_page = {}
    for b in orig_blocks:
        orig_by_page.setdefault(b['page'], []).append(b)
    for b in rt_blocks:
        rt_by_page.setdefault(b['page'], []).append(b)

    all_pages = sorted(set(list(orig_by_page.keys()) + list(rt_by_page.keys())))

    all_matches = []
    total_orphan_orig = 0
    total_orphan_rt = 0

    for pg in all_pages:
        ob = orig_by_page.get(pg, [])
        rb = rt_by_page.get(pg, [])
        matches, oo, or_ = match_page(ob, rb, alpha, beta, sim_cache)
        all_matches.extend(matches)
        total_orphan_orig += oo
        total_orphan_rt += or_

    # Save updated sim cache
    _save_sim_cache(sim_cache_path, sim_cache)

    # Categorize orphan RT blocks
    matched_rt_texts = [m['rt_text'] for m in all_matches]
    orphan_rt_blocks = [b for b in rt_blocks if b['text'] not in matched_rt_texts]
    orphan_analysis = _categorize_orphans(orphan_rt_blocks, matched_rt_texts, lang_a)

    # Step 6/7 \u2014 Aggregate metrics
    matched = len(all_matches)
    total_blocks = len(orig_blocks)

    color_mismatch = sum(1 for m in all_matches if not m['color_match'])
    line_overflow = sum(1 for m in all_matches if m['line_delta'] > 0)
    avg_line_delta = (sum(m['line_delta'] for m in all_matches) / matched) if matched else 0.0
    avg_fsd = (sum(m['font_size_delta_pct'] for m in all_matches) / matched) if matched else 0.0
    avg_cost = (sum(m['match_cost'] for m in all_matches) / matched) if matched else 0.0

    color_mismatch_pct = color_mismatch / matched * 100 if matched else 0.0
    line_overflow_pct = line_overflow / matched * 100 if matched else 0.0

    orphan_rt_rate = total_orphan_rt / (matched + total_orphan_rt) if (matched + total_orphan_rt) > 0 else 0.0
    score = 1.0 - (
        0.3 * color_mismatch_pct / 100
        + 0.3 * line_overflow_pct / 100
        + 0.1 * min(abs(avg_fsd) / 20, 1.0)
        + 0.3 * orphan_rt_rate
    )
    score = max(0.0, min(1.0, score))

    worst_blocks = sorted(all_matches, key=lambda m: m['match_cost'], reverse=True)[:20]

    report = {
        'pdf_path': str(pdf_path),
        'lang_a': lang_a,
        'lang_b': lang_b,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'summary': {
            'total_blocks': total_blocks,
            'matched_blocks': matched,
            'orphan_orig': total_orphan_orig,
            'orphan_rt': total_orphan_rt,
            'color_mismatch_count': color_mismatch,
            'color_mismatch_pct': color_mismatch_pct,
            'line_overflow_count': line_overflow,
            'line_overflow_pct': line_overflow_pct,
            'avg_line_delta': avg_line_delta,
            'avg_font_size_delta_pct': avg_fsd,
            'orphan_rt_rate': orphan_rt_rate,
            'avg_match_cost': avg_cost,
            'score': score,
        },
        'worst_blocks': worst_blocks,
        'orphan_analysis': orphan_analysis,
        'matches': all_matches,
    }

    report_path = work_dir / 'roundtrip_report.json'
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # Human-readable summary
    s = report['summary']
    print()
    print('=== Round-Trip Layout Evaluation ===')
    print(f'  PDF      : {pdf_path}')
    print(f'  Lang     : {lang_a} \u2192 {lang_b} \u2192 {lang_a}')
    print(f'  Timestamp: {report["timestamp"]}')
    print()
    print(f'  Blocks   : {s["total_blocks"]} original, {s["matched_blocks"]} matched')
    print(f'  Orphans  : {s["orphan_orig"]} orig-only, {s["orphan_rt"]} rt-only')
    print(f'  Color mismatch  : {s["color_mismatch_count"]} ({s["color_mismatch_pct"]:.1f}%)')
    print(f'  Line overflow   : {s["line_overflow_count"]} ({s["line_overflow_pct"]:.1f}%)')
    print(f'  Avg line delta  : {s["avg_line_delta"]:+.2f}')
    print(f'  Avg font \u0394      : {s["avg_font_size_delta_pct"]:+.1f}%')
    print(f'  Orphan RT rate  : {s["orphan_rt_rate"]:.1%}')
    oa = report.get('orphan_analysis', {})
    if oa:
        print(f'  Orphan breakdown:')
        for cat, info in oa.items():
            ex = info['examples'][:1]
            ex_str = f' e.g. {ex[0][:40]!r}' if ex else ''
            print(f'    {cat:12s}: {info["count"]:3d}{ex_str}')
    print(f'  Avg match cost  : {s["avg_match_cost"]:.4f}')
    print(f'  SCORE           : {s["score"]:.4f}')
    print(f'  Report saved to : {report_path}')

    return report


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Round-trip layout evaluation for PDF translation pipeline'
    )
    parser.add_argument('pdf_path', help='Input PDF path')
    parser.add_argument('--lang-b', default=None, help='Target language for round-trip')
    parser.add_argument('--lang-a', default=None, help='Source language (default: auto-detect)')
    parser.add_argument('--work-dir', default=None, help='Working directory for intermediates')
    parser.add_argument('--alpha', type=float, default=0.4,
                        help='Text similarity weight in cost (default: 0.4)')
    parser.add_argument('--beta', type=float, default=0.6,
                        help='Geo distance weight in cost (default: 0.6)')
    parser.add_argument('--force', action='store_true',
                        help='Re-run pipeline even if cached PDFs exist')
    parser.add_argument('--layout-only', action='store_true', default=False,
                        help='Re-run only layout_agent (B\u2192A) using cached translated.json')
    parser.add_argument('--identity', action='store_true',
                        help='Identity eval: no translation API, tests layout quality only.')
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path).resolve()
    if not pdf_path.exists():
        print(f'Error: PDF not found: {pdf_path}', file=sys.stderr)
        sys.exit(1)

    if not args.identity and not args.lang_b:
        parser.error('--lang-b is required unless --identity is set')

    lang_a = args.lang_a or _detect_lang_from_filename(pdf_path)
    lang_b = args.lang_b or ''

    if args.work_dir:
        work_dir = Path(args.work_dir).resolve()
    elif args.identity:
        work_dir = pdf_path.parent / 'work_rt_identity'
    else:
        work_dir = pdf_path.parent / f'work_rt_{lang_b}'

    run_eval(pdf_path, lang_a, lang_b, work_dir,
             alpha=args.alpha, beta=args.beta, force=args.force,
             layout_only=args.layout_only, identity=args.identity)


if __name__ == '__main__':
    main()
