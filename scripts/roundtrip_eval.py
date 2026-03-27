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

def extract_blocks(pdf_path):
    doc = fitz.open(str(pdf_path))
    all_blocks = []
    for page_num, page in enumerate(doc):
        page_w = page.rect.width
        page_h = page.rect.height
        raw = page.get_text('dict', flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for block in raw['blocks']:
            if block['type'] != 0:
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
            })
            assigned_orig.add(r)
            assigned_rt.add(c)

    orphan_orig = n - len(assigned_orig)
    orphan_rt = m - len(assigned_rt)
    return matches, orphan_orig, orphan_rt


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

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
# Main evaluation function (importable)
# ---------------------------------------------------------------------------

def run_eval(pdf_path, lang_a, lang_b, work_dir, alpha=0.4, beta=0.6, force=False):
    """
    Run full round-trip evaluation and return the report dict.
    Also writes <work_dir>/roundtrip_report.json.
    """
    pdf_path = Path(pdf_path).resolve()
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    rt_b_pdf = work_dir / 'rt_B.pdf'
    rt_a_pdf = work_dir / 'rt_A.pdf'

    # Step 1 \u2014 A\u2192B
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
    if force or not rt_a_pdf.exists():
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
    rt_blocks = extract_blocks(rt_a_pdf)

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
    parser.add_argument('--lang-b', required=True, help='Target language for round-trip')
    parser.add_argument('--lang-a', default=None, help='Source language (default: auto-detect)')
    parser.add_argument('--work-dir', default=None, help='Working directory for intermediates')
    parser.add_argument('--alpha', type=float, default=0.4,
                        help='Text similarity weight in cost (default: 0.4)')
    parser.add_argument('--beta', type=float, default=0.6,
                        help='Geo distance weight in cost (default: 0.6)')
    parser.add_argument('--force', action='store_true',
                        help='Re-run pipeline even if cached PDFs exist')
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path).resolve()
    if not pdf_path.exists():
        print(f'Error: PDF not found: {pdf_path}', file=sys.stderr)
        sys.exit(1)

    lang_a = args.lang_a or _detect_lang_from_filename(pdf_path)
    lang_b = args.lang_b

    if args.work_dir:
        work_dir = Path(args.work_dir).resolve()
    else:
        work_dir = pdf_path.parent / f'work_rt_{lang_b}'

    run_eval(pdf_path, lang_a, lang_b, work_dir,
             alpha=args.alpha, beta=args.beta, force=args.force)


if __name__ == '__main__':
    main()
