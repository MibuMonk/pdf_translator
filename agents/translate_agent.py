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


def _fix_unescaped_newlines(s: str) -> str:
    """Replace literal newlines inside JSON string values with \\n."""
    result = []
    in_string = False
    escape_next = False
    for ch in s:
        if escape_next:
            result.append(ch)
            escape_next = False
            continue
        if ch == '\\' and in_string:
            result.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if ch == '\n' and in_string:
            result.append('\\n')
            continue
        result.append(ch)
    return ''.join(result)


def _repair_json(raw: str) -> list:
    """Attempt to parse a JSON array from potentially malformed LLM output.

    Tries progressively aggressive repairs:
    1. Direct json.loads
    2. Extract outermost [...] substring
    2b. Fix unescaped newlines inside JSON strings
    3. Fix trailing commas before ] or }
    4. Fix single quotes → double quotes (structural only)
    5. Regex-based object extraction as last resort

    Returns parsed list on success, raises ValueError on complete failure.
    """
    # 1. Direct parse
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Extract outermost [...] — handles LLM adding explanation around JSON
    bracket_match = re.search(r'\[', raw)
    if bracket_match:
        start = bracket_match.start()
        # Find matching closing bracket
        depth = 0
        end = None
        for i in range(start, len(raw)):
            if raw[i] == '[':
                depth += 1
            elif raw[i] == ']':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is not None:
            extracted = raw[start:end]
            try:
                result = json.loads(extracted)
                if isinstance(result, list):
                    print("    [json-repair] extracted JSON array from surrounding text", flush=True)
                    return result
            except (json.JSONDecodeError, ValueError):
                pass

            # 2b. Fix unescaped newlines inside JSON strings
            nl_fixed = _fix_unescaped_newlines(extracted)
            try:
                result = json.loads(nl_fixed)
                if isinstance(result, list):
                    print("    [json-repair] fixed unescaped newlines", flush=True)
                    return result
            except (json.JSONDecodeError, ValueError):
                pass

            # 3. Fix trailing commas: ,] or ,}
            fixed = re.sub(r',\s*([}\]])', r'\1', nl_fixed)
            try:
                result = json.loads(fixed)
                if isinstance(result, list):
                    print("    [json-repair] fixed trailing commas", flush=True)
                    return result
            except (json.JSONDecodeError, ValueError):
                pass

            # 4. Fix single quotes → double quotes (structural)
            # Replace single-quoted keys/values but avoid mangling apostrophes inside strings
            sq_fixed = fixed.replace("'", '"')
            try:
                result = json.loads(sq_fixed)
                if isinstance(result, list):
                    print("    [json-repair] fixed single quotes to double quotes", flush=True)
                    return result
            except (json.JSONDecodeError, ValueError):
                pass

    # 5. Regex-based extraction: find all {"id": N, "text": "..."} objects
    obj_pattern = re.compile(
        r'\{\s*"id"\s*:\s*(\d+)\s*,\s*"text"\s*:\s*("(?:[^"\\]|\\.|\n)*?")\s*\}',
        re.DOTALL,
    )
    matches = obj_pattern.findall(raw)
    if matches:
        items = []
        for id_str, text_json in matches:
            try:
                # Escape literal newlines before JSON parsing
                text_json_fixed = text_json.replace('\n', '\\n')
                text_val = json.loads(text_json_fixed)
                items.append({"id": int(id_str), "text": text_val})
            except (json.JSONDecodeError, ValueError):
                continue
        if items:
            print(f"    [json-repair] reconstructed {len(items)} items via regex extraction", flush=True)
            return items

    raise ValueError(f"_repair_json: unable to parse JSON from LLM output ({len(raw)} chars)")


def _make_client() -> "anthropic.Anthropic":
    import anthropic as _anthropic
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if auth_token:
        return _anthropic.Anthropic(auth_token=auth_token, base_url=base_url)
    return _anthropic.Anthropic(api_key=api_key, base_url=base_url)


def _get_model() -> str:
    return os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6")


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
    # CJK characters → has text to translate
    if re.search(r'[\u3040-\u9fff\uac00-\ud7af]', stripped):
        return True
    # All-uppercase but long with spaces → likely a title/phrase, not just "KPI"
    if len(stripped) > 10 and ' ' in stripped:
        return True
    return False


def _is_target_language(text: str, tgt: str) -> bool:
    """Check if text already appears to be predominantly in the target language.

    When translating ja→en, English text in Japanese slides is already in the
    target language. The LLM correctly returns it unchanged, so we should not
    flag it as "untranslated" or trigger retries.
    """
    # Strip span tags and punctuation/symbols for analysis
    clean = re.sub(r'</?s\d+>', '', text)
    clean = re.sub(r'[^\w\s]', '', clean, flags=re.UNICODE)
    clean = clean.strip()
    if not clean:
        return False

    if tgt == 'en':
        # Predominantly Latin letters
        latin = len(re.findall(r'[a-zA-Z]', clean))
        total = len(re.findall(r'\S', clean))
        return total > 0 and latin / total > 0.5
    elif tgt == 'ja':
        # Has Hiragana or Katakana (unique to Japanese)
        return bool(re.search(r'[\u3040-\u30ff]', clean))
    elif tgt.startswith('zh'):
        # Has CJK ideographs but no Japanese kana
        has_cjk = bool(re.search(r'[\u4e00-\u9fff]', clean))
        has_kana = bool(re.search(r'[\u3040-\u30ff]', clean))
        return has_cjk and not has_kana
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

    _last_raw = None  # capture raw output for single-item fallback
    try:
        client = _make_client()
        message = client.messages.create(
            model=_get_model(),
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        _last_raw = message.content[0].text.strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", _last_raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = _repair_json(raw)
        return {item["id"]: _restore_newlines(item["text"]) for item in parsed}

    except Exception as exc:
        # timeout-like: split batch
        if depth < 2 and len(batch) > 1:
            print(
                f"    [error] batch of {len(batch)} failed ({exc}), splitting (depth={depth})...",
                flush=True,
            )
            mid = len(batch) // 2
            left = batch[:mid]
            right = batch[mid:]
            left_results = _call_claude_translate(
                left, src_name, tgt_name, context_section, depth + 1
            )
            right_results = _call_claude_translate(
                right, src_name, tgt_name, context_section, depth + 1
            )
            combined = dict(left_results)
            for k, v in right_results.items():
                combined[k + mid] = v
            return combined
        else:
            # For single-item batch, try using raw LLM output as translation
            if len(batch) == 1 and _last_raw:
                try:
                    raw_text = _last_raw
                    # Strip markdown fences
                    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
                    raw_text = re.sub(r"\s*```$", "", raw_text)
                    # Strip any JSON wrapper artifacts
                    raw_text = re.sub(r'^\s*\[\s*\{\s*"id"\s*:\s*\d+\s*,\s*"text"\s*:\s*"?', '', raw_text)
                    raw_text = re.sub(r'"?\s*\}\s*\]\s*$', '', raw_text)
                    raw_text = _restore_newlines(raw_text.strip())
                    _, orig_text = batch[0]
                    if raw_text and raw_text != orig_text:
                        print(
                            f"    [fallback] used raw LLM output as translation for single item",
                            flush=True,
                        )
                        return {0: raw_text}
                except Exception:
                    pass
            print(
                f"    [error] batch of {len(batch)} failed at max depth ({exc}), returning originals.",
                flush=True,
            )
            return {k: t for k, (_, t) in enumerate(batch)}


def _call_claude_retry_translate(
    batch: list,
    src_name: str,
    tgt_name: str,
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
        client = _make_client()
        message = client.messages.create(
            model=_get_model(),
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = _repair_json(raw)
        return {item["id"]: _restore_newlines(item["text"]) for item in parsed}
    except (json.JSONDecodeError, KeyError, Exception) as exc:
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
            if cached == text and _needs_translation(text) and not _is_target_language(text, tgt):
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
            batch, src_name, tgt_name, context
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
        if results[i] is not None and results[i] == text and _needs_translation(text) and not _is_target_language(text, tgt):
            unchanged.append((i, text))

    if unchanged:
        print(
            f"    [retry] {len(unchanged)} texts returned unchanged, retrying with stricter prompt...",
            flush=True,
        )
        for batch_start in range(0, len(unchanged), batch_size):
            retry_batch = unchanged[batch_start: batch_start + batch_size]
            retry_results = _call_claude_retry_translate(
                retry_batch, src_name, tgt_name
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
            if results[i] is not None and results[i] == text and _needs_translation(text) and not _is_target_language(text, tgt)
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
