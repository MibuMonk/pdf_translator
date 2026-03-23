#!/usr/bin/env python3
"""
QA Agent for pdf_translator
Inspects translated.json and output PDF for translation gaps and layout issues.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# ANSI color helpers
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_USE_COLOR = _supports_color()

GREEN  = "\033[92m" if _USE_COLOR else ""
YELLOW = "\033[93m" if _USE_COLOR else ""
RED    = "\033[91m" if _USE_COLOR else ""
BOLD   = "\033[1m"  if _USE_COLOR else ""
RESET  = "\033[0m"  if _USE_COLOR else ""


def green(s: str) -> str:
    return f"{GREEN}{s}{RESET}"


def yellow(s: str) -> str:
    return f"{YELLOW}{s}{RESET}"


def red(s: str) -> str:
    return f"{RED}{s}{RESET}"


def bold(s: str) -> str:
    return f"{BOLD}{s}{RESET}"


# ---------------------------------------------------------------------------
# Text weight helpers
# ---------------------------------------------------------------------------

_TRIVIAL_RE = re.compile(r'^[\d\s.,;:!?()[\]/%+\-=\\\'\"]*$')
# Acronym-definition lines: e.g. "DDOD: Data-Driven Object Detection"
# These are legitimately untranslated (all-caps abbreviations + ASCII expansion)
# Matches: "ABBR: description"  OR  "ABBR\n(description)"
_ACRONYM_DEF_RE = re.compile(r'^[A-Z]{2,}[0-9A-Z]*[\s\n]*[\(:]')


def _is_trivially_invariant(text: str) -> bool:
    """Return True if text is composed only of numbers/punctuation/symbols."""
    return bool(_TRIVIAL_RE.match(text))


def _is_acronym_definition(text: str) -> bool:
    """True if text looks like an acronym definition line (DDOD: Data-Driven …)."""
    return bool(_ACRONYM_DEF_RE.match(text.strip()))


def _weighted_len(text: str) -> float:
    """
    Information-density-weighted character count.
    CJK characters (kanji, hiragana, katakana, hangul) = 2.0 units each.
    Everything else = 1.0 unit.
    Rationale: CJK carries ~2x the semantic content per character vs ASCII,
    so length comparisons across language pairs must be normalised.
    """
    total = 0.0
    for ch in text:
        cp = ord(ch)
        if (
            0x3000 <= cp <= 0x9FFF    # CJK unified + hiragana/katakana
            or 0xAC00 <= cp <= 0xD7AF  # Hangul syllables
            or 0xF900 <= cp <= 0xFAFF  # CJK compatibility
            or 0x20000 <= cp <= 0x2FA1F  # CJK extensions B-F
        ):
            total += 2.0
        else:
            total += 1.0
    return total


# ---------------------------------------------------------------------------
# Issue detection
# ---------------------------------------------------------------------------

def _check_block(page_num: int, block_id: str, block: dict) -> list[dict]:
    """Return a list of issue dicts for a single block."""
    issues = []
    text       = (block.get("text") or "").strip()
    translated = (block.get("translated") or "").strip()

    if not text:
        # Nothing to translate — skip
        return issues

    # 1. missing_translation
    if not translated:
        issues.append({
            "page":       page_num,
            "block_id":   block_id,
            "type":       "missing_translation",
            "severity":   "critical",
            "text":       text,
            "translated": translated,
        })
        return issues  # further checks are meaningless without a translation

    # 2. unchanged_translation
    # Skip acronym-definition lines (e.g. "DDOD: Data-Driven …") — intentionally untranslated.
    if (
        translated == text
        and not _is_trivially_invariant(text)
        and not _is_acronym_definition(text)
        and len(text) > 5
    ):
        issues.append({
            "page":       page_num,
            "block_id":   block_id,
            "type":       "unchanged_translation",
            "severity":   "warning",
            "text":       text,
            "translated": translated,
        })

    # 3. likely_truncated
    # Only flag if the source doesn't already end with "…" (would be correct preservation)
    if translated.endswith("…") and not text.endswith("…"):
        issues.append({
            "page":       page_num,
            "block_id":   block_id,
            "type":       "likely_truncated",
            "severity":   "warning",
            "text":       text,
            "translated": translated,
        })

    # 4. suspiciously_short
    # Use information-density-weighted lengths so CJK→CJK and ASCII→CJK
    # translations are not falsely flagged (CJK chars carry ~2x ASCII information).
    # Threshold: translated weighted-length < 25% of source weighted-length,
    # only checked when source is substantive (weighted > 40 units ≈ 20+ CJK chars).
    wt_src = _weighted_len(text)
    wt_trl = _weighted_len(translated)
    if wt_src > 40 and wt_trl < wt_src * 0.25:
        issues.append({
            "page":       page_num,
            "block_id":   block_id,
            "type":       "suspiciously_short",
            "severity":   "warning",
            "text":       text,
            "translated": translated,
        })

    return issues


# ---------------------------------------------------------------------------
# Core QA logic
# ---------------------------------------------------------------------------

def run_qa(json_path: str) -> tuple[dict, list[dict]]:
    """
    Load translated.json and run all checks.

    Returns (summary_dict, issues_list).
    """
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    # Support both a list-of-pages and a dict with a "pages" key
    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
    else:
        raise ValueError(f"Unexpected JSON structure in {json_path}")

    total_blocks      = 0
    translated_blocks = 0
    all_issues: list[dict] = []

    per_page_stats: list[dict] = []

    for page_entry in pages:
        # page_entry may be {"page": 1, "blocks": [...]} or similar
        if isinstance(page_entry, dict):
            page_num = page_entry.get("page", page_entry.get("page_num", 0))
            blocks   = page_entry.get("blocks", [])
        else:
            continue

        page_total      = 0
        page_translated = 0
        page_issues: list[dict] = []

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue

            text = (block.get("text") or "").strip()
            if not text:
                continue  # blank / non-text block

            block_id = block.get(
                "block_id",
                block.get("id", f"p{page_num:02d}_b{idx:03d}"),
            )

            page_total += 1
            translated = (block.get("translated") or "").strip()
            if translated:
                page_translated += 1

            issues = _check_block(page_num, block_id, block)
            page_issues.extend(issues)

        total_blocks      += page_total
        translated_blocks += page_translated
        all_issues.extend(page_issues)

        per_page_stats.append({
            "page":       page_num,
            "total":      page_total,
            "translated": page_translated,
            "issues":     len(page_issues),
        })

    coverage_pct = (
        round(translated_blocks / total_blocks * 100, 1)
        if total_blocks > 0
        else 0.0
    )

    summary = {
        "total_blocks":      total_blocks,
        "translated_blocks": translated_blocks,
        "coverage_pct":      coverage_pct,
        "issue_count":       len(all_issues),
        "pass":              coverage_pct >= 95,
        "per_page":          per_page_stats,
    }

    return summary, all_issues


# ---------------------------------------------------------------------------
# Thumbnail rendering
# ---------------------------------------------------------------------------

def render_thumbnails(pdf_path: str, thumbs_dir: str, dpi: int = 80) -> None:
    """Render each page of pdf_path as a PNG thumbnail into thumbs_dir."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print(yellow("  [warn] PyMuPDF not installed — skipping thumbnail rendering"))
        return

    Path(thumbs_dir).mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    zoom  = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    for page_num in range(len(doc)):
        page = doc[page_num]
        pix  = page.get_pixmap(matrix=matrix)
        out_path = os.path.join(thumbs_dir, f"page_{page_num + 1:04d}.png")
        pix.save(out_path)

    doc.close()
    print(green(f"  Thumbnails written to: {thumbs_dir}  ({len(doc)} pages)"))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="QA agent: inspect translated.json for translation gaps."
    )
    parser.add_argument("--json",   required=True,                  help="Path to translated.json")
    parser.add_argument("--pdf",    default=None,                   help="Path to output PDF (for thumbnail rendering)")
    parser.add_argument("--output", default="qa_report.json",       help="QA report output path (default: qa_report.json)")
    parser.add_argument("--thumbs", default=None,                   help="Directory for page thumbnails (skipped if omitted)")
    args = parser.parse_args()

    # ------------------------------------------------------------------
    print(bold("\n=== PDF Translator QA Agent ===\n"))

    # 1. Run JSON checks
    print(f"Loading: {args.json}")
    try:
        summary, issues = run_qa(args.json)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        print(red(f"[ERROR] {exc}"))
        sys.exit(1)

    # 2. Per-page statistics
    print(bold("\nPer-page statistics:"))
    print(f"  {'Page':>5}  {'Blocks':>7}  {'Translated':>10}  {'Issues':>6}")
    print(f"  {'-'*5}  {'-'*7}  {'-'*10}  {'-'*6}")
    for ps in summary.get("per_page", []):
        cov = ps["translated"] / ps["total"] * 100 if ps["total"] else 0
        cov_str = f"{cov:.0f}%"
        row = f"  {ps['page']:>5}  {ps['total']:>7}  {ps['translated']:>8} {cov_str:>3}  {ps['issues']:>6}"
        if ps["issues"] > 0:
            print(yellow(row))
        elif cov >= 95:
            print(green(row))
        else:
            print(red(row))

    # 3. Summary
    cov = summary["coverage_pct"]
    total   = summary["total_blocks"]
    xlated  = summary["translated_blocks"]
    n_issues = summary["issue_count"]

    print(bold("\nSummary:"))
    cov_line = f"  Coverage : {xlated}/{total} blocks  ({cov}%)"
    if cov >= 95:
        print(green(cov_line))
    else:
        print(red(cov_line))

    issue_line = f"  Issues   : {n_issues}"
    print(yellow(issue_line) if n_issues > 0 else green(issue_line))

    # 4. Issue list
    if issues:
        print(bold("\nIssues:"))
        for iss in issues:
            tag = {
                "missing_translation":    red("[MISSING]"),
                "unchanged_translation":  yellow("[UNCHANGED]"),
                "likely_truncated":       yellow("[TRUNCATED]"),
                "suspiciously_short":     yellow("[SHORT]"),
            }.get(iss["type"], yellow(f"[{iss['type'].upper()}]"))

            text_preview = (iss["text"] or "")[:60]
            tr_preview   = (iss["translated"] or "")[:60]
            print(f"  {tag}  page={iss['page']}  id={iss['block_id']}")
            print(f"           text: {text_preview!r}")
            if iss["translated"]:
                print(f"           tran: {tr_preview!r}")

    # 5. Thumbnails
    if args.thumbs:
        if args.pdf:
            print(bold("\nRendering thumbnails…"))
            render_thumbnails(args.pdf, args.thumbs)
        else:
            print(yellow("  [warn] --thumbs requires --pdf — skipping thumbnail rendering"))

    # 6. Write report
    # Remove per_page from top-level summary in the report (keep it clean)
    report_summary = {k: v for k, v in summary.items() if k != "per_page"}

    # Build self_eval
    retry_candidates = [iss["block_id"] for iss in issues if iss.get("severity") == "critical"]
    confidence = 1.0 if cov >= 95 else round(cov / 100, 4)
    self_eval = {
        "retry_candidates": retry_candidates,
        "confidence":       confidence,
    }

    report = {
        "version":  "1.0",
        "summary":  report_summary,
        "issues":   issues,
        "self_eval": self_eval,
    }
    out_path = args.output
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(bold(f"\nReport written to: {out_path}"))

    # 6b. Validate output against schema
    try:
        _contracts_dir = Path(__file__).parent.parent / "contracts"
        sys.path.insert(0, str(_contracts_dir.parent))
        from contracts.validate import validate_output
        violations = validate_output(report, "qa_report")
        if violations:
            print(yellow(f"  [warn] Schema validation failed ({len(violations)} violation(s)):"))
            for v in violations:
                print(yellow(f"    {v}"))
        else:
            print(green("  [ok] Report passed schema validation"))
    except Exception as exc:
        print(yellow(f"  [warn] Could not run schema validation: {exc}"))

    # 7. Exit code
    if cov >= 95:
        print(green(f"\n[PASS] Coverage {cov}% >= 95%\n"))
        sys.exit(0)
    else:
        print(red(f"\n[FAIL] Coverage {cov}% < 95%\n"))
        sys.exit(1)


if __name__ == "__main__":
    main()
