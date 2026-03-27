"""
qa_utils.py — Shared utilities for QA checks.
Console helpers, JSON IO, geometry, PDF extraction, span matching, rendering, text metrics.
Imported by: test_agent, qa_readability, qa_llm, qa_regression
"""
import base64
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

try:
    import fitz
except ImportError:
    print("ERROR: PyMuPDF not installed.", file=sys.stderr)
    sys.exit(1)

PROJECT_ROOT = Path(__file__).parent.parent


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
    for each text LINE in the PDF.  Only includes lines with non-empty text
    and area >= 100 px².

    Using line-level bboxes avoids false positives from PyMuPDF merging
    spatially distant lines (different physical columns) into one block
    when they share the same font/size/color.
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
            for line in block.get("lines", []):
                has_text = any(
                    span.get("text", "").strip()
                    for span in line.get("spans", [])
                )
                if not has_text:
                    continue
                bbox = line.get("bbox")
                if not bbox:
                    continue
                x0, y0, x1, y1 = bbox
                if (x1 - x0) * (y1 - y0) < 100:
                    continue
                bboxes.append(list(bbox))
        result[page_num] = bboxes
    doc.close()
    return result


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
        # Require x-range overlap (with tolerance)
        if bx1 < tx0 - tolerance or bx0 > tx1 + tolerance:
            continue
        dx0 = abs(bx0 - tx0)
        dy = abs(bcy - tcy)
        candidates.append((dy, dx0, span))

    if not candidates:
        return None

    candidates.sort(key=lambda t: (t[0], t[1]))
    return candidates[0][2]


def _collect_spans_in_bbox(spans, bbox, tolerance=5.0):
    """Collect all PDF spans whose bbox overlaps with the target bbox."""
    tx0, ty0, tx1, ty1 = bbox
    return [
        span for span in spans
        if not (span["bbox"][3] < ty0 - tolerance
                or span["bbox"][1] > ty1 + tolerance
                or span["bbox"][2] < tx0 - tolerance
                or span["bbox"][0] > tx1 + tolerance)
    ]


def _render_page_to_png(pdf_path: str, page_num_0based: int, dpi: int = 300) -> str:
    """Render a single PDF page to a temporary PNG file. Returns the temp file path."""
    doc = fitz.open(pdf_path)
    page = doc[page_num_0based]
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat)
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    pix.save(tmp.name)
    doc.close()
    return tmp.name


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
