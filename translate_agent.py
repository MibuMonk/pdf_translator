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
    input_items = [{"id": k, "text": t} for k, (_, t) in enumerate(batch)]
    input_json = json.dumps(input_items, ensure_ascii=False)

    context_block = ""
    if context_section:
        context_block = f"\n参考术语与背景知识：\n{context_section}\n"

    prompt = (
        f"你是一位专业演示文稿翻译。将以下幻灯片文本从 {src_name} 翻译成 {tgt_name}。\n"
        "要求：简洁自然，保持幻灯片风格，专业术语准确，换行符（\\n）原样保留。\n"
        f"{context_block}"
        "输入格式：JSON 数组，每个元素有 id 和 text 字段。\n"
        "输出格式：仅返回 JSON 数组，结构相同，将每个 text 翻译后原样输出。\n"
        "不要输出任何其他内容，不要 markdown 代码块，仅纯 JSON。\n\n"
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
        return {item["id"]: item["text"] for item in parsed}

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
            results[i] = cache[text]
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

    # Process page by page
    for page_idx, page in enumerate(pages):
        blocks = page.get("blocks", [])
        if not blocks:
            continue

        page_num = page.get("page", page_idx + 1)
        texts = [block.get("text", "") for block in blocks]
        print(f"  [Page {page_num}] translating {len(blocks)} blocks...", flush=True)

        translated_texts = translate_texts(
            texts=texts,
            src=args.src,
            tgt=args.tgt,
            cache=cache,
            cache_path=cache_path,
            context=context_text,
            batch_size=args.batch,
            claude_cli=claude_cli,
        )

        for block, translated in zip(blocks, translated_texts):
            block["translated"] = translated

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print()
    print(f"Done. Written to: {output_path}")

    # Verify completeness
    missing = 0
    for page in pages:
        for block in page.get("blocks", []):
            if not block.get("translated"):
                missing += 1
    if missing:
        print(f"Warning: {missing} block(s) have empty translated fields.", file=sys.stderr)
    else:
        print(f"All blocks translated successfully.")


if __name__ == "__main__":
    main()
