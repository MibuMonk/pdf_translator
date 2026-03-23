#!/usr/bin/env python3
"""
Review Agent
Renders each page of a translated PDF to an image, then sends batches to
Claude CLI for subjective visual review from a target-reader perspective.
Outputs review_report.json.
"""

import argparse
import base64
import json
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("PyMuPDF (fitz) is required. Install with: pip install PyMuPDF")

try:
    import anthropic
except ImportError:
    sys.exit("anthropic SDK is required. Install with: pip install anthropic")


# ---------------------------------------------------------------------------
# Prompts per target language
# ---------------------------------------------------------------------------

_ROLE_PROMPTS = {
    "zh": (
        "你是一位日本汽车制造商的中层管理者（課長），正在审阅一份从日文/英文翻译成中文的技术资料。"
        "请从读者的角度逐页评估。"
    ),
    "ja": (
        "あなたは日本の自動車メーカーの課長です。"
        "英語から日本語に翻訳された技術資料を初めて見る立場で評価してください。"
    ),
}

_REVIEW_INSTRUCTION = """\
以下の画像は翻訳済み資料の各ページです。ページごとに以下の観点で評価してください。

## 評価観点
1. **可読性 (readability)**: 文字が小さすぎる、ぼやけている、枠からはみ出している、切れている
2. **視覚的完全性 (visual)**: テキストが消えている、本来テキストがあるべき空白領域
3. **色 (color)**: テキストの色が不自然、背景とのコントラスト不足
4. **レイアウト (layout)**: 配置、間隔、全体的なプロフェッショナル感
5. **翻訳品質 (translation)**: 内容が理解できるか、未翻訳の内容が混在していないか
6. **一貫性 (consistency)**: 類似ページ間でスタイルが統一されているか

## 出力形式
以下のJSON形式のみ出力してください。説明文やmarkdownコードブロックは不要です。

{
  "pages": [
    {
      "page": <ページ番号>,
      "score": <1-10の整数>,
      "issues": [
        {
          "type": "readability|visual|color|layout|translation|consistency",
          "severity": "error|warning",
          "description": "<具体的な問題の説明>",
          "location": "<ページ内のおおよその位置>"
        }
      ]
    }
  ]
}

問題がないページもscoreを付けてissuesを空配列で出力してください。
"""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_pages(pdf_path: str, tmpdir: str, pages=None,
                 dpi: int = 150) -> list[tuple[int, str]]:
    """Render selected pages to PNG. Returns list of (page_number, image_path)."""
    doc = fitz.open(pdf_path)
    total = doc.page_count
    if pages is None:
        pages = list(range(total))
    else:
        pages = [p for p in pages if 0 <= p < total]

    results = []
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    for pno in pages:
        page = doc[pno]
        pix = page.get_pixmap(matrix=mat)
        img_path = os.path.join(tmpdir, f"page_{pno + 1:04d}.png")
        pix.save(img_path)
        results.append((pno + 1, img_path))  # 1-based page number
    doc.close()
    return results


# ---------------------------------------------------------------------------
# LLM batch call
# ---------------------------------------------------------------------------

def _parse_llm_json(raw: str) -> dict:
    """Extract JSON from LLM output, tolerating markdown fences."""
    raw = raw.strip()
    # Try to extract ```json ... ``` block first
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    if m:
        raw = m.group(1)
    else:
        # Strip leading/trailing fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def review_batch(client: anthropic.Anthropic, batch: list[tuple[int, str]],
                 role_prompt: str) -> list[dict]:
    """Send a batch of page images to Anthropic API for review."""
    page_list = ", ".join(str(pno) for pno, _ in batch)
    review_prompt = (
        f"{role_prompt}\n\n"
        f"これからページ {page_list} の画像を送ります。\n\n"
        f"{_REVIEW_INSTRUCTION}"
    )

    # Build content with images and per-page labels
    content = []
    for page_num, img_path in batch:
        with open(img_path, "rb") as f:
            img_data = base64.standard_b64encode(f.read()).decode("utf-8")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": img_data}
        })
        content.append({"type": "text", "text": f"Above is page {page_num}."})

    content.append({"type": "text", "text": review_prompt})

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            messages=[{"role": "user", "content": content}],
        )
        raw_text = response.content[0].text
        parsed = _parse_llm_json(raw_text)
        return parsed.get("pages", [])

    except anthropic.APIError as e:
        print(f"  [warn] Anthropic API error for pages {page_list}: {e}", flush=True)
        return _fallback_pages(batch)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  [warn] failed to parse LLM output for pages {page_list}: {e}",
              flush=True)
        return _fallback_pages(batch)


def _fallback_pages(batch: list[tuple[int, str]]) -> list[dict]:
    """Return placeholder entries when LLM call fails."""
    return [
        {"page": pno, "score": 0, "issues": [
            {"type": "visual", "severity": "error",
             "description": "Review failed — LLM call did not succeed",
             "location": "entire page"}
        ]}
        for pno, _ in batch
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_page_range(spec: str, total: int) -> list[int]:
    """Parse page range spec like '1-5,8,10-12' into 0-based page indices."""
    pages = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            for p in range(int(a), int(b) + 1):
                if 1 <= p <= total:
                    pages.add(p - 1)
        else:
            p = int(part)
            if 1 <= p <= total:
                pages.add(p - 1)
    return sorted(pages)


def main():
    parser = argparse.ArgumentParser(description="Visual review of translated PDF")
    parser.add_argument("--pdf", required=True, help="Path to translated output PDF")
    parser.add_argument("--output", required=True, help="Path to write review_report.json")
    parser.add_argument("--tgt", default="zh", help="Target language (default: zh)")
    parser.add_argument("--pages", default=None, help="Page range, e.g. '1-5,8'")
    args = parser.parse_args()

    # Validate inputs
    if not os.path.isfile(args.pdf):
        sys.exit(f"PDF not found: {args.pdf}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("[error] ANTHROPIC_API_KEY environment variable is not set.")

    client = anthropic.Anthropic()

    # Determine page set
    doc = fitz.open(args.pdf)
    total_pages = doc.page_count
    doc.close()

    page_indices = None  # means all
    if args.pages:
        page_indices = parse_page_range(args.pages, total_pages)

    role_prompt = _ROLE_PROMPTS.get(args.tgt, _ROLE_PROMPTS["zh"])

    print(f"[review_agent] Reviewing {args.pdf} ({total_pages} pages, tgt={args.tgt})",
          flush=True)

    # Render to temp images
    with tempfile.TemporaryDirectory(prefix="review_agent_") as tmpdir:
        rendered = render_pages(args.pdf, tmpdir, page_indices)
        print(f"[review_agent] Rendered {len(rendered)} pages to images", flush=True)

        # Process in batches of 5
        batch_size = 5
        all_page_results = []
        for i in range(0, len(rendered), batch_size):
            batch = rendered[i:i + batch_size]
            page_nums = [pno for pno, _ in batch]
            print(f"[review_agent] Reviewing pages {page_nums} ...", flush=True)
            batch_results = review_batch(client, batch, role_prompt)
            all_page_results.extend(batch_results)

    # Sort by page number
    all_page_results.sort(key=lambda x: x.get("page", 0))

    # Compute summary
    scores = [p.get("score", 0) for p in all_page_results]
    all_issues = []
    for p in all_page_results:
        all_issues.extend(p.get("issues", []))
    errors = sum(1 for i in all_issues if i.get("severity") == "error")
    warnings = sum(1 for i in all_issues if i.get("severity") == "warning")

    report = {
        "version": "1.0",
        "run_date": date.today().isoformat(),
        "reviewer_role": "日本自動車メーカー課長",
        "target_lang": args.tgt,
        "total_pages": total_pages,
        "summary": {
            "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
            "total_issues": len(all_issues),
            "errors": errors,
            "warnings": warnings,
        },
        "pages": all_page_results,
    }

    # Write output
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[review_agent] Report written to {args.output}", flush=True)
    print(f"[review_agent] Summary: avg_score={report['summary']['avg_score']}, "
          f"errors={errors}, warnings={warnings}", flush=True)


if __name__ == "__main__":
    main()
