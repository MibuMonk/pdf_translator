#!/usr/bin/env python3
"""
Translate Agent
Reads parsed.json, translates each block's text field using Claude CLI,
writes translated.json with an added `translated` field per block.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Allow importing contracts/validate.py from any working directory
_CONTRACTS_DIR = Path(__file__).parent.parent / "contracts"
if str(_CONTRACTS_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTRACTS_DIR.parent))

SUPPORTED_LANGUAGES = {
    "en": "English",
    "ja": "日本語",
    "zh": "中文（简体）",
    "zh-TW": "中文（繁體）",
}


def find_claude_cli() -> str:
    cli = shutil.which("claude")
    if cli:
        return cli
    fallback = os.path.expanduser("~/.local/bin/claude")
    if os.path.isfile(fallback):
        return fallback
    raise FileNotFoundError(
        "claude CLI not found. Install it or ensure it is on PATH."
    )


def _is_trivially_invariant(text: str) -> bool:
    if not text.strip():
        return True
    return bool(re.match(r'^[\d\s\.\,\:\;\!\?\-\+\=\%\(\)\[\]\/\\\"\' ]*$', text))


def _needs_translation(text: str) -> bool:
    """Return True if text contains translatable natural-language words.

    Used to detect blocks where the LLM returned source text unchanged.
    A text "needs translation" if it has alphabetic words beyond pure
    acronyms/model-numbers — i.e. contains lowercase letters or is longer
    than a short identifier.
    """
    stripped = text.strip()
    if not stripped or _is_trivially_invariant(stripped):
        return False
    # Contains at least some lowercase letters → has real words to translate
    if re.search(r'[a-z]', stripped):
        return True
    # All-uppercase but long with spaces → likely a title/phrase, not just "KPI"
    if len(stripped) > 10 and ' ' in stripped:
        return True
    return False


# ---------------------------------------------------------------------------
# Span-aware translation helpers
# ---------------------------------------------------------------------------

_SPAN_TAG_RE = re.compile(r'<s(\d+)>(.*?)</s\1>', re.DOTALL)

# Newline preservation: use a placeholder that the LLM will pass through unchanged.
# This avoids relying on the LLM to correctly reproduce \n in its JSON output.
_NEWLINE_PLACEHOLDER = "⏎"

# Bullet / list markers that indicate a semantic line break (not layout wrapping)
_BULLET_RE = re.compile(
    r'^[\s]*'           # optional leading whitespace
    r'(?:'
    r'[•■\-–·\*▶▷►▸◆◇○●]'   # common bullet characters
    r'|\d+[.\)）]'              # numbered list: 1. 2) 3）
    r')'
)


def _clean_layout_breaks(text: str) -> str:
    """Replace layout-wrapping newlines with spaces, preserving semantic breaks.

    PDF text often contains hard newlines inserted by the layout engine to fit
    column widths.  These are *not* semantic paragraph breaks and should be
    collapsed before translation so the LLM sees fluent sentences.

    A newline is considered **semantic** (and kept) when the *next* line starts
    with a bullet marker or numbered-list prefix.  All other newlines are
    replaced with a single space.
    """
    if "\n" not in text:
        return text

    lines = text.split("\n")
    result = [lines[0]]
    for i in range(1, len(lines)):
        if _BULLET_RE.match(lines[i]):
            # Semantic break — keep the newline
            result.append("\n")
            result.append(lines[i])
        else:
            # Layout wrap — replace with space
            result.append(" ")
            result.append(lines[i])
    return "".join(result)


def _protect_newlines(text: str) -> str:
    """Replace real newlines with a placeholder before sending to LLM."""
    return text.replace("\n", _NEWLINE_PLACEHOLDER)


def _restore_newlines(text: str) -> str:
    """Restore newlines from placeholder after getting LLM output."""
    return text.replace(_NEWLINE_PLACEHOLDER, "\n")


def _build_tagged_text(color_spans: list) -> str:
    """Wrap each span's text with <s1>, <s2>, ... tags."""
    parts = []
    for i, span in enumerate(color_spans, 1):
        parts.append(f"<s{i}>{span['text']}</s{i}>")
    return "".join(parts)


def _parse_tagged_translation(tagged: str, color_spans: list) -> list:
    """Parse LLM output with <s1>...</s1> tags back into translated_spans.

    Returns list of {"text": str, "color": [R,G,B]} or empty list on failure.
    """
    matches = _SPAN_TAG_RE.findall(tagged)
    if not matches:
        return []

    result = []
    for idx_str, text in matches:
        idx = int(idx_str) - 1  # 0-based
        if idx < 0 or idx >= len(color_spans):
            return []  # malformed — fallback
        result.append({
            "text": text,
            "color": list(color_spans[idx]["color"]),
        })

    # Must have same number of spans as original
    if len(result) != len(color_spans):
        return []

    return result


def _call_claude_translate(
    batch: list,
    src_name: str,
    tgt_name: str,
    claude_cli: str,
    context_section: str = "",
    depth: int = 0,
) -> dict:
    """
    batch: list of (original_index, text) tuples
    Returns dict mapping local batch index -> translated text.
    """
    input_items = [{"id": k, "text": _protect_newlines(t)} for k, (_, t) in enumerate(batch)]
    input_json = json.dumps(input_items, ensure_ascii=False)

    context_block = ""
    if context_section:
        context_block = f"\n参考术语与背景知识：\n{context_section}\n"

    # Detect if any batch item contains span tags
    has_span_tags = any('<s1>' in t for _, t in batch)
    span_instruction = ""
    if has_span_tags:
        span_instruction = (
            f"\n**分段标记保持**\n"
            f"- 部分文本含有 <s1>...</s1><s2>...</s2> 等分段标记\n"
            f"- 翻译时必须保留所有 <sN>...</sN> 标记，数量和顺序不变\n"
            f"- 每个标记内的文本独立翻译，标记本身原样输出\n"
            f"- 标记之间不要添加额外空格或换行\n\n"
        )

    prompt = (
        f"你是一位资深演示文稿本地化专家，具备丰富的企业级幻灯片翻译经验，熟悉技术、商业与工程领域术语。\n\n"
        f"## 任务\n"
        f"将以下幻灯片文本从 {src_name} 翻译成 {tgt_name}。\n\n"
        f"## 翻译规范\n\n"
        f"**格式保真（最高优先级）**\n"
        f"- 换行符标记（{_NEWLINE_PLACEHOLDER}）必须原样保留，位置和数量不得改变\n"
        f"- 数字、单位、产品型号、代码片段保持原样\n"
        f"- 项目符号（•、-、▶ 等）及其后的空格保持原样\n\n"
        f"{span_instruction}"
        f"**必须翻译（关键规则）**\n"
        f"- 每一条文本都必须翻译成 {tgt_name}，禁止原样返回源文本\n"
        f"- 即使文本包含品牌名、缩略词或专有名词，其中的通用词/描述性部分也必须翻译\n"
        f"  例：\"Cloud Data Infra:\" → \"クラウドデータ基盤：\"（不能原样返回）\n"
        f"  例：\"DIS, Data Ingestion Service\" → \"DIS（データ取り込みサービス）\"\n"
        f"  例：\"SOP Vehicle\" → \"SOP 車両\" / \"量产车辆\"\n"
        f"- 只有纯数字、标点、符号构成的文本才可以原样输出\n\n"
        f"**术语准确性**\n"
        f"- 专业术语使用行业标准译名\n"
        f"- 品牌名（如 Momenta）、产品型号、纯缩略词（如 AI, KPI）可保留原文，但周围的描述性文字必须翻译\n\n"
        f"**幻灯片语言风格**\n"
        f"- 简洁精炼，避免冗长；标题类文本尤其要简短有力\n"
        f"- 自然流畅，符合 {tgt_name} 母语者表达习惯\n"
        f"{context_block}\n"
        f"## 输入格式\n"
        f"JSON 数组，每个元素有 id 和 text 字段。\n\n"
        f"## 输出格式\n"
        f"仅返回 JSON 数组，结构相同，将每个 text 替换为对应译文。\n"
        f"禁止输出任何说明文字、注释或 markdown 代码块，仅纯 JSON。\n\n"
        f"{input_json}"
    )

    try:
        result = subprocess.run(
            [claude_cli, "-p", prompt],
            capture_output=True,
            text=True,
            timeout=180,
        )
        raw = result.stdout.strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
        return {item["id"]: _restore_newlines(item["text"]) for item in parsed}

    except subprocess.TimeoutExpired:
        if depth < 2 and len(batch) > 1:
            print(
                f"    [timeout] batch of {len(batch)} timed out, splitting (depth={depth})...",
                flush=True,
            )
            mid = len(batch) // 2
            left = batch[:mid]
            right = batch[mid:]
            left_results = _call_claude_translate(
                left, src_name, tgt_name, claude_cli, context_section, depth + 1
            )
            right_results = _call_claude_translate(
                right, src_name, tgt_name, claude_cli, context_section, depth + 1
            )
            # Re-map right indices (they start from 0 within their sub-batch)
            combined = dict(left_results)
            for k, v in right_results.items():
                combined[k + mid] = v
            return combined
        else:
            print(
                f"    [timeout] batch of {len(batch)} timed out at max depth, returning originals.",
                flush=True,
            )
            return {k: t for k, (_, t) in enumerate(batch)}

    except (json.JSONDecodeError, KeyError) as exc:
        print(f"    [parse error] {exc} — returning originals for this batch.", flush=True)
        return {k: t for k, (_, t) in enumerate(batch)}


def _call_claude_retry_translate(
    batch: list,
    src_name: str,
    tgt_name: str,
    claude_cli: str,
) -> dict:
    """Retry translation with a forceful prompt for blocks the LLM left unchanged."""
    input_items = [{"id": k, "text": _protect_newlines(t)} for k, (_, t) in enumerate(batch)]
    input_json = json.dumps(input_items, ensure_ascii=False)

    prompt = (
        f"你是翻译质量审核员。以下文本在上一轮翻译中被错误地原样返回，没有翻译。\n\n"
        f"## 严格要求\n"
        f"- 每一条文本必须翻译成 {tgt_name}，绝对禁止原样返回\n"
        f"- 换行符标记（{_NEWLINE_PLACEHOLDER}）必须原样保留\n"
        f"- 品牌名（如 Momenta）和纯缩略词（如 DIS, FDC）可以保留，但描述性文字必须翻译\n"
        f"- 例：\"Cloud Data Infra:\" → \"クラウドデータ基盤：\"\n"
        f"- 例：\"SOP Vehicle\" → \"量産車両\"\n"
        f"- 例：\"DIS, Data Ingestion Service\" → \"DIS（データ取り込みサービス）\"\n\n"
        f"## 输入\n{input_json}\n\n"
        f"## 输出\n"
        f"仅返回 JSON 数组，将每个 text 替换为 {tgt_name} 译文。禁止输出任何说明文字。\n"
    )

    try:
        result = subprocess.run(
            [claude_cli, "-p", prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
        raw = result.stdout.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
        return {item["id"]: _restore_newlines(item["text"]) for item in parsed}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as exc:
        print(f"    [retry parse error] {exc} — keeping originals.", flush=True)
        return {k: t for k, (_, t) in enumerate(batch)}


def _save_cache(cache: dict, cache_path: Path) -> None:
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def translate_texts(
    texts: list,
    src: str,
    tgt: str,
    cache: dict,
    cache_path: Path,
    context: str,
    batch_size: int,
    claude_cli: str,
) -> list:
    """
    Translate a flat list of texts.
    Returns a list of translated strings (same length as input).
    """
    src_name = SUPPORTED_LANGUAGES.get(src, src)
    tgt_name = SUPPORTED_LANGUAGES.get(tgt, tgt)

    results = [None] * len(texts)

    # Indices that need actual translation
    to_translate = []
    for i, text in enumerate(texts):
        if text in cache:
            cached = cache[text]
            # Reject poisoned cache: source == translation for translatable text
            if cached == text and _needs_translation(text):
                to_translate.append((i, text))
            else:
                results[i] = cached
        elif _is_trivially_invariant(text):
            results[i] = text
        else:
            to_translate.append((i, text))

    # Translate in batches
    for batch_start in range(0, len(to_translate), batch_size):
        batch = to_translate[batch_start: batch_start + batch_size]
        batch_num = batch_start // batch_size + 1
        total_batches = (len(to_translate) + batch_size - 1) // batch_size
        print(
            f"    [batch {batch_num}/{total_batches}] translating {len(batch)} texts...",
            flush=True,
        )

        local_results = _call_claude_translate(
            batch, src_name, tgt_name, claude_cli, context
        )

        for local_idx, translated in local_results.items():
            orig_idx, orig_text = batch[local_idx]
            results[orig_idx] = translated
            cache[orig_text] = translated

        # Save cache after every batch
        _save_cache(cache, cache_path)

    # Retry: detect blocks where LLM returned source text unchanged
    unchanged = []
    for i, text in enumerate(texts):
        if results[i] is not None and results[i] == text and _needs_translation(text):
            unchanged.append((i, text))

    if unchanged:
        print(
            f"    [retry] {len(unchanged)} texts returned unchanged, retrying with stricter prompt...",
            flush=True,
        )
        for batch_start in range(0, len(unchanged), batch_size):
            retry_batch = unchanged[batch_start: batch_start + batch_size]
            retry_results = _call_claude_retry_translate(
                retry_batch, src_name, tgt_name, claude_cli
            )
            for local_idx, translated in retry_results.items():
                orig_idx, orig_text = retry_batch[local_idx]
                if translated != orig_text:  # Only accept if actually changed
                    results[orig_idx] = translated
                    cache[orig_text] = translated

            _save_cache(cache, cache_path)

        # Report remaining unchanged after retry
        still_unchanged = sum(
            1 for i, text in enumerate(texts)
            if results[i] is not None and results[i] == text and _needs_translation(text)
        )
        if still_unchanged:
            print(
                f"    [retry] {still_unchanged} texts still unchanged after retry.",
                flush=True,
            )

    # Fill any remaining None with original (safety fallback)
    for i, text in enumerate(texts):
        if results[i] is None:
            results[i] = text

    return results


def main():
    parser = argparse.ArgumentParser(description="Translate parsed.json blocks using Claude CLI.")
    parser.add_argument("--input", required=True, help="Path to parsed.json")
    parser.add_argument("--output", default=None, help="Output translated.json path")
    parser.add_argument("--cache", default=None, help="Translation cache .json path")
    parser.add_argument("--context", default=None, help="Optional context/glossary file path")
    parser.add_argument("--batch", type=int, default=40, help="Max blocks per batch (default: 40)")
    parser.add_argument("--src", default="en", help="Source language code (default: en)")
    parser.add_argument("--tgt", default="ja", help="Target language code (default: ja)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.parent / (input_path.stem + ".translated.json")

    # Determine cache path
    if args.cache:
        cache_path = Path(args.cache)
    else:
        # Try to derive stem from parsed.json name (e.g. "foo.parsed.json" -> "foo")
        stem = input_path.stem  # removes last extension
        if stem.endswith(".parsed"):
            stem = stem[: -len(".parsed")]
        cache_path = input_path.parent / f"{stem}.{args.tgt}.transcache.json"

    # Load context if provided
    context_text = ""
    if args.context:
        ctx_path = Path(args.context)
        if ctx_path.exists():
            context_text = ctx_path.read_text(encoding="utf-8").strip()
        else:
            print(f"Warning: context file not found: {ctx_path}", file=sys.stderr)

    # Load cache
    cache: dict = {}
    if cache_path.exists():
        with open(cache_path, "r", encoding="utf-8") as f:
            try:
                cache = json.load(f)
                print(f"Loaded {len(cache)} cached translations from {cache_path}")
            except json.JSONDecodeError:
                print(f"Warning: could not parse cache file {cache_path}, starting fresh.")

    # Find Claude CLI
    try:
        claude_cli = find_claude_cli()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Load parsed.json
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    pages = data if isinstance(data, list) else data.get("pages", [])

    total_blocks = sum(len(page.get("blocks", [])) for page in pages)
    print(f"Input: {input_path}")
    print(f"Pages: {len(pages)}, Total blocks: {total_blocks}")
    print(f"Cache: {cache_path}")
    print(f"Output: {output_path}")
    print(f"Translating {args.src} -> {args.tgt} (batch size: {args.batch})")
    print()

    # Ensure top-level version field
    if isinstance(data, dict) and "version" not in data:
        data["version"] = "1.0"

    # Process page by page
    for page_idx, page in enumerate(pages):
        blocks = page.get("blocks", [])
        if not blocks:
            continue

        page_num = page.get("page", page_idx + 1)

        # Separate blocks into plain (no multi-color spans) and span-aware
        plain_indices = []
        span_indices = []
        for i, block in enumerate(blocks):
            cs = block.get("color_spans", [])
            if len(cs) > 1:
                span_indices.append(i)
            else:
                plain_indices.append(i)

        # --- Translate plain blocks (original batch flow) ---
        # Clean layout-wrapping newlines before translation (L1 fix)
        plain_texts = [_clean_layout_breaks(blocks[i].get("text", "")) for i in plain_indices]
        print(f"  [Page {page_num}] translating {len(blocks)} blocks "
              f"({len(span_indices)} span-aware)...", flush=True)

        if plain_texts:
            plain_translated = translate_texts(
                texts=plain_texts,
                src=args.src,
                tgt=args.tgt,
                cache=cache,
                cache_path=cache_path,
                context=context_text,
                batch_size=args.batch,
                claude_cli=claude_cli,
            )
            for idx_in_plain, orig_idx in enumerate(plain_indices):
                t = plain_translated[idx_in_plain]
                blocks[orig_idx]["translated"] = t if isinstance(t, str) else ""

        # --- Translate span-aware blocks (tagged text) ---
        if span_indices:
            span_tagged_texts = []
            for i in span_indices:
                # Clean layout-wrapping newlines in each span before tagging (L1 fix)
                cs = [
                    {**span, "text": _clean_layout_breaks(span["text"])}
                    for span in blocks[i]["color_spans"]
                ]
                span_tagged_texts.append(_build_tagged_text(cs))

            span_translated = translate_texts(
                texts=span_tagged_texts,
                src=args.src,
                tgt=args.tgt,
                cache=cache,
                cache_path=cache_path,
                context=context_text,
                batch_size=args.batch,
                claude_cli=claude_cli,
            )

            for idx_in_span, orig_idx in enumerate(span_indices):
                block = blocks[orig_idx]
                cs = block["color_spans"]
                raw_translation = span_translated[idx_in_span]
                if not isinstance(raw_translation, str):
                    raw_translation = ""

                # Try to parse span tags from LLM output
                translated_spans = _parse_tagged_translation(raw_translation, cs)
                if translated_spans:
                    block["translated_spans"] = translated_spans
                    # Reconstruct `translated` preserving semantic newlines between spans.
                    # The original text may contain \n before bullet markers (e.g. "\n• VVP ...")
                    # but spans themselves don't carry inter-span separators.  We recover them
                    # by checking if the *original* span text starts with a bullet marker.
                    parts = []
                    for si, sp in enumerate(translated_spans):
                        if si > 0 and si < len(cs):
                            orig_span_text = cs[si].get("text", "")
                            if _BULLET_RE.match(orig_span_text):
                                parts.append("\n")
                        parts.append(sp["text"])
                    block["translated"] = "".join(parts)
                else:
                    # Fallback: strip any leftover tags and use as plain translation
                    plain_text = re.sub(r'</?s\d+>', '', raw_translation)
                    block["translated"] = plain_text

    # Ensure every block that was not processed also has a `translated` field
    for page in pages:
        for block in page.get("blocks", []):
            if "translated" not in block:
                block["translated"] = ""

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print()
    print(f"Done. Written to: {output_path}")

    # Verify completeness (count empty translated fields)
    empty_translated = 0
    for page in pages:
        for block in page.get("blocks", []):
            if block.get("translated") == "":
                empty_translated += 1
    if empty_translated:
        print(f"Warning: {empty_translated} block(s) have empty translated fields.", file=sys.stderr)
    else:
        print("All blocks translated successfully.")

    # --- Contract validation ---
    print()
    print("Validating output against translated.schema.json ...")
    validation_ok = True
    validation_messages = []

    try:
        from contracts.validate import validate_output  # type: ignore

        # translated.schema.json refs parsed.schema.json via $ref (cross-file, treated as valid).
        # We validate the parsed structure manually, then check the block extension.
        violations = validate_output(data, "parsed")

        # Additionally check that every block has a `translated` field (string)
        for page in pages:
            for block in page.get("blocks", []):
                bid = block.get("id", "<unknown>")
                if "translated" not in block:
                    violations.append(f"block {bid}: missing required field 'translated'")
                elif not isinstance(block["translated"], str):
                    violations.append(
                        f"block {bid}: 'translated' must be a string, "
                        f"got {type(block['translated']).__name__}"
                    )

        if violations:
            validation_ok = False
            for v in violations:
                validation_messages.append(f"  VIOLATION: {v}")
        else:
            validation_messages.append("  All checks passed.")

    except ImportError as exc:
        validation_ok = False
        validation_messages.append(f"  WARNING: could not import validate.py — {exc}")
    except Exception as exc:  # pragma: no cover
        validation_ok = False
        validation_messages.append(f"  WARNING: validation raised an unexpected error — {exc}")

    # Print validation summary
    print()
    print("=" * 60)
    print("VALIDATION SUMMARY")
    print("=" * 60)
    if validation_ok:
        print("STATUS: VALID")
    else:
        print("STATUS: INVALID (see warnings above — output was still written)")
    for msg in validation_messages:
        print(msg)
    print("=" * 60)


if __name__ == "__main__":
    main()
