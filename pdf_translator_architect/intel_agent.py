#!/usr/bin/env python3
"""
Architect Agent — pipeline workflow designer and architectural advisor.

Reads parsed.json, analyzes document structure and content characteristics,
identifies architectural constraints, then outputs plan.json that tells
run_pipeline.py how to execute the pipeline for this specific document.

Key responsibilities:
  1. Document analysis (statistical, no LLM)
  2. Architectural constraint detection (e.g. layout-translation dependencies)
  3. Workflow planning (what can parallelize, what cannot)
  4. Parameter recommendation (batch size, font strategy, render policy)
  5. Domain analysis and terminology extraction (via Claude)
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

SUPPORTED_LANGUAGES = {
    "en": "English",
    "ja": "日本語",
    "zh": "中文（简体）",
    "zh-TW": "中文（繁體）",
}

# Architectural constraint labels
CONSTRAINT_LAYOUT_DEPENDS_ON_TRANSLATION = "font_fitting_requires_translated_text"
CONSTRAINT_TOPOLOGY_PARALLELIZABLE = "topology_analysis_independent_of_text"
CONSTRAINT_DENSE_PAGE_NEEDS_PREPLAN = "dense_page_benefits_from_space_preplan"


# ---------------------------------------------------------------------------
# Statistical analysis (pure Python, no LLM)
# ---------------------------------------------------------------------------

def analyze_document(pages: list) -> dict:
    """
    Extract statistical features from parsed pages.
    Returns a doc_analysis dict that informs architectural decisions.
    """
    total_blocks = 0
    total_chars = 0
    font_sizes = []
    all_texts = []
    page_stats = []

    for page in pages:
        blocks = page.get("blocks", [])
        page_chars = sum(len(b.get("text", "")) for b in blocks)
        page_blocks = len(blocks)
        total_blocks += page_blocks
        total_chars += page_chars
        all_texts.extend(b.get("text", "") for b in blocks if b.get("text", "").strip())
        font_sizes.extend(b.get("font_size", 0) for b in blocks if b.get("font_size", 0) > 0)

        image_obstacles = page.get("image_obstacles", [])
        page_stats.append({
            "page_num": page.get("page_num", 0),
            "blocks": page_blocks,
            "chars": page_chars,
            "images": len(image_obstacles),
        })

    avg_block_len = total_chars / total_blocks if total_blocks else 0
    max_fs = max(font_sizes) if font_sizes else 12.0
    min_fs = min(font_sizes) if font_sizes else 8.0

    # Dense pages: blocks above 1.5x page average
    avg_blocks_per_page = total_blocks / len(pages) if pages else 0
    dense_pages = [
        ps["page_num"] for ps in page_stats
        if ps["blocks"] > avg_blocks_per_page * 1.5 and ps["blocks"] > 10
    ]
    image_heavy_pages = [
        ps["page_num"] for ps in page_stats if ps["images"] >= 2
    ]
    sparse_pages = [
        ps["page_num"] for ps in page_stats if ps["blocks"] <= 2
    ]

    return {
        "page_count": len(pages),
        "total_blocks": total_blocks,
        "total_chars": total_chars,
        "avg_block_len": round(avg_block_len, 1),
        "avg_blocks_per_page": round(avg_blocks_per_page, 1),
        "max_font_size": round(max_fs, 1),
        "min_font_size": round(min_fs, 1),
        "dense_pages": dense_pages,
        "image_heavy_pages": image_heavy_pages,
        "sparse_pages": sparse_pages,
        "all_texts": all_texts,  # used downstream, removed before serialization
    }


def extract_candidate_terms(all_texts: list) -> list:
    """Extract candidate technical terms: acronyms and CamelCase phrases."""
    COMMON_SKIP = {"PDF", "PNG", "JSON", "API", "URL", "HTTP", "HTTPS",
                   "CJK", "UTF", "RGB", "QA", "ROI", "KPI", "CEO", "CFO"}
    acronym_re = re.compile(r'\b([A-Z]{2,8})\b')
    camel_re = re.compile(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b')

    counts: Counter = Counter()
    for text in all_texts:
        for m in acronym_re.findall(text):
            if m not in COMMON_SKIP:
                counts[m] += 1
        for m in camel_re.findall(text):
            counts[m] += 1

    return [{"term": t, "count": c} for t, c in counts.most_common(25)]


# ---------------------------------------------------------------------------
# Architectural constraint detection
# ---------------------------------------------------------------------------

def detect_constraints(doc_analysis: dict) -> list:
    """
    Identify architectural constraints based on document characteristics.
    Returns a list of constraint dicts with rationale.
    """
    constraints = []

    # Font fitting always requires translated text — hard constraint
    constraints.append({
        "id": CONSTRAINT_LAYOUT_DEPENDS_ON_TRANSLATION,
        "severity": "hard",
        "description": (
            "Font-size fitting and text truncation require actual translated text. "
            "Full layout rendering cannot be parallelized with translation."
        ),
        "resolution": (
            "Split layout into two phases: "
            "(1) space_planner runs in parallel with translate — computes topology, "
            "Voronoi cells, image obstacles, snap maps; "
            "(2) render_agent runs sequentially after both complete — applies "
            "font fitting and final rendering."
        ),
    })

    # Topology analysis is text-independent — parallelizable
    constraints.append({
        "id": CONSTRAINT_TOPOLOGY_PARALLELIZABLE,
        "severity": "opportunity",
        "description": (
            "Topology analysis (Voronoi space allocation, image obstacle detection, "
            "Y-axis snap clustering, title detection) depends only on original PDF "
            "geometry, not on translated text content."
        ),
        "resolution": "Run space_planner in parallel with translate_agent.",
    })

    # Dense pages benefit from pre-planned space allocation
    if doc_analysis["dense_pages"]:
        constraints.append({
            "id": CONSTRAINT_DENSE_PAGE_NEEDS_PREPLAN,
            "severity": "recommendation",
            "description": (
                f"Pages {doc_analysis['dense_pages']} have significantly more blocks "
                f"than average ({doc_analysis['avg_blocks_per_page']:.1f}). "
                "Space contention is likely without pre-planned allocation."
            ),
            "resolution": (
                "space_planner should generate expanded bboxes for dense pages. "
                "render_agent should apply stricter font-shrink limits on these pages."
            ),
        })

    return constraints


def recommend_workflow(doc_analysis: dict, constraints: list) -> dict:
    """
    Decide the execution plan based on constraints and document size.
    """
    avg_len = doc_analysis["avg_block_len"]
    total_blocks = doc_analysis["total_blocks"]

    # Batch size: smaller batches for longer texts (more tokens per call)
    if avg_len > 200:
        batch_size = 10
    elif avg_len > 100:
        batch_size = 20
    elif avg_len > 50:
        batch_size = 30
    else:
        batch_size = 40

    # Space planner is always worth running if dense pages exist or doc is large
    space_plan_needed = bool(doc_analysis["dense_pages"]) or total_blocks > 100

    return {
        "steps": [
            {"name": "parse",        "parallel_group": None},
            {"name": "translate",    "parallel_group": "A"},
            {"name": "space_plan",   "parallel_group": "A"},
            {"name": "render",       "parallel_group": None},
            {"name": "qa",           "parallel_group": None},
        ],
        "parallel_groups": {
            "A": {
                "members": ["translate", "space_plan"],
                "constraint": CONSTRAINT_LAYOUT_DEPENDS_ON_TRANSLATION,
                "note": "Both read parsed.json independently; render waits for both.",
            }
        },
        "space_plan_needed": space_plan_needed,
        "recommended_batch_size": batch_size,
        "render_strategy": {
            "truncation_policy": "shrink_font_first",
            "min_font_size": 6.0,
            "conflict_resolution": "translation_takes_priority",
            "dense_page_font_floor": 7.0,
        },
    }


# ---------------------------------------------------------------------------
# Claude: domain analysis and terminology
# ---------------------------------------------------------------------------

def find_claude_cli() -> str:
    cli = shutil.which("claude")
    if cli:
        return cli
    fallback = Path.home() / ".local/bin/claude"
    if fallback.is_file():
        return str(fallback)
    raise FileNotFoundError("claude CLI not found. Install it or add it to PATH.")


def call_claude_domain_analysis(
    sample_texts: list,
    candidate_terms: list,
    src_name: str,
    tgt_name: str,
    claude_cli: str,
) -> dict:
    """
    Ask Claude to identify document domain, type, and translation strategy.
    Returns a domain_analysis dict.
    """
    sample = "\n---\n".join(sample_texts[:50])[:3000]
    terms_str = ", ".join(t["term"] for t in candidate_terms[:20])

    prompt = (
        f"你是一位专业的演示文稿翻译策略师。请分析以下幻灯片文本样本，输出翻译架构建议。\n\n"
        f"翻译方向：{src_name} → {tgt_name}\n"
        f"候选术语（按频率排序）：{terms_str or '（未检测到）'}\n\n"
        f"## 文本样本\n{sample}\n\n"
        f"## 输出要求\n"
        f"仅返回以下结构的纯 JSON，无 markdown 代码块：\n"
        f'{{\n'
        f'  "doc_type": "文档类型（如：技术演示/商业提案/培训材料/产品介绍）",\n'
        f'  "domain": "专业领域（如：汽车工程/软件开发/金融/制造业）",\n'
        f'  "complexity": "low/medium/high",\n'
        f'  "style_guide": "针对本文档的翻译风格指导，1-2句话",\n'
        f'  "key_terms": [\n'
        f'    {{"term": "原文术语", "translation": "建议译法或空字符串", "keep_original": true/false}}\n'
        f'  ],\n'
        f'  "architectural_notes": "翻译时需要注意的文档结构特征（如有）"\n'
        f'}}'
    )

    try:
        result = subprocess.run(
            [claude_cli, "-p", prompt],
            capture_output=True, text=True, timeout=120,
        )
        raw = result.stdout.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except subprocess.TimeoutExpired:
        print("  [architect] Domain analysis timed out — using defaults.", flush=True)
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  [architect] Domain analysis parse error ({e}) — using defaults.", flush=True)
    except Exception as e:
        print(f"  [architect] Domain analysis failed ({e}) — using defaults.", flush=True)

    return {
        "doc_type": "演示文稿",
        "domain": "general",
        "complexity": "medium",
        "style_guide": "简洁自然，保持幻灯片风格，专业术语准确。",
        "key_terms": [],
        "architectural_notes": "",
    }


def build_translation_context(domain: dict) -> str:
    """Compile a context string for translate_agent from domain analysis."""
    lines = []
    if domain.get("style_guide"):
        lines.append(f"翻译风格：{domain['style_guide']}")
    if domain.get("domain") and domain["domain"] != "general":
        lines.append(f"文档领域：{domain['domain']}")
    key_terms = domain.get("key_terms", [])
    if key_terms:
        lines.append("\n## 术语对照表")
        for kt in key_terms:
            term = kt.get("term", "")
            trans = kt.get("translation", "")
            keep = kt.get("keep_original", False)
            if not term:
                continue
            if keep:
                lines.append(f"  - {term}：保持原文，不翻译")
            elif trans:
                lines.append(f"  - {term} → {trans}")
    if domain.get("architectural_notes"):
        lines.append(f"\n## 结构说明\n{domain['architectural_notes']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Architect Agent: analyze document and design pipeline workflow."
    )
    parser.add_argument("--input",  required=True, help="Path to parsed.json")
    parser.add_argument("--output", default=None,  help="Output plan.json path")
    parser.add_argument("--src",    default="en",  help="Source language code (default: en)")
    parser.add_argument("--tgt",    default="ja",  help="Target language code (default: ja)")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip Claude domain analysis (use statistical analysis only)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[architect] ERROR: {input_path} not found", file=sys.stderr)
        sys.exit(1)

    output_path = (
        Path(args.output) if args.output
        else input_path.parent / "plan.json"
    )

    src_name = SUPPORTED_LANGUAGES.get(args.src, args.src)
    tgt_name = SUPPORTED_LANGUAGES.get(args.tgt, args.tgt)

    print(f"[architect] Analyzing: {input_path.name}")

    # Load parsed.json
    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)
    pages = data.get("pages", data) if isinstance(data, dict) else data

    # --- Phase 1: Statistical analysis ---
    print("[architect] Phase 1: statistical analysis...", flush=True)
    doc_analysis = analyze_document(pages)
    all_texts = doc_analysis.pop("all_texts")  # separate from serializable data

    candidate_terms = extract_candidate_terms(all_texts)
    print(
        f"  pages={doc_analysis['page_count']}  "
        f"blocks={doc_analysis['total_blocks']}  "
        f"avg_len={doc_analysis['avg_block_len']}  "
        f"dense_pages={doc_analysis['dense_pages']}"
    )

    # --- Phase 2: Constraint detection ---
    print("[architect] Phase 2: constraint detection...", flush=True)
    constraints = detect_constraints(doc_analysis)
    for c in constraints:
        tag = {"hard": "⛔", "opportunity": "✅", "recommendation": "💡"}.get(c["severity"], "•")
        print(f"  {tag} [{c['severity'].upper()}] {c['id']}")

    # --- Phase 3: Workflow planning ---
    print("[architect] Phase 3: workflow planning...", flush=True)
    workflow = recommend_workflow(doc_analysis, constraints)
    print(
        f"  parallel_group_A: {workflow['parallel_groups']['A']['members']}  "
        f"batch_size={workflow['recommended_batch_size']}"
    )

    # --- Phase 4: Domain analysis via Claude ---
    domain_analysis: dict = {}
    if not args.no_llm:
        print("[architect] Phase 4: domain analysis via Claude...", flush=True)
        try:
            claude_cli = find_claude_cli()
            domain_analysis = call_claude_domain_analysis(
                all_texts, candidate_terms, src_name, tgt_name, claude_cli
            )
            print(
                f"  doc_type={domain_analysis.get('doc_type')}  "
                f"domain={domain_analysis.get('domain')}  "
                f"complexity={domain_analysis.get('complexity')}"
            )
        except FileNotFoundError as e:
            print(f"  [warn] {e} — skipping LLM phase.", flush=True)
    else:
        print("[architect] Phase 4: skipped (--no-llm).", flush=True)

    translation_context = build_translation_context(domain_analysis)

    # --- Assemble plan ---
    plan = {
        "version": "1.0",
        "src": args.src,
        "tgt": args.tgt,
        "doc_analysis": doc_analysis,
        "candidate_terms": candidate_terms,
        "constraints": constraints,
        "workflow": workflow,
        "domain": {
            "doc_type":           domain_analysis.get("doc_type", "演示文稿"),
            "domain":             domain_analysis.get("domain", "general"),
            "complexity":         domain_analysis.get("complexity", "medium"),
            "style_guide":        domain_analysis.get("style_guide", ""),
            "key_terms":          domain_analysis.get("key_terms", []),
            "architectural_notes": domain_analysis.get("architectural_notes", ""),
        },
        "translation_context": translation_context,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    print(f"\n[architect] Plan written to: {output_path}")
    print(f"  Workflow: parse → [{' ∥ '.join(workflow['parallel_groups']['A']['members'])}] → render → qa")
    print(f"  Batch size: {workflow['recommended_batch_size']}")
    if domain_analysis.get("key_terms"):
        print(f"  Key terms: {len(domain_analysis['key_terms'])} identified")
    print()


if __name__ == "__main__":
    main()
