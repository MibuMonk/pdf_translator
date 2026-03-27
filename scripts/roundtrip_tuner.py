#!/usr/bin/env python3
"""
Round-trip layout tuner — iterative improvement loop.

Runs roundtrip_eval repeatedly, prints a diagnosis prompt after each
failing iteration, and waits for the user to fix layout_agent.py.

Usage:
  python3 scripts/roundtrip_tuner.py <pdf_path> --lang-b <lang> [options]
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from roundtrip_eval import run_eval, _detect_lang_from_filename


# ---------------------------------------------------------------------------
# Diagnosis prompt
# ---------------------------------------------------------------------------

def _truncate(text: str, n: int = 40) -> str:
    return text if len(text) <= n else text[:n] + '...'


def print_diagnosis_prompt(report: dict, target_score: float):
    s = report['summary']
    score = s['score']
    matched = s['matched_blocks']
    matches = report['matches']

    print()
    print('--- DIAGNOSIS PROMPT ---')
    print(f'Round-trip layout score: {score:.2f} (target: {target_score})')
    print()
    print('TOP ISSUES (by frequency):')

    issue_num = 0

    # 1. line_overflow
    overflow_blocks = [m for m in matches if m['line_delta'] > 0]
    if overflow_blocks:
        issue_num += 1
        pct = len(overflow_blocks) / matched * 100 if matched else 0.0
        avg_delta = sum(m['line_delta'] for m in overflow_blocks) / len(overflow_blocks)
        worst = sorted(overflow_blocks, key=lambda m: abs(m['line_delta']), reverse=True)[:3]
        print(f'{issue_num}. line_overflow: {len(overflow_blocks)} blocks ({pct:.1f}%) '
              f'\u2014 line_delta > 0, avg delta = +{avg_delta:.1f} lines')
        cases = ', '.join(
            f'page {m["page"]} block "{_truncate(m["orig_text"])}" (delta={m["line_delta"]:+d})'
            for m in worst
        )
        print(f'   Worst cases: {cases}')
        print()

    # 2. color_mismatch
    color_blocks = [m for m in matches if not m['color_match']]
    if color_blocks:
        issue_num += 1
        pct = len(color_blocks) / matched * 100 if matched else 0.0
        worst = sorted(color_blocks, key=lambda m: m['match_cost'], reverse=True)[:3]
        print(f'{issue_num}. color_mismatch: {len(color_blocks)} blocks ({pct:.1f}%)')
        examples = ', '.join(
            f'page {m["page"]} block "{_truncate(m["orig_text"])}" '
            f'orig={m["color_orig"]} rt={m["color_rt"]}'
            for m in worst
        )
        print(f'   Examples: {examples}')
        print()

    # 3. font_size_shrink
    shrink_blocks = [m for m in matches if m['font_size_delta_pct'] < -10]
    if shrink_blocks:
        issue_num += 1
        worst = sorted(shrink_blocks, key=lambda m: abs(m['font_size_delta_pct']), reverse=True)[:3]
        print(f'{issue_num}. font_size_shrink: {len(shrink_blocks)} blocks with delta < -10%')
        examples = ', '.join(
            f'page {m["page"]} block "{_truncate(m["orig_text"])}" '
            f'orig={m["font_size_orig"]:.1f}pt rt={m["font_size_rt"]:.1f}pt'
            for m in worst
        )
        print(f'   Examples: {examples}')
        print()
    # 4. orphan_rt
    orphan_rt_rate = s.get('orphan_rt_rate', 0.0)
    if orphan_rt_rate > 0.2:
        issue_num += 1
        print(f'{issue_num}. orphan_rt_rate: {orphan_rt_rate:.1%} of rt blocks unmatched '
              f'\u2014 text expansion creates blocks with no original counterpart')
        print()


    print('LAYOUT AGENT FILE: agents/layout_agent.py')
    print('Analyze the above issues and propose specific code fixes.')
    print('--- END PROMPT ---')


# ---------------------------------------------------------------------------
# Tuning history
# ---------------------------------------------------------------------------

def _load_history(path: Path) -> list:
    if path.exists():
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_history(path: Path, history: list):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _print_history_table(history: list):
    print()
    print('=== Tuning History ===')
    print(f'  {"Iter":>4}  {"Score":>7}  Timestamp')
    for entry in history:
        print(f'  {entry["iter"]:>4}  {entry["score"]:>7.4f}  {entry["timestamp"]}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _auto_fix(report: dict, target_score: float, work_dir: Path):
    """Dispatch claude CLI to auto-fix layout_agent.py based on diagnosis."""
    import shutil as shutil_inner
    project_root = Path(__file__).resolve().parent.parent
    layout_agent_path = project_root / 'agents' / 'layout_agent.py'

    claude_bin = shutil_inner.which('claude') or 'claude'

    s = report['summary']
    score = s['score']
    orphan_rt_rate = s.get('orphan_rt_rate', 0.0)
    line_overflow_pct = s.get('line_overflow_pct', 0.0)
    color_mismatch_pct = s.get('color_mismatch_pct', 0.0)

    # Build worst-cases string from top matches by match_cost
    worst = report.get('worst_blocks', [])[:5]
    worst_str = '\n'.join(
        f"  page {m['page']}: orig={m['orig_text'][:40]!r} rt={m['rt_text'][:40]!r} "
        f"line_delta={m['line_delta']:+d} color_match={m['color_match']}"
        for m in worst
    )

    prompt = f"""You are a layout fixer for the PDF translation pipeline at {project_root}.

TASK: Fix agents/layout_agent.py to improve the round-trip layout score.

Current score: {score:.4f} (target: {target_score})

TOP ISSUES:
- line_overflow: {line_overflow_pct:.1f}% of matched blocks have more lines than original
- color_mismatch: {color_mismatch_pct:.1f}% of matched blocks have wrong text color
- orphan_rt_rate: {orphan_rt_rate:.1%} of rt blocks are unmatched (text expansion creates extra blocks)

WORST BLOCKS (highest match cost):
{worst_str}

INSTRUCTIONS:
1. Read agents/layout_agent.py carefully
2. Identify the root cause of the top issues listed above
3. Make minimal, targeted fixes \u2014 do not refactor unrelated code
4. Focus on: font shrinking to fit text, color preservation, line wrapping logic
5. Do NOT change the I/O interface or agent contract

Working directory: {project_root}
"""

    print(f'\n[auto] Calling claude to fix layout_agent.py ...')
    result = subprocess.run(
        [claude_bin, '--dangerously-skip-permissions', '-p', prompt],
        cwd=str(project_root),
        timeout=600,
    )
    if result.returncode != 0:
        print(f'[auto] claude returned exit code {result.returncode}, continuing...')


def main():
    parser = argparse.ArgumentParser(
        description='Iterative round-trip layout tuner'
    )
    parser.add_argument('pdf_path', help='Input PDF path')
    parser.add_argument('--lang-b', required=True, help='Target language for round-trip')
    parser.add_argument('--lang-a', default=None, help='Source language (default: auto-detect)')
    parser.add_argument('--work-dir', default=None, help='Working directory for intermediates')
    parser.add_argument('--max-iters', type=int, default=10, help='Max iterations (default: 10)')
    parser.add_argument('--target-score', type=float, default=0.90,
                        help='Target score to stop at (default: 0.90)')
    parser.add_argument('--auto', action='store_true', default=False,
                        help='Automatically call claude CLI to fix layout_agent.py each iteration')
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

    work_dir.mkdir(parents=True, exist_ok=True)
    history_path = work_dir / 'tuning_history.json'
    history = _load_history(history_path)

    for iter_num in range(args.max_iters):
        print(f'\n=== Iteration {iter_num + 1} ===')
        # First iteration always does full eval; subsequent use layout_only
        use_layout_only = (iter_num > 0)
        report = run_eval(pdf_path, lang_a, lang_b, work_dir,
                          alpha=0.4, beta=0.6,
                          force=(not use_layout_only),
                          layout_only=use_layout_only)
        score = report['summary']['score']
        print(f'Score: {score:.4f}')

        history.append({
            'iter': iter_num + 1,
            'score': score,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })
        _save_history(history_path, history)

        if score >= args.target_score:
            print(f'Target score {args.target_score} reached. Done.')
            break

        print_diagnosis_prompt(report, args.target_score)

        if args.auto:
            _auto_fix(report, args.target_score, work_dir)
        else:
            try:
                resp = input('\nContinue to next iteration after fixing? [y/n]: ')
            except EOFError:
                break
            if resp.lower() != 'y':
                break

    _print_history_table(history)


if __name__ == '__main__':
    main()
