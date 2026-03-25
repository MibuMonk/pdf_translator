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
import math
import os
import re
import shutil
import subprocess
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
# Shared helpers
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def bbox_center_y(bbox):
    """Return vertical center of a bbox [x0, y0, x1, y1]."""
    return (bbox[1] + bbox[3]) / 2


def bbox_center_x(bbox):
    return (bbox[0] + bbox[2]) / 2


def bboxes_overlap_x(a, b):
    """True if two bboxes overlap horizontally."""
    return not (a[2] <= b[0] or b[2] <= a[0])


def extract_pdf_spans_by_page(pdf_path: Path) -> dict:
    """
    Return dict: page_num (1-based) -> list of span dicts with keys:
      bbox: [x0, y0, x1, y1], size: float, text: str
    Uses PyMuPDF get_text("dict") which returns y coords top-down.
    """
    doc = fitz.open(str(pdf_path))
    result = {}
    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_num = page_idx + 1
        spans = []
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if block.get("type") != 0:  # 0 = text block
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    r = span.get("bbox")
                    if r and span.get("size", 0) > 0:
                        spans.append({
                            "bbox": list(r),
                            "size": span["size"],
                            "text": span.get("text", ""),
                        })
        result[page_num] = spans
    doc.close()
    return result


def extract_pdf_text_block_bboxes_by_page(pdf_path: Path) -> dict:
    """
    Return dict: page_num (1-based) -> list of bbox [x0, y0, x1, y1]
    for each text block in the PDF.  Only includes text blocks (type 0)
    with non-empty text and area >= 100 px².
    """
    doc = fitz.open(str(pdf_path))
    result = {}
    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_num = page_idx + 1
        bboxes = []
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if block.get("type") != 0:
                continue
            bbox = block.get("bbox")
            if not bbox:
                continue
            x0, y0, x1, y1 = bbox
            w = x1 - x0
            h = y1 - y0
            if w * h < 100:
                continue
            # Check block has actual text
            has_text = False
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("text", "").strip():
                        has_text = True
                        break
                if has_text:
                    break
            if has_text:
                bboxes.append(list(bbox))
        result[page_num] = bboxes
    doc.close()
    return result


def _check_bbox_overlaps(page_bboxes: dict) -> list[dict]:
    """
    For each page, check all pairs of text block bboxes for overlap.
    Returns a list of bbox_overlap issues.
    An overlap is reported when the intersection area exceeds 10% of the
    smaller bbox's area.  Max 5 overlap issues per page.
    """
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
                if inter_area > min_area * 0.10:
                    page_issues.append({
                        "page": page_num,
                        "type": "bbox_overlap",
                        "severity": "error",
                        "bbox_a": [round(v, 1) for v in bboxes[i]],
                        "bbox_b": [round(v, 1) for v in bboxes[j]],
                        "intersection_area": round(inter_area, 1),
                        "smaller_bbox_area": round(min_area, 1),
                        "overlap_pct": round(inter_area / min_area * 100, 1),
                    })
        issues.extend(page_issues)
    return issues


def find_best_span_match(target_bbox, spans, tolerance=5.0):
    """
    Find the span whose y-center overlaps with target_bbox's y-range and whose
    x0 (left edge) is close to target_bbox's x0.
    """
    tx0, ty0, tx1, ty1 = target_bbox
    tcy = (ty0 + ty1) / 2

    candidates = []
    for span in spans:
        bx0, by0, bx1, by1 = span["bbox"]
        bcy = (by0 + by1) / 2
        if bcy < ty0 - tolerance or bcy > ty1 + tolerance:
            continue
        dx0 = abs(bx0 - tx0)
        dy = abs(bcy - tcy)
        candidates.append((dy, dx0, span))

    if not candidates:
        return None

    candidates.sort(key=lambda t: (t[0], t[1]))
    return candidates[0][2]


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
# Translation completeness & readability checks
# ---------------------------------------------------------------------------

_PRODUCT_NAME_RE = re.compile(r'^[A-Z][A-Z0-9._\-/]*$')


def _is_likely_product_name(text: str) -> bool:
    """
    Return True if text looks like a product name, acronym, or identifier
    that should NOT be translated (e.g. "CDI", "Mviz", "DDOD").
    Criteria: all uppercase (with digits/punctuation), no spaces, length < 15.
    """
    stripped = text.strip()
    if len(stripped) < 15 and ' ' not in stripped and _PRODUCT_NAME_RE.match(stripped):
        return True
    return False


def _text_similarity(a: str, b: str) -> float:
    """
    Simple character-level similarity ratio between two strings.
    Returns 0.0..1.0.
    """
    if not a or not b:
        return 0.0
    # Use set-based Jaccard on character bigrams for speed
    def bigrams(s):
        return set(s[i:i+2] for i in range(len(s) - 1)) if len(s) >= 2 else {s}
    ba = bigrams(a)
    bb = bigrams(b)
    if not ba or not bb:
        return 1.0 if a == b else 0.0
    intersection = ba & bb
    union = ba | bb
    return len(intersection) / len(union) if union else 0.0


def translation_completeness_check(translated_json_path: str) -> dict:
    """
    Check for untranslated content and low translation ratios per page.
    - untranslated_content: block where translated == text, len > 10, contains spaces (English sentence)
    - low_translation_ratio: page where < 50% of characters are translated
    """
    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
    else:
        return {"check_result": "fail", "details": [{"error": "Unexpected JSON structure"}]}

    issues: list[dict] = []
    page_ratios: list[dict] = []

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks = page_entry.get("blocks", [])

        total_chars = 0
        translated_chars = 0

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            text = (block.get("text") or "").strip()
            translated = (block.get("translated") or "").strip()
            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))

            if not text:
                continue

            total_chars += len(text)

            # Check if translated differs from source
            if translated and translated != text:
                translated_chars += len(text)
            elif translated == text:
                # Same as source — check if it should have been translated
                if (
                    len(text) > 10
                    and ' ' in text
                    and not _is_trivially_invariant(text)
                    and not _is_acronym_definition(text)
                    and not _is_likely_product_name(text)
                    and not _is_pure_ascii(text)
                ):
                    issues.append({
                        "page": page_num,
                        "block_id": block_id,
                        "type": "untranslated_content",
                        "severity": "error",
                        "text": text[:100],
                    })

        # Per-page translation ratio
        if total_chars > 0:
            ratio = translated_chars / total_chars
            page_ratios.append({"page": page_num, "ratio": round(ratio, 3)})
            if ratio < 0.50:
                issues.append({
                    "page": page_num,
                    "type": "low_translation_ratio",
                    "severity": "error",
                    "ratio": round(ratio, 3),
                    "total_chars": total_chars,
                    "translated_chars": translated_chars,
                })

    has_errors = any(i.get("severity") == "error" for i in issues)
    return {
        "check_result": "fail" if has_errors else "pass",
        "details": {
            "issues": issues,
            "page_ratios": page_ratios,
            "untranslated_count": sum(1 for i in issues if i["type"] == "untranslated_content"),
            "low_ratio_pages": sum(1 for i in issues if i["type"] == "low_translation_ratio"),
        },
    }


def linebreak_consistency_check(translated_json_path: str) -> dict:
    """
    Check for line breaks lost during translation.
    - missing_bullet_break: bullet marker (■/•) not preceded by \n in translation
      but original has \n before the corresponding marker → severity "error"
    - linebreak_count_mismatch: translation lost more than half of original \n → severity "warning"
    """
    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
    else:
        return {"check_result": "fail", "details": {"issues": [], "total_checked": 0, "blocks_with_missing_breaks": 0}}

    issues: list[dict] = []
    total_checked = 0

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks = page_entry.get("blocks", [])

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            text = block.get("text") or ""
            translated = block.get("translated") or ""
            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))

            if not text or not translated:
                continue

            total_checked += 1

            orig_breaks = text.count("\n")
            trans_breaks = translated.count("\n")

            # Rule 1: missing_bullet_break
            # Check if translated text has ■ or • NOT at position 0 and NOT preceded by \n
            for marker in ("■", "•"):
                start = 0
                while True:
                    pos = translated.find(marker, start)
                    if pos == -1:
                        break
                    if pos > 0 and translated[pos - 1] != "\n":
                        # Check if original has \n before this marker type
                        orig_has_break_before_marker = False
                        ostart = 0
                        while True:
                            opos = text.find(marker, ostart)
                            if opos == -1:
                                break
                            if opos > 0 and text[opos - 1] == "\n":
                                orig_has_break_before_marker = True
                                break
                            ostart = opos + 1

                        if orig_has_break_before_marker:
                            issues.append({
                                "page": page_num,
                                "block_id": block_id,
                                "type": "missing_bullet_break",
                                "severity": "error",
                                "original_breaks": orig_breaks,
                                "translated_breaks": trans_breaks,
                                "text_preview": translated[:80],
                            })
                            break  # one issue per block per marker is enough
                    start = pos + 1

            # Rule 2: linebreak_count_mismatch
            if orig_breaks > 0 and trans_breaks < orig_breaks / 2:
                issues.append({
                    "page": page_num,
                    "block_id": block_id,
                    "type": "linebreak_count_mismatch",
                    "severity": "warning",
                    "original_breaks": orig_breaks,
                    "translated_breaks": trans_breaks,
                    "text_preview": translated[:80],
                })

    has_errors = any(i.get("severity") == "error" for i in issues)
    blocks_with_missing = len(set(i["block_id"] for i in issues))
    return {
        "check_result": "fail" if has_errors else "pass",
        "details": {
            "issues": issues,
            "total_checked": total_checked,
            "blocks_with_missing_breaks": blocks_with_missing,
        },
    }


# ---------------------------------------------------------------------------
# mixed_language_check — detect untranslated English in CJK translations
# ---------------------------------------------------------------------------

# Regex: bullet/section marker followed by English word
_UNTRANSLATED_HEADING_RE = re.compile(r'[■•]\s*[A-Za-z]{2,}')

# Regex: 3+ consecutive English words (each 2+ chars)
_ENGLISH_PHRASE_RE = re.compile(r'[A-Za-z]{2,}(?:\s+[A-Za-z]{2,}){2,}')

# Regex: text inside parentheses
_PARENS_RE = re.compile(r'\([^)]*\)')

# Regex: ALL-CAPS abbreviation (2+ uppercase letters, optionally with digits)
_ALLCAPS_ABBREV_RE = re.compile(r'^[A-Z][A-Z0-9]{1,}$')

# Regex: code identifier (contains underscore)
_CODE_IDENT_RE = re.compile(r'_')

# Known product names / technical terms that should stay English
_ENGLISH_KEEP_TERMS = {
    "momenta box", "momenta model lab", "momenta",
    "vvp runtime", "vvp run time", "runtime",
    "google maps", "open street map", "point cloud",
    "deep learning", "machine learning", "neural network",
    "open source", "pull request", "merge request",
    "good event set", "road test dashboard",
    "ict dashboard", "rct report",
}

# Product name words that should not be flagged as untranslated headings
_HEADING_KEEP_WORDS = frozenset({
    "momenta", "google", "apple", "microsoft", "amazon", "tesla", "nvidia",
    "intel", "qualcomm", "huawei", "baidu", "alibaba",
})

sys.path.insert(0, str(Path(__file__).parent))
from shared_utils import has_cjk  # noqa: E402


def mixed_language_check(translated_json_path: str) -> dict:
    """
    Detect blocks where translated text still contains untranslated English phrases.
    - untranslated_heading: ■/• followed by English word (severity: error)
    - english_phrase_in_translation: 3+ consecutive English words in CJK text (severity: warning)
    """
    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
    else:
        return {"check_result": "fail", "details": {"issues": [], "total_checked": 0, "blocks_with_mixed_language": 0}}

    issues: list[dict] = []
    total_checked = 0

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks = page_entry.get("blocks", [])

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            text = block.get("text") or ""
            translated = block.get("translated") or ""
            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))

            if not translated:
                continue

            total_checked += 1

            # Rule 1: untranslated_heading — ■/• followed by English word
            for m in _UNTRANSLATED_HEADING_RE.finditer(translated):
                matched = m.group()
                # Exception: skip if original is English AND translation has no CJK
                # (entire block intentionally kept as-is, e.g. brand names)
                if not has_cjk(translated) and ' ' in text:
                    continue
                # Exception: skip ALL-CAPS abbreviations/product names after bullet
                # e.g. "• MBOX", "• VVP", "• FDR"
                eng_word = matched.lstrip("■•").strip()
                if _ALLCAPS_ABBREV_RE.match(eng_word):
                    continue
                # Exception: skip known product/brand names
                if eng_word.lower() in _HEADING_KEEP_WORDS:
                    continue
                # Exception: skip if followed by colon/CJK/English word (term used inline)
                # e.g. "• VVP Camera：...", "• Good Event Set"
                end_pos = m.end()
                if end_pos < len(translated):
                    rest = translated[end_pos:end_pos + 20].lstrip()
                    if rest and (rest[0] in '：:' or has_cjk(rest[:1])):
                        continue
                    # If followed by another English word, it's a multi-word term, not untranslated heading
                    if rest and re.match(r'[A-Za-z]', rest):
                        continue
                issues.append({
                    "page": page_num,
                    "block_id": block_id,
                    "type": "untranslated_heading",
                    "severity": "error",
                    "matched_text": matched,
                    "text_preview": translated[:100],
                })

            # Rule 2: english_phrase_in_translation — 3+ consecutive English words in CJK text
            if not has_cjk(translated):
                continue  # Not a CJK translation, skip phrase check

            # Remove parenthesised content before scanning
            cleaned = _PARENS_RE.sub('', translated)

            for m in _ENGLISH_PHRASE_RE.finditer(cleaned):
                phrase = m.group()
                words = phrase.split()

                # Exclude: all words are ALL-CAPS abbreviations
                if all(_ALLCAPS_ABBREV_RE.match(w) for w in words):
                    continue

                # Exclude: contains underscore (code identifier)
                if _CODE_IDENT_RE.search(phrase):
                    continue

                # Exclude: known product/technical terms
                if phrase.lower() in _ENGLISH_KEEP_TERMS:
                    continue

                # Exclude: any sub-phrase of 3 words matches known terms
                skip = False
                for term in _ENGLISH_KEEP_TERMS:
                    if term in phrase.lower():
                        skip = True
                        break
                if skip:
                    continue

                issues.append({
                    "page": page_num,
                    "block_id": block_id,
                    "type": "english_phrase_in_translation",
                    "severity": "warning",
                    "matched_text": phrase,
                    "text_preview": translated[:100],
                })

    has_errors = any(i.get("severity") == "error" for i in issues)
    blocks_with_mixed = len(set(i["block_id"] for i in issues))
    return {
        "check_result": "fail" if has_errors else "pass",
        "details": {
            "issues": issues,
            "total_checked": total_checked,
            "blocks_with_mixed_language": blocks_with_mixed,
        },
    }


# ---------------------------------------------------------------------------
# terminology_consistency_check — detect inconsistent translations of the same term
# ---------------------------------------------------------------------------

# Regex: CJK unified ideographs (BMP + Ext-A + CJK compat ideographs + SIP)
_CJK_RE = re.compile(r'[\u3000-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]')

# Known variant pairs: (variant_a, variant_b, description)
# These are CJK translation variants that indicate inconsistency
_VARIANT_PAIRS = [
    ("摄像头", "相机", "camera"),
    ("过滤器", "滤波器", "filter"),
    ("边界工况", "边缘场景", "edge case"),
    ("服务器", "伺服器", "server"),
    ("数据库", "资料库", "database"),
    ("接口", "界面", "interface (API vs UI)"),
    ("文件", "档案", "file/document"),
    ("激光雷达", "雷达", "LiDAR"),
    ("传感器", "感测器", "sensor"),
    ("算法", "演算法", "algorithm"),
    ("组件", "元件", "component"),
    ("模块", "模组", "module"),
    ("配置", "设定", "configuration/setting"),
    ("执行", "运行", "execute/run"),
    ("框架", "架构", "framework"),
]

# Stop words for English term extraction
_STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "for", "with", "from", "that",
    "this", "are", "was", "were", "been", "being", "have", "has", "had",
    "will", "would", "could", "should", "may", "might", "can", "shall",
    "not", "all", "each", "every", "both", "few", "more", "most", "other",
    "some", "such", "than", "too", "very", "also", "just", "about", "into",
    "over", "after", "before", "between", "through", "during", "without",
    "again", "further", "then", "once", "here", "there", "when", "where",
    "how", "what", "which", "who", "whom", "its", "their", "our", "your",
})


def terminology_consistency_check(translated_json_path: str) -> dict:
    """
    Detect inconsistent translations of the same term across the document.
    Two strategies:
    1. Known variant pairs: check if both variants appear in translated text
    2. Dynamic: find English terms in source that appear in 3+ blocks, check if
       they map to different CJK translations across blocks
    """
    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
    else:
        return {"check_result": "pass", "details": {"variant_pair_issues": [], "dynamic_issues": [], "total_checked": 0}}

    # Collect all translated text per page for variant-pair scanning
    all_translated_text = []  # list of (page_num, block_id, translated_text)
    total_checked = 0

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks = page_entry.get("blocks", [])

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            translated = block.get("translated") or ""
            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))
            if translated:
                total_checked += 1
                all_translated_text.append((page_num, block_id, translated))

    # --- Strategy 1: Known variant pairs ---
    variant_pair_issues = []
    full_translated = "\n".join(t for _, _, t in all_translated_text)

    for variant_a, variant_b, desc in _VARIANT_PAIRS:
        count_a = full_translated.count(variant_a)
        count_b = full_translated.count(variant_b)
        if count_a > 0 and count_b > 0:
            # Both variants present — collect sample locations
            pages_a = []
            pages_b = []
            for pg, bid, txt in all_translated_text:
                if variant_a in txt and len(pages_a) < 3:
                    pages_a.append({"page": pg, "block_id": bid})
                if variant_b in txt and len(pages_b) < 3:
                    pages_b.append({"page": pg, "block_id": bid})
            variant_pair_issues.append({
                "type": "variant_pair",
                "severity": "warning",
                "term_description": desc,
                "variant_a": variant_a,
                "variant_a_count": count_a,
                "variant_b": variant_b,
                "variant_b_count": count_b,
                "sample_locations_a": pages_a,
                "sample_locations_b": pages_b,
            })

    # --- Strategy 2: Dynamic English term consistency ---
    dynamic_issues = []

    # Build: english_term -> {chinese_context -> [block_ids]}
    term_contexts = defaultdict(lambda: defaultdict(list))

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks = page_entry.get("blocks", [])

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            text = block.get("text") or ""
            translated = block.get("translated") or ""
            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))

            if not text or not translated or not has_cjk(translated):
                continue

            # Extract English terms from original text
            eng_terms = set(re.findall(r'\b[A-Za-z]{3,}\b', text))
            eng_terms = {t.lower() for t in eng_terms} - _STOP_WORDS

            for term in eng_terms:
                # Find how this term's surrounding context was translated
                # Use a simple heuristic: find the term in original, get its line,
                # find the corresponding CJK segment in translation
                # For simplicity: record the first CJK phrase near the term position
                # ratio-based position mapping
                term_lower = term.lower()
                text_lower = text.lower()
                pos = text_lower.find(term_lower)
                if pos == -1:
                    continue

                # Map position ratio to translated text
                ratio = pos / max(len(text), 1)
                trans_pos = int(ratio * len(translated))

                # Extract a CJK window around the mapped position (up to 6 chars)
                window_start = max(0, trans_pos - 3)
                window_end = min(len(translated), trans_pos + 6)
                cjk_window = translated[window_start:window_end]

                # Extract only CJK characters from window
                cjk_chars = _CJK_RE.findall(cjk_window)
                if len(cjk_chars) >= 2:
                    cjk_key = "".join(cjk_chars[:4])  # first 4 CJK chars as key
                    term_contexts[term_lower][cjk_key].append((page_num, block_id))

    # Flag terms with 2+ distinct CJK translations, each appearing 2+ times
    for eng_term, translations in term_contexts.items():
        if len(translations) < 2:
            continue
        # Filter to translations that appear at least twice
        significant = {k: v for k, v in translations.items() if len(v) >= 2}
        if len(significant) < 2:
            continue
        # Sort by frequency descending
        sorted_trans = sorted(significant.items(), key=lambda x: -len(x[1]))
        dynamic_issues.append({
            "type": "inconsistent_term_translation",
            "severity": "warning",
            "english_term": eng_term,
            "translations": [
                {
                    "chinese": k,
                    "occurrences": len(v),
                    "sample_locations": [{"page": pg, "block_id": bid} for pg, bid in v[:3]],
                }
                for k, v in sorted_trans[:4]
            ],
        })

    all_issues = variant_pair_issues + dynamic_issues
    return {
        "check_result": "fail" if any(i.get("severity") == "error" for i in all_issues) else "pass",
        "details": {
            "variant_pair_issues": variant_pair_issues,
            "dynamic_issues": dynamic_issues,
            "total_checked": total_checked,
        },
    }


def readability_check(translated_json_path: str, pdf_path: str) -> dict:
    """
    Check for readability issues in the rendered output.
    - text_too_small: rendered font size < 8pt
    - content_truncated: translated text far exceeds bbox capacity (>2x)
    - multicolor_fallback: color_spans block with mismatched translated_spans char count
    - structure_collapse_suspect: single block dominating >50% page area with >200 chars
    - inconsistent_sizing: same-content pages with >30% font size difference
    - word_split: English word broken across \\n in translated text (e.g. "Sc\\nenarios")
    - bbox_overlap: overlapping text block bboxes in rendered PDF (intersection > 10% of smaller)
    """
    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
    else:
        return {"check_result": "fail", "details": [{"error": "Unexpected JSON structure"}]}

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
                    estimated_capacity = area / (effective_size * effective_size)
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
    pdf_block_bboxes = extract_pdf_text_block_bboxes_by_page(Path(pdf_path))
    overlap_issues = _check_bbox_overlaps(pdf_block_bboxes)
    issues.extend(overlap_issues)

    has_errors = any(i.get("severity") == "error" for i in issues)
    has_structural_warnings = any(
        i.get("type") in ("multicolor_fallback", "structure_collapse_suspect")
        for i in issues
    )
    return {
        "check_result": "fail" if (has_errors or has_structural_warnings) else "pass",
        "details": {
            "issues": issues,
            "text_too_small_count": sum(1 for i in issues if i["type"] == "text_too_small"),
            "content_truncated_count": sum(1 for i in issues if i["type"] == "content_truncated"),
            "inconsistent_sizing_count": sum(1 for i in issues if i["type"] == "inconsistent_sizing"),
            "multicolor_fallback_count": sum(1 for i in issues if i["type"] == "multicolor_fallback"),
            "structure_collapse_suspect_count": sum(1 for i in issues if i["type"] == "structure_collapse_suspect"),
            "word_split_count": sum(1 for i in issues if i["type"] == "word_split"),
            "bbox_overlap_count": sum(1 for i in issues if i["type"] == "bbox_overlap"),
        },
    }


# ---------------------------------------------------------------------------
# fragmentation_check — detect heading/bullet split across blocks
# ---------------------------------------------------------------------------

def fragmentation_check(translated_data) -> dict:
    """
    Detect paragraph fragmentation: ■ heading in one block and • bullets in the
    next block (same column, small y gap).  Also detects consecutive bullet blocks
    that were split apart.
    """
    if isinstance(translated_data, str):
        with open(translated_data, encoding="utf-8") as f:
            translated_data = json.load(f)

    if isinstance(translated_data, list):
        pages = translated_data
    elif isinstance(translated_data, dict):
        pages = translated_data.get("pages", [translated_data])
    else:
        return {"check_result": "pass", "details": {"issues": []}}

    issues: list[dict] = []

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks = page_entry.get("blocks", [])

        # Sort blocks by y0 (top of bbox)
        sorted_blocks = []
        for idx, blk in enumerate(blocks):
            if not isinstance(blk, dict):
                continue
            bbox = blk.get("bbox")
            translated = blk.get("translated") or ""
            if not bbox or len(bbox) < 4 or not translated.strip():
                continue
            sorted_blocks.append({
                "block": blk,
                "translated": translated,
                "bbox": bbox,
                "block_id": blk.get("block_id", blk.get("id", f"p{page_num:02d}_b{idx:03d}")),
            })
        sorted_blocks.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))

        for i in range(len(sorted_blocks) - 1):
            a = sorted_blocks[i]
            b = sorted_blocks[i + 1]
            a_text = a["translated"]
            b_text = b["translated"]
            a_x0 = a["bbox"][0]
            b_x0 = b["bbox"][0]
            a_y1 = a["bbox"][3]
            b_y0 = b["bbox"][1]

            same_column = abs(a_x0 - b_x0) < 30

            # Rule 1: ■ heading alone + next block starts with •
            if (a_text.lstrip().startswith("■")
                    and "•" not in a_text
                    and b_text.lstrip().startswith("•")
                    and same_column):
                issues.append({
                    "type": "section_fragmentation",
                    "severity": "warning",
                    "page": page_num,
                    "block_ids": [a["block_id"], b["block_id"]],
                    "text_preview": a_text[:40],
                })

            # Rule 2: block A ends with • line, block B starts with • (split bullets)
            if (a_text.rstrip().endswith("•") or a_text.rstrip().split("\n")[-1].lstrip().startswith("•")):
                if b_text.lstrip().startswith("•") and same_column:
                    y_gap = b_y0 - a_y1
                    if y_gap < 30:
                        # Avoid duplicate if already reported by Rule 1
                        already = any(
                            iss["block_ids"] == [a["block_id"], b["block_id"]]
                            for iss in issues
                        )
                        if not already:
                            issues.append({
                                "type": "section_fragmentation",
                                "severity": "warning",
                                "page": page_num,
                                "block_ids": [a["block_id"], b["block_id"]],
                                "text_preview": a_text[:40],
                            })

    return {
        "check_result": "fail" if issues else "pass",
        "details": {"issues": issues},
    }


# ---------------------------------------------------------------------------
# Translation QA checks (from qa_agent)
# ---------------------------------------------------------------------------

_TRIVIAL_RE = re.compile(r'^[\d\s.,;:!?()[\]/%+\-=\\\'\"]*$')
_ACRONYM_DEF_RE = re.compile(r'^[A-Z]{2,}[0-9A-Z]*[\s\n]*[\(:]')
# Matches strings that contain ONLY ASCII-range characters (letters, digits,
# punctuation, spaces).  No CJK, kana, hangul, or other non-ASCII scripts.
_PURE_ASCII_RE = re.compile(r'^[\x20-\x7E\t\n\r]*$')


def _is_trivially_invariant(text: str) -> bool:
    """Return True if text is composed only of numbers/punctuation/symbols."""
    return bool(_TRIVIAL_RE.match(text))


def _is_acronym_definition(text: str) -> bool:
    """True if text looks like an acronym definition line (DDOD: Data-Driven …)."""
    return bool(_ACRONYM_DEF_RE.match(text.strip()))


def _is_pure_ascii(text: str) -> bool:
    """True if text contains only ASCII printable chars, tabs, newlines.

    Pure-ASCII blocks (product names like 'HONDA', abbreviations like 'API',
    technical terms like 'Wi-Fi') are legitimately kept unchanged during
    translation.  Flagging them as 'unchanged_translation' is a false positive.

    Returns False if the text contains ANY CJK, kana, hangul, or other
    non-ASCII characters — those blocks should still be checked.
    """
    return bool(_PURE_ASCII_RE.match(text))


def _weighted_len(text: str) -> float:
    """
    Information-density-weighted character count.
    CJK characters = 2.0 units each; everything else = 1.0 unit.
    """
    total = 0.0
    for ch in text:
        cp = ord(ch)
        if (
            0x3000 <= cp <= 0x9FFF
            or 0xAC00 <= cp <= 0xD7AF
            or 0xF900 <= cp <= 0xFAFF
            or 0x20000 <= cp <= 0x2FA1F
        ):
            total += 2.0
        else:
            total += 1.0
    return total


def _check_translation_block(page_num: int, block_id: str, block: dict) -> list[dict]:
    """Return a list of translation issue dicts for a single block."""
    issues = []
    text       = (block.get("text") or "").strip()
    translated = (block.get("translated") or "").strip()

    if not text:
        return issues

    if not translated:
        issues.append({
            "page":       page_num,
            "block_id":   block_id,
            "type":       "missing_translation",
            "severity":   "critical",
            "text":       text,
            "translated": translated,
        })
        return issues

    if (
        translated == text
        and not _is_trivially_invariant(text)
        and not _is_acronym_definition(text)
        and not _is_pure_ascii(text)
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

    if translated.endswith("…") and not text.endswith("…"):
        issues.append({
            "page":       page_num,
            "block_id":   block_id,
            "type":       "likely_truncated",
            "severity":   "warning",
            "text":       text,
            "translated": translated,
        })

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


def coverage_check(translated_json_path: str) -> dict:
    """
    Check translation coverage and quality from translated.json.
    Returns a result dict compatible with the issue_results framework.
    """
    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
    else:
        return {"check_result": "fail", "details": [{"error": f"Unexpected JSON structure"}]}

    total_blocks      = 0
    translated_blocks = 0
    all_issues: list[dict] = []
    per_page_stats: list[dict] = []

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks   = page_entry.get("blocks", [])

        page_total      = 0
        page_translated = 0
        page_issues: list[dict] = []

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            text = (block.get("text") or "").strip()
            if not text:
                continue

            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))
            page_total += 1
            translated = (block.get("translated") or "").strip()
            if translated:
                page_translated += 1

            page_issues.extend(_check_translation_block(page_num, block_id, block))

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
        if total_blocks > 0 else 0.0
    )
    passed = coverage_pct >= 95

    retry_candidates = [iss["block_id"] for iss in all_issues if iss.get("severity") == "critical"]
    confidence = 1.0 if passed else round(coverage_pct / 100, 4)

    summary = {
        "total_blocks":      total_blocks,
        "translated_blocks": translated_blocks,
        "coverage_pct":      coverage_pct,
        "issue_count":       len(all_issues),
        "pass":              passed,
        "per_page":          per_page_stats,
    }

    return {
        "check_result": "pass" if passed else "fail",
        "details": {
            "summary":          summary,
            "translation_issues": all_issues,
            "self_eval": {
                "retry_candidates": retry_candidates,
                "confidence":       confidence,
            },
        },
    }


def quality_check(translated_json_path: str) -> dict:
    """
    Quality check: flag blocks with warning-level translation issues.
    Returns a result dict compatible with the issue_results framework.
    """
    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
    else:
        return {"check_result": "fail", "details": [{"error": "Unexpected JSON structure"}]}

    warnings: list[dict] = []

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks   = page_entry.get("blocks", [])

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            text = (block.get("text") or "").strip()
            if not text:
                continue
            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))
            for iss in _check_translation_block(page_num, block_id, block):
                if iss.get("severity") == "warning":
                    warnings.append(iss)

    if warnings:
        return {"check_result": "fail", "details": warnings}
    return {"check_result": "pass", "details": []}


def _find_claude_cli() -> str:
    """Locate the claude CLI binary."""
    cli = shutil.which("claude")
    if cli:
        return cli
    fallback = os.path.expanduser("~/.local/bin/claude")
    if os.path.isfile(fallback):
        return fallback
    raise FileNotFoundError(
        "claude CLI not found. Install it or ensure it is on PATH."
    )


def style_check(translated_json_path: str) -> dict:
    """
    Style check: use Claude CLI to evaluate translation style consistency.
    Checks tone consistency, terminology consistency, and sentence-ending style.
    Returns a result dict compatible with the issue_results framework.
    """
    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
        target_lang = "unknown"
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
        target_lang = data.get("target_lang", "unknown")
    else:
        return {"check_result": "fail", "details": [{"error": "Unexpected JSON structure"}]}

    # Collect translated text lines grouped by page
    page_lines: list[tuple[int, str]] = []
    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        for block in page_entry.get("blocks", []):
            if not isinstance(block, dict):
                continue
            translated = (block.get("translated") or "").strip()
            if translated:
                page_lines.append((page_num, translated))

    if not page_lines:
        return {"check_result": "pass", "details": {"style_issues": []}}

    # Sampling for large documents: >50 pages -> first 30 + last 10
    all_page_nums = sorted(set(pn for pn, _ in page_lines))
    if len(all_page_nums) > 50:
        keep_pages = set(all_page_nums[:30] + all_page_nums[-10:])
        page_lines = [(pn, t) for pn, t in page_lines if pn in keep_pages]

    # Build the text payload
    text_payload = "\n".join(f"[P{pn}] {t}" for pn, t in page_lines)

    prompt = (
        "你是一位资深本地化质量审查专家。请检查以下翻译文档的语言风格一致性。\n"
        f"目标语言：{target_lang}\n\n"
        "## 检查维度\n\n"
        "1. **语气一致性（tone）**：整篇文档是否保持统一的语气风格"
        '（例如日文的敬体/常体混用、中文的"您/你"混用）\n'
        "2. **术语一致性（terminology）**：同一英文术语在不同位置是否翻译一致"
        "（例如同一个词有时译为A有时译为B）\n"
        "3. **句尾风格（ending）**：句尾表达是否统一"
        "（例如日文的「です/ます」与「だ/である」混用）\n\n"
        "## 输出格式\n"
        "仅返回 JSON，禁止输出任何说明文字、注释或 markdown 代码块。\n"
        "格式如下：\n"
        '{"style_issues": [\n'
        '  {"page": <int>, "type": "<tone|terminology|ending>", '
        '"severity": "<error|warning>", '
        '"description": "<问题描述>", "examples": ["<示例>"]}\n'
        "]}\n\n"
        '如果没有发现问题，返回 {"style_issues": []}\n'
        "severity 判定标准：\n"
        "- error: 同一文档中出现明显矛盾的风格混用（如正式/非正式交替），严重影响阅读体验\n"
        "- warning: 轻微的风格不统一，不影响理解但可以改进\n\n"
        "## 文档内容\n"
        f"{text_payload}"
    )

    try:
        claude_cli = _find_claude_cli()
        # Run from /tmp to avoid project-level hooks/CLAUDE.md polluting stdout
        result = subprocess.run(
            [claude_cli, "-p", prompt],
            capture_output=True,
            text=True,
            timeout=180,
            cwd="/tmp",
        )
        raw = result.stdout.strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        style_result = json.loads(raw)
    except FileNotFoundError:
        return {
            "check_result": "skipped",
            "reason": "claude CLI not found",
        }
    except subprocess.TimeoutExpired:
        return {
            "check_result": "skipped",
            "reason": "claude CLI timed out",
        }
    except (json.JSONDecodeError, Exception) as e:
        return {
            "check_result": "fail",
            "details": {"error": f"Failed to parse LLM response: {e}", "raw": raw[:500]},
        }

    issues = style_result.get("style_issues", [])
    has_errors = any(iss.get("severity") == "error" for iss in issues)

    return {
        "check_result": "fail" if has_errors else "pass",
        "details": style_result,
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

        elif check_name in ("linebreak_consistency_check", "mixed_language_check", "fragmentation_check"):
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
# Thumbnail rendering (from qa_agent)
# ---------------------------------------------------------------------------

def render_thumbnails(pdf_path: str, thumbs_dir: str, dpi: int = 80) -> None:
    """Render each page of pdf_path as a PNG thumbnail into thumbs_dir."""
    Path(thumbs_dir).mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    zoom   = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    for page_num in range(len(doc)):
        page = doc[page_num]
        pix  = page.get_pixmap(matrix=matrix)
        out_path = os.path.join(thumbs_dir, f"page_{page_num + 1:04d}.png")
        pix.save(out_path)

    doc.close()
    print(green(f"  Thumbnails written to: {thumbs_dir}  ({len(doc)} pages)"))


# ---------------------------------------------------------------------------
# Main runner (testcase mode)
# ---------------------------------------------------------------------------

def run_checks(testcase: str, registry_path: Path, output_path: Path,
               translated_json_path: Path = None, output_pdf_path: Path = None):
    registry = load_json(registry_path)

    if translated_json_path is None:
        testcase_dir = PROJECT_ROOT / "testdata" / testcase
        output_pdf_path = testcase_dir / "output.pdf"
        translated_json_path = testcase_dir / "work" / "translated.json"

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
    rd_result = readability_check(str(translated_json_path), str(output_pdf_path))
    summary["total"] += 1
    if rd_result["check_result"] == "pass":
        summary["passed"] += 1
    else:
        summary["failed"] += 1
    print(f"  readability_check: {rd_result['check_result'].upper()}")

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

def run_pipeline_qa(translated_json: str, pdf_path: str, output: str, thumbs: str = None):
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
    if pdf_path:
        print("Running readability_check...")
        rd_result = readability_check(translated_json, pdf_path)
        print(f"  readability_check: {rd_result['check_result'].upper()}")

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

    all_checks = [cov_result, qual_result, style_result, tc_result, lb_result, ml_result, term_result, frag_result, rd_result]
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

    return {
        "testcase_dir": testcase_dir,
        "work_dir": work_dir,
        "output_pdf": testcase_dir / "output.pdf",
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
    parser.add_argument("--thumbs", default=None, help="Directory for page thumbnails")
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
        run_pipeline_qa(args.json, args.pdf, output, args.thumbs)
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
