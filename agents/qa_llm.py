"""
qa_llm.py — LLM-powered quality checks via Claude API.
Covers: style consistency (tone, terminology), visual layout review.
Imported by: test_agent
"""
import base64
import os
import sys
import tempfile
from pathlib import Path

try:
    import fitz
except ImportError:
    print("ERROR: PyMuPDF not installed.", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from qa_utils import _render_page_to_png  # noqa: E402

try:
    import anthropic
except ImportError:
    anthropic = None


def _make_client():
    import anthropic as _anthropic
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if auth_token:
        return _anthropic.Anthropic(auth_token=auth_token, base_url=base_url)
    return _anthropic.Anthropic(api_key=api_key, base_url=base_url)


def _get_model() -> str:
    return os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6")


def style_check(translated_json_path: str) -> dict:
    """
    Style check: use Claude CLI to evaluate translation style consistency.
    Checks tone consistency, terminology consistency, and sentence-ending style.
    Returns a result dict compatible with the issue_results framework.
    """
    import json
    import re

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

    # Sampling: cap to at most 30 pages spread across the document
    # (first 15, last 10, and 5 from middle) to avoid exceeding LLM output limits
    all_page_nums = sorted(set(pn for pn, _ in page_lines))
    if len(all_page_nums) > 30:
        n = len(all_page_nums)
        mid_pages = all_page_nums[n // 3: n // 3 + 5]
        keep_pages = set(all_page_nums[:15] + mid_pages + all_page_nums[-10:])
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
        client = _make_client()
        message = client.messages.create(
            model=_get_model(),
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        # Attempt to close truncated JSON (if LLM output was cut off)
        if not raw.rstrip().endswith("}"):
            raw = raw.rstrip().rstrip(",") + "\n]}"
        style_result = json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        return {
            "check_result": "fail",
            "details": {"error": f"Failed to parse LLM response: {e}", "raw": locals().get('raw', '')[:500]},
        }

    issues = style_result.get("style_issues", [])
    has_errors = any(iss.get("severity") == "error" for iss in issues)

    return {
        "check_result": "fail" if has_errors else "pass",
        "details": style_result,
    }


# ---------------------------------------------------------------------------
# Visual review check (Claude Vision)
# ---------------------------------------------------------------------------

_VISUAL_REVIEW_PROMPT = """\
You are a PDF translation quality reviewer. Compare the source page (original) with the translated page (output).

Identify layout anomalies in the translated page by comparing with the source. Use these defect codes:
- L1 (Word Split): Words broken across lines at non-hyphenation points
- L2 (Structure Collapse): Clear visual sections in source merged into undifferentiated text mass
- L3 (Content Drift): Text positioned far from where it appears in the source
- L4 (Section Fragmentation): Heading separated from its bullet list
- L5 (Linebreak Inconsistency): Same pattern rendered with/without line breaks inconsistently
- L6 (Bbox Overlap): Text blocks overlapping each other
- T1 (Missing Translation): Source language text appearing untranslated
- T2 (Truncated Translation): Translation visibly incomplete
- T3 (Terminology Inconsistency): Same term translated differently

Respond in JSON format:
{
  "grade": "A/B/C/D/F",
  "defects": [
    {"code": "L1", "description": "...", "location": "top-left / center / etc."}
  ],
  "summary": "one-line overall assessment"
}

If the page looks good, return grade A with empty defects array.
"""

_VISUAL_REVIEW_MAX_PAGES = 10


def visual_review_check(source_pdf: str, output_pdf: str,
                        review_pages: list[int] = None,
                        on_page_result=None) -> dict:
    """
    Visual review check using Claude Vision.
    Compares source and output PDF page screenshots to detect layout anomalies.

    Args:
        source_pdf: Path to the source (original) PDF.
        output_pdf: Path to the translated output PDF.
        review_pages: List of 1-based page numbers to review.
                      If None, reviews all pages (capped at _VISUAL_REVIEW_MAX_PAGES).
        on_page_result: Optional callable(page_num, grade, page_issues) called
                        immediately after each page is graded, before moving to
                        the next page. page_issues is a list of issue dicts
                        (may be empty for grades A/B).

    Returns:
        A check result dict compatible with the issue_results framework.
    """
    import json
    import re

    # Verify PDFs exist
    if not source_pdf or not os.path.isfile(source_pdf):
        return {
            "check_result": "skipped",
            "details": {"reason": f"source PDF not found: {source_pdf}"},
        }
    if not output_pdf or not os.path.isfile(output_pdf):
        return {
            "check_result": "skipped",
            "details": {"reason": f"output PDF not found: {output_pdf}"},
        }

    # Determine pages to review
    src_doc = fitz.open(source_pdf)
    out_doc = fitz.open(output_pdf)
    src_page_count = len(src_doc)
    out_page_count = len(out_doc)
    src_doc.close()
    out_doc.close()

    explicit_pages = review_pages is not None
    if review_pages is None:
        review_pages = list(range(1, min(src_page_count, out_page_count) + 1))

    # Filter to valid pages present in both PDFs
    review_pages = [
        p for p in review_pages
        if 1 <= p <= src_page_count and 1 <= p <= out_page_count
    ]

    # Cap at max pages only when pages were auto-detected (not explicitly provided)
    if not explicit_pages and len(review_pages) > _VISUAL_REVIEW_MAX_PAGES:
        review_pages = review_pages[:_VISUAL_REVIEW_MAX_PAGES]

    if not review_pages:
        return {
            "check_result": "skipped",
            "details": {"reason": "no valid pages to review"},
        }

    all_issues = []
    page_grades = {}
    errors_seen = False

    for page_num in review_pages:
        src_png = None
        out_png = None
        try:
            # Render page screenshots
            src_png = _render_page_to_png(source_pdf, page_num - 1)
            out_png = _render_page_to_png(output_pdf, page_num - 1)

            # Call Claude Vision via SDK
            with open(src_png, "rb") as f:
                src_b64 = base64.standard_b64encode(f.read()).decode()
            with open(out_png, "rb") as f:
                out_b64 = base64.standard_b64encode(f.read()).decode()

            client = _make_client()
            message = client.messages.create(
                model=_get_model(),
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": src_b64}},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": out_b64}},
                        {"type": "text", "text": _VISUAL_REVIEW_PROMPT},
                    ],
                }],
            )
            raw = message.content[0].text.strip()
            # Strip markdown fences if present
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

            page_result = json.loads(raw)
            grade = page_result.get("grade", "A").upper()
            defects = page_result.get("defects", [])
            page_grades[page_num] = grade

            # Convert defects to issues based on grade
            if grade in ("D", "F"):
                severity = "error"
                errors_seen = True
            elif grade == "C":
                severity = "warning"
            else:
                # Grade A or B: no issues to report
                if on_page_result is not None:
                    on_page_result(page_num, grade, [])
                continue

            page_issues = []
            for defect in defects:
                code = defect.get("code", "unknown").upper()
                page_issues.append({
                    "page": page_num,
                    "type": f"visual_review_{code.lower()}",
                    "severity": severity,
                    "description": defect.get("description", ""),
                    "location": defect.get("location", ""),
                    "grade": grade,
                })
            all_issues.extend(page_issues)

            if on_page_result is not None:
                on_page_result(page_num, grade, page_issues)

        except json.JSONDecodeError as e:
            print(f"  [visual_review] Page {page_num}: Failed to parse response: {e}",
                  file=sys.stderr)
            continue
        except Exception as e:
            print(f"  [visual_review] Page {page_num}: Unexpected error: {e}",
                  file=sys.stderr)
            continue
        finally:
            # Clean up temp files
            for tmp_path in (src_png, out_png):
                if tmp_path and os.path.isfile(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

    check_result = "fail" if errors_seen else "pass"

    return {
        "check_result": check_result,
        "details": {
            "pages_reviewed": len(review_pages),
            "page_grades": page_grades,
            "issues": all_issues,
        },
    }
