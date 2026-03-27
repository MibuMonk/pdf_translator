"""
qa_regression.py — Baseline management and regression checks.
Covers: save_baseline, run_regression, and their helpers.
Imported by: test_agent
"""
import json
import math
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF not installed.", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from qa_utils import (  # noqa: E402
    _render_page_to_png,
    render_thumbnails,
    _text_similarity,
    green,
    yellow,
    red,
    bold,
    load_json,
)

# Project root is parent of this file's directory (agents/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Baseline management
# ---------------------------------------------------------------------------

def _resolve_testcase_paths(testcase: str) -> dict:
    """Resolve standard file paths for a testcase."""
    testcase_dir = PROJECT_ROOT / "testdata" / testcase
    work_dir = testcase_dir / "work"

    # Handle different naming conventions across testcases
    parsed_path = work_dir / "parsed.json"
    if not parsed_path.exists():
        parsed_path = work_dir / "source.parsed.json"

    translated_path = work_dir / "translated.json"
    if not translated_path.exists():
        translated_path = work_dir / "source.translated.json"

    output_pdf = testcase_dir / "output.pdf"
    if not output_pdf.exists():
        candidates = sorted(testcase_dir.glob("*.pdf"))
        candidates = [p for p in candidates if p.name not in ("source.pdf", "review_bundle.pdf")]
        if candidates:
            output_pdf = candidates[0]

    return {
        "testcase_dir": testcase_dir,
        "work_dir": work_dir,
        "output_pdf": output_pdf,
        "parsed_json": parsed_path,
        "translated_json": translated_path,
        "baseline_dir": testcase_dir / "baseline",
    }


def _build_block_summary(parsed_json_path: Path) -> list:
    """
    Build a per-page block summary from parsed.json.
    Each entry: {page_num, block_count, blocks: [{id, bbox, color, font_size, text_prefix}]}
    """
    data = load_json(parsed_json_path)
    pages = data.get("pages", []) if isinstance(data, dict) else data
    summary = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_num = page.get("page_num", page.get("page", 0))
        blocks = page.get("blocks", [])
        block_entries = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            text = (b.get("text") or "")
            block_entries.append({
                "id": b.get("id", ""),
                "bbox": b.get("bbox", []),
                "color": b.get("color", []),
                "font_size": b.get("font_size", 0),
                "text_prefix": text[:20],
            })
        summary.append({
            "page_num": page_num,
            "block_count": len(block_entries),
            "blocks": block_entries,
        })
    return summary


def _build_translated_summary(translated_json_path: Path) -> list:
    """
    Build a per-page translation summary from translated.json.
    Each entry: {page_num, block_count, blocks: [{id, translated_prefix, font_size}]}
    """
    data = load_json(translated_json_path)
    pages = data.get("pages", []) if isinstance(data, dict) else data
    summary = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_num = page.get("page_num", page.get("page", 0))
        blocks = page.get("blocks", [])
        block_entries = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            translated = (b.get("translated") or "")
            block_entries.append({
                "id": b.get("id", ""),
                "translated_prefix": translated[:30],
                "font_size": b.get("font_size", 0),
            })
        summary.append({
            "page_num": page_num,
            "block_count": len(block_entries),
            "blocks": block_entries,
        })
    return summary


def save_baseline(testcase: str) -> Path:
    """
    Save the current output as a regression baseline.
    Creates testdata/{name}/baseline/ with:
      - block_summary.json (from parsed.json)
      - translated_summary.json (from translated.json)
      - thumbnails/ (PNG per page from output.pdf)
      - metadata.json (timestamp, source files)
    """
    paths = _resolve_testcase_paths(testcase)
    baseline_dir = paths["baseline_dir"]
    thumbs_dir = baseline_dir / "thumbnails"

    # Validate required files exist
    if not paths["output_pdf"].exists():
        print(red(f"ERROR: output.pdf not found at {paths['output_pdf']}"), file=sys.stderr)
        sys.exit(1)

    # Create baseline directory (clean if exists)
    if baseline_dir.exists():
        shutil.rmtree(baseline_dir)
    baseline_dir.mkdir(parents=True)
    thumbs_dir.mkdir()

    # 1. Block summary from parsed.json
    if paths["parsed_json"].exists():
        block_summary = _build_block_summary(paths["parsed_json"])
        with open(baseline_dir / "block_summary.json", "w", encoding="utf-8") as f:
            json.dump(block_summary, f, ensure_ascii=False, indent=2)
        print(green(f"  Saved block_summary.json ({sum(p['block_count'] for p in block_summary)} blocks)"))
    else:
        print(yellow(f"  Skipped block_summary: parsed.json not found"))

    # 2. Translated summary from translated.json
    if paths["translated_json"].exists():
        translated_summary = _build_translated_summary(paths["translated_json"])
        with open(baseline_dir / "translated_summary.json", "w", encoding="utf-8") as f:
            json.dump(translated_summary, f, ensure_ascii=False, indent=2)
        print(green(f"  Saved translated_summary.json"))
    else:
        print(yellow(f"  Skipped translated_summary: translated.json not found"))

    # 3. Thumbnails from output.pdf
    doc = fitz.open(str(paths["output_pdf"]))
    zoom = 80 / 72.0  # 80 DPI
    matrix = fitz.Matrix(zoom, zoom)
    num_pages = len(doc)
    for page_idx in range(num_pages):
        page = doc[page_idx]
        pix = page.get_pixmap(matrix=matrix)
        pix.save(str(thumbs_dir / f"page_{page_idx + 1:04d}.png"))
    doc.close()
    print(green(f"  Saved {num_pages} page thumbnails"))

    # 4. Metadata
    metadata = {
        "testcase": testcase,
        "created_at": datetime.now().isoformat(),
        "output_pdf": str(paths["output_pdf"]),
        "parsed_json": str(paths["parsed_json"]) if paths["parsed_json"].exists() else None,
        "translated_json": str(paths["translated_json"]) if paths["translated_json"].exists() else None,
        "num_pages": num_pages,
    }
    with open(baseline_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    print(green(f"\nBaseline saved to: {baseline_dir}"))
    return baseline_dir


# ---------------------------------------------------------------------------
# Regression detection
# ---------------------------------------------------------------------------

def _compute_image_mse(img_path_a: str, img_path_b: str) -> float:
    """
    Compute mean squared error between two PNG images using PyMuPDF Pixmaps.
    Returns MSE as float. If images differ in size, returns a high value.
    """
    pix_a = fitz.Pixmap(img_path_a)
    pix_b = fitz.Pixmap(img_path_b)

    if pix_a.width != pix_b.width or pix_a.height != pix_b.height:
        pix_a = None
        pix_b = None
        return 99999.0  # Size mismatch -> high MSE

    samples_a = pix_a.samples
    samples_b = pix_b.samples
    n = len(samples_a)
    if n == 0:
        return 0.0

    total = 0.0
    for i in range(n):
        diff = samples_a[i] - samples_b[i]
        total += diff * diff

    mse = total / n
    pix_a = None
    pix_b = None
    return mse


def _check_block_count(baseline_summary: list, current_summary: list) -> list:
    """
    Compare block counts per page. Flag pages where count deviates > 10%.
    Returns list of regression findings.
    """
    findings = []
    baseline_by_page = {p["page_num"]: p for p in baseline_summary}
    current_by_page = {p["page_num"]: p for p in current_summary}

    total_baseline = sum(p["block_count"] for p in baseline_summary)
    total_current = sum(p["block_count"] for p in current_summary)

    if total_baseline > 0:
        deviation = abs(total_current - total_baseline) / total_baseline
        if deviation > 0.10:
            findings.append({
                "check": "block_count_total",
                "severity": "error",
                "message": f"Total block count changed: {total_baseline} -> {total_current} ({deviation:.1%} deviation)",
                "baseline": total_baseline,
                "current": total_current,
            })

    # Per-page check
    all_pages = sorted(set(list(baseline_by_page.keys()) + list(current_by_page.keys())))
    pages_with_change = []
    for pn in all_pages:
        b_count = baseline_by_page.get(pn, {}).get("block_count", 0)
        c_count = current_by_page.get(pn, {}).get("block_count", 0)
        if b_count != c_count:
            pages_with_change.append({
                "page": pn,
                "baseline_count": b_count,
                "current_count": c_count,
            })

    if pages_with_change:
        findings.append({
            "check": "block_count_per_page",
            "severity": "warning",
            "message": f"Block count changed on {len(pages_with_change)} page(s)",
            "pages": pages_with_change,
        })

    return findings


def _check_title_preservation(baseline_summary: list, current_summary: list) -> list:
    """
    Check that title blocks (font_size >= 20 and in top portion of page) from
    the baseline still exist in the current output.
    """
    findings = []
    current_by_page = {p["page_num"]: p for p in current_summary}

    for page in baseline_summary:
        pn = page["page_num"]
        for block in page["blocks"]:
            fs = block.get("font_size", 0)
            if isinstance(fs, str):
                try:
                    fs = float(fs)
                except (ValueError, TypeError):
                    fs = 0
            bbox = block.get("bbox", [0, 0, 0, 0])
            if isinstance(bbox, str):
                try:
                    bbox = json.loads(bbox)
                except (json.JSONDecodeError, TypeError):
                    bbox = [0, 0, 0, 0]

            # Title heuristic: font_size >= 20 and y position in top 40% of typical slide (792pt)
            is_title = fs >= 20 and len(bbox) >= 4 and bbox[1] < 320
            if not is_title:
                continue

            # Check if this block exists in current output
            current_page = current_by_page.get(pn)
            if current_page is None:
                findings.append({
                    "check": "title_preservation",
                    "severity": "error",
                    "message": f"Page {pn} missing entirely in current output",
                    "page": pn,
                    "block_id": block["id"],
                    "text_prefix": block.get("text_prefix", ""),
                })
                continue

            # Look for matching block by id
            found = False
            for cb in current_page["blocks"]:
                if cb["id"] == block["id"]:
                    found = True
                    break
            if not found:
                findings.append({
                    "check": "title_preservation",
                    "severity": "error",
                    "message": f"Title block '{block['id']}' disappeared on page {pn}",
                    "page": pn,
                    "block_id": block["id"],
                    "text_prefix": block.get("text_prefix", ""),
                    "font_size": fs,
                })

    return findings


def _check_color_consistency(baseline_summary: list, current_summary: list) -> list:
    """
    Compare block colors between baseline and current. Report changed blocks.
    """
    findings = []
    current_blocks = {}
    for page in current_summary:
        for block in page["blocks"]:
            current_blocks[block["id"]] = block

    changed_colors = []
    for page in baseline_summary:
        pn = page["page_num"]
        for block in page["blocks"]:
            bid = block["id"]
            cb = current_blocks.get(bid)
            if cb is None:
                continue

            b_color = block.get("color", [])
            c_color = cb.get("color", [])

            # Normalize colors to lists of floats
            if isinstance(b_color, str):
                try:
                    b_color = json.loads(b_color)
                except (json.JSONDecodeError, TypeError):
                    continue
            if isinstance(c_color, str):
                try:
                    c_color = json.loads(c_color)
                except (json.JSONDecodeError, TypeError):
                    continue

            if not b_color or not c_color:
                continue
            if len(b_color) != len(c_color):
                changed_colors.append({"page": pn, "block_id": bid,
                                       "baseline_color": b_color, "current_color": c_color})
                continue

            # Check if colors differ (tolerance 0.01 per channel)
            differs = any(abs(a - b) > 0.01 for a, b in zip(b_color, c_color))
            if differs:
                changed_colors.append({"page": pn, "block_id": bid,
                                       "baseline_color": b_color, "current_color": c_color})

    if changed_colors:
        findings.append({
            "check": "color_consistency",
            "severity": "warning" if len(changed_colors) <= 5 else "error",
            "message": f"{len(changed_colors)} block(s) changed color vs baseline",
            "changed_blocks": changed_colors[:20],  # Cap detail output
            "total_changed": len(changed_colors),
        })

    return findings


def _check_bbox_coverage(baseline_summary: list, current_summary: list) -> list:
    """
    Check that blocks present in baseline (with text) are also present in current.
    """
    findings = []
    current_ids = set()
    for page in current_summary:
        for block in page["blocks"]:
            current_ids.add(block["id"])

    missing_blocks = []
    for page in baseline_summary:
        pn = page["page_num"]
        for block in page["blocks"]:
            if block.get("text_prefix", "").strip() and block["id"] not in current_ids:
                missing_blocks.append({
                    "page": pn,
                    "block_id": block["id"],
                    "text_prefix": block.get("text_prefix", ""),
                })

    if missing_blocks:
        findings.append({
            "check": "bbox_coverage",
            "severity": "error" if len(missing_blocks) >= 5 else "warning",
            "message": f"{len(missing_blocks)} text block(s) from baseline are missing",
            "missing_blocks": missing_blocks[:30],
            "total_missing": len(missing_blocks),
        })

    return findings


def _check_visual_diff(baseline_dir: Path, current_pdf: Path) -> list:
    """
    Compare page thumbnails between baseline and current output.
    Pages with MSE > threshold are flagged.
    """
    findings = []
    baseline_thumbs = baseline_dir / "thumbnails"
    if not baseline_thumbs.exists():
        return findings

    # Render current thumbnails to a temp directory
    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="test_agent_thumbs_")
    try:
        doc = fitz.open(str(current_pdf))
        zoom = 80 / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            pix = page.get_pixmap(matrix=matrix)
            pix.save(os.path.join(tmp_dir, f"page_{page_idx + 1:04d}.png"))
        doc.close()

        # Compare each page
        mse_threshold = 150.0  # Tunable; typical text changes produce MSE 50-300
        high_diff_pages = []
        baseline_pngs = sorted(baseline_thumbs.glob("page_*.png"))
        for bp in baseline_pngs:
            cp = Path(tmp_dir) / bp.name
            if not cp.exists():
                continue
            mse = _compute_image_mse(str(bp), str(cp))
            page_num = int(bp.stem.split("_")[1])
            if mse > mse_threshold:
                high_diff_pages.append({
                    "page": page_num,
                    "mse": round(mse, 2),
                })

        if high_diff_pages:
            findings.append({
                "check": "visual_diff",
                "severity": "warning",
                "message": f"{len(high_diff_pages)} page(s) have significant visual differences vs baseline",
                "pages": high_diff_pages,
            })
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return findings


def run_regression(testcase: str) -> dict:
    """
    Run all regression checks against the saved baseline.
    Returns a result dict with check_result and details.
    """
    paths = _resolve_testcase_paths(testcase)
    baseline_dir = paths["baseline_dir"]

    if not baseline_dir.exists():
        return None  # No baseline, skip regression

    print(bold("\n--- Regression checks (vs baseline) ---\n"))

    all_findings = []

    # Load baseline summaries
    block_summary_path = baseline_dir / "block_summary.json"
    translated_summary_path = baseline_dir / "translated_summary.json"

    # Build current summaries
    current_block_summary = None
    current_translated_summary = None

    if paths["parsed_json"].exists():
        current_block_summary = _build_block_summary(paths["parsed_json"])

    if paths["translated_json"].exists():
        current_translated_summary = _build_translated_summary(paths["translated_json"])

    # 1. Block count check
    if block_summary_path.exists() and current_block_summary is not None:
        baseline_block_summary = load_json(block_summary_path)
        print("Running block_count_check...")
        findings = _check_block_count(baseline_block_summary, current_block_summary)
        all_findings.extend(findings)
        status = "FAIL" if findings else "PASS"
        print(f"  block_count_check: {status}")

        # 2. Title preservation
        print("Running title_preservation_check...")
        findings = _check_title_preservation(baseline_block_summary, current_block_summary)
        all_findings.extend(findings)
        status = "FAIL" if findings else "PASS"
        print(f"  title_preservation_check: {status}")

        # 3. Color consistency
        print("Running color_consistency_check...")
        findings = _check_color_consistency(baseline_block_summary, current_block_summary)
        all_findings.extend(findings)
        status = "FAIL" if findings else "PASS"
        print(f"  color_consistency_check: {status}")

        # 4. Bbox coverage
        print("Running bbox_coverage_check...")
        findings = _check_bbox_coverage(baseline_block_summary, current_block_summary)
        all_findings.extend(findings)
        status = "FAIL" if findings else "PASS"
        print(f"  bbox_coverage_check: {status}")
    else:
        print(yellow("  Skipped structural checks: block_summary baseline or current parsed.json missing"))

    # 5. Visual diff
    if paths["output_pdf"].exists():
        print("Running visual_diff_check...")
        findings = _check_visual_diff(baseline_dir, paths["output_pdf"])
        all_findings.extend(findings)
        status = "FAIL" if findings else "PASS"
        print(f"  visual_diff_check: {status}")
    else:
        print(yellow("  Skipped visual_diff: output.pdf not found"))

    errors = [f for f in all_findings if f.get("severity") == "error"]
    warnings = [f for f in all_findings if f.get("severity") == "warning"]

    check_result = "fail" if errors else "pass"
    print(f"\n  Regression summary: {len(errors)} error(s), {len(warnings)} warning(s)")

    return {
        "check_result": check_result,
        "details": {
            "error_count": len(errors),
            "warning_count": len(warnings),
            "findings": all_findings,
        },
    }
