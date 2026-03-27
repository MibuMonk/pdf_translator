#!/usr/bin/env python3
"""
test_agent.py — Structural regression checker + translation QA for pdf_translator output.

Usage:
    python test_agent.py --testcase 成果物4
    python test_agent.py --testcase 成果物4 --registry issues/registry.json
    python test_agent.py --testcase 成果物4 --output testdata/成果物4/test_report.json

    # Pipeline mode (QA checks against translated.json + output.pdf directly)
    python test_agent.py --json translated.json --pdf output.pdf --output test_report.json
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF not installed. Run: pip install pymupdf", file=sys.stderr)
    sys.exit(1)

# Project root is parent of this file's directory (agents/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).parent))

from qa_utils import (  # noqa: E402
    green, yellow, red, bold,
    load_json,
    bbox_center_y, bbox_center_x, bboxes_overlap_x,
    extract_pdf_spans_by_page, extract_pdf_text_block_bboxes_by_page,
    find_best_span_match, _collect_spans_in_bbox,
    _render_page_to_png, render_thumbnails,
    _text_similarity, _weighted_len,
)
from qa_translation import (  # noqa: E402
    coverage_check, quality_check,
    translation_completeness_check, linebreak_consistency_check,
    mixed_language_check, terminology_consistency_check, fragmentation_check,
)
from qa_readability import readability_check, glyph_dropout_check  # noqa: E402
from qa_llm import style_check, visual_review_check  # noqa: E402
from qa_regression import save_baseline, run_regression  # noqa: E402


# ---------------------------------------------------------------------------
# Issue detection methods (structural regression checks)
# ---------------------------------------------------------------------------

def check_font_size_ratio(issue: dict, translated: dict, pdf_spans: dict) -> dict:
    """
    ISS-001: Compare expected font_size (translated.json) vs actual rendered size (PDF spans).
    Flag blocks where actual_size < expected_size * 0.75.
    Only checks title-like blocks: stream_rank == 0 or bbox.y < 100.
    """
    threshold = 0.75
    fails = []

    for page in translated.get("pages", []):
        page_num = page["page_num"]
        spans = pdf_spans.get(page_num, [])

        for block in page.get("blocks", []):
            bbox = block.get("bbox", [0, 0, 0, 0])
            stream_rank = block.get("stream_rank")
            is_title_like = (stream_rank == 0) or (bbox[1] < 100)
            if not is_title_like:
                continue

            expected_size = block.get("font_size")
            if not expected_size:
                continue

            matched_span = find_best_span_match(bbox, spans, tolerance=5.0)
            if matched_span is None:
                fails.append({
                    "page": page_num,
                    "block_id": block["id"],
                    "expected_font_size": expected_size,
                    "actual_font_size": None,
                    "match": "not_found",
                })
                continue

            actual_size = matched_span["size"]
            ratio = actual_size / expected_size if expected_size else 1.0
            if ratio < threshold:
                fails.append({
                    "page": page_num,
                    "block_id": block["id"],
                    "expected_font_size": round(expected_size, 2),
                    "actual_font_size": round(actual_size, 2),
                    "ratio": round(ratio, 3),
                })

    if fails:
        return {"check_result": "fail", "details": fails}
    return {"check_result": "pass", "details": []}


def check_sibling_font_size(issue: dict, translated: dict, pdf_spans: dict) -> dict:
    """
    ISS-004: Find horizontally adjacent spans on the same page with similar y-center
    (within 30pt) but font size ratio > 1.3.
    """
    y_tolerance = 30.0
    size_ratio_threshold = 1.3
    fails = []

    for page_num, spans in pdf_spans.items():
        sorted_spans = sorted(spans, key=lambda s: bbox_center_y(s["bbox"]))

        groups = []
        used = [False] * len(sorted_spans)
        for i, s in enumerate(sorted_spans):
            if used[i]:
                continue
            group = [s]
            used[i] = True
            cy_i = bbox_center_y(s["bbox"])
            for j in range(i + 1, len(sorted_spans)):
                if used[j]:
                    continue
                cy_j = bbox_center_y(sorted_spans[j]["bbox"])
                if abs(cy_j - cy_i) <= y_tolerance:
                    group.append(sorted_spans[j])
                    used[j] = True
                else:
                    break
            if len(group) >= 2:
                groups.append(group)

        for group in groups:
            sizes = [s["size"] for s in group if s["size"] > 0]
            if len(sizes) < 2:
                continue
            max_sz = max(sizes)
            min_sz = min(sizes)
            if min_sz > 0 and max_sz / min_sz > size_ratio_threshold:
                fails.append({
                    "page": page_num,
                    "y_center_range": [
                        round(min(bbox_center_y(s["bbox"]) for s in group), 1),
                        round(max(bbox_center_y(s["bbox"]) for s in group), 1),
                    ],
                    "font_sizes": sorted(set(round(s, 2) for s in sizes)),
                    "max_min_ratio": round(max_sz / min_sz, 3),
                })

    if fails:
        return {"check_result": "fail", "details": fails}
    return {"check_result": "pass", "details": []}


def check_manual(issue: dict, **kwargs) -> dict:
    return {
        "check_result": "skipped",
        "reason": "manual check required",
    }


def check_fixed(issue: dict, **kwargs) -> dict:
    return {
        "check_result": "skipped",
        "reason": "marked as fixed, no regression check implemented",
    }


# ---------------------------------------------------------------------------
# Per-page confidence scoring
# ---------------------------------------------------------------------------

# Severity deductions for page confidence scoring
_CONFIDENCE_DEDUCTIONS = {
    "error":    0.3,
    "critical": 0.3,  # coverage_check uses "critical" instead of "error"
    "warning":  0.1,
}

# Confidence tier thresholds
_CONFIDENCE_HIGH   = 0.8   # >= 0.8: auto-pass, no review needed
_CONFIDENCE_MEDIUM = 0.5   # 0.5-0.8: summary review


def _extract_page_findings(issue_results: dict) -> list[dict]:
    """
    Walk all check results and extract findings with page + severity info.
    Returns a flat list of {"page": int, "severity": str} dicts.
    """
    findings = []

    for check_name, check_data in issue_results.items():
        if not isinstance(check_data, dict):
            continue
        details = check_data.get("details")
        if details is None:
            continue

        # Each check stores findings in different structures.
        # We extract (page, severity) from each.

        if check_name == "coverage_check" and isinstance(details, dict):
            for iss in details.get("translation_issues", []):
                if "page" in iss and "severity" in iss:
                    findings.append({"page": iss["page"], "severity": iss["severity"]})

        elif check_name == "quality_check" and isinstance(details, list):
            for iss in details:
                if "page" in iss and "severity" in iss:
                    findings.append({"page": iss["page"], "severity": iss["severity"]})

        elif check_name in ("linebreak_consistency_check", "mixed_language_check", "fragmentation_check", "glyph_dropout_check"):
            if isinstance(details, dict):
                for iss in details.get("issues", []):
                    if "page" in iss and "severity" in iss:
                        findings.append({"page": iss["page"], "severity": iss["severity"]})

        elif check_name == "translation_completeness_check" and isinstance(details, dict):
            for iss in details.get("issues", []):
                if "page" in iss and "severity" in iss:
                    findings.append({"page": iss["page"], "severity": iss["severity"]})

        elif check_name == "readability_check" and isinstance(details, dict):
            for iss in details.get("issues", []):
                sev = iss.get("severity")
                if not sev:
                    continue
                if "page" in iss:
                    findings.append({"page": iss["page"], "severity": sev})
                # inconsistent_sizing uses page_a / page_b
                if "page_a" in iss:
                    findings.append({"page": iss["page_a"], "severity": sev})
                if "page_b" in iss:
                    findings.append({"page": iss["page_b"], "severity": sev})

        elif check_name == "style_check" and isinstance(details, dict):
            for iss in details.get("style_issues", []):
                if "page" in iss and "severity" in iss:
                    findings.append({"page": iss["page"], "severity": iss["severity"]})

        elif check_name == "terminology_consistency_check" and isinstance(details, dict):
            # variant_pair_issues: sample_locations_a/b contain page refs
            for iss in details.get("variant_pair_issues", []):
                sev = iss.get("severity", "warning")
                for loc in iss.get("sample_locations_a", []):
                    if "page" in loc:
                        findings.append({"page": loc["page"], "severity": sev})
                for loc in iss.get("sample_locations_b", []):
                    if "page" in loc:
                        findings.append({"page": loc["page"], "severity": sev})
            # dynamic_issues: translations[].sample_locations contain page refs
            for iss in details.get("dynamic_issues", []):
                sev = iss.get("severity", "warning")
                for trans in iss.get("translations", []):
                    for loc in trans.get("sample_locations", []):
                        if "page" in loc:
                            findings.append({"page": loc["page"], "severity": sev})

        elif check_name == "regression_check" and isinstance(details, dict):
            for finding in details.get("findings", []):
                sev = finding.get("severity")
                if not sev:
                    continue
                # regression findings may have "page" or "pages"
                if "page" in finding:
                    findings.append({"page": finding["page"], "severity": sev})
                for pg in finding.get("pages", []):
                    findings.append({"page": pg, "severity": sev})

        elif check_name == "visual_review_check" and isinstance(details, dict):
            for iss in details.get("issues", []):
                if "page" in iss and "severity" in iss:
                    findings.append({"page": iss["page"], "severity": iss["severity"]})

    return findings


def _compute_page_confidence(issue_results: dict, num_pages: int) -> dict:
    """
    Compute per-page confidence scores based on check findings.

    Args:
        issue_results: the issue_results dict from the test report
        num_pages: total number of pages in the document

    Returns:
        {
            "pages": {"1": 0.95, "2": 0.6, ...},
            "summary": {"high": N, "medium": N, "low": N},
            "review_needed": [list of LOW confidence page numbers]
        }
    """
    # Initialize all pages at 1.0
    scores = {pg: 1.0 for pg in range(1, num_pages + 1)}

    # Deduct based on findings
    findings = _extract_page_findings(issue_results)
    for f in findings:
        pg = f["page"]
        if pg not in scores:
            continue
        deduction = _CONFIDENCE_DEDUCTIONS.get(f["severity"], 0.0)
        scores[pg] -= deduction

    # Clamp to [0.0, 1.0]
    for pg in scores:
        scores[pg] = round(max(0.0, min(1.0, scores[pg])), 2)

    # Classify tiers
    high = medium = low = 0
    review_needed = []
    for pg in sorted(scores):
        s = scores[pg]
        if s >= _CONFIDENCE_HIGH:
            high += 1
        elif s >= _CONFIDENCE_MEDIUM:
            medium += 1
        else:
            low += 1
            review_needed.append(pg)

    return {
        "pages": {str(pg): scores[pg] for pg in sorted(scores)},
        "summary": {"high": high, "medium": medium, "low": low},
        "review_needed": review_needed,
    }


def _get_num_pages(translated_json_path: str) -> int:
    """Get total page count from translated.json."""
    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
    else:
        return 0
    page_nums = set()
    for p in pages:
        if isinstance(p, dict):
            pn = p.get("page", p.get("page_num", 0))
            if pn > 0:
                page_nums.add(pn)
    return max(page_nums) if page_nums else 0


# ---------------------------------------------------------------------------
# Main runner (testcase mode)
# ---------------------------------------------------------------------------

def run_checks(testcase: str, registry_path: Path, output_path: Path,
               translated_json_path: Path = None, output_pdf_path: Path = None):
    registry = load_json(registry_path)

    if translated_json_path is None:
        testcase_dir = PROJECT_ROOT / "testdata" / testcase
        output_pdf_path = testcase_dir / "output.pdf"
        if not output_pdf_path.exists():
            candidates = sorted(testcase_dir.glob("*.pdf"))
            candidates = [p for p in candidates if p.name not in ("source.pdf", "review_bundle.pdf")]
            if candidates:
                output_pdf_path = candidates[0]
        translated_json_path = testcase_dir / "work" / "translated.json"
        if not translated_json_path.exists():
            # fallback: pipeline uses {stem}.translated.json naming
            candidates = sorted((testcase_dir / "work").glob("*.translated.json"))
            if candidates:
                translated_json_path = candidates[0]

    if not output_pdf_path.exists():
        print(f"ERROR: output.pdf not found at {output_pdf_path}", file=sys.stderr)
        sys.exit(1)

    if not translated_json_path.exists():
        print(f"ERROR: translated.json not found at {translated_json_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading translated.json from {translated_json_path}")
    translated = load_json(translated_json_path)

    print(f"Extracting spans from {output_pdf_path}")
    pdf_spans = extract_pdf_spans_by_page(output_pdf_path)
    print(f"  -> Extracted spans for {len(pdf_spans)} pages")

    results = []
    summary = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}

    for issue in registry["issues"]:
        iid = issue["id"]
        method = issue["detection"]["method"]
        reg_status = issue["status"]

        summary["total"] += 1

        if reg_status == "fixed":
            result = check_fixed(issue)
        elif method == "manual":
            result = check_manual(issue)
        elif method == "font_size_ratio":
            print(f"Running {iid} ({method})...")
            result = check_font_size_ratio(issue, translated, pdf_spans)
        elif method == "sibling_font_size":
            print(f"Running {iid} ({method})...")
            result = check_sibling_font_size(issue, translated, pdf_spans)
        else:
            result = {"check_result": "skipped", "reason": f"unknown method: {method}"}

        check_result = result["check_result"]
        if check_result == "pass":
            summary["passed"] += 1
        elif check_result == "fail":
            summary["failed"] += 1
        else:
            summary["skipped"] += 1

        entry = {
            "issue_id": iid,
            "title": issue["title"],
            "severity": issue["severity"],
            "registry_status": reg_status,
            **result,
        }
        results.append(entry)
        print(f"  {iid}: {check_result.upper()}")

    # Run translation QA checks
    print("Running coverage_check...")
    cov_result = coverage_check(str(translated_json_path))
    summary["total"] += 1
    if cov_result["check_result"] == "pass":
        summary["passed"] += 1
    else:
        summary["failed"] += 1
    print(f"  coverage_check: {cov_result['check_result'].upper()}")

    print("Running quality_check...")
    qual_result = quality_check(str(translated_json_path))
    summary["total"] += 1
    if qual_result["check_result"] == "pass":
        summary["passed"] += 1
    else:
        summary["failed"] += 1
    print(f"  quality_check: {qual_result['check_result'].upper()}")

    print("Running style_check...")
    style_result = style_check(str(translated_json_path))
    summary["total"] += 1
    if style_result["check_result"] == "pass":
        summary["passed"] += 1
    elif style_result["check_result"] == "fail":
        summary["failed"] += 1
    else:
        summary["skipped"] += 1
    print(f"  style_check: {style_result['check_result'].upper()}")

    print("Running translation_completeness_check...")
    tc_result = translation_completeness_check(str(translated_json_path))
    summary["total"] += 1
    if tc_result["check_result"] == "pass":
        summary["passed"] += 1
    else:
        summary["failed"] += 1
    print(f"  translation_completeness_check: {tc_result['check_result'].upper()}")

    print("Running readability_check...")
    _src_pdf = output_pdf_path.parent / "source.pdf"
    rd_result = readability_check(
        str(translated_json_path), str(output_pdf_path),
        source_pdf_path=str(_src_pdf) if _src_pdf.exists() else None,
    )
    summary["total"] += 1
    if rd_result["check_result"] == "pass":
        summary["passed"] += 1
    else:
        summary["failed"] += 1
    print(f"  readability_check: {rd_result['check_result'].upper()}")

    print("Running glyph_dropout_check...")
    gd_result = glyph_dropout_check(str(translated_json_path), str(output_pdf_path))
    summary["total"] += 1
    if gd_result["check_result"] == "pass":
        summary["passed"] += 1
    else:
        summary["failed"] += 1
    print(f"  glyph_dropout_check: {gd_result['check_result'].upper()}")

    # Run regression checks if baseline exists
    regression_result = run_regression(testcase)
    if regression_result is not None:
        summary["total"] += 1
        if regression_result["check_result"] == "pass":
            summary["passed"] += 1
        else:
            summary["failed"] += 1
        print(f"  regression_check: {regression_result['check_result'].upper()}")

    issue_results = {
        "coverage_check": cov_result,
        "quality_check":  qual_result,
        "style_check":    style_result,
        "translation_completeness_check": tc_result,
        "readability_check": rd_result,
        "glyph_dropout_check": gd_result,
    }
    if regression_result is not None:
        issue_results["regression_check"] = regression_result

    report = {
        "version": "1.0",
        "testcase": testcase,
        "run_date": str(date.today()),
        "results": results,
        "summary": summary,
        "issue_results": issue_results,
    }

    # Per-page confidence scoring
    num_pages = _get_num_pages(str(translated_json_path))
    if num_pages > 0:
        page_conf = _compute_page_confidence(issue_results, num_pages)
        report["page_confidence"] = page_conf
        n_review = len(page_conf["review_needed"])
        conf_summ = page_conf["summary"]
        print(bold("\nPage confidence:"))
        print(f"  HIGH ({_CONFIDENCE_HIGH}+): {conf_summ['high']}  "
              f"MEDIUM ({_CONFIDENCE_MEDIUM}-{_CONFIDENCE_HIGH}): {conf_summ['medium']}  "
              f"LOW (<{_CONFIDENCE_MEDIUM}): {conf_summ['low']}")
        if n_review > 0:
            print(yellow(f"  Review needed: pages {page_conf['review_needed']}"))
        else:
            print(green("  No pages require manual review."))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\nReport written to {output_path}")
    print(f"Summary: total={summary['total']}, passed={summary['passed']}, "
          f"failed={summary['failed']}, skipped={summary['skipped']}")
    return report


# ---------------------------------------------------------------------------
# Pipeline mode (QA checks only, no testcase / registry required)
# ---------------------------------------------------------------------------

def run_pipeline_qa(translated_json: str, pdf_path: str, output: str,
                     thumbs: str = None, source_pdf: str = None,
                     no_visual: bool = False):
    """
    Minimal QA mode for pipeline integration.
    Runs coverage_check and quality_check; writes test_report.json.
    """
    print(bold("\n=== PDF Translator Test Agent (Pipeline QA) ===\n"))

    print(f"Loading: {translated_json}")
    cov_result  = coverage_check(translated_json)
    qual_result = quality_check(translated_json)

    print("Running style_check...")
    style_result = style_check(translated_json)
    print(f"  style_check: {style_result['check_result'].upper()}")

    print("Running translation_completeness_check...")
    tc_result = translation_completeness_check(translated_json)
    print(f"  translation_completeness_check: {tc_result['check_result'].upper()}")

    print("Running linebreak_consistency_check...")
    lb_result = linebreak_consistency_check(translated_json)
    print(f"  linebreak_consistency_check: {lb_result['check_result'].upper()}")

    print("Running mixed_language_check...")
    ml_result = mixed_language_check(translated_json)
    print(f"  mixed_language_check: {ml_result['check_result'].upper()}")

    print("Running terminology_consistency_check...")
    term_result = terminology_consistency_check(translated_json)
    print(f"  terminology_consistency_check: {term_result['check_result'].upper()}")

    print("Running fragmentation_check...")
    frag_result = fragmentation_check(translated_json)
    print(f"  fragmentation_check: {frag_result['check_result'].upper()}")

    rd_result = {"check_result": "skipped", "reason": "no PDF provided"}
    gd_result = {"check_result": "skipped", "reason": "no PDF provided"}
    if pdf_path:
        print("Running readability_check...")
        rd_result = readability_check(translated_json, pdf_path,
                                      source_pdf_path=source_pdf if source_pdf else None)
        print(f"  readability_check: {rd_result['check_result'].upper()}")

        print("Running glyph_dropout_check...")
        gd_result = glyph_dropout_check(translated_json, pdf_path)
        print(f"  glyph_dropout_check: {gd_result['check_result'].upper()}")

    # Print coverage summary
    if "details" in cov_result and isinstance(cov_result["details"], dict):
        summ = cov_result["details"].get("summary", {})
        cov_pct = summ.get("coverage_pct", 0)
        n_issues = summ.get("issue_count", 0)
        xlated  = summ.get("translated_blocks", 0)
        total   = summ.get("total_blocks", 0)

        cov_line = f"  Coverage : {xlated}/{total} blocks  ({cov_pct}%)"
        print(bold("\nSummary:"))
        print(green(cov_line) if cov_pct >= 95 else red(cov_line))
        issue_line = f"  Issues   : {n_issues}"
        print(yellow(issue_line) if n_issues > 0 else green(issue_line))

    if thumbs and pdf_path:
        print(bold("\nRendering thumbnails..."))
        render_thumbnails(pdf_path, thumbs)

    all_checks = [cov_result, qual_result, style_result, tc_result, lb_result, ml_result, term_result, frag_result, rd_result, gd_result]
    passed = all(r["check_result"] in ("pass", "skipped") for r in all_checks)

    report = {
        "version":      "1.0",
        "testcase":     None,
        "run_date":     str(date.today()),
        "results":      [],
        "summary":      {
            "total":   sum(1 for r in all_checks if r["check_result"] != "skipped"),
            "passed":  sum(1 for r in all_checks if r["check_result"] == "pass"),
            "failed":  sum(1 for r in all_checks if r["check_result"] == "fail"),
            "skipped": sum(1 for r in all_checks if r["check_result"] == "skipped"),
        },
        "issue_results": {
            "coverage_check": cov_result,
            "quality_check":  qual_result,
            "style_check":    style_result,
            "translation_completeness_check": tc_result,
            "linebreak_consistency_check": lb_result,
            "mixed_language_check": ml_result,
            "terminology_consistency_check": term_result,
            "fragmentation_check": frag_result,
            "readability_check": rd_result,
            "glyph_dropout_check": gd_result,
        },
    }

    # Per-page confidence scoring
    num_pages = _get_num_pages(translated_json)
    if num_pages > 0:
        page_conf = _compute_page_confidence(report["issue_results"], num_pages)
        report["page_confidence"] = page_conf
        n_review = len(page_conf["review_needed"])
        conf_summ = page_conf["summary"]
        print(bold("\nPage confidence:"))
        print(f"  HIGH ({_CONFIDENCE_HIGH}+): {conf_summ['high']}  "
              f"MEDIUM ({_CONFIDENCE_MEDIUM}-{_CONFIDENCE_HIGH}): {conf_summ['medium']}  "
              f"LOW (<{_CONFIDENCE_MEDIUM}): {conf_summ['low']}")
        if n_review > 0:
            print(yellow(f"  Review needed: pages {page_conf['review_needed']}"))
        else:
            print(green("  No pages require manual review."))

    # Visual review check (Claude Vision) — runs after rule-based checks
    if not no_visual and source_pdf and pdf_path:
        # Select pages to review: LOW confidence pages, or all if no confidence data
        if "page_confidence" in report:
            pc = report["page_confidence"]
            review_pages = pc.get("review_needed", [])
            if not review_pages:
                # Also include MEDIUM confidence pages if no LOW ones
                review_pages = [
                    int(p) for p, score in pc.get("pages", {}).items()
                    if score < _CONFIDENCE_HIGH
                ]
        else:
            # No confidence data — review all pages (capped internally)
            review_pages = None

        if review_pages is None or len(review_pages) > 0:
            print("Running visual_review_check...")
            work_dir = Path(output).parent

            def _on_page_result(page_num, grade, page_issues):
                if page_issues:
                    summary = "; ".join(
                        i.get("description", "") or i.get("type", "")
                        for i in page_issues
                    )
                else:
                    summary = "no issues"
                print(f"  [visual_review] Page {page_num}: {grade} — {summary}")
                if grade in ("C", "D", "F"):
                    per_page_path = work_dir / f"visual_review_p{page_num:02d}.json"
                    with open(per_page_path, "w", encoding="utf-8") as _f:
                        json.dump(
                            {"page": page_num, "grade": grade, "issues": page_issues},
                            _f, ensure_ascii=False, indent=2,
                        )

            vr_result = visual_review_check(source_pdf, pdf_path, review_pages,
                                            on_page_result=_on_page_result)
            print(f"  visual_review_check: {vr_result['check_result'].upper()}")
            report["issue_results"]["visual_review_check"] = vr_result

            # Update pass/fail accounting
            if vr_result["check_result"] == "fail":
                passed = False
                report["summary"]["failed"] += 1
                report["summary"]["total"] += 1
            elif vr_result["check_result"] == "pass":
                report["summary"]["passed"] += 1
                report["summary"]["total"] += 1
            # skipped does not change pass/fail
            elif vr_result["check_result"] == "skipped":
                report["summary"]["skipped"] += 1
    elif no_visual:
        print("  visual_review_check: SKIPPED (--no-visual)")

    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(bold(f"\nReport written to: {out_path}"))

    if passed:
        print(green("\n[PASS]\n"))
        sys.exit(0)
    else:
        print(red("\n[FAIL]\n"))
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="pdf_translator test agent (regression + QA)")
    # Testcase mode
    parser.add_argument("--testcase", default=None, help="Testcase name (subdirectory under testdata/)")
    parser.add_argument(
        "--registry",
        default=str(PROJECT_ROOT / "issues" / "registry.json"),
        help="Path to registry.json",
    )
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="Save current output as regression baseline (testcase mode only)",
    )
    # Pipeline mode
    parser.add_argument("--json",   default=None, help="Path to translated.json (pipeline mode)")
    parser.add_argument("--pdf",    default=None, help="Path to output PDF (for thumbnail rendering)")
    parser.add_argument("--source-pdf", default=None, help="Path to source PDF (for visual review check)")
    parser.add_argument("--thumbs", default=None, help="Directory for page thumbnails")
    parser.add_argument("--no-visual", action="store_true",
                        help="Skip visual_review_check (Claude Vision comparison)")
    # Common
    parser.add_argument(
        "--output",
        default=None,
        help="Path to output test_report.json",
    )
    args = parser.parse_args()

    if args.json:
        # Pipeline QA mode
        output = args.output or "test_report.json"
        run_pipeline_qa(args.json, args.pdf, output, args.thumbs,
                        source_pdf=args.source_pdf, no_visual=args.no_visual)
    elif args.testcase:
        if args.save_baseline:
            # Save baseline mode
            print(bold(f"\n=== Saving baseline for '{args.testcase}' ===\n"))
            save_baseline(args.testcase)
            sys.exit(0)

        # Testcase regression mode
        registry_path = Path(args.registry)
        if not registry_path.is_absolute():
            registry_path = PROJECT_ROOT / registry_path

        if args.output:
            output_path = Path(args.output)
            if not output_path.is_absolute():
                output_path = PROJECT_ROOT / output_path
        else:
            output_path = PROJECT_ROOT / "testdata" / args.testcase / "test_report.json"

        run_checks(args.testcase, registry_path, output_path)
    else:
        parser.error("Either --testcase or --json is required.")


if __name__ == "__main__":
    main()
